"""The critique engine: observable failures become structured critiques.

A critique here is not prose. It is a record that names which component failed,
which category the failure belongs to, how severe it is, what was observed,
what was expected, what evidence supports the reading, and — when a correction
is known — what to do instead. That shape is what lets a critique become
training data safely: it can be filtered, ranked, verified and paired, and it
never contains a reasoning trace because there is no field for one.

Failure detection walks what the system already records:

    verification results → failed checks (the strongest evidence)
    trajectory           → failed tools, blocked steps, failed verifications,
                           failed recoveries, retry/latency inefficiency
    evaluation           → dimensions that scored FAIL or WARN

Each source contributes only what it can SEE. A tool that failed because its
arguments were rejected is an ARGUMENT_ERROR (the error says so); a step whose
verification never ran is a VERIFICATION_ERROR; a blocked action is a
SAFETY_ERROR at CRITICAL severity. When nothing classifiable happened, the
engine produces no critique rather than an empty one — silence is data.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.evaluation.models import EvaluationDimension
from novacontrol.rlvr.config import DEFAULT_SEVERITY_BY_STATUS, CritiqueConfig
from novacontrol.rlvr.models import (
    SEVERITY_RANK,
    CounterfactualComparison,
    CritiqueCategory,
    CritiqueResult,
    CritiqueSeverity,
    CritiqueSource,
    VerificationResult,
    VerificationStatus,
    _text,
    _texts,
)

#: Which category a dimension's failure belongs to. The nine Phase 15
#: dimensions map onto the eleven critique categories; nothing is invented.
DIMENSION_CATEGORIES: Mapping[str, str] = {
    EvaluationDimension.NLU.value: CritiqueCategory.CONTEXT_ERROR.value,
    EvaluationDimension.DECISION.value: CritiqueCategory.PLANNING_ERROR.value,
    EvaluationDimension.TOOL_SELECTION.value: CritiqueCategory.TOOL_SELECTION_ERROR.value,
    EvaluationDimension.PLANNING.value: CritiqueCategory.PLANNING_ERROR.value,
    EvaluationDimension.EXECUTION.value: CritiqueCategory.EXECUTION_ERROR.value,
    EvaluationDimension.VERIFICATION.value: CritiqueCategory.VERIFICATION_ERROR.value,
    EvaluationDimension.RECOVERY.value: CritiqueCategory.RECOVERY_ERROR.value,
    EvaluationDimension.SAFETY.value: CritiqueCategory.SAFETY_ERROR.value,
    EvaluationDimension.EFFICIENCY.value: CritiqueCategory.EFFICIENCY_ISSUE.value,
}

#: Which category a verifier's failure belongs to when the verdict itself does
#: not name one. This is derived from the verifier's subject, not from a guess.
VERIFIER_CATEGORIES: Mapping[str, str] = {
    "file": CritiqueCategory.EXECUTION_ERROR.value,
    "process": CritiqueCategory.EXECUTION_ERROR.value,
    "test": CritiqueCategory.VERIFICATION_ERROR.value,
    "http": CritiqueCategory.OUTPUT_FORMAT_ERROR.value,
    "database": CritiqueCategory.EXECUTION_ERROR.value,
    "git": CritiqueCategory.EXECUTION_ERROR.value,
    "output": CritiqueCategory.OUTPUT_FORMAT_ERROR.value,
    "schema": CritiqueCategory.OUTPUT_FORMAT_ERROR.value,
    "custom": CritiqueCategory.OTHER.value,
}

#: Error-text markers that mean "the arguments were the problem", checked
#: before the generic execution reading so the specific fact wins.
_ARGUMENT_MARKERS: tuple[str, ...] = (
    "argument",
    "parameter",
    "invalid",
    "missing",
    "validation",
    "schema",
)


@dataclass(frozen=True, slots=True)
class CritiqueSummary:
    """A batch of critiques, counted the way a report needs them."""

    total: int = 0
    by_category: Mapping[str, int] = field(default_factory=dict)
    by_severity: Mapping[str, int] = field(default_factory=dict)
    by_source: Mapping[str, int] = field(default_factory=dict)
    corrections: int = 0
    verified: int = 0
    highest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "by_category": dict(self.by_category),
            "by_severity": dict(self.by_severity),
            "by_source": dict(self.by_source),
            "corrections": self.corrections,
            "verified": self.verified,
            "highest": self.highest,
        }


class CritiqueEngine:
    """Turns observable failures into structured, evidence-based critiques."""

    def __init__(self, config: CritiqueConfig | None = None) -> None:
        self.config = config if config is not None else CritiqueConfig()

    # -- entry points ------------------------------------------------------------

    def critique(
        self,
        results: Sequence[VerificationResult],
        *,
        context: Mapping[str, Any] | None = None,
    ) -> tuple[CritiqueResult, ...]:
        """Critiques for verification results that did not pass."""
        found: list[CritiqueResult] = []
        for result in results:
            item = self.from_verification(result, context=context)
            if item is not None:
                found.append(item)
        return self._sorted(found)

    def from_verification(
        self,
        result: VerificationResult,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> CritiqueResult | None:
        """One critique for one failed or partial verification."""
        if result.status in {
            VerificationStatus.PASS.value,
            VerificationStatus.INCONCLUSIVE.value,
        }:
            return None
        category = _text(result.error_category)
        if not any(member.value == category for member in CritiqueCategory):
            category = VERIFIER_CATEGORIES.get(result.verifier_id, CritiqueCategory.OTHER.value)
        severity = self._severity_for(result)
        correction = self._correction_for(result)
        critique = CritiqueResult(
            trajectory_id=result.trajectory_id,
            task_id=result.task_id,
            step_id=result.step_id,
            category=category,
            severity=severity,
            failed_component=result.verifier_id or "verification",
            evidence=(
                tuple(result.evidence)
                if result.evidence
                else ((f"verifier:{result.verifier_id}",) if result.verifier_id else ())
            ),
            observed_behavior=dict(result.observed),
            expected_behavior=dict(result.expected),
            correction=correction,
            confidence=result.confidence,
            source=CritiqueSource.VERIFIER.value,
            verification_id=result.verification_id,
            detail=result.detail
            or f"verifier {result.verifier_id!r} reported {result.status}",
            metadata={"context": dict(context or {})},
        )
        return critique if self._admit(critique) else None

    def critique_trajectory(
        self,
        trajectory: Any,
        *,
        verifications: Sequence[VerificationResult] = (),
        evaluation: Any = None,
        ignore_verification_ids: Sequence[str] = (),
    ) -> tuple[CritiqueResult, ...]:
        """Every critique a recorded run supports, from all of its evidence.

        Verification results that were already critiqued by id are skipped when
        ``ignore_verification_ids`` names them, so the engine called from two
        places does not produce the same critique twice.
        """
        found: list[CritiqueResult] = []
        seen = {str(item) for item in ignore_verification_ids}
        for result in verifications:
            if result.verification_id in seen:
                continue
            item = self.from_verification(result)
            if item is not None:
                found.append(item)
        found.extend(self._from_trajectory(trajectory))
        if evaluation is not None:
            found.extend(self.critique_evaluation(evaluation, trajectory=trajectory))
        if not self.config.analyze_successes and getattr(trajectory, "success", None) is True:
            # A success is evidence of what worked, not a failure to critique;
            # the engine still reports FAILED VERIFICATIONS above, because a
            # failed check on a "successful" run is exactly the false-success
            # case worth learning from.
            found = [item for item in found if item.verification_id]
        return self._sorted(found)

    def critique_evaluation(
        self, evaluation: Any, *, trajectory: Any = None
    ) -> tuple[CritiqueResult, ...]:
        """Critiques for the dimensions Phase 15's evaluator scored badly."""
        found: list[CritiqueResult] = []
        dimensions = getattr(evaluation, "dimensions", ()) or ()
        for dimension in dimensions:
            status = _text(getattr(dimension, "status", ""))
            if status not in {"fail", "warn"}:
                continue
            name = _text(getattr(dimension, "dimension", ""))
            category = DIMENSION_CATEGORIES.get(name, CritiqueCategory.OTHER.value)
            findings = _texts(getattr(dimension, "findings", ()))
            severity = (
                CritiqueSeverity.HIGH.value
                if status == "fail"
                else CritiqueSeverity.LOW.value
            )
            if name == EvaluationDimension.SAFETY.value and status == "fail":
                severity = CritiqueSeverity.CRITICAL.value
            critique = CritiqueResult(
                trajectory_id=_text(getattr(evaluation, "trajectory_id", "")),
                task_id=_text(getattr(trajectory, "task_id", ""))
                or _text(getattr(evaluation, "task_id", "")),
                category=category,
                severity=severity,
                failed_component=f"evaluation:{name}",
                evidence=tuple(findings) or (f"evaluation_dimension:{name}",),
                observed_behavior={
                    "dimension": name,
                    "status": status,
                    "score": getattr(dimension, "score", None),
                },
                expected_behavior={"dimension": name, "status": "ok"},
                confidence=None,
                source=CritiqueSource.RULE_BASED.value,
                evaluator_version=_text(getattr(evaluation, "evaluator_version", "")),
                detail=(
                    f"the {name} dimension scored {status}: "
                    + ("; ".join(findings) if findings else "no finding was recorded")
                ),
            )
            if self._admit(critique):
                found.append(critique)
        return self._sorted(found)

    # -- trajectory analysis ------------------------------------------------------

    def _from_trajectory(self, trajectory: Any) -> list[CritiqueResult]:
        found: list[CritiqueResult] = []
        task_id = _text(getattr(trajectory, "task_id", ""))
        trajectory_id = _text(getattr(trajectory, "trajectory_id", ""))
        failed = getattr(trajectory, "success", None) is False
        for call in getattr(trajectory, "tool_calls", ()) or ():
            status = _text(getattr(call, "status", ""))
            error = _text(getattr(call, "error", ""))
            tool = _text(getattr(call, "tool", "")) or "tool"
            blocked = status in {"blocked", "denied", "refused"} or (
                bool(getattr(call, "requires_confirmation", False))
                and getattr(call, "approved", None) is False
            )
            if blocked:
                found.append(
                    self._trajectory_critique(
                        trajectory_id,
                        task_id,
                        category=CritiqueCategory.SAFETY_ERROR.value,
                        severity=CritiqueSeverity.CRITICAL.value,
                        component=tool,
                        detail=f"the action on {tool!r} was blocked before it ran",
                        observed={"tool": tool, "status": status, "error": error},
                        evidence=("tool_blocked",),
                    )
                )
                continue
            if status not in {"failed", "error"} and not error:
                continue
            lowered = error.lower()
            category = (
                CritiqueCategory.ARGUMENT_ERROR.value
                if any(marker in lowered for marker in _ARGUMENT_MARKERS)
                and "tool" not in lowered
                else CritiqueCategory.EXECUTION_ERROR.value
            )
            found.append(
                self._trajectory_critique(
                    trajectory_id,
                    task_id,
                    category=category,
                    severity=CritiqueSeverity.HIGH.value,
                    component=tool,
                    detail=f"tool {tool!r} did not complete: {error or 'no error was recorded'}",
                    observed={
                        "tool": tool,
                        "arguments": dict(getattr(call, "arguments", {}) or {}),
                        "status": status,
                        "error": error,
                    },
                    evidence=("tool_failed", f"tool:{tool}"),
                )
            )
        for step in getattr(trajectory, "execution_steps", ()) or ():
            status = _text(getattr(step, "status", ""))
            if status not in {"failed", "error"}:
                continue
            error = _text(getattr(step, "error", ""))
            attempts = int(getattr(step, "attempts", 0) or 0)
            category = (
                CritiqueCategory.PLANNING_ERROR.value
                if attempts > 1 or _text(getattr(step, "depends_on", None))
                else CritiqueCategory.EXECUTION_ERROR.value
            )
            found.append(
                self._trajectory_critique(
                    trajectory_id,
                    task_id,
                    category=category,
                    severity=CritiqueSeverity.HIGH.value,
                    component=_text(getattr(step, "step_id", "")) or "step",
                    detail=(
                        "step "
                        + repr(
                            _text(getattr(step, "description", ""))
                            or _text(getattr(step, "step_id", ""))
                        )
                        + f" failed after {attempts} attempt(s): "
                        + (error or "no error was recorded")
                    ),
                    observed={
                        "step_id": _text(getattr(step, "step_id", "")),
                        "action": _text(getattr(step, "action", "")),
                        "status": status,
                        "attempts": attempts,
                        "error": error,
                    },
                    evidence=("step_failed",),
                )
            )
        for record in getattr(trajectory, "verification_results", ()) or ():
            if _text(getattr(record, "status", "")) not in {"failed", "error"}:
                continue
            found.append(
                self._trajectory_critique(
                    trajectory_id,
                    task_id,
                    category=CritiqueCategory.VERIFICATION_ERROR.value,
                    severity=CritiqueSeverity.HIGH.value,
                    component=_text(getattr(record, "verifier", "")) or "verification",
                    detail=(
                        "verification did not pass: "
                        + (_text(getattr(record, "detail", "")) or "no detail was recorded")
                    ),
                    observed={
                        "step_id": _text(getattr(record, "step_id", "")),
                        "status": _text(getattr(record, "status", "")),
                        "attempts": int(getattr(record, "attempts", 0) or 0),
                    },
                    evidence=("verification_failed",),
                )
            )
        for event in getattr(trajectory, "recovery_events", ()) or ():
            outcome = _text(getattr(event, "outcome", ""))
            if outcome not in {"failed", "exhausted", "gave_up"}:
                continue
            found.append(
                self._trajectory_critique(
                    trajectory_id,
                    task_id,
                    category=CritiqueCategory.RECOVERY_ERROR.value,
                    severity=CritiqueSeverity.HIGH.value,
                    component=_text(getattr(event, "strategy", "")) or "recovery",
                    detail=(
                        f"recovery {outcome}: "
                        + (_text(getattr(event, "detail", "")) or "no detail was recorded")
                    ),
                    observed={
                        "step_id": _text(getattr(event, "step_id", "")),
                        "outcome": outcome,
                        "attempts": int(getattr(event, "attempts", 0) or 0),
                    },
                    evidence=("recovery_failed",),
                )
            )
        retries = getattr(trajectory, "retries", 0)
        if isinstance(retries, int) and retries > 0 and failed:
            found.append(
                self._trajectory_critique(
                    trajectory_id,
                    task_id,
                    category=CritiqueCategory.EFFICIENCY_ISSUE.value,
                    severity=CritiqueSeverity.LOW.value,
                    component="retries",
                    detail=f"the run retried {retries} time(s) and still failed",
                    observed={"retries": retries},
                    evidence=("retries_exceeded",),
                )
            )
        if (
            failed
            and not getattr(trajectory, "context_summary", None)
            and not found
        ):
            found.append(
                self._trajectory_critique(
                    trajectory_id,
                    task_id,
                    category=CritiqueCategory.CONTEXT_ERROR.value,
                    severity=CritiqueSeverity.MEDIUM.value,
                    component="context",
                    detail=(
                        "the run failed with no context recorded, so what it was "
                        "working from cannot be checked"
                    ),
                    observed={"context_summary": {}},
                    evidence=("context_missing",),
                )
            )
        return [item for item in found if self._admit(item)]

    def _trajectory_critique(
        self,
        trajectory_id: str,
        task_id: str,
        *,
        category: str,
        severity: str,
        component: str,
        detail: str,
        observed: Mapping[str, Any],
        evidence: Sequence[str],
    ) -> CritiqueResult:
        return CritiqueResult(
            trajectory_id=trajectory_id,
            task_id=task_id,
            category=category,
            severity=severity,
            failed_component=component,
            evidence=tuple(evidence),
            observed_behavior=dict(observed),
            expected_behavior={},
            source=CritiqueSource.RULE_BASED.value,
            detail=detail,
        )

    # -- counterfactuals ------------------------------------------------------------

    def counterfactual(
        self,
        failed_action: Mapping[str, Any],
        successful_action: Mapping[str, Any],
        *,
        verification: VerificationResult | None = None,
        task_id: str = "",
        trajectory_id: str = "",
    ) -> CounterfactualComparison | None:
        """Compare a failed action with a verified-successful alternative.

        Only offered when the successful side carries a PASS verification: an
        unverified alternative is a hypothesis, and a hypothesis must not become
        a correction. The difference is structural — which keys changed and to
        what — never a narration of why.
        """
        if not self.config.allow_counterfactual:
            return None
        if verification is None or not verification.verified:
            return None
        if not failed_action or not successful_action:
            return None
        failed = {str(key): value for key, value in failed_action.items()}
        success = {str(key): value for key, value in successful_action.items()}
        changed: dict[str, Any] = {}
        for key in sorted(set(failed) | set(success)):
            if failed.get(key) != success.get(key):
                changed[key] = {"failed": failed.get(key), "successful": success.get(key)}
        return CounterfactualComparison(
            task_id=task_id,
            trajectory_id=trajectory_id,
            failed_action=failed,
            successful_action=success,
            difference={
                "changed": changed,
                "changed_keys": sorted(changed),
                "failed_only_keys": sorted(set(failed) - set(success)),
                "successful_only_keys": sorted(set(success) - set(failed)),
            },
            evidence=tuple(verification.evidence),
            verification_id=verification.verification_id,
            confidence=verification.confidence,
            detail=(
                "the successful alternative differs in "
                + (", ".join(sorted(changed)) or "no field")
            ),
        )

    # -- helpers -------------------------------------------------------------------

    def _severity_for(self, result: VerificationResult) -> str:
        if result.status == VerificationStatus.ERROR.value:
            return CritiqueSeverity.MEDIUM.value
        if result.status == VerificationStatus.PARTIAL.value:
            return CritiqueSeverity.MEDIUM.value
        text = " ".join((result.detail, *result.evidence)).lower()
        if "safety" in text or "unsafe" in text:
            return CritiqueSeverity.CRITICAL.value
        return DEFAULT_SEVERITY_BY_STATUS.get(result.status, CritiqueSeverity.MEDIUM.value)

    def _correction_for(self, result: VerificationResult) -> dict[str, Any]:
        """A structured suggestion, only where the expectation shows the target."""
        if not result.expected:
            return {}
        category = _text(result.error_category)
        if category in {
            CritiqueCategory.OUTPUT_FORMAT_ERROR.value,
            CritiqueCategory.ARGUMENT_ERROR.value,
        } or result.verifier_id in {"schema", "output", "http"}:
            return {
                "produce": dict(result.expected),
                "reason": f"{result.verifier_id} checked against this expectation",
            }
        return {}

    def _admit(self, critique: CritiqueResult) -> bool:
        config = self.config
        if not config.enabled:
            return False
        if not config.allows_category(critique.category):
            return False
        if not config.allows_source(critique.source):
            return False
        return config.meets_severity(critique.severity)

    def _sorted(self, items: Sequence[CritiqueResult]) -> tuple[CritiqueResult, ...]:
        ordered = sorted(items, key=lambda item: item.rank())
        if self.config.max_critiques and len(ordered) > self.config.max_critiques:
            ordered = ordered[: self.config.max_critiques]
        return tuple(ordered)

    @staticmethod
    def summarise(critiques: Sequence[CritiqueResult]) -> CritiqueSummary:
        by_category: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        by_source: dict[str, int] = {}
        highest = ""
        for item in critiques:
            by_category[item.category] = by_category.get(item.category, 0) + 1
            by_severity[item.severity] = by_severity.get(item.severity, 0) + 1
            by_source[item.source] = by_source.get(item.source, 0) + 1
            if SEVERITY_RANK.get(item.severity, 0) > SEVERITY_RANK.get(highest, -1):
                highest = item.severity
        return CritiqueSummary(
            total=len(critiques),
            by_category=by_category,
            by_severity=by_severity,
            by_source=by_source,
            corrections=sum(1 for item in critiques if item.has_correction),
            verified=sum(1 for item in critiques if item.verified),
            highest=highest,
        )


__all__ = [
    "DIMENSION_CATEGORIES",
    "VERIFIER_CATEGORIES",
    "CritiqueEngine",
    "CritiqueSummary",
]
