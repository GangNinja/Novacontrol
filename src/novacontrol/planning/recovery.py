"""Recovery: what to do when a step fails, decided from evidence.

A failure is not an instruction to try again. Trying again is one of four
answers, and picking the wrong one is how an automation system turns a bad
afternoon into a deleted directory:

    RETRY               the failure was transient; the same step may run again
    MODIFY_PARAMETERS   the error told us something usable (a type, an unknown
                        argument) and the step can be repaired, not repeated
    ESCALATE            the step needs reasoning NovaControl does not have
                        locally — hand it to the model, with the error and the
                        step, rather than guessing again
    STOP                nothing safe is left to try; report it and stop

The rules are ordered, and the first three are hard:

  * a destructive or external action is NEVER retried automatically — a delete
    that half-succeeded, run again, deletes something else;
  * a DENIED action is not retried at all: that was a human decision, and
    asking the same question twice is not recovery;
  * attempts are bounded by the plan's :class:`RetryPolicy`, whose own ceiling
    makes an infinite loop unrepresentable rather than merely unlikely.

This advisor executes nothing and holds no state. It reads a step, an error and
an attempt count, and returns an opinion — the executor applies it, and where it
cannot act, the decision travels upward as ESCALATE rather than being swallowed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from novacontrol.planning.models import (
    DEFAULT_RETRYABLE,
    EFFECTS_NEVER_RETRIED,
    FailureKind,
    PlanStep,
    RetryPolicy,
    StepError,
)


class RecoveryAction(StrEnum):
    RETRY = "retry"
    MODIFY_PARAMETERS = "modify_parameters"
    ESCALATE = "escalate"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """What to do about one failure, and why — always with a reason."""

    action: RecoveryAction
    reason: str
    #: Replacement parameters, when the action repairs the step.
    adjustments: Mapping[str, Any] = field(default_factory=dict)
    #: How long to wait before a retry (from the plan's retry policy).
    backoff_seconds: float = 0.0

    @property
    def will_run_again(self) -> bool:
        return self.action in (RecoveryAction.RETRY, RecoveryAction.MODIFY_PARAMETERS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "adjustments": dict(self.adjustments),
            "backoff_seconds": self.backoff_seconds,
        }


class RecoveryAdvisor:
    """Chooses a recovery action for a failed step, within safe bounds."""

    def __init__(
        self,
        *,
        retry_policy: RetryPolicy | None = None,
        escalation_available: bool = True,
    ) -> None:
        self.retry_policy = retry_policy or RetryPolicy()
        #: Whether anything downstream can take over from a failure. When no
        #: escalation target was wired, an unretryable failure stops honestly
        #: instead of proposing a rescue that does not exist.
        self.escalation_available = escalation_available

    def advise(
        self,
        step: PlanStep,
        error: StepError,
        *,
        attempts_made: int,
        retry_policy: RetryPolicy | None = None,
    ) -> RecoveryDecision:
        policy = retry_policy or self.retry_policy

        # 1. Safety first: some steps are never repeated, whatever went wrong.
        if step.effect in EFFECTS_NEVER_RETRIED:
            return RecoveryDecision(
                RecoveryAction.STOP,
                f"{step.effect.value} actions are never retried automatically: "
                "a repeated destructive or external action is a new risk, not a second chance.",
            )

        # 2. A refusal is an answer. Retrying it would be arguing with a human.
        if error.kind is FailureKind.PERMISSION_DENIED:
            return RecoveryDecision(
                RecoveryAction.STOP,
                "The action was not authorized; only a person can change that, "
                "so it is reported rather than re-attempted.",
            )

        # 3. Missing machinery: the model can plan around it, a loop cannot.
        if error.kind in (FailureKind.MISSING_DEPENDENCY, FailureKind.UNSUPPORTED):
            return self._escalate_or_stop(
                error,
                "A prerequisite is missing or unsupported here; retrying cannot supply it.",
            )

        # 4. Repair before repeating: if the error names what was wrong with the
        #    parameters, a corrected attempt is a different attempt.
        if error.kind is FailureKind.INVALID_PARAMETERS:
            adjustments = repair_parameters(step, error)
            # Only the BUDGET matters here: a repaired attempt is not the same
            # attempt again, so "is this kind worth repeating?" does not apply.
            repairable = policy.attempts_left(attempts_made=attempts_made)
            if adjustments and repairable:
                return RecoveryDecision(
                    RecoveryAction.MODIFY_PARAMETERS,
                    f"Repaired the parameters the tool rejected: {', '.join(sorted(adjustments))}.",
                    adjustments=adjustments,
                    backoff_seconds=policy.backoff_seconds,
                )
            return self._escalate_or_stop(
                error,
                "The tool rejected the parameters and the error does not say how to repair them.",
            )

        # 5. Bounded retry for the kinds where trying again is the actual remedy.
        if policy.may_retry(error.kind, attempts_made=attempts_made):
            return RecoveryDecision(
                RecoveryAction.RETRY,
                f"{error.kind.value} failure with retries left "
                f"({attempts_made}/{policy.max_attempts} attempts used).",
                backoff_seconds=policy.backoff_seconds,
            )

        # 6. Out of attempts, or a kind not worth repeating.
        if attempts_made >= policy.max_attempts and error.kind in policy.retryable:
            return self._escalate_or_stop(
                error,
                f"Retry budget exhausted after {attempts_made} attempt(s); "
                "a repeated failure needs a different approach, not another try.",
            )
        return RecoveryDecision(
            RecoveryAction.STOP,
            f"{error.kind.value} failures are not retried by this plan's policy.",
        )

    def _escalate_or_stop(self, error: StepError, reason: str) -> RecoveryDecision:
        if self.escalation_available:
            return RecoveryDecision(
                RecoveryAction.ESCALATE,
                f"{reason} Escalating for reasoning: {error.message}",
            )
        return RecoveryDecision(RecoveryAction.STOP, f"{reason} (no escalation target is wired)")

    def classify(
        self, step: PlanStep, message: str, *, denied: bool = False
    ) -> StepError:
        """Turn a raw failure into a :class:`StepError` with a kind.

        Classification is by the messages this codebase's own executors and
        Python produce, plus the tool schema's validation text — deliberately a
        short, readable list rather than a guess at every possible error.
        """
        if denied:
            return StepError(
                FailureKind.PERMISSION_DENIED, message, retryable=False, tool=step.tool
            )
        kind = _classify(message)
        return StepError(
            kind,
            message,
            retryable=kind in DEFAULT_RETRYABLE and step.effect not in EFFECTS_NEVER_RETRIED,
            tool=step.tool,
        )


# --------------------------------------------------------------------------- #
# Failure classification
# --------------------------------------------------------------------------- #

#: Ordered markers: the FIRST list that matches names the kind. Order is the
#: whole design — "permission denied" must be read as a refusal before the
#: word "denied" can be read as anything else, and a missing dependency must be
#: read as un-retryable before a generic "not found" makes it look transient.
_CLASSIFIERS: tuple[tuple[FailureKind, tuple[str, ...]], ...] = (
    (
        FailureKind.PERMISSION_DENIED,
        (
            "permission denied",
            "access is denied",
            "not approved",
            "approval was not",
            "operation not permitted",
            "unauthorized",
            "forbidden",
        ),
    ),
    (
        FailureKind.MISSING_DEPENDENCY,
        (
            "not installed",
            "no module named",
            "command not found",
            "is not available",
            "not found on path",
            "no such file",
            "file not found",
            "tool is not registered",
            "is not registered",
            "not configured",
            "no such project",
        ),
    ),
    (
        FailureKind.UNSUPPORTED,
        ("unsupported", "not supported", "no executor", "cannot be carried out", "not implemented"),
    ),
    (
        FailureKind.INVALID_PARAMETERS,
        (
            "missing required argument",
            "must be string",
            "must be integer",
            "must be number",
            "must be boolean",
            "must be object",
            "must be array",
            "unknown argument",
            "unexpected argument",
            "unexpected keyword",
            "invalid argument",
            "invalid parameter",
        ),
    ),
    (
        FailureKind.TRANSIENT,
        (
            "timeout",
            "timed out",
            "temporarily",
            "try again",
            "connection reset",
            "connection aborted",
            "in use",
            "is locked",
            "busy",
            "element not found",
            "no matching element",
            "stale",
        ),
    ),
)


def _classify(message: str) -> FailureKind:
    text = message.lower()
    for kind, markers in _CLASSIFIERS:
        if any(marker in text for marker in markers):
            return kind
    return FailureKind.UNKNOWN


# --------------------------------------------------------------------------- #
# Parameter repair
# --------------------------------------------------------------------------- #

_MISSING_ARGUMENT = re.compile(
    r"missing required argument[s]?[:\s]+([A-Za-z0-9_]+)", re.IGNORECASE
)
_UNKNOWN_ARGUMENT = re.compile(
    r"(?:unknown|unexpected|unrecognized) (?:argument|parameter)[:\s]+'?([A-Za-z0-9_]+)'?",
    re.IGNORECASE,
)
_TYPE_MISMATCH = re.compile(
    r"argument '?([A-Za-z0-9_]+)'? must be "
    r"(string|integer|number|boolean|object|array),\s*got (\w+)",
    re.IGNORECASE,
)


def repair_parameters(step: PlanStep, error: StepError) -> dict[str, Any]:
    """Parameters the error says were wrong, corrected where that is decidable.

    Only repairs that cannot change MEANING are applied: a quoted number becomes
    a number, and an argument the tool explicitly rejected is dropped. Anything
    that would require inventing a value is left alone — a planner that guesses
    an argument is worse than one that stops.
    """
    message = error.message
    parameters = dict(step.parameters)
    repaired: dict[str, Any] = {}

    for name in _UNKNOWN_ARGUMENT.findall(message):
        if name in parameters:
            parameters.pop(name)
            repaired[name] = None

    for name, expected, _actual in _TYPE_MISMATCH.findall(message):
        if name not in parameters:
            continue
        coerced = _coerce(parameters[name], expected)
        if coerced is not None:
            parameters[name] = coerced
            repaired[name] = coerced

    missing = [name for name in _MISSING_ARGUMENT.findall(message) if name not in parameters]
    if missing:
        supplied = _recover_missing(step, missing)
        parameters.update(supplied)
        repaired.update(supplied)

    return repaired


def _coerce(value: Any, expected: str) -> Any:
    """Convert ``value`` to ``expected`` only when the conversion is lossless-looking."""
    if expected == "integer" and isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?\d+", text):
            return int(text)
    if expected == "number" and isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    if (
        expected == "boolean"
        and isinstance(value, str)
        and value.strip().lower() in ("true", "false")
    ):
        return value.strip().lower() == "true"
    return None


def _recover_missing(step: PlanStep, missing: list[str]) -> dict[str, Any]:
    """A missing argument can only be supplied from information the step ALREADY
    carries — its own parameters, or a plainly matching key."""
    supplied: dict[str, Any] = {}
    for name in missing:
        for key, value in step.parameters.items():
            if key == name or key.endswith(f"_{name}") or key == f"{name}_path":
                supplied[name] = value
                break
    return supplied
