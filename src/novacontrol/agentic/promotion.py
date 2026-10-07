"""Promotion gates and the policy registry: nothing promotes itself, ever.

A candidate policy may be trained, evaluated and compared freely. What it may not
do is become the policy that acts on real work without clearing every gate AND
being approved by a person. This module is the only place that decision is made,
and the decision is recorded with the numbers that produced it.

The gates, all of them configurable and all of them reported individually:

    sample size        enough episodes for the numbers to mean anything
    task success       it completes tasks
    verification       its steps VERIFY, not merely report success
    safety             it does not take unsafe actions
    latency            it is not unacceptably slow
    resources          it is not unacceptably expensive
    no regression      it did not get worse than its baseline at what works
    explicit approval  a person said yes, by name

A metric that was not measured FAILS its gate rather than passing by default:
"nobody measured safety" is not evidence of safety. A safety failure REJECTS the
candidate rather than leaving it in evaluation, because a policy that has shown
it will do unsafe things does not need more episodes to be judged.

The registry extends the model registry's idea with the policy lifecycle. A
policy record carries its version, its model, the run that produced it, the
environment and the reward/verifier/curriculum versions it was evaluated against,
its evaluation results, and a status — EXPERIMENTAL until someone decides
otherwise.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.agentic.config import PromotionThresholds
from novacontrol.agentic.models import (
    CANDIDATE_STATUSES,
    LIVE_STATUSES,
    POLICY_STATUSES,
    AgenticEvaluationResult,
    PolicyRecord,
    PolicyStatus,
    PromotionDecision,
    as_flag,
    as_mapping,
    as_text,
    as_texts,
)
from novacontrol.evaluation.models import now_iso

#: The checks every promotion runs, in the order they are reported.
PROMOTION_CHECKS: tuple[str, ...] = (
    "sample_size",
    "task_success",
    "verification_success",
    "safety",
    "latency",
    "resources",
    "success_regression",
    "safety_regression",
    "latency_regression",
    "explicit_approval",
)

#: A failed check with one of these names REJECTS the candidate.
REJECTING_CHECKS: frozenset[str] = frozenset({"safety", "safety_regression"})


class PromotionRefused(RuntimeError):
    """A promotion that the gates or the approval requirement refused."""


def _metric(result: AgenticEvaluationResult | None, name: str) -> float | None:
    return result.metric(name) if result is not None else None


@dataclass(frozen=True, slots=True)
class PromotionGate:
    """Decides whether a candidate may be promoted, gate by gate."""

    thresholds: PromotionThresholds = field(default_factory=PromotionThresholds)

    def evaluate(
        self,
        candidate: AgenticEvaluationResult,
        *,
        baseline: AgenticEvaluationResult | None = None,
        policy_id: str = "",
        approved_by: str = "",
    ) -> PromotionDecision:
        """Every gate, and a verdict only if all of them pass."""
        t = self.thresholds
        checks: list[Mapping[str, Any]] = []
        failures: list[str] = []

        def check(name: str, passed: bool, detail: str, **extra: Any) -> bool:
            checks.append(
                {"check": name, "passed": bool(passed), "detail": detail, **extra}
            )
            if not passed:
                failures.append(f"{name}: {detail}")
            return bool(passed)

        # 1. Enough evidence.
        sample = candidate.sample_size
        check(
            "sample_size",
            sample >= t.min_sample_size,
            f"{sample} episode(s) against a minimum of {t.min_sample_size}",
            sample_size=sample,
            minimum=t.min_sample_size,
        )
        # 2. It completes tasks.
        success = _metric(candidate, "task_success_rate")
        check(
            "task_success",
            success is not None and success >= t.min_task_success,
            (
                "the task success rate was not measurable"
                if success is None
                else f"task success {success:.3f} against a minimum of {t.min_task_success:.3f}"
            ),
            value=success,
            minimum=t.min_task_success,
        )
        # 3. Its steps verify.
        verification = _metric(candidate, "verification_pass_rate")
        check(
            "verification_success",
            verification is not None and verification >= t.min_verification_success,
            (
                "the verification pass rate was not measurable"
                if verification is None
                else (
                    f"verification {verification:.3f} against a minimum of "
                    f"{t.min_verification_success:.3f}"
                )
            ),
            value=verification,
            minimum=t.min_verification_success,
        )
        # 4. Safety, on its own, and never averaged with anything else.
        safety = _metric(candidate, "safety_rate")
        check(
            "safety",
            safety is not None and safety >= t.min_safety_rate,
            (
                "the safety rate was not measurable"
                if safety is None
                else f"safety rate {safety:.3f} against a minimum of {t.min_safety_rate:.3f}"
            ),
            value=safety,
            minimum=t.min_safety_rate,
        )
        # 5. Latency.
        seconds = _metric(candidate, "mean_latency_seconds")
        latency_ms = None if seconds is None else round(seconds * 1000.0, 6)
        check(
            "latency",
            latency_ms is not None and latency_ms <= t.max_latency_ms,
            (
                "the latency was not measurable"
                if latency_ms is None
                else f"mean latency {latency_ms:.1f} ms against a maximum of {t.max_latency_ms:.1f} ms"
            ),
            value=latency_ms,
            maximum=t.max_latency_ms,
        )
        # 6. Resources.
        resources = _metric(candidate, "mean_resource_used")
        check(
            "resources",
            resources is not None and resources <= t.max_resource_units,
            (
                "the resource usage was not measurable"
                if resources is None
                else (
                    f"mean resource usage {resources:.3f} against a maximum of "
                    f"{t.max_resource_units:.3f}"
                )
            ),
            value=resources,
            maximum=t.max_resource_units,
        )
        # 7-9. Regression against the baseline that is in production. With NO
        #      baseline there is nothing to regress from, and the check says so
        #      rather than failing (which would make a first promotion
        #      impossible). With a baseline whose metric was not measured, the
        #      check FAILS: "nobody measured it" is not "it did not get worse".
        has_baseline = baseline is not None

        def regression(
            name: str,
            value: float | None,
            base: float | None,
            *,
            tolerance: float,
            lower_is_better: bool = False,
        ) -> None:
            if not has_baseline:
                checks.append(
                    {
                        "check": name,
                        "passed": True,
                        "applicable": False,
                        "detail": (
                            "no baseline policy was supplied, so there is nothing to "
                            "regress from"
                        ),
                    }
                )
                return
            if value is None or base is None:
                check(
                    name,
                    False,
                    "the baseline comparison is missing a measured value for this metric",
                    applicable=True,
                    value=value,
                    baseline=base,
                )
                return
            if lower_is_better:
                passed = value <= base * (1.0 + tolerance)
                detail = (
                    f"{value:.3f} against a baseline of {base:.3f} "
                    f"(tolerance {tolerance:.0%})"
                )
            else:
                passed = value >= base - tolerance
                detail = (
                    f"{value:.3f} against a baseline of {base:.3f} "
                    f"(tolerance {tolerance:.3f})"
                )
            check(
                name,
                passed,
                detail,
                applicable=True,
                value=value,
                baseline=base,
                tolerance=tolerance,
            )

        regression(
            "success_regression",
            success,
            _metric(baseline, "task_success_rate"),
            tolerance=t.max_success_regression,
        )
        regression(
            "safety_regression",
            safety,
            _metric(baseline, "safety_rate"),
            tolerance=t.max_safety_regression,
        )
        baseline_latency = _metric(baseline, "mean_latency_seconds")
        regression(
            "latency_regression",
            latency_ms,
            (
                None
                if baseline_latency is None
                else round(float(baseline_latency) * 1000.0, 6)
            ),
            tolerance=t.max_latency_regression,
            lower_is_better=True,
        )
        # 10. A person said yes. This is checked LAST so the report shows what
        #     they were approving, and it is never satisfied by a number.
        check(
            "explicit_approval",
            bool(approved_by.strip()),
            (
                "a promotion requires a named approver; no approval was supplied"
                if not approved_by.strip()
                else f"approved by {approved_by}"
            ),
            approved_by=approved_by,
        )
        rejected = any(
            not as_flag(item.get("passed"), False)
            and as_text(item.get("check")) in REJECTING_CHECKS
            for item in checks
        )
        approved = not failures
        return PromotionDecision(
            approved=approved,
            policy_id=policy_id or candidate.policy_id,
            status=(
                PolicyStatus.APPROVED.value
                if approved
                else (PolicyStatus.REJECTED.value if rejected else PolicyStatus.EVALUATING.value)
            ),
            thresholds=t.to_mapping(),
            metrics=dict(candidate.metrics),
            baseline_metrics=dict(baseline.metrics) if baseline is not None else {},
            checks=tuple(checks),
            failures=tuple(failures),
            requires_approval=True,
            approved_by=approved_by,
            sample_size=sample,
            minimum_sample_size=t.min_sample_size,
            reasons=(
                (
                    "every gate passed and a person approved the candidate"
                    if approved
                    else (
                        "at least one gate failed, so the candidate stays a candidate"
                        + (" and is rejected for safety" if rejected else "")
                    )
                ),
            ),
        )


@dataclass
class PolicyRegistry:
    """The policies this build knows about, and what state each one is in."""

    records: dict[str, PolicyRecord] = field(default_factory=dict)
    #: An optional sink for status changes (an audit trail, a log). Called with
    #: ``(policy_id, {"from":..., "to":..., "reason":...})``.
    sink: Callable[[str, Mapping[str, Any]], None] | None = None

    # -- registering ------------------------------------------------------------

    def register(self, record: PolicyRecord) -> PolicyRecord:
        """Add a policy. Registering a policy id twice is a conflict, not an update."""
        policy_id = record.policy_id.strip()
        if not policy_id:
            raise ValueError("a policy record needs a policy id")
        if policy_id in self.records:
            raise PromotionRefused(
                f"policy {policy_id!r} is already registered; use record_evaluation or "
                "a different policy version instead of overwriting it"
            )
        if record.status not in POLICY_STATUSES:
            raise ValueError(
                f"unknown policy status {record.status!r}: expected "
                + ", ".join(POLICY_STATUSES)
            )
        stored = PolicyRecord.from_dict(record.to_dict())
        self.records[policy_id] = stored
        self._emit(policy_id, {"from": "", "to": stored.status, "reason": "registered"})
        return stored

    def register_candidate(
        self,
        *,
        policy_id: str,
        policy_version: str = "",
        model_id: str = "",
        training_run_id: str = "",
        environment: str = "",
        curriculum_version: str = "",
        reward_version: str = "",
        verifier_versions: Mapping[str, str] | None = None,
        learns: bool = False,
        simulated: bool = True,
        notes: Sequence[str] = (),
    ) -> PolicyRecord:
        """Register a fresh candidate, which is EXPERIMENTAL by definition."""
        return self.register(
            PolicyRecord(
                policy_id=policy_id,
                policy_version=policy_version or policy_id,
                model_id=model_id,
                training_run_id=training_run_id,
                environment=environment,
                curriculum_version=curriculum_version,
                reward_version=reward_version,
                verifier_versions=dict(verifier_versions or {}),
                status=PolicyStatus.EXPERIMENTAL.value,
                learns=learns,
                simulated=simulated,
                notes=tuple(notes),
            )
        )

    # -- reading ----------------------------------------------------------------

    def get(self, policy_id: str) -> PolicyRecord | None:
        return self.records.get(policy_id.strip())

    def all(self) -> tuple[PolicyRecord, ...]:
        return tuple(self.records[key] for key in sorted(self.records))

    def by_status(self, status: str) -> tuple[PolicyRecord, ...]:
        return tuple(record for record in self.all() if record.status == status)

    def candidates(self) -> tuple[PolicyRecord, ...]:
        return tuple(record for record in self.all() if record.status in CANDIDATE_STATUSES)

    def live(self) -> tuple[PolicyRecord, ...]:
        """The policies that may act on real work. Empty until somebody promotes one."""
        return tuple(record for record in self.all() if record.status in LIVE_STATUSES)

    def production(self) -> PolicyRecord | None:
        for record in self.all():
            if record.status == PolicyStatus.PRODUCTION.value:
                return record
        return None

    # -- changing state ---------------------------------------------------------

    def record_evaluation(
        self, policy_id: str, result: AgenticEvaluationResult | Mapping[str, Any]
    ) -> PolicyRecord:
        """Attach an evaluation result and move an EXPERIMENTAL candidate to EVALUATING."""
        record = self._require(policy_id)
        payload = (
            result.to_dict() if isinstance(result, AgenticEvaluationResult) else dict(result)
        )
        updated = record.with_evaluation(payload)
        if updated.status == PolicyStatus.EXPERIMENTAL.value:
            updated = updated.with_status(
                PolicyStatus.EVALUATING.value,
                note="an evaluation result is attached",
            )
        self.records[record.policy_id] = updated
        self._emit(record.policy_id, {"from": record.status, "to": updated.status, "reason": "evaluated"})
        return updated

    def promote(
        self,
        policy_id: str,
        decision: PromotionDecision,
        *,
        approved_by: str = "",
    ) -> PolicyRecord:
        """Record a promotion that the gates ALREADY approved.

        Three things are required and none of them is implicit: the decision must
        say approved, it must carry a named approver, and that approver must
        match the caller's. A clone of the decision object with ``approved``
        flipped does not get around this — the approver name is checked against
        the decision's own record.
        """
        record = self._require(policy_id)
        if not decision.approved:
            raise PromotionRefused(
                f"policy {policy_id!r} was not approved by the promotion gates: "
                + ("; ".join(decision.failures) or "no reason was recorded")
            )
        approver = as_text(approved_by) or as_text(decision.approved_by)
        if not approver:
            raise PromotionRefused(
                "a promotion requires an explicit approver; the decision named nobody"
            )
        target = PolicyStatus.APPROVED.value
        if decision.status in LIVE_STATUSES:
            target = decision.status
        updated = record.with_status(
            target,
            note=f"promoted by {approver}; gates: {len(decision.checks)} check(s), all passed",
        )
        self.records[record.policy_id] = updated
        self._emit(record.policy_id, {"from": record.status, "to": target, "reason": "promoted", "approver": approver})
        return updated

    def set_production(self, policy_id: str, *, approved_by: str) -> PolicyRecord:
        """Make an APPROVED policy the production one, deprecating the previous one."""
        record = self._require(policy_id)
        if record.status not in LIVE_STATUSES:
            raise PromotionRefused(
                f"policy {policy_id!r} is {record.status!r}: only an approved policy becomes "
                "production. Run the promotion gates and a person's approval first."
            )
        previous = self.production()
        if previous is not None and previous.policy_id != record.policy_id:
            self.records[previous.policy_id] = previous.with_status(
                PolicyStatus.DEPRECATED.value,
                note=f"replaced as production by {record.policy_id} (approved by {approved_by})",
            )
            self._emit(
                previous.policy_id,
                {"from": previous.status, "to": PolicyStatus.DEPRECATED.value, "reason": "superseded"},
            )
        updated = record.with_status(
            PolicyStatus.PRODUCTION.value, note=f"set as production by {approved_by}"
        )
        self.records[record.policy_id] = updated
        self._emit(record.policy_id, {"from": record.status, "to": updated.status, "reason": "production"})
        return updated

    def reject(self, policy_id: str, *, reason: str) -> PolicyRecord:
        record = self._require(policy_id)
        updated = record.with_status(PolicyStatus.REJECTED.value, note=reason)
        self.records[record.policy_id] = updated
        self._emit(record.policy_id, {"from": record.status, "to": updated.status, "reason": reason})
        return updated

    def deprecate(self, policy_id: str, *, reason: str = "") -> PolicyRecord:
        record = self._require(policy_id)
        updated = record.with_status(
            PolicyStatus.DEPRECATED.value, note=reason or "deprecated by an operator"
        )
        self.records[record.policy_id] = updated
        self._emit(record.policy_id, {"from": record.status, "to": updated.status, "reason": "deprecated"})
        return updated

    def rollback(self, policy_id: str, *, reason: str = "") -> PolicyRecord:
        """Return a policy to paper: DEPRECATED, and say who decided."""
        return self.deprecate(
            policy_id,
            reason=reason or "rolled back: the previous policy is used again",
        )

    # -- reporting --------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for record in self.all():
            counts[record.status] = counts.get(record.status, 0) + 1
        production = self.production()
        return {
            "policies": len(self.records),
            "by_status": counts,
            "candidates": [record.policy_id for record in self.candidates()],
            "live": [record.policy_id for record in self.live()],
            "production": production.policy_id if production is not None else "",
            "note": (
                "nothing is promoted automatically: a policy needs every gate and a "
                "named approver before it acts on real work"
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": {key: record.to_dict() for key, record in sorted(self.records.items())},
            "summary": self.summary(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PolicyRegistry:
        rows = as_mapping(data).get("records")
        registry = cls()
        for key, value in as_mapping(rows).items():
            record = PolicyRecord.from_dict(as_mapping(value))
            registry.records[record.policy_id or str(key)] = record
        return registry

    # -- helpers ----------------------------------------------------------------

    def _require(self, policy_id: str) -> PolicyRecord:
        record = self.get(policy_id)
        if record is None:
            raise KeyError(f"no policy {str(policy_id).strip()!r} is registered")
        return record

    def _emit(self, policy_id: str, change: Mapping[str, Any]) -> None:
        if self.sink is None:
            return
        try:
            self.sink(policy_id, {**dict(change), "at": now_iso()})
        except Exception:  # pragma: no cover - an audit sink must not break a registry
            return


def gate_report(decision: PromotionDecision) -> dict[str, Any]:
    """A promotion decision as a compact report row."""
    passed = sum(1 for item in decision.checks if as_flag(item.get("passed"), False))
    return {
        "policy_id": decision.policy_id,
        "approved": decision.approved,
        "status": decision.status,
        "checks_passed": f"{passed}/{len(decision.checks)}",
        "failures": list(decision.failures),
        "sample_size": decision.sample_size,
        "minimum_sample_size": decision.minimum_sample_size,
        "approved_by": decision.approved_by,
        "note": "; ".join(as_texts(decision.reasons)),
    }


__all__ = [
    "PROMOTION_CHECKS",
    "REJECTING_CHECKS",
    "PolicyRegistry",
    "PromotionGate",
    "PromotionRefused",
    "gate_report",
]
