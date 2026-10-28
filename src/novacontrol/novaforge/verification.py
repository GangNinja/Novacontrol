"""NF-21 — outcome verification and bounded recovery.

NF-19 sends ONE bounded input and reports that the *input operation* completed.
NF-20 decides which supported action should be attempted and states an expected
effect as a hypothesis. This module is the missing step between them: it takes a
plan's executed steps, asks for a NEW observation, decides whether the declared
effect is supported by that evidence, and selects one bounded recovery response
when it is not.

The rules this module holds itself to, stated so a reader does not have to infer
them from the absence of code:

* **A selected, approved or dispatched action is not an outcome.** Nothing here
  reads an input result as an effect. ``ControlStatus.COMPLETED`` and
  ``OutcomeStatus.VERIFIED_SUCCESS`` are different claims, and only the second
  one requires an observation.
* **Missing evidence is not negative evidence.** A field that was not read is
  ``OUTCOME_UNKNOWN``, never ``VERIFIED_FAILURE``. A comparison is only REFUTED
  when the reading that would show the effect was actually taken.
* **Nothing here can repeat an action.** The service has no dispatch seam at
  all: it can observe, invalidate a plan, ask for a new plan, pause, stop, or
  escalate — never send input. That is why an unknown outcome cannot cause a
  second, possibly non-idempotent, attempt.
* **A measured transition in progress is waited for, not repeated.** When a
  relative observable moved in the declared direction by LESS than the contract
  asked for, the effect is still arriving: recovery waits briefly (bounded, once
  per action) and reads again, instead of replanning on evidence that says the
  action is working. When every reading for an action was stale, nothing on
  screen can be trusted, so the safe pause is chosen over dispatching blind.
* **The recovery layer is bounded in calls AND in time.** Attempts per action,
  replans per plan, recoveries per session and the total time a session may
  spend recovering are all ceilings in code, re-read when a recovery is APPLIED
  as well as when it is decided.
* **Expected effects come from declarations.** A verification rule names the
  observable it expects, and so does a VERIFIED NF-18 ``control`` record that
  declares one. A step nobody has declared an observable for is
  ``NOT_VERIFIABLE``; a success criterion is never invented for a game whose
  mechanics are not understood.
* **Confidence is a heuristic share, labelled as one.** ``confidence`` is the
  share of declared observables decided SATISFIED by a direct reading, and
  ``confidence_basis`` says so. It is not a calibrated probability and is never
  described as one.
* **An action nobody ran is SKIPPED, not failed.** A step that never dispatched
  produced no evidence, so it is reported as skipped with its reason; judging it
  as a failure would claim a fault that was never observed.

Structure: the models (contracts, evidence, verdicts, outcomes, decisions) are
pure and synchronous; :class:`GameVerificationService` owns the bounded loop and
every collaborator is injected, so a test drives the real service with scripted
observations and no desktop.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from .knowledge import KnowledgeCategory, KnowledgeRecord, KnowledgeStatus
from .objectives import ObjectiveState, ObjectiveStatus
from .planner import ActionPlan, PlanStatus
from .state import GameState, HudStatus

__all__ = [
    "COMPARISONS",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "MAX_OBSERVATION_ATTEMPTS",
    "MAX_RECOVERY_ATTEMPTS",
    "MAX_RECOVERY_MS",
    "MAX_SESSION_RECOVERIES",
    "MAX_TRANSITION_WAITS",
    "MAX_VERIFICATION_MS",
    "OUTCOME_STATUSES",
    "RECOVERY_ACTIONS",
    "VERDICTS",
    "VERIFICATION_SCHEMA_VERSION",
    "ActionOutcome",
    "EffectContract",
    "GameVerificationService",
    "ObservableRequirement",
    "ObservationEvidence",
    "OutcomeStatus",
    "RecoveryAction",
    "RecoveryDecision",
    "RequirementVerdict",
    "VerificationContext",
    "VerificationPolicy",
    "VerificationResult",
    "VerificationRule",
    "VerificationRuleRegistry",
    "VerificationStatus",
    "classify_outcome",
    "default_verification_rules",
    "evaluate_requirement",
    "merge_verdicts",
    "requirement_problems",
    "transition_pending",
]

VERIFICATION_SCHEMA_VERSION = "nf21.1"

#: The share of declared observables that would make a contract's evidence
#: comfortable. It gates nothing on its own — the classification is decided by
#: what was actually read — and it is published so a reader can see the line the
#: report reasons about instead of inferring one.
DEFAULT_CONFIDENCE_THRESHOLD = 0.75

#: Hard ceilings. A policy may lower these, never raise them.
MAX_VERIFICATION_MS = 60_000
MAX_OBSERVATION_ATTEMPTS = 5
MAX_RECOVERY_ATTEMPTS = 3
MAX_SESSION_RECOVERIES = 8
MAX_TRANSITION_WAITS = 3
MAX_RECOVERY_MS = 60_000
MAX_CONTRACT_REQUIREMENTS = 4
MAX_OUTCOMES_KEPT = 60

#: The comparisons a declared observable may use. "changes", "increases" and
#: "decreases" are RELATIVE and need the action's own pre-action reading;
#: the rest are absolute and are decided from the post-action reading alone.
COMPARISONS = (
    "changes",
    "increases",
    "decreases",
    "equals",
    "at_least",
    "at_most",
    "present",
    "absent",
)

RELATIVE_COMPARISONS = frozenset({"changes", "increases", "decreases"})

#: Per-requirement answers. There is no "unknown" spelling: an undecidable
#: requirement is ``indeterminate`` and says why.
VERDICTS = ("satisfied", "refuted", "indeterminate")


class OutcomeStatus(StrEnum):
    """What the evidence established about ONE executed action.

    The seven answers are deliberately distinct. ``OUTCOME_UNKNOWN``,
    ``VERIFICATION_TIMED_OUT`` and ``VERIFIED_FAILURE`` are three different
    findings: nothing was established, the deadline expired, and the expected
    effect was established NOT to have happened.
    """

    VERIFIED_SUCCESS = "verified_success"
    VERIFIED_FAILURE = "verified_failure"
    PARTIAL_SUCCESS = "partial_success"
    OUTCOME_UNKNOWN = "outcome_unknown"
    VERIFICATION_TIMED_OUT = "verification_timed_out"
    VERIFICATION_ABORTED = "verification_aborted"
    NOT_VERIFIABLE = "not_verifiable"


OUTCOME_STATUSES = tuple(item.value for item in OutcomeStatus)


class VerificationStatus(StrEnum):
    """The summary of one verification run over a plan's executed steps."""

    VERIFIED = "verified"
    FAILED = "failed"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    TIMED_OUT = "timed_out"
    ABORTED = "aborted"
    NOT_VERIFIABLE = "not_verifiable"
    REFUSED = "refused"


class RecoveryAction(StrEnum):
    """One bounded recovery response. None of these dispatch input.

    ``REFRESH_OBSERVATION`` and ``REEVALUATE_STATE`` read again; ``REPLAN`` asks
    the planner for a new plan (which still needs its own approval before it can
    run); ``PAUSE_SESSION``, ``STOP_OPERATION`` and ``INVALIDATE_PLAN`` change
    what may run next. No member of this enum can send a key or a click.
    """

    NONE = "none"
    REFRESH_OBSERVATION = "refresh_observation"
    WAIT_FOR_TRANSITION = "wait_for_transition"
    REEVALUATE_STATE = "reevaluate_state"
    INVALIDATE_PLAN = "invalidate_plan"
    REPLAN = "replan"
    PAUSE_SESSION = "pause_session"
    REQUEST_OPERATOR = "request_operator"
    STOP_OPERATION = "stop_operation"


RECOVERY_ACTIONS = tuple(item.value for item in RecoveryAction)

#: Recovery responses that change what may run next, or that could lead to a new
#: dispatch. They require an explicit approval when the policy asks for one;
#: reading again and invalidating an obsolete plan do not (they can only reduce
#: what happens, never add an action).
SIDE_EFFECTING_RECOVERIES = frozenset(
    {RecoveryAction.REPLAN, RecoveryAction.PAUSE_SESSION, RecoveryAction.STOP_OPERATION}
)

#: Step-record codes that mean the input operation never completed. A refused or
#: failed dispatch is not an unverified effect: it is a step that produced none.
_REFUSAL_CODES = frozenset(
    {
        "wrong_session",
        "target_changed",
        "target_lost",
        "unvalidated",
        "not_allowed",
        "denied",
        "policy",
        "budget_exhausted",
        "unsupported",
        "busy",
    }
)

#: Gate states that mean the session's target is no longer trustworthy. They stop
#: the operation instead of being recovered by observation.
_UNSAFE_GATE_STATES = frozenset({"target_lost", "target_changed", "no_session"})


class Publish(Protocol):
    def __call__(self, event_type: str, payload: dict[str, Any]) -> Awaitable[None]: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat()


def _number(value: Any) -> float | None:
    """A measured number, or ``None`` — never a zero standing in for one."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Scope A — expected-outcome contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObservableRequirement:
    """One thing that must be observable for an expected effect to be supported.

    ``field`` names a HUD element id that the state reader actually reads (an
    NF-16 profile field). ``value``/``min_change`` are declarations, never
    measurements. ``change_kinds`` optionally names the NF-16 change kinds that
    corroborate the requirement in the reading itself.
    """

    field: str
    comparison: str
    value: float | None = None
    min_change: float | None = None
    tolerance: float = 0.0
    change_kinds: tuple[str, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        if not str(self.field or "").strip():
            raise ValueError("An observable requirement needs the field it is about.")
        if self.comparison not in COMPARISONS:
            raise ValueError(
                f"Unknown comparison {self.comparison!r}; use one of {sorted(COMPARISONS)}."
            )
        if self.tolerance < 0:
            raise ValueError("A tolerance cannot be negative.")
        if self.min_change is not None and self.min_change < 0:
            raise ValueError("A minimum change cannot be negative.")
        if self.comparison in {"equals", "at_least", "at_most"} and self.value is None:
            raise ValueError(f"{self.comparison!r} needs the value it compares against.")

    @property
    def relative(self) -> bool:
        """Whether deciding this needs the action's own pre-action reading."""
        return self.comparison in RELATIVE_COMPARISONS

    def describe(self) -> str:
        target = "" if self.value is None else f" {self.value:g}"
        change = "" if self.min_change is None else f" by at least {self.min_change:g}"
        tolerance = f" (±{self.tolerance:g})" if self.tolerance else ""
        if self.comparison == "changes":
            return f"{self.field} changes{change}{tolerance}"
        if self.comparison in {"increases", "decreases"}:
            return f"{self.field} {self.comparison}{change}{tolerance}"
        if self.comparison in {"equals", "at_least", "at_most"}:
            return f"{self.field} is {self.comparison.replace('_', ' ')}{target}{tolerance}"
        return f"{self.field} is {self.comparison}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "comparison": self.comparison,
            "value": self.value,
            "min_change": self.min_change,
            "tolerance": self.tolerance,
            "change_kinds": list(self.change_kinds),
            "note": self.note,
            "relative": self.relative,
            "describe": self.describe(),
        }


def requirement_problems(requirement: ObservableRequirement) -> list[str]:
    """Why a declared observable could never decide anything.

    A requirement that cannot be decided is a configuration error, and reporting
    it as a contract problem keeps it visible instead of letting a step look
    verifiable when no reading could ever satisfy it. Only declaration errors are
    reported here: whether the pre-action reading a RELATIVE requirement needs is
    still available is a runtime fact, and a missing baseline is answered
    ``indeterminate`` rather than treated as a bad declaration.
    """
    problems: list[str] = []
    if requirement.comparison in {"increases", "decreases"} and requirement.min_change is None:
        problems.append(
            f"{requirement.field}: {requirement.comparison!r} declares no minimum change, "
            "so any drift would satisfy it"
        )
    return problems


@dataclass(frozen=True, slots=True)
class EffectContract:
    """The verification contract for ONE planned step.

    Situational problems (a missing reference, an unsupported action, no declared
    observable) are FIELDS rather than exceptions: a step this build cannot
    verify has to become a recorded ``NOT_VERIFIABLE`` outcome, not a crash
    halfway through a run.
    """

    contract_id: str
    session_id: str
    plan_id: str
    action_id: str
    step_index: int
    kind: str
    expected_effect: str
    requirements: tuple[ObservableRequirement, ...] = ()
    pre_state_id: str = ""
    pre_observation_id: str = ""
    pre_sequence: int = 0
    deadline_ms: int = 30_000
    settle_ms: int = 250
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    failure_conditions: tuple[str, ...] = ()
    recovery_eligible: bool = False
    #: The input operation's own code from the plan's execution record. A refused
    #: or failed dispatch is what makes a failed effect a PRECONDITION failure
    #: rather than a plain miss, and the outcome carries it for the recovery layer.
    dispatch_code: str = ""
    source: str = ""
    problems: tuple[str, ...] = ()
    schema_version: str = VERIFICATION_SCHEMA_VERSION

    @property
    def verifiable(self) -> bool:
        """Whether the evidence required for this step is declared at all."""
        return not self.problems and bool(self.requirements)

    def describe(self) -> str:
        if not self.requirements:
            return "no observable was declared for this action"
        return "; ".join(item.describe() for item in self.requirements)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "session_id": self.session_id,
            "plan_id": self.plan_id,
            "action_id": self.action_id,
            "step_index": self.step_index,
            "kind": self.kind,
            "expected_effect": self.expected_effect,
            "requirements": [item.to_dict() for item in self.requirements],
            "observable": self.describe(),
            "pre_state_id": self.pre_state_id,
            "pre_observation_id": self.pre_observation_id,
            "pre_sequence": self.pre_sequence,
            "deadline_ms": self.deadline_ms,
            "settle_ms": self.settle_ms,
            "confidence_threshold": self.confidence_threshold,
            "failure_conditions": list(self.failure_conditions),
            "recovery_eligible": self.recovery_eligible,
            "dispatch_code": self.dispatch_code,
            "source": self.source,
            "problems": list(self.problems),
            "verifiable": self.verifiable,
        }


# ---------------------------------------------------------------------------
# Scope B — evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObservationEvidence:
    """ONE post-action reading, with everything needed to judge its standing.

    ``accepted`` is the gate every verdict passes through: a rejected reading can
    still be recorded (and is, in the outcome's ``rejected`` list) but it never
    scores a requirement. ``readings`` is the HUD surface as it was read, copied
    here so an outcome stays readable after the state service has moved on.
    """

    observation_id: str = ""
    state_id: str = ""
    session_id: str = ""
    game: str = ""
    sequence: int = 0
    at: str = ""
    age_seconds: float | None = None
    status: str = ""
    accepted: bool = False
    code: str = ""
    reason: str = ""
    readings: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    changes: tuple[Mapping[str, Any], ...] = ()
    capture: Mapping[str, Any] = field(default_factory=dict)
    target: Mapping[str, Any] = field(default_factory=dict)
    derived: bool = False
    state: GameState | None = field(default=None, compare=False, repr=False)

    @property
    def ref(self) -> str:
        """The reference an audit row can resolve later."""
        if self.observation_id:
            return f"state:{self.state_id or self.observation_id}"
        return "state:"

    @property
    def fresh(self) -> bool:
        return bool(self.accepted and self.age_seconds is not None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "state_id": self.state_id,
            "session_id": self.session_id,
            "game": self.game,
            "sequence": self.sequence,
            "at": self.at,
            "age_seconds": self.age_seconds,
            "status": self.status,
            "accepted": self.accepted,
            "code": self.code,
            "reason": self.reason,
            "readings": {
                str(key): dict(value) for key, value in list(self.readings.items())[:24]
            },
            "changes": [dict(row) for row in self.changes[:24]],
            "capture": dict(self.capture),
            "target": dict(self.target),
            "derived": self.derived,
            "ref": self.ref,
        }


@dataclass(frozen=True, slots=True)
class RequirementVerdict:
    """One requirement's answer from ONE accepted reading."""

    field: str
    comparison: str
    verdict: str
    observed: str = ""
    reason: str = ""
    baseline: str = ""
    evidence_ref: str = ""
    observation_id: str = ""
    direct: bool = False
    corroborated: bool = False
    #: The change actually MEASURED for a relative requirement (signed) and the
    #: magnitude the contract declared. ``None`` when nothing numeric was
    #: measured (an absolute comparison, a text-only field, or no baseline).
    #: They are kept as numbers, not only as the rendered text, so a recovery
    #: decision can tell "the declared transition is in progress but short of its
    #: target" from "the effect did not happen" without parsing a string.
    motion: float | None = None
    motion_target: float | None = None

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(f"Unknown verdict {self.verdict!r}; use one of {list(VERDICTS)}.")

    @property
    def decisive(self) -> bool:
        return self.verdict in {"satisfied", "refuted"}

    @property
    def satisfied(self) -> bool:
        return self.verdict == "satisfied"

    @property
    def refuted(self) -> bool:
        return self.verdict == "refuted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "comparison": self.comparison,
            "verdict": self.verdict,
            "observed": self.observed,
            "reason": self.reason,
            "baseline": self.baseline,
            "evidence_ref": self.evidence_ref,
            "observation_id": self.observation_id,
            "direct": self.direct,
            "corroborated": self.corroborated,
            "motion": self.motion,
            "motion_target": self.motion_target,
        }


def _reading(evidence: ObservationEvidence, field_name: str) -> Mapping[str, Any] | None:
    row = evidence.readings.get(field_name)
    return row if isinstance(row, Mapping) else None


def _row_status(row: Mapping[str, Any] | None) -> str:
    return str((row or {}).get("status") or "")


def _row_value(row: Mapping[str, Any] | None) -> float | None:
    return _number((row or {}).get("value"))


def _row_text(row: Mapping[str, Any] | None) -> str:
    return str((row or {}).get("text") or "")


def _change_rows(
    evidence: ObservationEvidence, requirement: ObservableRequirement
) -> list[Mapping[str, Any]]:
    """NF-16 change rows about this field, computed in the reading being judged.

    The change list is a diff of two REAL readings, so it can corroborate a
    relative requirement whose pre-action reading is gone. It is never allowed to
    REFUTE one: the previous reading in that diff is not necessarily this
    action's pre-action reading.
    """
    wanted = set(requirement.change_kinds)
    rows: list[Mapping[str, Any]] = []
    for row in evidence.changes:
        if str(row.get("field_name") or "") != requirement.field:
            continue
        if str(row.get("observation_id") or "") != evidence.observation_id:
            continue
        if wanted and str(row.get("kind") or "") not in wanted:
            continue
        rows.append(row)
    return rows


def evaluate_requirement(
    requirement: ObservableRequirement,
    evidence: ObservationEvidence,
    baseline: Mapping[str, Any] | None,
    *,
    pre_observation_id: str = "",
    min_change: float = 1.0,
) -> RequirementVerdict:
    """Decide ONE requirement against ONE accepted reading (pure, total).

    Absolute comparisons are decided from the reading alone and can be REFUTED.
    Relative comparisons need the action's own pre-action reading (or a
    corroborating change row from the same reading) and are never refuted from
    missing evidence.
    """
    field_name = requirement.field
    row = _reading(evidence, field_name)
    status = _row_status(row)
    value = _row_value(row)
    text = _row_text(row)
    observed = f"{field_name}={text or value}" if row is not None else f"{field_name} was not read"

    def find(
        verdict: str,
        *,
        observed_text: str = "",
        reason: str = "",
        baseline: str = "",
        direct: bool = False,
        corroborated: bool = False,
        motion: float | None = None,
        motion_target: float | None = None,
    ) -> RequirementVerdict:
        """One verdict about THIS requirement, from THIS reading."""
        return RequirementVerdict(
            field=field_name,
            comparison=requirement.comparison,
            verdict=verdict,
            observed=observed_text,
            reason=reason,
            baseline=baseline,
            evidence_ref=evidence.ref,
            observation_id=evidence.observation_id,
            direct=direct,
            corroborated=corroborated,
            motion=motion,
            motion_target=motion_target,
        )

    if requirement.comparison == "present":
        if row is None:
            return find(
                "indeterminate",
                reason="the reading's profile declares no such field, so nothing was looked at",
            )
        if status == HudStatus.RECOGNIZED.value:
            return find(
                "satisfied",
                observed_text=observed,
                reason="the declared field was recognized",
                direct=True,
            )
        if status == HudStatus.ABSENT.value:
            return find(
                "refuted",
                observed_text=observed,
                reason="the declared region was read and held nothing",
                direct=True,
            )
        return find(
            "indeterminate",
            reason=f"the field was read as {status or 'unreadable'}: {row.get('reason', '')}".strip(),
        )

    if requirement.comparison == "absent":
        if row is None:
            return find(
                "indeterminate",
                reason="no such field is declared; undeclared is not the same as absent",
            )
        if status == HudStatus.ABSENT.value:
            return find(
                "satisfied",
                observed_text=observed,
                reason="the declared region was read and held nothing",
                direct=True,
            )
        if status == HudStatus.RECOGNIZED.value:
            return find(
                "refuted",
                observed_text=observed,
                reason="the field was still recognized in the post-action reading",
                direct=True,
            )
        return find("indeterminate", reason=f"the field was read as {status or 'unreadable'}")

    # Absolute numeric comparisons.
    if requirement.comparison in {"equals", "at_least", "at_most"}:
        if row is None:
            return find(
                "indeterminate",
                reason="the reading declares no such field, so its value was not measured",
            )
        if status != HudStatus.RECOGNIZED.value or value is None:
            return find(
                "indeterminate",
                observed_text=observed,
                reason=(
                    f"the field read as {status or 'unreadable'}"
                    + (f": {row.get('reason', '')}" if row.get("reason") else "")
                ).strip(),
            )
        target = float(requirement.value or 0.0)
        tolerance = float(requirement.tolerance)
        if requirement.comparison == "equals":
            ok = abs(value - target) <= tolerance
        elif requirement.comparison == "at_least":
            ok = value >= target - tolerance
        else:
            ok = value <= target + tolerance
        return find(
            "satisfied" if ok else "refuted",
            observed_text=observed,
            reason=(
                f"read {value:g}; the contract declares "
                f"{requirement.comparison.replace('_', ' ')} {target:g}"
            ),
            direct=True,
        )

    # Relative comparisons: these need a baseline.
    wanted_change = (
        float(requirement.min_change) if requirement.min_change is not None else float(min_change)
    )
    if pre_observation_id and evidence.observation_id == pre_observation_id:
        return find(
            "indeterminate",
            observed_text=observed,
            reason=(
                "this is the same observation the action was planned from; a second "
                "reading is required before a change can be established"
            ),
        )
    baseline_value = _row_value(baseline)
    baseline_text = _row_text(baseline)
    baseline_desc = (
        ""
        if baseline is None
        else f"{requirement.field}={baseline_text or baseline_value} ({_row_status(baseline)})"
    )
    if baseline is not None and _row_status(baseline) == HudStatus.RECOGNIZED.value:
        if value is not None and baseline_value is not None:
            delta = value - baseline_value
            if requirement.comparison == "changes":
                ok = abs(delta) > requirement.tolerance
            elif requirement.comparison == "increases":
                ok = delta >= wanted_change
            else:
                ok = -delta >= wanted_change
            return find(
                "satisfied" if ok else "refuted",
                observed_text=f"{baseline_value:g} → {value:g}",
                baseline=baseline_desc,
                direct=True,
                motion=delta,
                motion_target=(
                    float(requirement.tolerance)
                    if requirement.comparison == "changes"
                    else wanted_change
                ),
                reason=(
                    f"{requirement.field} was measured before and after: {delta:+g} "
                    f"(declared {requirement.comparison}, minimum {wanted_change:g})"
                ),
            )
        if requirement.comparison == "changes" and text and baseline_text:
            ok = text.strip() != baseline_text.strip()
            return find(
                "satisfied" if ok else "refuted",
                observed_text=f"{baseline_text} → {text}",
                baseline=baseline_desc,
                direct=True,
                reason="the declared field's text was read before and after",
            )
    corroborating = _change_rows(evidence, requirement)
    if corroborating:
        row_change = corroborating[0]
        return find(
            "satisfied",
            observed_text=str(row_change.get("detail") or ""),
            baseline=baseline_desc,
            corroborated=True,
            reason=(
                "the pre-action reading is unavailable, but this reading recorded the "
                f"declared change ({row_change.get('kind')}) itself"
            ),
        )
    return find(
        "indeterminate",
        observed_text=observed,
        baseline=baseline_desc,
        reason=(
            "the action's pre-action reading is not available, so no change can be "
            "established from this reading"
        ),
    )


# ---------------------------------------------------------------------------
# Scope C/D — classification and conflicting evidence
# ---------------------------------------------------------------------------


def merge_verdicts(
    attempts: Sequence[Sequence[RequirementVerdict]],
) -> tuple[list[RequirementVerdict], str]:
    """The NEWEST verdict per requirement, plus any unresolved contradiction.

    Stale evidence never overrides newer valid evidence: only the latest verdict
    for each requirement survives. When the same requirement was decided both
    SATISFIED and REFUTED across attempts, the contradiction is reported (and the
    caller turns the outcome into ``OUTCOME_UNKNOWN``) instead of picking one.
    """
    latest: OrderedDict[tuple[str, str], RequirementVerdict] = OrderedDict()
    seen: dict[tuple[str, str], set[str]] = {}
    for attempt in attempts:
        for verdict in attempt:
            key = (verdict.field, verdict.comparison)
            latest[key] = verdict
            if verdict.decisive:
                seen.setdefault(key, set()).add(verdict.verdict)
    conflicts = sorted(
        f"{field} ({comparison})" for (field, comparison), kinds in seen.items() if len(kinds) > 1
    )
    note = ""
    if conflicts:
        note = (
            "contradictory readings disagreed about "
            + ", ".join(conflicts)
            + "; the outcome is reported unknown rather than picking one"
        )
    return list(latest.values()), note


def classify_outcome(
    contract: EffectContract,
    verdicts: Sequence[RequirementVerdict],
    *,
    attempts: int = 0,
    timed_out: bool = False,
    aborted: str = "",
) -> tuple[OutcomeStatus, str, str]:
    """The ONE status the evidence supports, with its code and reason (pure).

    Precedence is deliberate: an unverifiable contract first, then an aborted
    run, then a REFUTED requirement (the effect was established not to happen),
    then full success, then partial, then the deadline, then unknown.
    """
    if aborted:
        return (
            OutcomeStatus.VERIFICATION_ABORTED,
            "aborted",
            aborted,
        )
    if not contract.verifiable:
        problem = "; ".join(contract.problems) or "no observable evidence was declared"
        return (
            OutcomeStatus.NOT_VERIFIABLE,
            "not_verifiable",
            f"This action cannot be verified: {problem}.",
        )
    satisfied = [item for item in verdicts if item.satisfied]
    refuted = [item for item in verdicts if item.refuted]
    if refuted:
        names = ", ".join(item.field for item in refuted)
        return (
            OutcomeStatus.VERIFIED_FAILURE,
            "effect_not_observed",
            (
                f"The declared observable ({names}) was read in the post-action observation "
                "and did not meet the expected effect."
            ),
        )
    if satisfied and len(satisfied) == len(contract.requirements):
        return (
            OutcomeStatus.VERIFIED_SUCCESS,
            "effect_observed",
            "Every declared observable was satisfied by a direct post-action reading.",
        )
    if satisfied:
        missing = ", ".join(
            item.field for item in verdicts if not item.satisfied and not item.refuted
        )
        return (
            OutcomeStatus.PARTIAL_SUCCESS,
            "partial_effect_observed",
            (
                "Part of the expected effect was observed ("
                + ", ".join(item.field for item in satisfied)
                + f"); {missing or 'the remaining observables'} could not be established."
            ),
        )
    if timed_out:
        return (
            OutcomeStatus.VERIFICATION_TIMED_OUT,
            "verification_deadline",
            (
                f"The verification deadline expired after {attempts} observation attempt(s) "
                "without establishing the expected effect."
            ),
        )
    return (
        OutcomeStatus.OUTCOME_UNKNOWN,
        "evidence_insufficient",
        (
            f"{attempts} observation attempt(s) produced no reading that could decide the "
            "declared observable; missing evidence is not negative evidence."
        ),
    )


def _confidence_basis(decided: int, total: int) -> str:
    if total <= 0:
        return "no declared observable"
    return (
        f"heuristic: {decided} of {total} declared observable(s) decided as satisfied by a "
        "direct reading. This is a share of what was read, not a calibrated probability."
    )


# ---------------------------------------------------------------------------
# Scope E/F — recovery decisions and their bounds
# ---------------------------------------------------------------------------


def transition_pending(outcome: ActionOutcome) -> bool:
    """Did a declared reference observable move TOWARD its target, but not far enough?

    Only a relative requirement that was actually measured before and after can
    say this: the field moved in the declared direction by less than the contract
    asked for. That is an effect still arriving — a *known delayed transition* —
    not a missing reading (the field WAS read) and not a refuted absolute value,
    so the bounded response is to wait briefly and read again instead of
    replanning around an action that is visibly working.

    Measured from the verdicts' own numbers: a requirement the contract did not
    describe numerically (a text-only field, an absolute comparison) can never
    report a pending transition.
    """
    for verdict in outcome.verdicts:
        if verdict.verdict != "refuted" or verdict.motion is None:
            continue
        measured = float(verdict.motion)
        target = abs(float(verdict.motion_target or 0.0))
        if target <= 0 or measured == 0:
            continue
        if verdict.comparison == "increases" and 0 < measured < target:
            return True
        if verdict.comparison == "decreases" and -target < measured < 0:
            return True
        if verdict.comparison == "changes" and 0 < abs(measured) <= target:
            return True
    return False


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """One bounded recovery response, and why it was or was not allowed."""

    action: RecoveryAction
    code: str = ""
    reason: str = ""
    allowed: bool = False
    blocked_reason: str = ""
    requires_operator: bool = False
    requires_approval: bool = False
    repeats_action: bool = False
    attempt: int = 0
    max_attempts: int = 0
    detail: str = ""

    @property
    def side_effecting(self) -> bool:
        return self.action in SIDE_EFFECTING_RECOVERIES

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "code": self.code,
            "reason": self.reason,
            "allowed": self.allowed,
            "blocked_reason": self.blocked_reason,
            "requires_operator": self.requires_operator,
            "requires_approval": self.requires_approval,
            "repeats_action": self.repeats_action,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "detail": self.detail,
        }


@dataclass(slots=True)
class ActionOutcome:
    """The verified (or explicitly unverified) result of ONE executed action.

    Mutable for one reason only: the recovery decision is derived FROM the
    outcome (its status, code and rejected readings), so it is attached after the
    outcome exists instead of being guessed before it does.
    """

    outcome_id: str
    session_id: str
    plan_id: str
    action_id: str
    step_index: int
    status: OutcomeStatus
    contract_id: str = ""
    kind: str = ""
    expected_effect: str = ""
    observed_effect: str = ""
    code: str = ""
    reason: str = ""
    #: The input operation's own code, when the plan's record carried one.
    dispatch_code: str = ""
    verdicts: tuple[RequirementVerdict, ...] = ()
    evidence: tuple[Mapping[str, Any], ...] = ()
    rejected: tuple[Mapping[str, Any], ...] = ()
    confidence: float | None = None
    confidence_basis: str = ""
    ambiguity: str = ""
    method: str = ""
    #: Accepted readings that SCORED a requirement, and observation calls made.
    #: They differ when a reading was rejected (stale, duplicate, wrong target),
    #: and the recovery layer bounds itself by how many reads were taken.
    attempts: int = 0
    observations: int = 0
    recovery: Mapping[str, Any] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    elapsed_ms: float | None = None
    objective_id: str = ""
    objective_revision: int = 0
    knowledge_refs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    schema_version: str = VERIFICATION_SCHEMA_VERSION

    @property
    def verified_success(self) -> bool:
        return self.status is OutcomeStatus.VERIFIED_SUCCESS

    @property
    def verified_failure(self) -> bool:
        return self.status in {
            OutcomeStatus.VERIFIED_FAILURE,
            OutcomeStatus.PARTIAL_SUCCESS,
        }

    @property
    def unverified(self) -> bool:
        return not self.verified_success

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "outcome_id": self.outcome_id,
            "session_id": self.session_id,
            "plan_id": self.plan_id,
            "action_id": self.action_id,
            "step_index": self.step_index,
            "kind": self.kind,
            "status": self.status.value,
            "contract_id": self.contract_id,
            "expected_effect": self.expected_effect,
            "observed_effect": self.observed_effect,
            "code": self.code,
            "reason": self.reason,
            "dispatch_code": self.dispatch_code,
            "verdicts": [item.to_dict() for item in self.verdicts],
            "evidence": [dict(row) for row in self.evidence],
            "rejected": [dict(row) for row in self.rejected],
            "confidence": self.confidence,
            "confidence_basis": self.confidence_basis,
            "ambiguity": self.ambiguity,
            "method": self.method,
            "attempts": self.attempts,
            "observations": self.observations,
            "recovery": dict(self.recovery),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_ms": self.elapsed_ms,
            "objective_id": self.objective_id,
            "objective_revision": self.objective_revision,
            "knowledge_refs": list(self.knowledge_refs),
            "limitations": list(self.limitations),
            "effect_verified": self.verified_success,
            "effect_note": (
                "Verified means the declared observable was read and met the expected "
                "effect. Nothing here claims more than the declared observable."
                if self.verified_success
                else "No verified effect: this action's outcome is explicitly not a success."
            ),
        }


@dataclass(slots=True)
class VerificationResult:
    """One verification run over a plan's executed steps."""

    verification_id: str
    session_id: str
    plan_id: str
    status: VerificationStatus
    code: str = ""
    reason: str = ""
    outcomes: list[ActionOutcome] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    recovery: RecoveryDecision | None = None
    counts: dict[str, int] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    elapsed_ms: float | None = None
    deadline_ms: int = 0
    steps_verified: int = 0
    steps_skipped: int = 0
    steps_capped: int = 0
    effect_verified: bool = False
    policy: dict[str, Any] = field(default_factory=dict)
    schema_version: str = VERIFICATION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verification_id": self.verification_id,
            "session_id": self.session_id,
            "plan_id": self.plan_id,
            "status": self.status.value,
            "code": self.code,
            "reason": self.reason,
            "counts": dict(self.counts),
            "steps": [dict(row) for row in self.steps],
            "steps_verified": self.steps_verified,
            "steps_skipped": self.steps_skipped,
            "steps_capped": self.steps_capped,
            "outcomes": [item.to_dict() for item in self.outcomes],
            "recovery": self.recovery.to_dict() if self.recovery is not None else None,
            "evidence_refs": list(self.evidence_refs),
            "reasons": list(self.reasons),
            "limitations": list(self.limitations),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_ms": self.elapsed_ms,
            "deadline_ms": self.deadline_ms,
            "policy": dict(self.policy),
            "effect_verified": self.effect_verified,
            "effect_note": (
                "Every verified step's declared observable was observed. No wider claim "
                "is made about the game."
                if self.effect_verified
                else "At least one step's effect was not established; no success is claimed."
            ),
        }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    """The verifier's own bounds. Separate from the plan and control policies.

    Separate on purpose: widening an input limit or a plan's action count must
    never silently widen how long verification may run or how many recoveries a
    session may take.
    """

    #: How many executed steps ONE verification run examines.
    max_steps_verified: int = 4
    #: Observation attempts per action, and the wait between them.
    max_observation_attempts: int = 3
    observation_interval_ms: int = 400
    #: The whole run's deadline, and how long a single action's contract may take.
    max_verification_ms: int = 30_000
    max_action_ms: int = 8_000
    #: The declared settle window before the first post-action reading.
    settle_ms: int = 250
    #: How old a reading or an action may be and still be treated as current.
    max_state_age_seconds: float = 5.0
    max_action_age_seconds: float = 60.0
    #: The default minimum change for a relative observable.
    min_change: float = 1.0
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    #: Recovery bounds.
    max_recovery_attempts: int = 2
    max_replans_per_plan: int = 1
    max_session_recoveries: int = 4
    #: How many times ONE action may be given its "wait for the transition" pause,
    #: and the total time a whole session may spend recovering.
    max_transition_waits: int = 1
    max_recovery_ms: int = 20_000
    wait_for_transition_ms: int = 600
    #: Recovery that changes what may run next needs an explicit approval.
    require_approval_for_recovery: bool = True
    allow_replan: bool = True
    allow_pause: bool = True
    max_outcomes_kept: int = 40
    max_results_kept: int = 20

    def errors(self) -> list[str]:
        problems: list[str] = []
        if not 1 <= self.max_steps_verified <= 8:
            problems.append("max_steps_verified must be between 1 and 8.")
        if not 1 <= self.max_observation_attempts <= MAX_OBSERVATION_ATTEMPTS:
            problems.append(f"max_observation_attempts must be between 1 and {MAX_OBSERVATION_ATTEMPTS}.")
        if not 0 <= self.observation_interval_ms <= 5_000:
            problems.append("observation_interval_ms must be between 0 and 5000.")
        if not 100 <= self.max_verification_ms <= MAX_VERIFICATION_MS:
            problems.append(f"max_verification_ms must be between 100 and {MAX_VERIFICATION_MS}.")
        if not 100 <= self.max_action_ms <= MAX_VERIFICATION_MS:
            problems.append(f"max_action_ms must be between 100 and {MAX_VERIFICATION_MS}.")
        if self.max_action_ms > self.max_verification_ms:
            problems.append("max_action_ms must not exceed max_verification_ms.")
        if not 0 <= self.settle_ms <= 5_000:
            problems.append("settle_ms must be between 0 and 5000.")
        if self.max_state_age_seconds <= 0:
            problems.append("max_state_age_seconds must be positive.")
        if self.max_action_age_seconds <= 0:
            problems.append("max_action_age_seconds must be positive.")
        if self.min_change < 0:
            problems.append("min_change cannot be negative.")
        if not 0.0 <= self.confidence_threshold <= 1.0:
            problems.append("confidence_threshold must be a fraction in [0, 1].")
        if not 0 <= self.max_recovery_attempts <= MAX_RECOVERY_ATTEMPTS:
            problems.append(f"max_recovery_attempts must be between 0 and {MAX_RECOVERY_ATTEMPTS}.")
        if not 0 <= self.max_session_recoveries <= MAX_SESSION_RECOVERIES:
            problems.append(f"max_session_recoveries must be between 0 and {MAX_SESSION_RECOVERIES}.")
        if not 0 <= self.max_replans_per_plan <= 3:
            problems.append("max_replans_per_plan must be between 0 and 3.")
        if not 0 <= self.max_transition_waits <= MAX_TRANSITION_WAITS:
            problems.append(f"max_transition_waits must be between 0 and {MAX_TRANSITION_WAITS}.")
        if not 0 <= self.max_recovery_ms <= MAX_RECOVERY_MS:
            problems.append(f"max_recovery_ms must be between 0 and {MAX_RECOVERY_MS}.")
        if not 1 <= self.max_outcomes_kept <= 500:
            problems.append("max_outcomes_kept must be between 1 and 500.")
        if not 1 <= self.max_results_kept <= 200:
            problems.append("max_results_kept must be between 1 and 200.")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_steps_verified": self.max_steps_verified,
            "max_observation_attempts": self.max_observation_attempts,
            "observation_interval_ms": self.observation_interval_ms,
            "max_verification_ms": self.max_verification_ms,
            "max_action_ms": self.max_action_ms,
            "settle_ms": self.settle_ms,
            "max_state_age_seconds": self.max_state_age_seconds,
            "max_action_age_seconds": self.max_action_age_seconds,
            "min_change": self.min_change,
            "confidence_threshold": self.confidence_threshold,
            "max_recovery_attempts": self.max_recovery_attempts,
            "max_replans_per_plan": self.max_replans_per_plan,
            "max_session_recoveries": self.max_session_recoveries,
            "max_transition_waits": self.max_transition_waits,
            "max_recovery_ms": self.max_recovery_ms,
            "wait_for_transition_ms": self.wait_for_transition_ms,
            "require_approval_for_recovery": self.require_approval_for_recovery,
            "allow_replan": self.allow_replan,
            "allow_pause": self.allow_pause,
            "max_outcomes_kept": self.max_outcomes_kept,
            "max_results_kept": self.max_results_kept,
            "confidence_note": (
                "confidence is the share of declared observables decided by a direct "
                "reading; it is a heuristic, not a calibrated probability"
            ),
            "repetition_note": (
                "no recovery action dispatches input, so an unknown outcome cannot "
                "repeat a possibly non-idempotent action"
            ),
        }


# ---------------------------------------------------------------------------
# Declared rules — where an expected observable comes from
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerificationRule:
    """One declared observable for one action shape of one game/objective family.

    A rule is configuration, not perception: it says which HUD field would show
    the effect, never that the effect happened.
    """

    rule_id: str
    requirements: tuple[ObservableRequirement, ...]
    description: str
    game_tokens: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    keys: tuple[str, ...] = ()
    source_tokens: tuple[str, ...] = ()
    settle_ms: int = 250
    deadline_ms: int = 8_000
    failure_conditions: tuple[str, ...] = ()
    recovery_eligible: bool = True
    fixture_only: bool = False
    source: str = "declared verification rule"

    def __post_init__(self) -> None:
        if not str(self.rule_id or "").strip():
            raise ValueError("A verification rule needs an id.")
        if not self.requirements:
            raise ValueError("A verification rule must declare at least one observable.")
        problems = [
            problem for item in self.requirements for problem in requirement_problems(item)
        ]
        if problems:
            raise ValueError("Invalid verification rule: " + "; ".join(problems))
        if not 0 <= self.settle_ms <= 5_000:
            raise ValueError("A settle window must be between 0 and 5000 ms.")
        if not 100 <= self.deadline_ms <= MAX_VERIFICATION_MS:
            raise ValueError(f"A deadline must be between 100 and {MAX_VERIFICATION_MS} ms.")

    def matches(self, game: str, step: Mapping[str, Any]) -> tuple[bool, str]:
        """Whether this rule declares the observable for ONE executed step."""
        lowered = str(game or "").casefold()
        if self.game_tokens and not all(token.casefold() in lowered for token in self.game_tokens):
            return False, "game does not match this rule"
        kind = str(step.get("kind") or "")
        if self.kinds and kind not in self.kinds:
            return False, f"action kind {kind!r} is not declared"
        parameters = step.get("parameters")
        key = ""
        if isinstance(parameters, Mapping):
            key = str(parameters.get("key") or "")
        if self.keys and key not in self.keys:
            return False, f"action parameter key {key!r} is not declared"
        source_text = str(step.get("source") or "").casefold()
        if self.source_tokens and not any(
            token.casefold() in source_text for token in self.source_tokens
        ):
            return False, "the step's declared source is not covered by this rule"
        return True, ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "description": self.description,
            "requirements": [item.to_dict() for item in self.requirements],
            "observable": "; ".join(item.describe() for item in self.requirements),
            "game_tokens": list(self.game_tokens),
            "kinds": list(self.kinds),
            "keys": list(self.keys),
            "source_tokens": list(self.source_tokens),
            "settle_ms": self.settle_ms,
            "deadline_ms": self.deadline_ms,
            "failure_conditions": list(self.failure_conditions),
            "recovery_eligible": self.recovery_eligible,
            "fixture_only": self.fixture_only,
            "source": self.source,
        }


class VerificationRuleRegistry:
    """The declared rules, with duplicates reported rather than hidden."""

    def __init__(self, rules: Sequence[VerificationRule] = ()) -> None:
        self._rules: dict[str, VerificationRule] = {}
        self.rejected: list[dict[str, str]] = []
        for rule in rules:
            if rule.rule_id in self._rules:
                self.rejected.append(
                    {"rule_id": rule.rule_id, "reason": "duplicate rule id; kept the first"}
                )
                continue
            self._rules[rule.rule_id] = rule

    def add(self, rule: VerificationRule) -> bool:
        if rule.rule_id in self._rules:
            return False
        self._rules[rule.rule_id] = rule
        return True

    def all(self) -> tuple[VerificationRule, ...]:
        return tuple(rule for _, rule in sorted(self._rules.items()))

    def rows(self) -> list[dict[str, Any]]:
        return [rule.to_dict() for rule in self.all()]

    def matching(self, game: str, step: Mapping[str, Any]) -> list[VerificationRule]:
        found: list[VerificationRule] = []
        for _, rule in sorted(self._rules.items()):
            ok, _reason = rule.matches(game, step)
            if ok:
                found.append(rule)
        return found


def default_verification_rules() -> VerificationRuleRegistry:
    """The rules this build ships: ONE fixture-scenario rule, no game rules.

    The NF-17 mission fixture's "collect" objective maps onto an NF-20 ``w`` hold
    whose only declared observable is the fixture's own ``progress`` counter. The
    rule says that in its text. No commercial game gets a rule: none has a
    verified effect mapping in this build, and inventing an observable would be
    exactly the fabricated success criterion this phase forbids.
    """
    return VerificationRuleRegistry(
        (
            VerificationRule(
                rule_id="fixture-collect-effect@1",
                requirements=(
                    ObservableRequirement(
                        field="progress",
                        comparison="increases",
                        min_change=1.0,
                        change_kinds=("hud_value_changed", "hud_text_changed"),
                        note="the fixture's declared progress counter",
                    ),
                ),
                description="The fixture progress counter advances after the declared input",
                game_tokens=("nc forge mission",),
                kinds=("key_hold",),
                keys=("w",),
                source_tokens=("fixture-collect@1",),
                settle_ms=250,
                deadline_ms=8_000,
                failure_conditions=("the progress counter was read and did not advance",),
                recovery_eligible=True,
                fixture_only=True,
                source="NF-17 fixture scenario verification rule (not a commercial game)",
            ),
        )
    )


# ---------------------------------------------------------------------------
# Scope G/I — the service
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerificationContext:
    """Everything one verification run is allowed to read before observing.

    Built by the manager from the session's OWN services; a test builds it from
    fixtures. ``baseline_hud`` is the plan's own pre-action reading, recovered
    from the state service's bounded history — when it is gone, relative
    observables report themselves indeterminate instead of guessing a baseline.
    """

    session_id: str
    game: str
    plan: ActionPlan | None
    target: Mapping[str, Any] = field(default_factory=dict)
    gate: Mapping[str, Any] = field(default_factory=dict)
    objective_state: ObjectiveState | None = None
    baseline_hud: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    baseline_state_id: str = ""
    baseline_observation_id: str = ""
    baseline_sequence: int = 0
    knowledge: tuple[KnowledgeRecord, ...] = ()
    now: datetime = field(default_factory=_now)

    def baseline_for(self, field_name: str) -> Mapping[str, Any] | None:
        row = self.baseline_hud.get(field_name)
        return row if isinstance(row, Mapping) else None


@dataclass(slots=True)
class GameVerificationService:
    """Bounded, evidence-grounded outcome verification over ONE session's plan.

    Every collaborator is injected: ``context_reader`` returns the plan plus its
    pre-action evidence, ``observe`` asks for a fresh post-action reading, and the
    recovery seams (``invalidate``/``replan``/``pause``/``stop``/``notify``) are
    the established planning and session interfaces. There is deliberately no
    dispatch seam: this service cannot send input, so no recovery path can repeat
    an action.
    """

    policy: VerificationPolicy
    context_reader: Callable[[str, str], Awaitable[VerificationContext]]
    observe: Callable[[str], Awaitable[ObservationEvidence]]
    rules: VerificationRuleRegistry = field(default_factory=default_verification_rules)
    #: ``(session_id, plan_id, outcomes) -> plan``: NF-20's own record of results.
    record: Callable[[str, str, Sequence[Mapping[str, Any]]], Awaitable[Any]] | None = None
    invalidate: Callable[[str, str], Awaitable[Any]] | None = None
    replan: Callable[[str, str, str], Awaitable[Any]] | None = None
    pause: Callable[[str, str], Awaitable[Any]] | None = None
    stop: Callable[[str, str], Awaitable[Any]] | None = None
    notify: Callable[[str, Mapping[str, Any]], Awaitable[Any]] | None = None
    publish: Publish | None = None
    clock: Callable[[], datetime] = _now
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    _outcomes: OrderedDict[str, ActionOutcome] = field(
        default_factory=OrderedDict, init=False, repr=False
    )
    _results: OrderedDict[str, VerificationResult] = field(
        default_factory=OrderedDict, init=False, repr=False
    )
    _recovery_attempts: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _plan_replans: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _session_recoveries: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    #: Transition waits used per action, and the recovery TIME spent per session.
    #: Both are bound the same way the call counters are: read when a decision is
    #: made AND again when it is applied.
    _transition_waits: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _session_recovery_ms: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _contracts: OrderedDict[str, EffectContract] = field(
        default_factory=OrderedDict, init=False, repr=False
    )
    _last: VerificationResult | None = field(default=None, init=False, repr=False)
    _running: bool = field(default=False, init=False, repr=False)
    _running_session: str = field(default="", init=False, repr=False)
    _cancel: asyncio.Event | None = field(default=None, init=False, repr=False)
    _stopped: bool = field(default=False, init=False, repr=False)
    #: Outcomes are counted by their OWN status (one place, ``_store_outcome``)
    #: so a summary can never disagree with the rows it summarises.
    _counts: dict[str, int] = field(
        default_factory=lambda: {
            **{status.value: 0 for status in OutcomeStatus},
            "runs": 0,
            "refused_runs": 0,
            "recoveries": 0,
            "recovery_refused": 0,
        },
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        problems = self.policy.errors()
        if problems:
            raise ValueError("Invalid verification policy: " + "; ".join(problems))

    # ── read surface ───────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._running

    @property
    def running_session(self) -> str:
        return self._running_session

    @property
    def stopping_requested(self) -> bool:
        return bool(self._cancel is not None and self._cancel.is_set())

    def outcome(self, outcome_id: str) -> ActionOutcome | None:
        return self._outcomes.get(outcome_id)

    def result(self, verification_id: str) -> VerificationResult | None:
        return self._results.get(verification_id)

    def outcomes(self, session_id: str = "", limit: int = 10) -> list[dict[str, Any]]:
        rows = [
            item
            for item in self._outcomes.values()
            if not session_id or item.session_id == session_id
        ]
        return [item.to_dict() for item in rows[-max(1, limit) :]]

    def history(self, session_id: str = "", limit: int = 10) -> list[dict[str, Any]]:
        rows = [
            item
            for item in self._results.values()
            if not session_id or item.session_id == session_id
        ]
        return [item.to_dict() for item in rows[-max(1, limit) :]]

    def status(self, session_id: str = "") -> dict[str, Any]:
        """The panel's view: the bounds, what ran, and why nothing is running."""
        current = self._last if self._last is not None and (
            not session_id or self._last.session_id == session_id
        ) else None
        return {
            "schema_version": VERIFICATION_SCHEMA_VERSION,
            "available": True,
            "running": self._running,
            "running_session": self._running_session,
            "stopped": self._stopped,
            "cancel_requested": self.stopping_requested,
            "policy": self.policy.to_dict(),
            "rules": self.rules.rows(),
            "rejected_rules": list(self.rules.rejected),
            "last": current.to_dict() if current is not None else None,
            "recoveries": {
                "session_total": self._session_recoveries.get(session_id, 0),
                "session_limit": self.policy.max_session_recoveries,
                "session_ms": round(self._session_recovery_ms.get(session_id, 0.0), 1),
                "session_ms_limit": self.policy.max_recovery_ms,
                "transition_waits": {
                    exec_id: waits
                    for exec_id, waits in self._transition_waits.items()
                    if not session_id or self._owns(exec_id, session_id)
                },
                "transition_wait_limit": self.policy.max_transition_waits,
                "per_action": {
                    exec_id: calls
                    for exec_id, calls in self._recovery_attempts.items()
                    if not session_id or self._owns(exec_id, session_id)
                },
                "per_action_limit": self.policy.max_recovery_attempts,
                "replans": {
                    plan_id: calls
                    for plan_id, calls in self._plan_replans.items()
                    if not session_id or self._plan_owned(plan_id, session_id)
                },
                "replan_limit": self.policy.max_replans_per_plan,
            },
            "counts": dict(self._counts),
            "limitations": [
                "Verification reads the state reader's own HUD fields. An effect that "
                "leaves no declared field unchanged on screen cannot be established here.",
                "No recovery action dispatches input: this phase never repeats an action.",
                "confidence is a heuristic share of declared observables, not a probability.",
            ],
        }

    # ── contracts ──────────────────────────────────────────────────────────
    def contract_for(
        self,
        context: VerificationContext,
        step: Mapping[str, Any],
        *,
        plan: ActionPlan,
    ) -> EffectContract:
        """The typed contract for ONE executed step, problems included.

        Two declared sources, both inspectable: a shipped/declared rule, and a
        VERIFIED NF-18 ``control`` record that declares its own observable. A step
        neither covers is a contract with no requirements — reported as
        ``NOT_VERIFIABLE`` rather than given an invented criterion.
        """
        view = self._step_view(plan, step)
        step_index = int(view.get("index") or 0)
        action_id = str(view.get("action_id") or "")
        kind = str(view.get("kind") or "")
        problems: list[str] = []
        requirements: tuple[ObservableRequirement, ...] = ()
        source = ""
        settle_ms = self.policy.settle_ms
        deadline_ms = min(self.policy.max_action_ms, self.policy.max_verification_ms)
        failure_conditions: tuple[str, ...] = ()
        recovery_eligible = False
        matched = self.rules.matching(context.game, view)
        if len(matched) > 1:
            problems.append(
                "more than one verification rule declares the observable for this step "
                f"({', '.join(rule.rule_id for rule in matched)}); refusing to guess"
            )
        elif matched:
            rule = matched[0]
            requirements = tuple(rule.requirements)
            source = f"rule:{rule.rule_id}"
            settle_ms = rule.settle_ms
            deadline_ms = min(rule.deadline_ms, self.policy.max_verification_ms)
            failure_conditions = rule.failure_conditions
            recovery_eligible = rule.recovery_eligible
        else:
            record = self._knowledge_record(context, view)
            if record is not None:
                requirements, record_problems = self._knowledge_requirements(record)
                problems.extend(record_problems)
                source = f"knowledge:{record.knowledge_id}"
                recovery_eligible = True
        if not action_id:
            problems.append("the step record names no action id")
        if len(requirements) > MAX_CONTRACT_REQUIREMENTS:
            problems.append(
                f"the contract declares {len(requirements)} observables; this build checks "
                f"at most {MAX_CONTRACT_REQUIREMENTS}"
            )
        if not str(view.get("dispatched_at") or ""):
            problems.append("the step was never dispatched, so it produced no evidence")
        if not requirements and not problems:
            problems.append(
                "no verification rule and no verified knowledge record declares an "
                "observable for this action"
            )
        contract = EffectContract(
            contract_id=f"verify-{uuid4().hex[:12]}",
            session_id=context.session_id,
            plan_id=plan.plan_id,
            action_id=action_id,
            step_index=step_index,
            kind=kind,
            expected_effect=self._expected_effect(plan, view),
            requirements=requirements,
            pre_state_id=plan.state_id,
            pre_observation_id=plan.observation_id,
            pre_sequence=plan.state_sequence,
            deadline_ms=max(100, int(deadline_ms)),
            settle_ms=max(0, int(settle_ms)),
            confidence_threshold=self.policy.confidence_threshold,
            failure_conditions=failure_conditions,
            recovery_eligible=recovery_eligible,
            dispatch_code=str(view.get("code") or ""),
            source=source,
            problems=tuple(dict.fromkeys(problems)),
        )
        self._contracts[contract.contract_id] = contract
        while len(self._contracts) > self.policy.max_outcomes_kept:
            self._contracts.popitem(last=False)
        return contract

    @staticmethod
    def _step_view(plan: ActionPlan, step: Mapping[str, Any]) -> dict[str, Any]:
        """The EXECUTION record joined with the planned step it came from.

        An execution row carries the input operation's timings, not the shape of
        the action it sent; the planned step carries the kind, the parameters and
        the declared source. Matching a verification rule needs both, so the two
        are joined by action id here instead of at every read site.
        """
        view = dict(step)
        planned = next(
            (
                item
                for item in plan.steps
                if item.action.action_id == str(step.get("action_id") or "")
            ),
            None,
        )
        if planned is not None:
            view["kind"] = str(view.get("kind") or planned.action.kind.value)
            view.setdefault("parameters", planned.action.parameters())
            view["source"] = f"{planned.candidate_id} {planned.source}".strip()
        return view

    @staticmethod
    def _expected_effect(plan: ActionPlan, step: Mapping[str, Any]) -> str:
        for planned in plan.steps:
            if planned.action.action_id == str(step.get("action_id") or ""):
                return planned.expected_effect
        return str(step.get("kind") or "")

    def _knowledge_record(
        self, context: VerificationContext, step: Mapping[str, Any]
    ) -> KnowledgeRecord | None:
        """The verified control record this step came from, if any.

        Matched on the OBSERVED evidence of the step (kind plus its bound key), so
        a record that does not describe this exact action is never used as its
        success criterion.
        """
        parameters = step.get("parameters")
        key = str(parameters.get("key") or "") if isinstance(parameters, Mapping) else ""
        kind = str(step.get("kind") or "")
        for record in context.knowledge:
            if record.status is not KnowledgeStatus.VERIFIED:
                continue
            if record.category not in {KnowledgeCategory.CONTROL, KnowledgeCategory.INTERACTION_RULE}:
                continue
            if not record.verified_effect:
                continue
            content = record.content
            record_kind = str(content.get("kind") or "").strip().lower()
            record_key = str(content.get("key") or "").strip()
            if record_key and record_key != key:
                continue
            if record_kind and record_kind != kind:
                continue
            if not record_key and not record_kind:
                continue
            return record
        return None

    @staticmethod
    def _knowledge_requirements(
        record: KnowledgeRecord,
    ) -> tuple[tuple[ObservableRequirement, ...], list[str]]:
        raw = record.content.get("observable")
        problems: list[str] = []
        if not isinstance(raw, Mapping) or not raw:
            return (), [
                f"the verified control {record.knowledge_id} declares no `observable`, "
                "so its effect cannot be checked"
            ]
        field_name = str(raw.get("field") or "").strip()
        comparison = str(raw.get("comparison") or "").strip().lower()
        if not field_name or comparison not in COMPARISONS:
            return (), [
                f"the verified control {record.knowledge_id} declares an unreadable "
                "observable (it needs `field` and a known `comparison`)"
            ]
        kinds = raw.get("change_kinds")
        change_kinds = (
            tuple(str(item) for item in kinds if str(item).strip())
            if isinstance(kinds, (list, tuple))
            else ()
        )
        try:
            requirement = ObservableRequirement(
                field=field_name,
                comparison=comparison,
                value=_number(raw.get("value")),
                min_change=_number(raw.get("min_change")),
                tolerance=float(raw.get("tolerance") or 0.0),
                change_kinds=change_kinds,
                note=f"declared by knowledge {record.knowledge_id}",
            )
        except (ValueError, TypeError) as exc:
            return (), [f"the verified control {record.knowledge_id} declares a bad observable: {exc}"]
        return (requirement,), problems + requirement_problems(requirement)

    # ── verification ───────────────────────────────────────────────────────
    async def verify(
        self, session_id: str, plan_id: str, *, max_steps: int | None = None
    ) -> VerificationResult:
        """Verify the executed steps of ONE plan against fresh observations."""
        started_at = _stamp(self.clock())
        started = self.monotonic()
        limit = self.policy.max_steps_verified
        if max_steps is not None:
            limit = max(1, min(int(max_steps), self.policy.max_steps_verified))
        result = VerificationResult(
            verification_id=uuid4().hex[:12],
            session_id=session_id,
            plan_id=plan_id,
            status=VerificationStatus.REFUSED,
            started_at=started_at,
            deadline_ms=self.policy.max_verification_ms,
            policy=self.policy.to_dict(),
        )
        if self._stopped:
            return self._finish(result, started, "service_stopped", "Verification is shut down.")
        if self._running:
            return self._finish(
                result,
                started,
                "busy",
                "A verification run is already in progress; one at a time.",
            )
        try:
            context = await self.context_reader(session_id, plan_id)
        except Exception as exc:  # noqa: BLE001 - an unreadable context is a refusal
            return self._finish(
                result,
                started,
                "context_unavailable",
                f"The plan's evidence could not be read: {type(exc).__name__}: {exc}",
            )
        plan = context.plan
        if plan is None or plan.session_id != session_id or plan.plan_id != plan_id:
            # The caller asked to verify ONE plan. Verifying a different plan (the
            # newest one, say) would attach this run's outcomes to the wrong
            # action ids, so a mismatch is refused rather than resolved.
            return self._finish(
                result,
                started,
                "unknown_plan",
                "No plan with that id belongs to this session.",
            )
        gate = context.gate or {}
        if not gate.get("allow"):
            result.reasons.append(str(gate.get("reason") or ""))
            result.status = VerificationStatus.ABORTED
            return self._finish(
                result,
                started,
                str(gate.get("state") or "session_not_allowed"),
                "The session does not allow input right now ("
                f"{gate.get('state') or 'session_not_allowed'}); no verification was attempted.",
            )
        rows = [dict(row) for row in plan.execution.get("steps", []) if isinstance(row, Mapping)]
        if not rows:
            return self._finish(
                result,
                started,
                "no_execution_record",
                "The plan carries no execution record, so there is no outcome to verify.",
            )
        executed = [row for row in rows if str(row.get("dispatched_at") or "")]
        skipped = [row for row in rows if not str(row.get("dispatched_at") or "")]
        if not executed:
            # Nothing ran. Every step is reported SKIPPED and the run claims
            # nothing about the game — an action that was never sent cannot have
            # produced an effect, and it is certainly not a failure.
            for row in skipped:
                result.steps.append(self._skipped_step(row))
            result.steps_skipped = len(skipped)
            return self._finish(
                result,
                started,
                "no_dispatched_steps",
                "The plan dispatched no step, so there is no outcome to verify; "
                f"{len(skipped)} step(s) are reported as skipped.",
            )
        self._running = True
        self._running_session = session_id
        self._cancel = asyncio.Event()
        try:
            await self._announce(
                "novaforge.outcome.started",
                {
                    "verification_id": result.verification_id,
                    "session_id": session_id,
                    "plan_id": plan_id,
                    "steps": len(executed),
                },
            )
            try:
                await self._verify_steps(result, context, plan, executed, skipped, limit, started)
            except Exception as exc:  # noqa: BLE001 - a verifier bug is never a verdict
                result.status = VerificationStatus.REFUSED
                result.code = "verification_error"
                result.reason = (
                    f"The verification run failed ({type(exc).__name__}: {exc}); "
                    "no outcome is reported for it."
                )
                result.effect_verified = False
        finally:
            self._running = False
            self._running_session = ""
            self._cancel = None
        self._counts["runs"] += 1
        self._store_result(result)
        if self.record is not None:
            rows = [self._record_row(item) for item in result.outcomes]
            if rows:
                try:
                    await self.record(session_id, plan_id, rows)
                except Exception as exc:  # noqa: BLE001 - recording is not the verdict
                    result.limitations.append(
                        f"The planner could not be given these outcomes ({type(exc).__name__})."
                    )
        await self._announce("novaforge.outcome.verified", result.to_dict())
        return result

    async def _verify_steps(
        self,
        result: VerificationResult,
        context: VerificationContext,
        plan: ActionPlan,
        executed: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        limit: int,
        started_monotonic: float,
    ) -> None:
        """The bounded loop: one contract per step, one observation per attempt.

        ``executed`` holds the steps whose input operation actually ran and
        ``skipped`` the ones that never did; only the first can produce evidence,
        and the second are reported rather than silently dropped.
        """
        for row in skipped:
            result.steps.append(self._skipped_step(row))
            result.steps_skipped += 1
        if skipped:
            result.reasons.append(
                f"{len(skipped)} step(s) never dispatched and are reported as skipped; "
                "no evidence exists for them."
            )
        if len(executed) > limit:
            result.steps_capped = len(executed) - limit
            result.reasons.append(
                f"Only the first {limit} dispatched step(s) were verified; "
                f"{result.steps_capped} were capped by the policy's bound."
            )
        for step in executed[:limit]:
            if self._elapsed(started_monotonic) > self.policy.max_verification_ms:
                result.steps.append(
                    {
                        "index": step.get("index"),
                        "action_id": step.get("action_id"),
                        "verified": False,
                        "status": "skipped",
                        "code": "deadline_reached",
                        "reason": "the verification deadline was already reached",
                    }
                )
                result.steps_skipped += 1
                result.reasons.append(
                    "The verification deadline was reached before every step was judged."
                )
                break
            if self._stop_requested():
                result.reasons.append("Verification was cancelled before every step was judged.")
                break
            contract = self.contract_for(context, step, plan=plan)
            outcome = await self._verify_action(context, contract, started_monotonic)
            result.outcomes.append(outcome)
            result.steps_verified += 1
            for item in outcome.evidence:
                ref = str(item.get("ref") or "")
                if ref and ref not in result.evidence_refs:
                    result.evidence_refs.append(ref)
            result.steps.append(
                {
                    "index": step.get("index"),
                    "action_id": contract.action_id,
                    "contract_id": contract.contract_id,
                    "verified": True,
                    "status": outcome.status.value,
                    "code": outcome.code,
                    "reason": outcome.reason,
                }
            )
            if outcome.status is OutcomeStatus.VERIFICATION_ABORTED:
                result.reasons.append(outcome.reason)
                break
        # Plan order, so a reader sees the steps in the order they were sent
        # rather than the order the loop happened to judge them in.
        result.steps.sort(key=lambda row: str(row.get("index") or ""))
        result.counts = self._count_statuses(result)
        if not result.outcomes:
            result.status = VerificationStatus.REFUSED
            result.code = "nothing_verified"
            result.reason = (
                "No dispatched step could be verified; see the step records for the reason."
            )
            return
        result.recovery = self._decision_from(result.outcomes[-1])
        result.status = self._summarise(result.outcomes)
        result.code = result.outcomes[-1].code
        result.reason = result.outcomes[-1].reason
        result.effect_verified = all(item.verified_success for item in result.outcomes)

    @staticmethod
    def _skipped_step(row: Mapping[str, Any]) -> dict[str, Any]:
        """One step that produced no evidence, reported as skipped with its reason."""
        return {
            "index": row.get("index"),
            "action_id": row.get("action_id"),
            "verified": False,
            "status": "skipped",
            "code": "not_dispatched",
            "reason": "the input operation never ran, so it produced no evidence",
        }

    @staticmethod
    def _count_statuses(result: VerificationResult) -> dict[str, int]:
        """Per-status counts over what was actually verified (nothing invented)."""
        counts = {status.value: 0 for status in OutcomeStatus}
        counts["total"] = len(result.outcomes)
        for outcome in result.outcomes:
            counts[outcome.status.value] += 1
        return counts

    async def _verify_action(
        self,
        context: VerificationContext,
        contract: EffectContract,
        run_started: float,
    ) -> ActionOutcome:
        """Observe, score, merge and classify ONE action, inside every bound."""
        started_at = _stamp(self.clock())
        started = self.monotonic()
        attempts: list[list[RequirementVerdict]] = []
        accepted: list[Mapping[str, Any]] = []
        rejected: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        aborted = ""
        timed_out = False
        observations = 0
        if not contract.verifiable:
            status, code, reason = classify_outcome(contract, [])
            return self._store_outcome(
                context=context,
                contract=contract,
                status=status,
                code=code,
                reason=reason,
                verdicts=[],
                accepted=[],
                rejected=[],
                attempts=0,
                observations=0,
                ambiguity="",
                method="no-declared-observable",
                started_at=started_at,
                started=started,
            )
        # The deadline is the contract's own, never longer than what is left of
        # the run's bound: a contract may not extend the run it belongs to.
        remaining_ms = self.policy.max_verification_ms - self._elapsed(run_started)
        deadline_ms = max(100, min(contract.deadline_ms, int(remaining_ms)))
        if contract.settle_ms and self._wait_allowed(deadline_ms, started, contract.settle_ms):
            await self._wait(contract.settle_ms)
        for attempt in range(1, self.policy.max_observation_attempts + 1):
            if self._stop_requested():
                aborted = "Verification was cancelled; no further reading was taken."
                break
            if self._elapsed(run_started) > self.policy.max_verification_ms:
                timed_out = True
                break
            if self._elapsed(started) >= deadline_ms:
                timed_out = True
                break
            observations = attempt
            try:
                evidence = await self.observe(context.session_id)
            except Exception as exc:  # noqa: BLE001 - an unreadable reading is evidence of nothing
                rejected.append(
                    {
                        "attempt": attempt,
                        "code": "observation_failed",
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
                if await self._sleep_before_retry(attempt, started, deadline_ms) == "deadline_reached":
                    timed_out = True
                    break
                continue
            gate = self._evidence_gate(evidence, context, contract, seen)
            if gate is not None:
                rejected.append({"attempt": attempt, **gate})
                if gate.get("code") in {"wrong_session", "wrong_target"}:
                    aborted = (
                        "The post-action reading belongs to another session or target; "
                        "the outcome cannot be attributed to this action."
                    )
                    break
                if await self._sleep_before_retry(attempt, started, deadline_ms) == "deadline_reached":
                    timed_out = True
                    break
                continue
            seen.add(evidence.observation_id or evidence.state_id)
            accepted.append(evidence.to_dict())
            scored = [
                evaluate_requirement(
                    requirement,
                    evidence,
                    context.baseline_for(requirement.field),
                    pre_observation_id=contract.pre_observation_id,
                    min_change=self.policy.min_change,
                )
                for requirement in contract.requirements
            ]
            attempts.append(scored)
            if all(item.decisive for item in scored):
                break
            # Readings are exhausted, not the clock: the loop simply has no
            # further observation to take, which is OUTCOME_UNKNOWN and not a
            # deadline that expired.
            if await self._sleep_before_retry(attempt, started, deadline_ms) == "deadline_reached":
                timed_out = True
                break
        verdicts, ambiguity = merge_verdicts(attempts)
        status, code, reason = classify_outcome(
            contract,
            verdicts,
            attempts=len(attempts),
            timed_out=timed_out,
            aborted=aborted,
        )
        if ambiguity and status in {
            OutcomeStatus.VERIFIED_SUCCESS,
            OutcomeStatus.VERIFIED_FAILURE,
            OutcomeStatus.PARTIAL_SUCCESS,
        }:
            status = OutcomeStatus.OUTCOME_UNKNOWN
            code = "conflicting_evidence"
            reason = ambiguity
        elif ambiguity:
            reason = f"{reason} {ambiguity}"
        return self._store_outcome(
            context=context,
            contract=contract,
            status=status,
            code=code,
            reason=reason,
            verdicts=verdicts,
            accepted=accepted,
            rejected=rejected,
            attempts=len(attempts),
            observations=observations,
            ambiguity=ambiguity,
            method="post-action-observation",
            started_at=started_at,
            started=started,
        )

    def _evidence_gate(
        self,
        evidence: ObservationEvidence,
        context: VerificationContext,
        contract: EffectContract,
        seen: set[str],
    ) -> dict[str, Any] | None:
        """Why this reading cannot be used, or ``None`` when it can.

        The five rejections are the phase's five rules: the wrong session, the
        wrong target, a stale reading, an unreadable timestamp, and a reading that
        was already scored.
        """
        identity = evidence.observation_id or evidence.state_id
        if not evidence.accepted:
            return {
                "code": evidence.code or "unusable_reading",
                "reason": evidence.reason or "the reading was not usable",
                "observation_id": evidence.observation_id,
            }
        if evidence.session_id and evidence.session_id != context.session_id:
            return {
                "code": "wrong_session",
                "reason": (
                    f"the reading belongs to session {evidence.session_id!r}, not "
                    f"{context.session_id!r}"
                ),
                "observation_id": evidence.observation_id,
            }
        mismatch = self._target_mismatch(evidence, context)
        if mismatch:
            return {
                "code": "wrong_target",
                "reason": mismatch,
                "observation_id": evidence.observation_id,
            }
        if evidence.age_seconds is None:
            return {
                "code": "age_unknown",
                "reason": "the reading has no readable timestamp, so its freshness is unknown",
                "observation_id": evidence.observation_id,
            }
        if evidence.age_seconds > self.policy.max_state_age_seconds:
            return {
                "code": "stale_reading",
                "reason": (
                    f"the reading is {evidence.age_seconds:.1f}s old (limit "
                    f"{self.policy.max_state_age_seconds:.1f}s)"
                ),
                "observation_id": evidence.observation_id,
            }
        # A sequence of 0 is the model's "not recorded" value, so only a KNOWN
        # sequence that is older than the plan's own state is superseded.
        if contract.pre_sequence and evidence.sequence and evidence.sequence < contract.pre_sequence:
            return {
                "code": "superseded_reading",
                "reason": "the reading is older than the state the action was planned from",
                "observation_id": evidence.observation_id,
            }
        if identity and identity in seen:
            return {
                "code": "duplicate_reading",
                "reason": "this reading was already scored in this verification",
                "observation_id": evidence.observation_id,
            }
        return None

    @staticmethod
    def _target_mismatch(
        evidence: ObservationEvidence, context: VerificationContext
    ) -> str:
        pinned = {
            "handle": str(context.target.get("handle") or ""),
            "pid": str(context.target.get("pid") or ""),
            "start_time": str(context.target.get("start_time") or ""),
        }
        live = {
            "handle": str(evidence.target.get("handle") or ""),
            "pid": str(evidence.target.get("pid") or ""),
            "start_time": str(evidence.target.get("start_time") or ""),
        }
        if not any(pinned.values()) or not any(live.values()):
            return ""
        changed = [
            f"{key} {pinned[key]} → {live[key]}"
            for key in ("handle", "pid", "start_time")
            if pinned[key] and live[key] and pinned[key] != live[key]
        ]
        return (
            "the reading's target is no longer the target the action was sent to ("
            + "; ".join(changed)
            + ")"
            if changed
            else ""
        )

    def _store_outcome(
        self,
        *,
        context: VerificationContext,
        contract: EffectContract,
        status: OutcomeStatus,
        code: str,
        reason: str,
        verdicts: Sequence[RequirementVerdict],
        accepted: Sequence[Mapping[str, Any]],
        rejected: Sequence[Mapping[str, Any]],
        attempts: int,
        observations: int,
        ambiguity: str,
        method: str,
        started_at: str,
        started: float,
    ) -> ActionOutcome:
        satisfied = [item for item in verdicts if item.satisfied]
        objective = self._objective(context)
        outcome = ActionOutcome(
            outcome_id=uuid4().hex[:12],
            session_id=context.session_id,
            plan_id=contract.plan_id,
            action_id=contract.action_id,
            step_index=contract.step_index,
            status=status,
            contract_id=contract.contract_id,
            kind=contract.kind,
            expected_effect=contract.expected_effect,
            observed_effect="; ".join(
                f"{item.field}: {item.observed or item.verdict}" for item in verdicts
            ),
            code=code,
            reason=reason,
            dispatch_code=contract.dispatch_code,
            verdicts=tuple(verdicts),
            evidence=tuple(dict(row) for row in accepted),
            rejected=tuple(dict(row) for row in rejected),
            confidence=(
                round(len(satisfied) / len(contract.requirements), 3)
                if verdicts and contract.requirements
                else None
            ),
            confidence_basis=_confidence_basis(len(satisfied), len(contract.requirements)),
            ambiguity=ambiguity,
            method=method,
            attempts=attempts,
            observations=observations,
            started_at=started_at,
            finished_at=_stamp(self.clock()),
            elapsed_ms=round(max(0.0, (self.monotonic() - started) * 1000.0), 1),
            objective_id=objective.objective_id if objective is not None else "",
            objective_revision=objective.revision if objective is not None else 0,
            knowledge_refs=(contract.source,) if contract.source else (),
            limitations=(
                "The expected effect is the contract's declared observable, not a claim "
                "about the game's wider behaviour.",
            ),
        )
        # The decision is derived FROM the outcome, so it is attached here; the
        # status counter also moves here so a summary can never disagree with the
        # rows it summarises.
        outcome.recovery = self.decide_recovery(
            outcome, context, recovery_eligible=contract.recovery_eligible
        ).to_dict()
        self._outcomes[outcome.outcome_id] = outcome
        self._counts[outcome.status.value] = self._counts.get(outcome.status.value, 0) + 1
        while len(self._outcomes) > self.policy.max_outcomes_kept:
            self._outcomes.popitem(last=False)
        return outcome

    def _store_result(self, result: VerificationResult) -> None:
        result.finished_at = _stamp(self.clock())
        self._results[result.verification_id] = result
        while len(self._results) > self.policy.max_results_kept:
            self._results.popitem(last=False)
        self._last = result

    def _finish(
        self,
        result: VerificationResult,
        started: float,
        code: str,
        reason: str,
    ) -> VerificationResult:
        """Close a run that never reached the loop, without pretending it did."""
        result.code = code
        result.reason = reason
        result.counts = self._count_statuses(result)
        self._counts["refused_runs"] += 1
        result.finished_at = _stamp(self.clock())
        result.elapsed_ms = round(max(0.0, self.monotonic() - started) * 1000.0, 1)
        self._store_result(result)
        return result

    @staticmethod
    def _summarise(outcomes: Sequence[ActionOutcome]) -> VerificationStatus:
        """The worst finding that matters, in one order (like a diagnostic state)."""
        if not outcomes:
            return VerificationStatus.REFUSED
        values = [item.status for item in outcomes]
        if OutcomeStatus.VERIFICATION_ABORTED in values:
            return VerificationStatus.ABORTED
        if OutcomeStatus.NOT_VERIFIABLE in values and len(set(values)) == 1:
            return VerificationStatus.NOT_VERIFIABLE
        if OutcomeStatus.VERIFIED_FAILURE in values:
            return VerificationStatus.FAILED
        if OutcomeStatus.PARTIAL_SUCCESS in values:
            return VerificationStatus.PARTIAL
        if OutcomeStatus.VERIFICATION_TIMED_OUT in values:
            return VerificationStatus.TIMED_OUT
        if OutcomeStatus.OUTCOME_UNKNOWN in values:
            return VerificationStatus.UNKNOWN
        if OutcomeStatus.NOT_VERIFIABLE in values:
            return VerificationStatus.NOT_VERIFIABLE
        return VerificationStatus.VERIFIED

    @staticmethod
    def _objective(context: VerificationContext) -> Any:
        source = context.objective_state
        if source is None:
            return None
        for item in source.objectives:
            if item.session_id == context.session_id and item.status in {
                ObjectiveStatus.ACTIVE,
                ObjectiveStatus.IN_PROGRESS,
                ObjectiveStatus.DISCOVERED,
                ObjectiveStatus.BLOCKED,
            }:
                return item
        return source.objectives[0] if source.objectives else None

    # ── recovery ───────────────────────────────────────────────────────────
    def recovery_block(self, action: RecoveryAction, outcome: ActionOutcome) -> str:
        """Why this recovery may NOT run right now, or ``""`` when it may.

        Read at BOTH moments — when the decision is made and again when it is
        applied — because the counters move in between: a decision recorded
        before the first attempt must not authorise an unlimited number of them.
        """
        attempts = self._recovery_attempts.get(outcome.action_id, 0)
        session_calls = self._session_recoveries.get(outcome.session_id, 0)
        if attempts >= self.policy.max_recovery_attempts:
            return f"the recovery limit for this action ({self.policy.max_recovery_attempts}) is reached"
        if session_calls >= self.policy.max_session_recoveries:
            return (
                "the session's recovery limit "
                f"({self.policy.max_session_recoveries}) is reached"
            )
        spent_ms = self._session_recovery_ms.get(outcome.session_id, 0.0)
        if self.policy.max_recovery_ms and spent_ms >= self.policy.max_recovery_ms:
            return (
                "the session's recovery time budget "
                f"({self.policy.max_recovery_ms} ms) is spent ({spent_ms:.0f} ms used)"
            )
        if action is RecoveryAction.WAIT_FOR_TRANSITION and (
            not self.policy.wait_for_transition_ms
            or self._transition_waits.get(outcome.action_id, 0)
            >= self.policy.max_transition_waits
        ):
            return "this action's transition wait is already spent"
        if action is RecoveryAction.REPLAN:
            if not self.policy.allow_replan:
                return "replanning is disabled by policy"
            if self.replan is None:
                return "no planning interface is wired for recovery"
            if self._plan_replans.get(outcome.plan_id, 0) >= self.policy.max_replans_per_plan:
                return f"this plan was already replanned {self.policy.max_replans_per_plan} time(s)"
        if action is RecoveryAction.PAUSE_SESSION and not self.policy.allow_pause:
            return "pausing the session is disabled by policy"
        if action is RecoveryAction.STOP_OPERATION and not self.policy.allow_pause:
            return "stopping the session is disabled by policy"
        if action is RecoveryAction.INVALIDATE_PLAN and self.invalidate is None:
            return "no plan interface is wired to invalidate it"
        return ""

    def decide_recovery(
        self,
        outcome: ActionOutcome,
        context: VerificationContext,
        *,
        recovery_eligible: bool = True,
    ) -> RecoveryDecision:
        """One bounded response for ONE outcome — never a universal retry."""
        attempts = self._recovery_attempts.get(outcome.action_id, 0)
        max_attempts = self.policy.max_recovery_attempts
        gate = context.gate or {}
        gate_state = str(gate.get("state") or "")
        objective_changed = bool(
            context.plan is not None
            and context.objective_state is not None
            and outcome.objective_id
            and context.plan.objective_id
            and outcome.objective_id != context.plan.objective_id
        )

        def waiting_for_transition() -> bool:
            """May this action be given its bounded "wait for the transition" pause?

            A wait is only chosen when the contract observes the transition IN
            PROGRESS and the action has not already been given one: a wait that
            did not resolve the outcome must not be repeated.
            """
            return bool(
                self.policy.wait_for_transition_ms
                and self._transition_waits.get(outcome.action_id, 0)
                < self.policy.max_transition_waits
                and transition_pending(outcome)
            )

        def decision(action: RecoveryAction, code: str, reason: str, **extra: Any) -> RecoveryDecision:
            blocked = self.recovery_block(action, outcome)
            if blocked and action is not RecoveryAction.REQUEST_OPERATOR:
                return RecoveryDecision(
                    action=RecoveryAction.REQUEST_OPERATOR,
                    code="recovery_exhausted",
                    reason=f"{reason} Recovery was escalated instead: {blocked}.",
                    allowed=True,
                    blocked_reason=blocked,
                    requires_operator=True,
                    requires_approval=False,
                    attempt=attempts,
                    max_attempts=max_attempts,
                    detail="Unresolved outcomes are escalated rather than retried.",
                )
            return RecoveryDecision(
                action=action,
                code=code,
                reason=reason,
                allowed=True,
                blocked_reason="",
                requires_approval=bool(
                    self.policy.require_approval_for_recovery
                    and action in SIDE_EFFECTING_RECOVERIES
                ),
                requires_operator=bool(extra.get("requires_operator", False)),
                repeats_action=False,
                attempt=attempts,
                max_attempts=max_attempts,
                detail=str(extra.get("detail") or ""),
            )

        if outcome.status is OutcomeStatus.VERIFIED_SUCCESS:
            return decision(
                RecoveryAction.NONE,
                "nothing_to_recover",
                "The declared observable was observed; no recovery is needed.",
            )
        if gate_state in _UNSAFE_GATE_STATES or not gate.get("allow"):
            return decision(
                RecoveryAction.STOP_OPERATION,
                f"unsafe_session:{gate_state or 'gate_refused'}",
                (
                    "The session's target is no longer trustworthy ("
                    f"{gate_state or 'the gate refused'}); further action dispatch stops."
                ),
                detail=str(gate.get("reason") or ""),
            )
        if objective_changed:
            return decision(
                RecoveryAction.INVALIDATE_PLAN,
                "objective_changed",
                "The active objective changed since this plan was built; the plan is obsolete.",
            )
        if outcome.status is OutcomeStatus.VERIFICATION_ABORTED:
            return decision(
                RecoveryAction.STOP_OPERATION,
                "verification_aborted",
                "Verification was aborted, so the session is stopped rather than retried.",
            )
        if outcome.status is OutcomeStatus.NOT_VERIFIABLE:
            return decision(
                RecoveryAction.REQUEST_OPERATOR,
                "not_verifiable",
                (
                    "No observable is declared for this action, so its effect cannot be "
                    "checked; a person has to decide what to do next."
                ),
                requires_operator=True,
            )
        if outcome.status is OutcomeStatus.PARTIAL_SUCCESS:
            if waiting_for_transition():
                return decision(
                    RecoveryAction.WAIT_FOR_TRANSITION,
                    "delayed_transition",
                    (
                        "A declared reference observable is moving toward its target but "
                        "has not reached it; this is a known delayed transition, so the "
                        "action's own settle window is waited out and read again."
                    ),
                    detail=(
                        "Nothing is dispatched by the wait, and it is allowed once per "
                        "action."
                    ),
                )
            return decision(
                RecoveryAction.REEVALUATE_STATE,
                "partial_effect",
                (
                    "Only part of the expected effect was observed; the state is read again "
                    "rather than the action being repeated."
                ),
            )
        if outcome.status is OutcomeStatus.OUTCOME_UNKNOWN:
            code = outcome.code or "evidence_insufficient"
            every_reading_stale = bool(outcome.rejected) and not outcome.evidence and all(
                str(row.get("code") or "") == "stale_reading" for row in outcome.rejected
            )
            if every_reading_stale:
                # Nothing was EVER scored: every frame the observer returned was too
                # old to use, so the capture path itself is not producing current
                # evidence. Asking for one more reading of the same frozen screen
                # would spend the whole recovery budget on a doomed read, and
                # dispatching on it would be blind — the safe pause, with the
                # operator told why.
                return decision(
                    RecoveryAction.PAUSE_SESSION,
                    "evidence_stale",
                    (
                        "Every reading for this action was stale and none was scored, so "
                        "no current evidence exists; the session is paused safely instead "
                        "of dispatching on a frame nobody can trust."
                    ),
                    requires_operator=True,
                )
            if any(str(row.get("code") or "") == "stale_reading" for row in outcome.rejected):
                return decision(
                    RecoveryAction.REFRESH_OBSERVATION,
                    "stale_evidence",
                    (
                        "At least one reading was stale (and others were read), so a fresh "
                        "bounded observation is requested before anything else."
                    ),
                )
            if (outcome.observations or outcome.attempts) < self.policy.max_observation_attempts:
                return decision(
                    RecoveryAction.REFRESH_OBSERVATION,
                    code,
                    "No reading established the effect; another bounded observation is asked for.",
                )
            return decision(
                RecoveryAction.REQUEST_OPERATOR,
                "evidence_exhausted",
                (
                    f"{outcome.observations or outcome.attempts} reading(s) did not establish "
                    "the effect and no new one is expected; a person has to decide what to "
                    "do next."
                ),
                requires_operator=True,
            )
        if outcome.status is OutcomeStatus.VERIFICATION_TIMED_OUT:
            return decision(
                RecoveryAction.REQUEST_OPERATOR,
                "verification_timeout",
                (
                    "The verification deadline expired without establishing the effect; the "
                    "action is not repeated on a timeout."
                ),
                requires_operator=True,
            )
        # VERIFIED_FAILURE: the kind of failure decides, never a universal retry.
        refused = (outcome.dispatch_code or outcome.code) in _REFUSAL_CODES or any(
            str(row.get("code") or "") in _REFUSAL_CODES for row in outcome.rejected
        )
        if refused:
            return decision(
                RecoveryAction.INVALIDATE_PLAN,
                "action_refused",
                "The step's input operation was refused; the plan built on it is invalidated.",
            )
        if not recovery_eligible:
            return decision(
                RecoveryAction.REQUEST_OPERATOR,
                "not_recoverable",
                "This action is declared not eligible for recovery; a person decides next.",
                requires_operator=True,
            )
        if waiting_for_transition():
            return decision(
                RecoveryAction.WAIT_FOR_TRANSITION,
                "delayed_transition",
                (
                    "The declared effect was measured moving toward its target but has not "
                    "reached it yet; the transition is waited out and read again rather "
                    "than replanning around an effect that is still arriving."
                ),
                detail="Nothing is dispatched by the wait, and it is allowed once per action.",
            )
        if self.policy.allow_replan:
            return decision(
                RecoveryAction.REPLAN,
                "action_failed",
                (
                    "The declared effect was established NOT to have happened; a new plan may "
                    "be requested from fresh evidence (it still needs its own approval)."
                ),
                detail=(
                    "Replanning produces a plan, not an action: nothing is dispatched by "
                    "the recovery itself."
                ),
            )
        return decision(
            RecoveryAction.REQUEST_OPERATOR,
            "replan_disabled",
            "The action failed and replanning is disabled; a person decides what to do next.",
            requires_operator=True,
        )

    async def recover(
        self, session_id: str, outcome_id: str, *, approve: bool = False
    ) -> dict[str, Any]:
        """Carry out the recorded decision for ONE outcome, inside its bounds."""
        outcome = self._outcomes.get(outcome_id)
        if outcome is None or outcome.session_id != session_id:
            return self._recovery_refusal("unknown_outcome", "No such outcome belongs to this session.")
        decision = self._decision_from(outcome)
        if decision.action is RecoveryAction.NONE:
            return {
                "session_id": session_id,
                "outcome_id": outcome_id,
                "applied": False,
                "requires_approval": False,
                "decision": decision.to_dict(),
                "code": "nothing_to_recover",
                "reason": decision.reason,
                "result": None,
            }
        # The bounds are re-read HERE, not only when the decision was recorded:
        # the counters have moved since then, and a recorded decision must not
        # authorise a second, third or hundredth attempt past its own limit.
        blocked = self.recovery_block(decision.action, outcome)
        if blocked or not decision.allowed:
            return self._recovery_refusal(
                "recovery_not_allowed",
                blocked or decision.blocked_reason or decision.reason,
                outcome=outcome,
                blocked=blocked,
            )
        if decision.requires_approval and not approve and self.policy.require_approval_for_recovery:
            return {
                "session_id": session_id,
                "outcome_id": outcome_id,
                "applied": False,
                "requires_approval": True,
                "decision": decision.to_dict(),
                "code": "approval_required",
                "reason": (
                    f"Recovery action {decision.action.value!r} changes what may run next and "
                    "needs the operator's explicit approval."
                ),
                "result": None,
            }
        self._recovery_attempts[outcome.action_id] = (
            self._recovery_attempts.get(outcome.action_id, 0) + 1
        )
        if decision.action is RecoveryAction.WAIT_FOR_TRANSITION:
            self._transition_waits[outcome.action_id] = (
                self._transition_waits.get(outcome.action_id, 0) + 1
            )
        self._session_recoveries[session_id] = self._session_recoveries.get(session_id, 0) + 1
        self._counts["recoveries"] += 1
        code = ""
        reason = ""
        applied = False
        started = self.monotonic()
        try:
            applied, code, reason, detail = await self._apply_recovery(
                session_id, outcome, decision
            )
        except Exception as exc:  # noqa: BLE001 - a refused seam is a refusal, not a crash
            applied = False
            code = "recovery_failed"
            reason = f"{type(exc).__name__}: {exc}"
            detail = None
        # The session's recovery TIME is spent whether the seam worked or not: a
        # recovery that failed still consumed the session's budget for recovering.
        self._session_recovery_ms[session_id] = self._session_recovery_ms.get(
            session_id, 0.0
        ) + max(0.0, (self.monotonic() - started) * 1000.0)
        await self._announce(
            "novaforge.outcome.recovery",
            {
                "session_id": session_id,
                "plan_id": outcome.plan_id,
                "outcome_id": outcome_id,
                "action_id": outcome.action_id,
                "outcome_status": outcome.status.value,
                "recovery": decision.action.value,
                "applied": applied,
                "code": code,
                "reason": reason,
                "attempts": self._recovery_attempts.get(outcome.action_id, 0),
            },
        )
        return {
            "session_id": session_id,
            "outcome_id": outcome_id,
            "applied": applied,
            "requires_approval": False,
            "decision": decision.to_dict(),
            "code": code,
            "reason": reason,
            "attempts": self._recovery_attempts.get(outcome.action_id, 0),
            "result": detail if isinstance(detail, (dict, list, str)) else None,
        }

    async def _apply_recovery(
        self, session_id: str, outcome: ActionOutcome, decision: RecoveryDecision
    ) -> tuple[bool, str, str, Any]:
        action = decision.action
        if action is RecoveryAction.REQUEST_OPERATOR:
            payload = {
                "session_id": session_id,
                "plan_id": outcome.plan_id,
                "action_id": outcome.action_id,
                "outcome_id": outcome.outcome_id,
                "status": outcome.status.value,
                "code": outcome.code,
                "reason": decision.reason,
            }
            detail = None
            if self.notify is not None:
                detail = await self.notify(session_id, payload)
            return (
                True,
                "operator_notified",
                "The unresolved outcome was escalated to the operator with its evidence.",
                detail,
            )
        if action is RecoveryAction.INVALIDATE_PLAN:
            if self.invalidate is None:
                return False, "no_plan_interface", "No plan interface is wired to invalidate it.", None
            detail = await self.invalidate(session_id, outcome.plan_id)
            return (
                True,
                "plan_invalidated",
                "The plan whose step produced this outcome was invalidated; it cannot run again.",
                detail,
            )
        if action is RecoveryAction.REPLAN:
            if self.replan is None:
                return False, "no_plan_interface", "No planning interface is wired for recovery.", None
            reason = "action_failed" if outcome.status is OutcomeStatus.VERIFIED_FAILURE else "action_refused"
            self._plan_replans[outcome.plan_id] = self._plan_replans.get(outcome.plan_id, 0) + 1
            detail = await self.replan(session_id, outcome.plan_id, reason)
            return (
                True,
                "replanned",
                (
                    "A new plan was requested from fresh evidence; it needs its own approval "
                    "before anything can run."
                ),
                detail,
            )
        if action is RecoveryAction.PAUSE_SESSION:
            if self.pause is None:
                return False, "no_session_interface", "No session interface is wired to pause it.", None
            detail = await self.pause(session_id, "outcome_recovery")
            return True, "session_paused", "The session was paused for the operator to review.", detail
        if action is RecoveryAction.STOP_OPERATION:
            stop_detail: dict[str, Any] = {}
            if self.invalidate is not None and outcome.plan_id:
                try:
                    stop_detail["invalidate"] = _summarise_seam(
                        await self.invalidate(session_id, outcome.plan_id)
                    )
                except Exception as exc:  # noqa: BLE001 - report both halves
                    stop_detail["invalidate"] = f"{type(exc).__name__}: {exc}"
            if self.stop is not None:
                stop_detail["stop"] = _summarise_seam(
                    await self.stop(session_id, "outcome_recovery")
                )
            elif self.pause is not None:
                stop_detail["pause"] = _summarise_seam(
                    await self.pause(session_id, "outcome_recovery")
                )
            if not any(key in stop_detail for key in ("stop", "pause")):
                # Without a session interface there is nothing to stop through,
                # and saying "stopped" would be a cleanup claim with no evidence.
                return (
                    False,
                    "no_session_interface",
                    "No session interface is wired to stop or pause the session.",
                    stop_detail,
                )
            return (
                True,
                "operation_stopped",
                (
                    "Further action dispatch stopped, outstanding plan work was cancelled and "
                    "the session was stopped through its own interface."
                ),
                stop_detail,
            )
        if action in {
            RecoveryAction.REFRESH_OBSERVATION,
            RecoveryAction.WAIT_FOR_TRANSITION,
            RecoveryAction.REEVALUATE_STATE,
        }:
            if action is RecoveryAction.WAIT_FOR_TRANSITION and self.policy.wait_for_transition_ms:
                await self._pause_for(self.policy.wait_for_transition_ms / 1000.0)
            contract = self._contracts.get(outcome.contract_id)
            if contract is None:
                return (
                    False,
                    "contract_unavailable",
                    "This action's contract is no longer retained; run a new verification.",
                    None,
                )
            try:
                context = await self.context_reader(session_id, outcome.plan_id)
            except Exception as exc:  # noqa: BLE001 - an unreadable context is a refusal
                return False, "context_unavailable", f"{type(exc).__name__}: {exc}", None
            if not (context.gate or {}).get("allow"):
                return (
                    False,
                    "session_not_allowed",
                    "The session does not allow a verification reading right now.",
                    None,
                )
            fresh = await self._verify_action(context, contract, self.monotonic())
            return (
                True,
                "reobserved",
                (
                    "A fresh bounded reading was taken for this action (no input was "
                    f"dispatched by the recovery); the new outcome is {fresh.status.value}."
                ),
                {
                    "outcome_id": fresh.outcome_id,
                    "status": fresh.status.value,
                    "code": fresh.code,
                    "reason": fresh.reason,
                    "confidence": fresh.confidence,
                },
            )
        return False, "unsupported_recovery", f"{action.value!r} is not implemented.", None

    def _recovery_refusal(
        self,
        code: str,
        reason: str,
        *,
        outcome: ActionOutcome | None = None,
        blocked: str = "",
    ) -> dict[str, Any]:
        """Refuse a recovery, reporting WHICH bound refused it.

        ``blocked`` is folded into the decision so a caller reading the answer
        back sees the reason this particular request was declined instead of a
        decision that looks allowed.
        """
        self._counts["recovery_refused"] += 1
        decision = self._decision_from(outcome) if outcome is not None else None
        if decision is not None and blocked:
            decision = replace(decision, allowed=False, blocked_reason=blocked)
        return {
            "session_id": outcome.session_id if outcome is not None else "",
            "outcome_id": outcome.outcome_id if outcome is not None else "",
            "applied": False,
            "requires_approval": False,
            "decision": decision.to_dict() if decision is not None else None,
            "code": code,
            "reason": reason,
            "result": None,
        }

    @staticmethod
    def _decision_from(outcome: ActionOutcome) -> RecoveryDecision:
        raw = dict(outcome.recovery or {})
        try:
            action = RecoveryAction(str(raw.get("action") or RecoveryAction.NONE.value))
        except ValueError:
            action = RecoveryAction.REQUEST_OPERATOR
        return RecoveryDecision(
            action=action,
            code=str(raw.get("code") or ""),
            reason=str(raw.get("reason") or ""),
            allowed=bool(raw.get("allowed", False)),
            blocked_reason=str(raw.get("blocked_reason") or ""),
            requires_operator=bool(raw.get("requires_operator", False)),
            requires_approval=bool(raw.get("requires_approval", False)),
            repeats_action=bool(raw.get("repeats_action", False)),
            attempt=int(raw.get("attempt") or 0),
            max_attempts=int(raw.get("max_attempts") or 0),
            detail=str(raw.get("detail") or ""),
        )

    async def cancel(self, session_id: str = "") -> dict[str, Any]:
        """Ask an in-flight verification to stop at its next checkpoint (idempotent)."""
        if self._cancel is not None and self._running:
            self._cancel.set()
            return {
                "session_id": self._running_session,
                "cancel_requested": True,
                "running": True,
                "reason": "The in-flight verification stops at its next checkpoint.",
            }
        return {
            "session_id": session_id or self._running_session,
            "cancel_requested": False,
            "running": False,
            "reason": "No verification is in flight.",
        }

    async def shutdown(self) -> None:
        """Stop accepting runs and ask one in flight to stop. Safe to repeat."""
        self._stopped = True
        if self._cancel is not None:
            self._cancel.set()

    # ── records / helpers ──────────────────────────────────────────────────
    def _record_row(self, outcome: ActionOutcome) -> dict[str, Any]:
        """The bounded summary NF-20 accepts: a status, never a story."""
        return {
            "action_id": outcome.action_id,
            "outcome_id": outcome.outcome_id,
            "status": outcome.status.value,
            "effect_verified": outcome.verified_success,
            "code": outcome.code,
            "reason": outcome.reason,
            "observed_effect": outcome.observed_effect,
            "confidence": outcome.confidence,
            "observed_at": outcome.finished_at,
            "evidence_refs": [str(row.get("ref") or "") for row in outcome.evidence],
            "recovery": str((outcome.recovery or {}).get("action") or ""),
        }

    def _stop_requested(self) -> bool:
        return bool(self._cancel is not None and self._cancel.is_set()) or self._stopped

    def _elapsed(self, started: float) -> float:
        return max(0.0, (self.monotonic() - started) * 1000.0)

    def _wait_allowed(self, deadline_ms: int, started: float, wait_ms: int) -> bool:
        return (self.monotonic() - started) * 1000.0 + wait_ms <= deadline_ms

    async def _wait(self, wait_ms: int) -> None:
        """Wait ONE bounded window through the injected sleep.

        Awaited, never ``time.sleep``: this service runs inside the web server's
        event loop, and a settle window that blocked the loop would stall every
        other request for its duration.
        """
        if wait_ms <= 0:
            return
        await self.sleep(wait_ms / 1000.0)

    async def _pause_for(self, seconds: float) -> None:
        if seconds <= 0:
            return
        await self.sleep(seconds)

    async def _sleep_before_retry(
        self, attempt: int, started: float, deadline_ms: int
    ) -> str:
        """Wait one bounded interval, or say why no further reading is taken.

        The two refusals are kept apart on purpose: ``attempts_exhausted`` means
        the policy's observation bound was reached and the evidence is simply
        insufficient (``OUTCOME_UNKNOWN``), while ``deadline_reached`` means the
        clock ran out (``VERIFICATION_TIMED_OUT``). Folding them together would
        report a blurry reading as an expired deadline.
        """
        if attempt >= self.policy.max_observation_attempts:
            return "attempts_exhausted"
        wait_ms = self.policy.observation_interval_ms
        if not self._wait_allowed(deadline_ms, started, wait_ms):
            return "deadline_reached"
        await self._wait(wait_ms)
        return "waited"

    def _owns(self, action_id: str, session_id: str) -> bool:
        outcome = next(
            (item for item in self._outcomes.values() if item.action_id == action_id), None
        )
        return outcome is None or outcome.session_id == session_id

    def _plan_owned(self, plan_id: str, session_id: str) -> bool:
        outcome = next((item for item in self._outcomes.values() if item.plan_id == plan_id), None)
        return outcome is None or outcome.session_id == session_id

    async def _announce(self, event_type: str, payload: dict[str, Any]) -> None:
        """Announce one edge; a broken observer never fails the verification."""
        if self.publish is None:
            return
        try:
            await self.publish(event_type, payload)
        except Exception:  # noqa: BLE001 - an announcement is not the work
            return


def _summarise_seam(value: Any) -> Any:
    """A bounded summary of one recovery seam's answer, for the audit row."""
    if isinstance(value, Mapping):
        return {
            str(key): value[key]
            for key in (
                "status",
                "code",
                "reason",
                "plan_id",
                "state",
                "cancelled",
                "invalidated",
                "released",
                "held_after",
            )
            if key in value
        }
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:200]
