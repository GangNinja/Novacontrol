"""The data-quality gate: what is good enough to learn from, said out loud.

A trajectory store is only worth as much as the rows in it. This filter reads
each row, runs a fixed set of CHECKS, and answers with one of three verdicts —
ACCEPTED, REJECTED, NEEDS_REVIEW — plus every reason behind the answer, in a
shape a reader (and a diff) can act on:

    {"code": "failed_verification", "severity": "warn",
     "detail": "verification failed on step-2 while the run reported success"}

Three rules shape it:

  * **nothing is deleted silently.** The filter classifies; it never removes a
    row from a store. Rejected rows keep their structured reasons, so "why did
    the dataset shrink" is answerable afterwards.
  * **review is a real verdict, not a soft rejection.** A run that ended without
    anything deciding whether it worked, or with a failed verification beside a
    success claim, is genuinely ambiguous. Calling that "rejected" would throw
    away the interesting half of the data; calling it "accepted" would poison
    the set. It is held.
  * **the lines it draws are configuration.** Thresholds (how many steps is too
    many, how many events is noise, whether residual sensitive text rejects or
    is held for review) live in :class:`QualityConfig`, because the right line
    moves with the workload.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from novacontrol.audit.redact import Redactor
from novacontrol.evaluation.models import (
    TRAJECTORY_SCHEMA_VERSION,
    AgentTrajectory,
    QualityVerdictValue,
    now_iso,
)

#: Bounds on :class:`QualityConfig`, so a configured threshold cannot become
#: "no threshold at all" by accident.
MAX_QUALITY_STEPS = 10_000
MAX_QUALITY_CALLS = 10_000
MAX_QUALITY_EVENTS = 100_000
MAX_DUPLICATE_WINDOW = 100_000


class QualityRuleCode(StrEnum):
    """Every reason this filter can give, as a machine-readable code."""

    FILTER_DISABLED = "filter_disabled"
    EMPTY_TRAJECTORY = "empty_trajectory"
    MISSING_REQUEST = "missing_request"
    MISSING_OUTCOME = "missing_outcome"
    INCOMPLETE_EXECUTION = "incomplete_execution"
    UNKNOWN_SCHEMA = "unknown_schema"
    MALFORMED_DATA = "malformed_data"
    CONTRADICTORY_OUTCOME = "contradictory_outcome"
    FAILED_VERIFICATION = "failed_verification"
    INVALID_TOOL_CALL = "invalid_tool_call"
    DUPLICATE = "duplicate"
    UNSAFE_ACTION = "unsafe_action"
    SENSITIVE_DATA = "sensitive_data"
    NOISY_EXAMPLE = "noisy_example"
    EMPTY_RESULT = "empty_result"


class IssueSeverity(StrEnum):
    """How much an issue matters. ``ERROR`` rejects, ``WARN`` holds, ``INFO`` notes."""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"


#: What each rule means by default. One table, so the severity of every finding
#: is auditable in a single place rather than scattered through ten checks.
DEFAULT_SEVERITIES: Mapping[QualityRuleCode, IssueSeverity] = {
    QualityRuleCode.FILTER_DISABLED: IssueSeverity.INFO,
    QualityRuleCode.EMPTY_TRAJECTORY: IssueSeverity.ERROR,
    QualityRuleCode.MISSING_REQUEST: IssueSeverity.ERROR,
    QualityRuleCode.MISSING_OUTCOME: IssueSeverity.WARN,
    QualityRuleCode.INCOMPLETE_EXECUTION: IssueSeverity.WARN,
    QualityRuleCode.UNKNOWN_SCHEMA: IssueSeverity.WARN,
    QualityRuleCode.MALFORMED_DATA: IssueSeverity.ERROR,
    QualityRuleCode.CONTRADICTORY_OUTCOME: IssueSeverity.WARN,
    QualityRuleCode.FAILED_VERIFICATION: IssueSeverity.WARN,
    QualityRuleCode.INVALID_TOOL_CALL: IssueSeverity.ERROR,
    QualityRuleCode.DUPLICATE: IssueSeverity.ERROR,
    QualityRuleCode.UNSAFE_ACTION: IssueSeverity.ERROR,
    QualityRuleCode.SENSITIVE_DATA: IssueSeverity.ERROR,
    QualityRuleCode.NOISY_EXAMPLE: IssueSeverity.WARN,
    QualityRuleCode.EMPTY_RESULT: IssueSeverity.WARN,
}

#: Names of the checks :meth:`DataQualityFilter.assess` runs, in order. Recorded
#: on the verdict, so a stored row says WHAT was checked, not only what was found.
CHECKS_RUN: tuple[str, ...] = (
    "presence",
    "serialisable",
    "schema",
    "execution",
    "consistency",
    "tools",
    "safety",
    "privacy",
    "noise",
    "duplicate",
)


@dataclass(frozen=True, slots=True)
class QualityConfig:
    """Every line this filter draws, in one place."""

    enabled: bool = True
    #: Residual sensitive text (a secret the recorder's redactor did not catch)
    #: rejects the row outright by default. Holding it for review is the other
    #: defensible answer, for an operator who reviews by hand.
    reject_on_sensitive: bool = True
    #: Whether an unfinished or undecided run is held for review rather than
    #: accepted as-is.
    hold_unknown_outcome: bool = True
    #: Whether a refused/denied action is held for review. A refusal is exactly
    #: the safety evidence a future phase wants, so it is held, not dropped.
    hold_denials: bool = True
    max_steps: int = 60
    max_tool_calls: int = 60
    max_events: int = 400
    #: How many recent fingerprints are remembered for duplicate detection.
    duplicate_window: int = 500

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "reject_on_sensitive": self.reject_on_sensitive,
            "hold_unknown_outcome": self.hold_unknown_outcome,
            "hold_denials": self.hold_denials,
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "max_events": self.max_events,
            "duplicate_window": self.duplicate_window,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> QualityConfig:
        defaults = cls()

        def flag(key: str, current: bool) -> bool:
            value = data.get(key)
            return current if not isinstance(value, bool) else value

        def count(key: str, current: int, maximum: int) -> int:
            value = data.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                return current
            return min(value, maximum)

        return cls(
            enabled=flag("enabled", defaults.enabled),
            reject_on_sensitive=flag("reject_on_sensitive", defaults.reject_on_sensitive),
            hold_unknown_outcome=flag("hold_unknown_outcome", defaults.hold_unknown_outcome),
            hold_denials=flag("hold_denials", defaults.hold_denials),
            max_steps=count("max_steps", defaults.max_steps, MAX_QUALITY_STEPS),
            max_tool_calls=count(
                "max_tool_calls", defaults.max_tool_calls, MAX_QUALITY_CALLS
            ),
            max_events=count("max_events", defaults.max_events, MAX_QUALITY_EVENTS),
            duplicate_window=count(
                "duplicate_window", defaults.duplicate_window, MAX_DUPLICATE_WINDOW
            ),
        )


def issue(
    code: QualityRuleCode, detail: str, *, severity: IssueSeverity | None = None
) -> QualityIssue:
    """Build a finding; the code's entry in :data:`DEFAULT_SEVERITIES` decides.

    An explicit severity is for the checks whose line the CONFIG draws (a
    residual secret, an unknown outcome, a refusal): there the operator's setting
    is the answer, not the table.
    """
    chosen = severity if severity is not None else DEFAULT_SEVERITIES[code]
    return QualityIssue(code=code.value, detail=detail, severity=chosen.value)


@dataclass(frozen=True, slots=True)
class QualityIssue:
    """One finding: which rule, how much it matters, and what it saw."""

    code: str
    detail: str
    severity: str = IssueSeverity.WARN.value

    @property
    def rejects(self) -> bool:
        return self.severity == IssueSeverity.ERROR.value

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> QualityIssue:
        return cls(
            code=str(data.get("code", "")),
            detail=str(data.get("detail", "")),
            severity=str(data.get("severity", IssueSeverity.WARN.value)),
        )


@dataclass(frozen=True, slots=True)
class QualityVerdict:
    """The filter's answer about one trajectory, with its reasons."""

    verdict: str
    issues: tuple[QualityIssue, ...] = ()
    checks: tuple[str, ...] = ()
    fingerprint: str = ""
    checked_at: str = field(default_factory=now_iso)

    @property
    def accepted(self) -> bool:
        return self.verdict == QualityVerdictValue.ACCEPTED.value

    @property
    def rejected(self) -> bool:
        return self.verdict == QualityVerdictValue.REJECTED.value

    @property
    def needs_review(self) -> bool:
        return self.verdict == QualityVerdictValue.NEEDS_REVIEW.value

    def codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "accepted": self.accepted,
            "issues": [item.to_dict() for item in self.issues],
            "reasons": [item.code for item in self.issues],
            "checks": list(self.checks),
            "fingerprint": self.fingerprint,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> QualityVerdict:
        rows = data.get("issues")
        parsed: list[QualityIssue] = []
        if isinstance(rows, (list, tuple)):
            for row in rows:
                if isinstance(row, Mapping):
                    parsed.append(QualityIssue.from_dict(row))
        checks = data.get("checks")
        return cls(
            verdict=str(data.get("verdict", QualityVerdictValue.NEEDS_REVIEW.value)),
            issues=tuple(parsed),
            checks=tuple(str(item) for item in checks)
            if isinstance(checks, (list, tuple))
            else (),
            fingerprint=str(data.get("fingerprint", "")),
            checked_at=str(data.get("checked_at", "")) or now_iso(),
        )


class DataQualityFilter:
    """Classifies trajectories; never deletes one, never hides a reason."""

    def __init__(
        self,
        config: QualityConfig | None = None,
        *,
        redactor: Redactor | None = None,
    ) -> None:
        self.config = config if config is not None else QualityConfig()
        self._redactor = redactor if redactor is not None else Redactor()
        self._seen: dict[str, str] = {}
        self._counts: dict[str, int] = {member.value: 0 for member in QualityVerdictValue}
        self._code_counts: dict[str, int] = {}

    # -- the check -------------------------------------------------------------

    def assess(self, trajectory: AgentTrajectory) -> QualityVerdict:
        """Run every check and resolve the verdict. Deterministic and non-destructive.

        The only state touched is the duplicate window and the counters, both of
        which describe THIS filter's history rather than the trajectory.
        """
        if not self.config.enabled:
            note = issue(
                QualityRuleCode.FILTER_DISABLED,
                "filtering is disabled; the row was accepted without checks",
            )
            return self._resolve(trajectory, (note,), run_checks=())

        found: list[QualityIssue] = []
        found.extend(self._check_presence(trajectory))
        found.extend(self._check_outcome(trajectory))
        found.extend(self._check_serialisable(trajectory))
        found.extend(self._check_schema(trajectory))
        found.extend(self._check_execution(trajectory))
        found.extend(self._check_consistency(trajectory))
        found.extend(self._check_tools(trajectory))
        found.extend(self._check_safety(trajectory))
        found.extend(self._check_privacy(trajectory))
        found.extend(self._check_noise(trajectory))
        found.extend(self._check_duplicate(trajectory))
        return self._resolve(trajectory, found, run_checks=CHECKS_RUN)

    def assess_many(
        self, trajectories: Sequence[AgentTrajectory]
    ) -> tuple[QualityVerdict, ...]:
        return tuple(self.assess(row) for row in trajectories)

    # -- individual checks -----------------------------------------------------

    def _check_presence(self, trajectory: AgentTrajectory) -> list[QualityIssue]:
        empty = not any(
            (
                trajectory.user_request,
                trajectory.structured_intent,
                trajectory.decision,
                trajectory.plan,
                trajectory.execution_steps,
                trajectory.tool_calls,
                trajectory.final_result,
            )
        )
        if empty:
            # A row that names a TASK is a partial capture (the recorder saw the
            # work start); a row that names nothing at all is empty. The
            # difference matters: the first is held for review, the second is
            # not data in any sense.
            if not trajectory.task_id:
                return [
                    issue(
                        QualityRuleCode.EMPTY_TRAJECTORY,
                        "nothing was captured: no request, intent, decision, plan or result",
                    )
                ]
            return []
        if not trajectory.user_request and not trajectory.structured_intent:
            return [
                issue(
                    QualityRuleCode.MISSING_REQUEST,
                    "no request and no structured intent: there is nothing to learn from",
                )
            ]
        return []

    def _check_outcome(self, trajectory: AgentTrajectory) -> list[QualityIssue]:
        """Whether the run's ending is known. Split out because it is configured."""
        held = IssueSeverity.WARN if self.config.hold_unknown_outcome else IssueSeverity.INFO
        if not trajectory.terminal:
            return [
                issue(
                    QualityRuleCode.INCOMPLETE_EXECUTION,
                    f"the run is still {trajectory.status}: the capture is partial",
                    severity=held,
                )
            ]
        if trajectory.success is None:
            return [
                issue(
                    QualityRuleCode.MISSING_OUTCOME,
                    f"the run ended as {trajectory.status} without deciding success",
                    severity=held,
                )
            ]
        return []

    def _check_serialisable(self, trajectory: AgentTrajectory) -> list[QualityIssue]:
        try:
            json.dumps(trajectory.to_dict(), ensure_ascii=False, allow_nan=False, default=str)
        except (TypeError, ValueError) as exc:
            return [
                issue(
                    QualityRuleCode.MALFORMED_DATA,
                    f"the row cannot be serialised: {type(exc).__name__}: {exc}",
                )
            ]
        return []

    def _check_schema(self, trajectory: AgentTrajectory) -> list[QualityIssue]:
        if trajectory.schema_version > TRAJECTORY_SCHEMA_VERSION:
            return [
                issue(
                    QualityRuleCode.UNKNOWN_SCHEMA,
                    (
                        f"schema version {trajectory.schema_version} is newer than this "
                        f"build understands ({TRAJECTORY_SCHEMA_VERSION})"
                    ),
                )
            ]
        return []

    def _check_execution(self, trajectory: AgentTrajectory) -> list[QualityIssue]:
        if trajectory.success is True and not (
            trajectory.final_result or trajectory.tool_calls or trajectory.observations
        ):
            return [
                issue(
                    QualityRuleCode.EMPTY_RESULT,
                    "the run reports success with no result, tool call or observation",
                )
            ]
        return []

    def _check_consistency(self, trajectory: AgentTrajectory) -> list[QualityIssue]:
        found: list[QualityIssue] = []
        failed_calls = trajectory.failed_tools
        recovered = {record.step_id for record in trajectory.recovery_events if record.succeeded}
        unresolved = [
            call
            for call in failed_calls
            if call.step_id not in recovered and call.status != "denied"
        ]
        if trajectory.success is True and unresolved:
            found.append(
                issue(
                    QualityRuleCode.CONTRADICTORY_OUTCOME,
                    (
                        "the run reports success while "
                        f"{len(unresolved)} tool call(s) failed without recovery: "
                        + ", ".join(call.tool or "?" for call in unresolved[:3])
                    ),
                )
            )
        if trajectory.success is False and not trajectory.failure_reason:
            found.append(
                issue(
                    QualityRuleCode.CONTRADICTORY_OUTCOME,
                    "the run reports failure with no reason recorded",
                    severity=IssueSeverity.INFO,
                )
            )
        if trajectory.success is True and trajectory.failed_verifications:
            found.append(
                issue(
                    QualityRuleCode.FAILED_VERIFICATION,
                    (
                        "the run reports success while verification failed on "
                        + ", ".join(
                            record.step_id or "?" for record in trajectory.failed_verifications[:3]
                        )
                    ),
                )
            )
        return found

    def _check_tools(self, trajectory: AgentTrajectory) -> list[QualityIssue]:
        found: list[QualityIssue] = []
        for index, call in enumerate(trajectory.tool_calls):
            if not call.tool:
                found.append(
                    issue(
                        QualityRuleCode.INVALID_TOOL_CALL,
                        f"tool call #{index} has no tool name",
                    )
                )
            elif not isinstance(call.arguments, Mapping):
                found.append(
                    issue(
                        QualityRuleCode.INVALID_TOOL_CALL,
                        f"tool call #{index} ({call.tool}) has non-mapping arguments",
                    )
                )
        return found

    def _check_safety(self, trajectory: AgentTrajectory) -> list[QualityIssue]:
        found: list[QualityIssue] = []
        for call in trajectory.tool_calls:
            denied = call.status in {"denied", "refused"} or "denied" in call.error.lower()
            if denied and self.config.hold_denials:
                found.append(
                    issue(
                        QualityRuleCode.UNSAFE_ACTION,
                        (
                            f"{call.tool} was refused ({call.error or 'denied'}): the refusal "
                            "is safety evidence and is held for review"
                        ),
                        severity=IssueSeverity.WARN,
                    )
                )
            if (
                call.requires_confirmation
                and call.approved is False
                and call.status == "completed"
            ):
                found.append(
                    issue(
                        QualityRuleCode.UNSAFE_ACTION,
                        (
                            f"{call.tool} reports completion after the confirmation it "
                            "required was refused"
                        ),
                    )
                )
        return found

    def _check_privacy(self, trajectory: AgentTrajectory) -> list[QualityIssue]:
        report = self._redactor.report(trajectory.to_dict())
        if not report.count:
            return []
        return [
            issue(
                QualityRuleCode.SENSITIVE_DATA,
                (
                    "residual sensitive text was detected in "
                    + ", ".join(report.kinds)
                    + f" ({report.count} value(s))"
                ),
                severity=(
                    IssueSeverity.ERROR if self.config.reject_on_sensitive else IssueSeverity.WARN
                ),
            )
        ]

    def _check_noise(self, trajectory: AgentTrajectory) -> list[QualityIssue]:
        found: list[QualityIssue] = []
        for label, count, maximum in (
            ("steps", len(trajectory.execution_steps), self.config.max_steps),
            ("tool calls", len(trajectory.tool_calls), self.config.max_tool_calls),
            ("events", trajectory.event_count, self.config.max_events),
        ):
            if count > maximum:
                found.append(
                    issue(
                        QualityRuleCode.NOISY_EXAMPLE,
                        (
                            f"{count} {label} exceeds the configured maximum of "
                            f"{maximum}: the row is noise"
                        ),
                    )
                )
        return found

    def _check_duplicate(self, trajectory: AgentTrajectory) -> list[QualityIssue]:
        fingerprint = trajectory.fingerprint()
        seen = self._seen.get(fingerprint)
        if seen is not None and seen != trajectory.trajectory_id:
            return [
                issue(
                    QualityRuleCode.DUPLICATE,
                    (
                        "this row repeats the work of trajectory "
                        f"{seen} (same request, route, tools and outcome)"
                    ),
                )
            ]
        self._seen[fingerprint] = trajectory.trajectory_id
        self._trim_window()
        return []

    def _trim_window(self) -> None:
        window = max(1, int(self.config.duplicate_window))
        while len(self._seen) > window:
            # dicts keep insertion order, so the first key is the oldest.
            self._seen.pop(next(iter(self._seen)))

    # -- resolution and reporting ---------------------------------------------

    def _resolve(
        self,
        trajectory: AgentTrajectory,
        found: Sequence[QualityIssue],
        *,
        run_checks: Sequence[str],
    ) -> QualityVerdict:
        # The severity decides, not the count: an ERROR rejects, a WARN holds the
        # row for review, and an INFO is a note attached to an accepted row ("the
        # run failed and recorded no reason" is worth saying, not worth stopping).
        if any(item.rejects for item in found):
            verdict = QualityVerdictValue.REJECTED
        elif any(item.severity == IssueSeverity.WARN.value for item in found):
            verdict = QualityVerdictValue.NEEDS_REVIEW
        else:
            verdict = QualityVerdictValue.ACCEPTED
        self._counts[verdict.value] = self._counts.get(verdict.value, 0) + 1
        for item in found:
            self._code_counts[item.code] = self._code_counts.get(item.code, 0) + 1
        return QualityVerdict(
            verdict=verdict.value,
            issues=tuple(found),
            checks=tuple(run_checks),
            fingerprint=trajectory.fingerprint(),
        )

    def stats(self) -> dict[str, Any]:
        """Counts by verdict and by reason, for the metrics surface."""
        return {
            "considered": sum(self._counts.values()),
            "by_verdict": dict(self._counts),
            "by_code": dict(self._code_counts),
            "duplicate_window": len(self._seen),
            "config": self.config.to_mapping(),
        }

    def reset(self) -> None:
        """Forget the duplicate window and the counters (a test, or a new day).

        The stored trajectories are untouched: this filter has never deleted a
        row and does not start here.
        """
        self._seen.clear()
        self._counts = {member.value: 0 for member in QualityVerdictValue}
        self._code_counts.clear()


__all__ = [
    "CHECKS_RUN",
    "DEFAULT_SEVERITIES",
    "DataQualityFilter",
    "IssueSeverity",
    "QualityConfig",
    "QualityIssue",
    "QualityRuleCode",
    "QualityVerdict",
    "issue",
]
