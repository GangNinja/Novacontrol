"""Strategy versions: proposing, validating, promoting and rolling back.

A strategy is a DECLARATIVE plan template plus a tool policy. Nothing in this
module executes anything; promoting a strategy changes which template the engine
recommends, and the existing planner and executor still decide whether and how
to run it.

The lifecycle is deliberately asymmetric:

* a proposal is built only from DISTINCT verified successes of the same step
  shape, so it always has provenance;
* validation demands executed trials (or the supporting successes themselves
  when they are all this shape) and refuses on any decided failure;
* promotion keeps the previous version, superseded but intact;
* rollback restores it, and a regression check can trigger that automatically.

Versions are stored one row per version (key ``<strategy id>#v<n>``), because a
rollback target that was overwritten is not a rollback target. ``strategy_id``
stays stable across versions so trials can cite it; ``strategy_version`` says
which one they used.

A later version is never "better because it is newer": it is better when the
trials say so.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Iterable, Sequence

from novacontrol.continual.models import (
    ExecutionEvidence,
    LearningProposal,
    OutcomeClass,
    ProposalStatus,
    StrategyKind,
    StrategyRecord,
    StrategyStats,
    StrategyStatus,
    new_id,
    now_iso,
)

__all__ = [
    "StrategyBook",
    "MIN_VALIDATION_TRIALS",
    "PROMOTION_FLOOR",
    "MAX_VERSIONS_KEPT",
    "record_key",
    "strategy_id_for",
    "StrategyStats",
]

#: Independent verified successes a proposal needs before it may be validated.
MIN_VALIDATION_TRIALS = 2

#: A strategy is promoted only when its verified success rate clears this line.
PROMOTION_FLOOR = 0.6

#: Superseded versions kept per strategy. The active version and its immediate
#: parent are always retained; older history is trimmed.
MAX_VERSIONS_KEPT = 4


def strategy_id_for(task_kind: str) -> str:
    digest = hashlib.sha1(str(task_kind or "").strip().lower().encode("utf-8")).hexdigest()[:12]
    return f"strategy:{digest}"


def record_key(record: StrategyRecord) -> str:
    return f"{record.strategy_id}#v{record.version}"


class StrategyBook:
    """Versioned strategies with statistics recomputed from evidence."""

    def __init__(
        self,
        *,
        min_support: int = MIN_VALIDATION_TRIALS,
        promotion_floor: float = PROMOTION_FLOOR,
        max_versions: int = MAX_VERSIONS_KEPT,
    ) -> None:
        self.min_support = max(1, int(min_support))
        self.promotion_floor = max(0.0, min(1.0, float(promotion_floor)))
        self.max_versions = max(2, int(max_versions))

    # -- lookup -----------------------------------------------------------

    def active_for(
        self, task_kind: str, records: dict[str, StrategyRecord]
    ) -> StrategyRecord | None:
        for record in self.versions(task_kind, records):
            if record.status == StrategyStatus.ACTIVE.value:
                return record
        return None

    def versions(self, task_kind: str, records: dict[str, StrategyRecord]) -> list[StrategyRecord]:
        wanted = str(task_kind or "").strip().lower()
        found = [
            record
            for record in records.values()
            if record.task_kind.lower() == wanted
        ]
        found.sort(key=lambda record: record.version, reverse=True)
        return found

    # -- proposal ---------------------------------------------------------

    def build_proposal(
        self,
        *,
        task_kind: str,
        experiences: Sequence[ExecutionEvidence],
        records: dict[str, StrategyRecord],
        avoid_tools: Iterable[str] = (),
        reasons: Iterable[str] = (),
    ) -> LearningProposal | None:
        """Distil the common successful step shape, or return None.

        Two successes with DIFFERENT step shapes are not evidence for a template;
        disjoint shapes mean the tasks were not actually the same. The shape is
        keyed on (kind, target) only — a tool improvement should not look like a
        different plan.
        """
        successes = [
            evidence
            for evidence in experiences
            if evidence.task_kind.lower() == task_kind.lower()
            and evidence.verified_outcome
            and evidence.steps
        ]
        if len({evidence.experience_id for evidence in successes}) < self.min_support:
            return None
        shapes: dict[tuple[tuple[str, str], ...], list[ExecutionEvidence]] = defaultdict(list)
        for evidence in successes:
            shapes[_shape(evidence)].append(evidence)
        if not shapes:
            return None
        shape, group = max(shapes.items(), key=lambda item: (len(item[1]), len(item[0])))
        if len(group) < self.min_support:
            return None
        latest = max(group, key=lambda item: item.finished_at or item.started_at)
        template = [
            {
                "kind": step.kind,
                "target": step.target,
                "tool": step.tool,
                "description": f"{step.kind} {step.target}".strip(),
                "example_target": step.target,
            }
            for step in latest.steps
            if step.status in {"completed", "unknown", "skipped"}
        ]
        if not template:
            return None
        active = self.active_for(task_kind, records)
        if active is not None and active.steps == template:
            return None  # already covered: never duplicate an active capability
        tool_order: list[str] = []
        for step in latest.steps:
            name = str(step.tool or "").strip().lower()
            if name and name not in tool_order:
                tool_order.append(name)
        verified = sum(1 for evidence in group if evidence.verified_outcome)
        return LearningProposal(
            proposal_id=new_id("proposal"),
            kind=StrategyKind.PLAN_TEMPLATE.value,
            task_kind=task_kind,
            summary=(
                f"reuse the {len(template)}-step plan that succeeded in "
                f"{verified} verified execution(s) of {task_kind!r}"
            ),
            payload={
                "name": f"{task_kind} template",
                "steps": template,
                "tool_order": tool_order,
                "avoid_tools": sorted({str(tool).lower() for tool in avoid_tools if str(tool).strip()}),
                "retry_budget": _retry_budget(group),
                "verification_required": True,
                "adapt_targets": True,
                "shape": [f"{kind}:{tool}" for kind, tool in shape],
            },
            evidence_ids=sorted(evidence.experience_id for evidence in group),
            support=len(group),
            confidence=round(min(0.95, 0.55 + 0.1 * len(group)), 3),
            risk="low",
            reasons=[
                *reasons,
                f"template taken from the latest of {len(group)} verified execution(s)",
                "the template is data; concrete targets are bound by the caller's planner",
                "the existing executor still runs every step",
            ],
            strategy_id=strategy_id_for(task_kind),
        )

    # -- validation -------------------------------------------------------

    def validate(
        self,
        proposal: LearningProposal,
        *,
        trials: Sequence[ExecutionEvidence],
        fallback_evidence: Sequence[ExecutionEvidence],
    ) -> tuple[bool, dict[str, object]]:
        """Decide whether the proposal may be promoted, from executed evidence.

        ``trials`` are executions that used the candidate step shape AFTER it was
        proposed. When none exist yet — the common first case, because a proposal
        arrives before anything has used it — the supporting successes are the
        evidence: they are real executions of exactly this shape. Either way the
        verdict is computed from outcomes, never from the proposal's existence.
        """
        decided = [
            evidence
            for evidence in (*trials, *fallback_evidence)
            if evidence.outcome
            in {
                OutcomeClass.SUCCESS.value,
                OutcomeClass.RECOVERED.value,
                OutcomeClass.PARTIAL.value,
                OutcomeClass.FAILURE.value,
                OutcomeClass.USER_CORRECTED.value,
            }
        ]
        success = [item for item in decided if item.verified_outcome]
        partial = [item for item in decided if item.outcome == OutcomeClass.PARTIAL.value]
        failure = [
            item
            for item in decided
            if item.outcome in {OutcomeClass.FAILURE.value, OutcomeClass.USER_CORRECTED.value}
        ]
        unverified = [item for item in trials if item.outcome == OutcomeClass.UNVERIFIED.value]
        denominator = len(success) + len(partial) + len(failure)
        rate = len(success) / denominator if denominator else 0.0
        reasons: list[str] = []
        if len(success) < self.min_support:
            reasons.append(
                f"{len(success)} verified success(es); {self.min_support} independent ones are required"
            )
        if failure:
            reasons.append(f"{len(failure)} decided failure(s) contradict the template")
        if denominator and rate < self.promotion_floor:
            reasons.append(
                f"verified success rate {rate:.2f} is below the promotion floor {self.promotion_floor:.2f}"
            )
        if unverified and not success:
            reasons.append("trials were recorded but none could be verified")
        validated = not reasons
        summary = {
            "validated": validated,
            "trials": len(trials),
            "supporting": len(fallback_evidence),
            "success": len(success),
            "partial": len(partial),
            "failure": len(failure),
            "unverified": len(unverified),
            "success_rate": round(rate, 4),
            "minimum_support": self.min_support,
            "floor": self.promotion_floor,
            "reasons": reasons or ["every judged trial supported the template"],
        }
        return validated, summary

    # -- promotion / rollback --------------------------------------------

    def promote(
        self,
        proposal: LearningProposal,
        *,
        records: dict[str, StrategyRecord],
        evidence: Sequence[ExecutionEvidence],
        reason: str = "",
    ) -> StrategyRecord:
        """Create the next ACTIVE version and retain the one it replaced."""
        existing = self.versions(proposal.task_kind, records)
        previous = next(
            (record for record in existing if record.status == StrategyStatus.ACTIVE.value),
            None,
        )
        version = max((record.version for record in existing), default=0) + 1
        record = StrategyRecord(
            strategy_id=strategy_id_for(proposal.task_kind),
            task_kind=proposal.task_kind,
            name=str(proposal.payload.get("name", "") or f"{proposal.task_kind} template"),
            version=version,
            status=StrategyStatus.ACTIVE.value,
            kind=str(proposal.kind or StrategyKind.PLAN_TEMPLATE.value),
            steps=[dict(step) for step in proposal.payload.get("steps", []) or []],
            tool_order=[str(item) for item in proposal.payload.get("tool_order", []) or []],
            avoid_tools=[str(item) for item in proposal.payload.get("avoid_tools", []) or []],
            retry_budget=int(proposal.payload.get("retry_budget", 0) or 0),
            verification_required=bool(proposal.payload.get("verification_required", True)),
            provenance=list(proposal.evidence_ids),
            stats=StrategyStats(),
            parent_version=previous.version if previous else 0,
            promoted_at=now_iso(),
            decided_reason=reason or "validated against executed evidence",
        )
        if previous is not None:
            previous.status = StrategyStatus.SUPERSEDED.value
            previous.decided_reason = f"superseded by version {version}"
        record.stats = stats_for(record, evidence)
        records[record_key(record)] = record
        self._trim_versions(records, proposal.task_kind)
        proposal.status = ProposalStatus.PROMOTED.value
        proposal.strategy_id = record.strategy_id
        proposal.decided_at = now_iso()
        proposal.decision_reason = reason or "promoted after validation"
        return record

    def rollback(
        self,
        task_kind: str,
        *,
        records: dict[str, StrategyRecord],
        reason: str,
        evidence: Sequence[ExecutionEvidence] = (),
    ) -> StrategyRecord | None:
        active = self.active_for(task_kind, records)
        if active is None:
            return None
        previous = next(
            (
                record
                for record in self.versions(task_kind, records)
                if record.version == active.parent_version
                and record.status in {StrategyStatus.SUPERSEDED.value, StrategyStatus.ROLLED_BACK.value}
            ),
            None,
        )
        if previous is None:
            return None
        active.status = StrategyStatus.ROLLED_BACK.value
        active.decided_reason = reason
        previous.status = StrategyStatus.ACTIVE.value
        previous.decided_reason = f"restored by rollback of version {active.version}: {reason}"
        previous.stats = stats_for(previous, evidence or ())
        return previous

    def _trim_versions(self, records: dict[str, StrategyRecord], task_kind: str) -> None:
        found = self.versions(task_kind, records)
        if len(found) <= self.max_versions:
            return
        active = next((record for record in found if record.status == StrategyStatus.ACTIVE.value), None)
        keep: set[int] = set()
        if active is not None:
            keep.add(active.version)
            if active.parent_version:
                keep.add(active.parent_version)
        for record in found:
            if len(keep) >= self.max_versions:
                break
            keep.add(record.version)
        for record in found:
            if record.version not in keep:
                records.pop(record_key(record), None)


def stats_for(record: StrategyRecord, evidence: Sequence[ExecutionEvidence]) -> StrategyStats:
    """Recompute a strategy's stats from evidence citing it, never accumulate."""
    stats = StrategyStats()
    seen: set[str] = set()
    for item in evidence:
        if item.experience_id in seen or not item.assessed:
            continue
        cites = item.strategy_id == record.strategy_id and (
            not item.strategy_version or item.strategy_version == record.version
        )
        if not cites:
            continue
        seen.add(item.experience_id)
        stats.record(item)
    return stats


def _shape(evidence: ExecutionEvidence) -> tuple[tuple[str, str], ...]:
    # Structural, not literal: a template says which kind of step used which
    # tool. Concrete targets belong to the task that produced them and must be
    # bound by the caller's planner, never replayed onto a different task.
    return tuple((step.kind, step.tool) for step in evidence.steps if step.counts_as_done)


def _retry_budget(group: Sequence[ExecutionEvidence]) -> int:
    return min(3, max(0, max((evidence.retries for evidence in group), default=0) + 1))
