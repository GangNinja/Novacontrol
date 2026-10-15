"""Did the learning actually learn? Measured, including when the answer is no.

Everything here exists to stop one specific false claim: *"the experiment ran, so
learning succeeded"*. So:

* **Execution is not learning.** An experiment that completed is counted as
  completed; a hypothesis moves only when evidence moved it. ``unsupported`` and
  ``inconclusive`` counts are reported beside the supported ones, not folded into
  them (§10).
* **Agreement is over EVALUABLE predictions only**, and the count of predictions
  that could not be evaluated is reported next to the rate. A run with no prediction
  is a run with no prediction — it is not a miss and it is not a hit (§6/§10).
* **A rate nobody measured is ``None``**, never ``0.0``. The same rule Phase 14
  established for hardware figures applies to learning figures.
* **Generalization is only claimed for held-out cases.** ``TransferResult.claim``
  says what was actually shown, and it refuses to call a same-scenario result
  generalization (§10).

``LearningEvaluator`` reads records that already exist — it has no execution path of
its own, so it cannot flatter the numbers by doing extra work.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from novacontrol.interactive.models import (
    ExperimentRun,
    ExperimentSpec,
    ExperimentStatus,
    Hypothesis,
    HypothesisStatus,
    LearnedRule,
    LearningObjective,
    ObjectiveStatus,
    OutcomeKind,
    PredictionBasis,
    RuleStatus,
    as_float,
    as_text,
)

__all__ = [
    "LearningEvaluator",
    "LearningMetrics",
    "StrategyComparison",
    "StrategyOutcome",
    "TransferResult",
    "compare_strategies",
    "transfer_result",
]

_METRIC_LIMITATIONS = (
    "counts describe this build's own records; they are not a claim about the world",
    "a synthetic environment's result does not establish real-world performance",
)


@dataclass(frozen=True, slots=True)
class LearningMetrics:
    """What happened across the objectives a caller asked about."""

    objectives: int = 0
    objectives_by_status: Mapping[str, int] = field(default_factory=dict)
    hypotheses: int = 0
    hypotheses_by_status: Mapping[str, int] = field(default_factory=dict)
    experiments: int = 0
    experiments_by_status: Mapping[str, int] = field(default_factory=dict)
    runs: int = 0
    predictions_available: int = 0
    predictions_unavailable: int = 0
    predictions_evaluable: int = 0
    outcomes_by_kind: Mapping[str, int] = field(default_factory=dict)
    agreement_rate: float | None = None
    contradiction_rate: float | None = None
    questions_resolved: int = 0
    duplicate_experiments: int = 0
    duplicate_rate: float | None = None
    safety_rejections: int = 0
    blocked_experiments: int = 0
    budget_stops: int = 0
    uninformative_stops: int = 0
    rules_total: int = 0
    rules_by_status: Mapping[str, int] = field(default_factory=dict)
    generalization_confirmations: int = 0
    evidence_references: int = 0
    hypotheses_without_evidence: int = 0
    average_experiment_cost_units: float | None = None
    average_experiment_latency_ms: float | None = None
    total_actions: int = 0
    limitations: tuple[str, ...] = _METRIC_LIMITATIONS
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "objectives": self.objectives,
            "objectives_by_status": dict(self.objectives_by_status),
            "hypotheses": self.hypotheses,
            "hypotheses_by_status": dict(self.hypotheses_by_status),
            "experiments": self.experiments,
            "experiments_by_status": dict(self.experiments_by_status),
            "runs": self.runs,
            "predictions": {
                "available": self.predictions_available,
                "unavailable": self.predictions_unavailable,
                "evaluable": self.predictions_evaluable,
            },
            "outcomes_by_kind": dict(self.outcomes_by_kind),
            "agreement_rate": self.agreement_rate,
            "contradiction_rate": self.contradiction_rate,
            "questions_resolved": self.questions_resolved,
            "duplicate_experiments": self.duplicate_experiments,
            "duplicate_rate": self.duplicate_rate,
            "safety_rejections": self.safety_rejections,
            "blocked_experiments": self.blocked_experiments,
            "budget_stops": self.budget_stops,
            "uninformative_stops": self.uninformative_stops,
            "rules": {
                "total": self.rules_total,
                "by_status": dict(self.rules_by_status),
                "generalization_confirmations": self.generalization_confirmations,
            },
            "evidence_references": self.evidence_references,
            "hypotheses_without_evidence": self.hypotheses_without_evidence,
            "average_experiment_cost_units": self.average_experiment_cost_units,
            "average_experiment_latency_ms": self.average_experiment_latency_ms,
            "total_actions": self.total_actions,
            "notes": list(self.notes),
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class StrategyOutcome:
    """One strategy's run over one scenario — the input to a comparison."""

    strategy: str
    scenario: str
    resolved: bool
    experiments: int = 0
    agreement: float | None = None
    duplicates: int = 0
    rejections: int = 0
    seconds: float | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "scenario": self.scenario,
            "resolved": self.resolved,
            "experiments": self.experiments,
            "agreement": self.agreement,
            "duplicates": self.duplicates,
            "rejections": self.rejections,
            "seconds": self.seconds,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class StrategyComparison:
    """Two or more strategies over the SAME scenarios, compared honestly."""

    rows: tuple[StrategyOutcome, ...] = ()
    by_strategy: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    winner: str = ""
    comparable: bool = True
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": [row.to_dict() for row in self.rows],
            "by_strategy": {key: dict(value) for key, value in self.by_strategy.items()},
            "winner": self.winner,
            "comparable": self.comparable,
            "limitations": list(self.limitations),
        }


def compare_strategies(rows: Iterable[StrategyOutcome]) -> StrategyComparison:
    """Aggregate per-strategy outcomes — resolution first, then cost, then agreement.

    A strategy that resolves more scenarios wins; ties are broken by spending fewer
    experiments and then by better prediction agreement. Fewer experiments is only a
    virtue *given the same resolutions*, which is why resolution is the first key and
    not, say, the experiment count (§10: actions are not the objective).
    """
    collected = tuple(rows)
    by: dict[str, list[StrategyOutcome]] = {}
    for row in collected:
        by.setdefault(row.strategy, []).append(row)
    summary: dict[str, dict[str, Any]] = {}
    for strategy, items in by.items():
        total = len(items)
        resolved = sum(1 for item in items if item.resolved)
        agreement_values = [item.agreement for item in items if item.agreement is not None]
        summary[strategy] = {
            "scenarios": total,
            "resolved": resolved,
            "resolution_rate": (resolved / total) if total else None,
            "experiments": sum(item.experiments for item in items),
            "average_experiments": (
                sum(item.experiments for item in items) / total if total else None
            ),
            "duplicates": sum(item.duplicates for item in items),
            "rejections": sum(item.rejections for item in items),
            "agreement": (
                sum(agreement_values) / len(agreement_values) if agreement_values else None
            ),
            "seconds": (
                sum(item.seconds for item in items if item.seconds is not None)
                if any(item.seconds is not None for item in items)
                else None
            ),
        }
    scenario_sets = {strategy: {item.scenario for item in items} for strategy, items in by.items()}
    comparable = len({frozenset(value) for value in scenario_sets.values()}) <= 1
    winner = ""
    if summary and comparable:
        winner = sorted(
            summary,
            key=lambda name: (
                -(summary[name]["resolved"]),
                (summary[name]["average_experiments"] or 0.0),
                -(summary[name]["agreement"] or 0.0),
                name,
            ),
        )[0]
    limitations = []
    if not comparable:
        limitations.append(
            "the strategies did not run the same scenarios, so they are reported "
            "side by side and NOT ranked"
        )
    if not collected:
        limitations.append("no strategy runs were recorded")
    return StrategyComparison(
        rows=collected,
        by_strategy=summary,
        winner=winner,
        comparable=comparable,
        limitations=tuple(limitations),
    )


@dataclass(frozen=True, slots=True)
class TransferResult:
    """Before-and-after performance on a measured task — and what it does NOT show."""

    before: float | None = None
    after: float | None = None
    held_out: bool = False
    cases: int = 0
    improvement: float | None = None
    claim: str = ""
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "before": self.before,
            "after": self.after,
            "held_out": self.held_out,
            "cases": self.cases,
            "improvement": self.improvement,
            "claim": self.claim,
            "limitations": list(self.limitations),
        }


def transfer_result(
    *,
    before_seconds: float | None,
    after_seconds: float | None,
    experiments_before: int,
    experiments_after: int,
    held_out: bool,
    cases: int,
) -> TransferResult:
    """A measured before/after, with the claim it supports and the one it does not.

    Improvement is reported as the drop in experiments needed to answer the same
    question, because that is what was actually measured here. When the same cases
    were used to build the rule, the claim says so and stops there.
    """
    improvement: float | None = None
    if experiments_before or experiments_after:
        improvement = float(experiments_before - experiments_after)
    claim = (
        "the same question was answered with fewer experiments after the rule was learned"
        if improvement is not None and improvement > 0
        else "no improvement was measured on this task"
    )
    limitations = []
    if not held_out:
        limitations.append(
            "the task was NOT held out: this shows the rule is stored and reused, "
            "not that it generalizes"
        )
    if before_seconds is not None and after_seconds is not None:
        limitations.append(
            "wall-clock figures come from one synthetic run on this machine and are not a benchmark"
        )
    return TransferResult(
        before=float(experiments_before),
        after=float(experiments_after),
        held_out=held_out,
        cases=cases,
        improvement=improvement,
        claim=claim,
        limitations=tuple(limitations),
    )


class LearningEvaluator:
    """Metrics from records that already exist — nothing is re-run to improve them."""

    def __init__(self, *, support_threshold: int = 3) -> None:
        self.support_threshold = max(1, support_threshold)

    def evaluate(
        self,
        *,
        objectives: Iterable[LearningObjective] = (),
        hypotheses: Iterable[Hypothesis] = (),
        experiments: Iterable[ExperimentSpec] = (),
        runs: Iterable[ExperimentRun] = (),
        rules: Iterable[LearnedRule] = (),
        duplicate: int | None = None,
        rejections: int = 0,
        notes: Iterable[str] = (),
    ) -> LearningMetrics:
        objectives = tuple(objectives)
        hypotheses = tuple(hypotheses)
        experiments = tuple(experiments)
        runs = tuple(runs)
        rules = tuple(rules)

        objectives_by_status = _counts(item.status.value for item in objectives)
        hypotheses_by_status = _counts(item.status.value for item in hypotheses)
        experiments_by_status = _counts(item.status.value for item in experiments)
        rules_by_status = _counts(item.status.value for item in rules)

        available = sum(1 for run in runs if run.prediction.available)
        unavailable = sum(
            1
            for run in runs
            if not run.prediction.available or run.prediction.basis is PredictionBasis.UNAVAILABLE
        )
        kinds = _counts(
            run.comparison.kind.value
            for run in runs
            if run.comparison is not None
            and run.comparison.kind is not OutcomeKind.UNABLE_TO_EVALUATE
            and run.comparison.prediction_basis is not PredictionBasis.UNAVAILABLE
        )
        evaluable = sum(kinds.values())
        confirmed = (kinds.get(OutcomeKind.CONFIRMED.value, 0)) + kinds.get(
            OutcomeKind.PARTIALLY_CONFIRMED.value, 0
        )
        contradicted = kinds.get(OutcomeKind.CONTRADICTED.value, 0)
        agreement = (confirmed / evaluable) if evaluable else None
        contradiction_rate = (contradicted / evaluable) if evaluable else None

        duplicate_count = (
            duplicate
            if duplicate is not None
            else sum(1 for run in runs if as_text(run.reason).startswith("duplicate"))
        )
        total_experiments = len(experiments)
        costs = [run.cost_units for run in runs if run.cost_units >= 0]
        latencies = [run.duration_ms for run in runs if run.duration_ms > 0]
        resolved = sum(
            1
            for item in hypotheses
            if item.status
            in {HypothesisStatus.SUPPORTED, HypothesisStatus.CONTRADICTED, HypothesisStatus.RETIRED}
        )
        budget_stops = sum(
            1
            for item in objectives
            if item.status is ObjectiveStatus.EXHAUSTED
            or "budget" in as_text(item.stop_reason).lower()
            or "ceiling" in as_text(item.stop_reason).lower()
        )
        uninformative_stops = sum(
            1
            for item in objectives
            if item.status is ObjectiveStatus.UNJUSTIFIED
            or "uninformative" in as_text(item.stop_reason).lower()
        )
        return LearningMetrics(
            objectives=len(objectives),
            objectives_by_status=objectives_by_status,
            hypotheses=len(hypotheses),
            hypotheses_by_status=hypotheses_by_status,
            experiments=total_experiments,
            experiments_by_status=experiments_by_status,
            runs=len(runs),
            predictions_available=available,
            predictions_unavailable=unavailable,
            predictions_evaluable=evaluable,
            outcomes_by_kind=kinds,
            agreement_rate=None if agreement is None else round(agreement, 4),
            contradiction_rate=None if contradiction_rate is None else round(contradiction_rate, 4),
            questions_resolved=resolved,
            duplicate_experiments=duplicate_count,
            duplicate_rate=(
                round(duplicate_count / total_experiments, 4) if total_experiments else None
            ),
            safety_rejections=rejections,
            blocked_experiments=experiments_by_status.get(ExperimentStatus.BLOCKED.value, 0),
            budget_stops=budget_stops,
            uninformative_stops=uninformative_stops,
            rules_total=len(rules),
            rules_by_status=rules_by_status,
            generalization_confirmations=sum(
                1 for rule in rules if rule.status is RuleStatus.CONFIRMED
            ),
            evidence_references=sum(
                len(item.supporting) + len(item.contradicting) + len(item.inconclusive)
                for item in hypotheses
            ),
            hypotheses_without_evidence=sum(
                1
                for item in hypotheses
                if not item.supporting and not item.contradicting and not item.inconclusive
            ),
            average_experiment_cost_units=(round(sum(costs) / len(costs), 4) if costs else None),
            average_experiment_latency_ms=(
                round(sum(latencies) / len(latencies), 3) if latencies else None
            ),
            total_actions=sum(run.actions for run in runs),
            notes=tuple(notes),
        )

    def promote_ready(self, hypothesis: Hypothesis) -> bool:
        """Whether this hypothesis has the independent support a rule needs."""
        return hypothesis.independent_support >= self.support_threshold


def _counts(values: Iterable[str]) -> dict[str, int]:
    """A stable count table — every key that appears, in a deterministic order."""
    table: dict[str, int] = {}
    for value in values:
        key = as_text(value, "unknown")
        table[key] = table.get(key, 0) + 1
    return dict(sorted(table.items()))


def ratio_or_none(numerator: int, denominator: int) -> float | None:
    """A rate, or ``None`` when nothing was measured — never a fabricated zero."""
    if denominator <= 0:
        return None
    return round(numerator / float(denominator), 4)


def mean_or_none(values: Iterable[float | None]) -> float | None:
    """The mean of what was actually measured, or ``None`` when nothing was."""
    present = [as_float(value) for value in values if value is not None]
    if not present:
        return None
    return round(sum(present) / len(present), 4)
