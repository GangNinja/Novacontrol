"""Choosing what to try: three strategies, one contract, every rejection explained.

The contract is small on purpose — a strategy RANKS candidates that have already been
validated — so a new strategy cannot smuggle in an experiment the safety gate or the
budget refused. ``ExperimentSelector`` does the enrichment (validation, safety,
information value, curiosity) once, then hands the ranked list to the strategy.

The three that ship:

``uncertainty_first``
    The baseline. Highest uncertainty first, cheapest tie-break. It needs no
    arithmetic beyond what a hypothesis already stores, which is why it is the
    baseline the alternative has to beat.

``information_value``
    Highest heuristic information value per cost. It reads the weighted score in
    ``information.py`` and inherits its limitations.

``bounded_random``
    Reproducible random choice over the eligible candidates. It exists to be
    COMPARED against the other two — a random baseline is how "the selector is
    helping" becomes a measurement instead of a belief. The seed is stated, so the
    same seed and the same candidates give the same answer.

What the selector refuses is as important as what it picks: an environment it does
not know, an action that cannot run, an experiment that would exceed a ceiling, one
whose predictions cannot separate the hypotheses, and a repeat whose evidence is
still current. Each refusal is returned with its reason, so a preview can show what
was considered rather than only what was chosen.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from novacontrol.interactive.curiosity import CuriosityWeights, curiosity_score
from novacontrol.interactive.environments import Environment
from novacontrol.interactive.experiments import (
    DispositionPolicy,
    intervention_key,
    validate_experiment,
)
from novacontrol.interactive.governance import SafetyGate
from novacontrol.interactive.hypotheses import HypothesisBook
from novacontrol.interactive.information import InformationWeights, information_value
from novacontrol.interactive.models import (
    BudgetUsage,
    CandidateExperiment,
    ExperimentSpec,
    Hypothesis,
    LearningBudget,
    LearningObjective,
    MatchingPolicy,
    as_text,
)

__all__ = [
    "BoundedRandomStrategy",
    "ExperimentSelector",
    "InformationValueStrategy",
    "SelectionContext",
    "SelectionOutcome",
    "SelectionStrategy",
    "UncertaintyFirstStrategy",
    "available_strategies",
    "strategy_by_name",
]


@dataclass(frozen=True, slots=True)
class SelectionContext:
    """Everything a strategy may consider — and nothing it may act through."""

    objective: LearningObjective
    hypotheses: tuple[Hypothesis, ...]
    environment: Environment | None
    usage: BudgetUsage
    budget: LearningBudget
    matching: MatchingPolicy
    policy: DispositionPolicy
    #: Interventions already answered by evidence that is still current.
    answered_interventions: tuple[str, ...] = ()
    known_actions: tuple[str, ...] = ()
    approval: bool = False
    information_weights: InformationWeights | None = None
    curiosity_weights: CuriosityWeights | None = None
    random_seed: int = 0


@dataclass(frozen=True, slots=True)
class SelectionOutcome:
    """What was chosen, what was considered, and why the rest were not."""

    strategy: str
    chosen: CandidateExperiment | None = None
    candidates: tuple[CandidateExperiment, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()
    limitations: tuple[str, ...] = ()
    considered: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "considered": self.considered,
            "chosen": None if self.chosen is None else self.chosen.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "rejected": [
                {"experiment_id": experiment_id, "reason": reason}
                for experiment_id, reason in self.rejected
            ],
            "limitations": list(self.limitations),
        }


class SelectionStrategy(Protocol):
    """Ranking only. Strategies never execute, validate or override a refusal."""

    name: str

    def rank(
        self, candidates: Sequence[CandidateExperiment], context: SelectionContext
    ) -> tuple[CandidateExperiment, ...]:
        """The candidates in the order this strategy would try them."""
        ...


class UncertaintyFirstStrategy:
    """The baseline: unsettled questions first, cheaper experiments before dearer."""

    name = "uncertainty_first"

    def rank(
        self, candidates: Sequence[CandidateExperiment], context: SelectionContext
    ) -> tuple[CandidateExperiment, ...]:
        return tuple(
            sorted(
                candidates,
                key=lambda item: (
                    -_uncertainty(item, context),
                    item.experiment.estimated_seconds,
                    item.experiment.experiment_id,
                ),
            )
        )


class InformationValueStrategy:
    """The alternative: the most information per unit of cost and risk."""

    name = "information_value"

    def rank(
        self, candidates: Sequence[CandidateExperiment], context: SelectionContext
    ) -> tuple[CandidateExperiment, ...]:
        return tuple(
            sorted(
                candidates,
                key=lambda item: (
                    -(item.information_value if item.information_value is not None else 0.0),
                    item.experiment.estimated_seconds,
                    item.experiment.experiment_id,
                ),
            )
        )


class BoundedRandomStrategy:
    """Reproducible random choice — the control the other strategies are measured against."""

    name = "bounded_random"

    def __init__(self, seed: int = 0) -> None:
        self.seed = int(seed)

    def rank(
        self, candidates: Sequence[CandidateExperiment], context: SelectionContext
    ) -> tuple[CandidateExperiment, ...]:
        ordered = sorted(candidates, key=lambda item: item.experiment.experiment_id)
        chooser = random.Random(self.seed if self.seed else context.random_seed)
        shuffled = list(ordered)
        chooser.shuffle(shuffled)
        return tuple(shuffled)


def _uncertainty(candidate: CandidateExperiment, context: SelectionContext) -> float:
    """The mean uncertainty of the hypotheses a candidate tests."""
    wanted = set(candidate.experiment.hypothesis_ids)
    tested = [item for item in context.hypotheses if item.hypothesis_id in wanted]
    if not tested:
        return 0.0
    return sum(item.uncertainty for item in tested) / len(tested)


_STRATEGIES: Mapping[str, type[Any]] = {
    UncertaintyFirstStrategy.name: UncertaintyFirstStrategy,
    InformationValueStrategy.name: InformationValueStrategy,
    BoundedRandomStrategy.name: BoundedRandomStrategy,
}


def available_strategies() -> tuple[str, ...]:
    """Every selection strategy this build ships."""
    return tuple(sorted(_STRATEGIES))


def strategy_by_name(name: str, *, seed: int = 0) -> SelectionStrategy | None:
    """A strategy by name, or ``None`` — an unknown name is not silently defaulted."""
    key = as_text(name)
    factory = _STRATEGIES.get(key)
    if factory is None:
        return None
    if key == BoundedRandomStrategy.name:
        return BoundedRandomStrategy(seed=seed)
    strategy: SelectionStrategy = factory()
    return strategy


@dataclass(frozen=True, slots=True)
class _Enriched:
    """A candidate with everything the selector computed about it."""

    candidate: CandidateExperiment
    eligible: bool
    reason: str
    score: tuple[float, ...] = ()


class ExperimentSelector:
    """Validates, enriches and ranks candidates; picks one, or explains why not."""

    def __init__(
        self,
        strategy: SelectionStrategy | None = None,
        *,
        strategies: Sequence[SelectionStrategy] = (),
        gate: SafetyGate | None = None,
    ) -> None:
        self.strategy = strategy or UncertaintyFirstStrategy()
        self.strategies = tuple(strategies)
        self.gate = gate or SafetyGate()

    def select(
        self,
        candidates: Sequence[ExperimentSpec],
        *,
        context: SelectionContext,
        book: HypothesisBook | None = None,
    ) -> SelectionOutcome:
        """The best candidate that may run, or an outcome saying none may."""
        del book  # hypotheses arrive in the context; the book is not read here
        enriched: list[_Enriched] = []
        rejected: list[tuple[str, str]] = []
        for spec in candidates:
            allowed, reason = validate_experiment(
                spec,
                objective=context.objective,
                environment=context.environment,
                policy=context.policy,
                usage=context.usage,
                budget=context.budget,
                matching=context.matching,
                answered_interventions=context.answered_interventions,
                approval=context.approval,
            )
            safety = self.gate.evaluate(
                spec, environment=context.environment, approval=context.approval
            )
            if allowed and not safety.allowed:
                allowed, reason = False, safety.reason
            answered = intervention_key(
                environment_id=spec.environment_id,
                action=spec.action,
                parameters=spec.parameters,
            ) in set(context.answered_interventions)
            value = information_value(
                spec,
                context.hypotheses,
                usage=context.usage,
                budget=context.budget,
                matching=context.matching,
                answered=answered,
                objective_scope=context.objective.environment_id,
                weights=context.information_weights,
            )
            curiosity = curiosity_score(
                spec,
                context.hypotheses,
                usage=context.usage,
                budget=context.budget,
                matching=context.matching,
                answered=answered,
                known_actions=context.known_actions,
                weights=context.curiosity_weights,
            )
            candidate = CandidateExperiment(
                experiment=spec,
                strategy=self.strategy.name,
                rejected=not allowed,
                reason=reason,
                information_value=value.score,
                curiosity=curiosity.score,
                safety=safety,
            )
            if not allowed:
                rejected.append((spec.experiment_id, reason))
            enriched.append(_Enriched(candidate=candidate, eligible=allowed, reason=reason))

        eligible = [item.candidate for item in enriched if item.eligible]
        ranked = self.strategy.rank(eligible, context) if eligible else ()
        if ranked:
            chosen = ranked[0]
            ranked = (
                CandidateExperiment(
                    experiment=chosen.experiment,
                    strategy=chosen.strategy,
                    selected=True,
                    rejected=False,
                    reason="",
                    information_value=chosen.information_value,
                    curiosity=chosen.curiosity,
                    safety=chosen.safety,
                ),
                *ranked[1:],
            )
        considered = tuple(
            item.candidate if item.candidate in ranked else item.candidate for item in enriched
        )
        ordered = tuple(ranked) + tuple(item.candidate for item in enriched if not item.eligible)
        return SelectionOutcome(
            strategy=self.strategy.name,
            chosen=ranked[0] if ranked else None,
            candidates=ordered or considered,
            rejected=tuple(rejected),
            limitations=(
                "selection ranks what may run; it cannot make an experiment informative",
                "the information-value and curiosity scores are heuristics (see their modules)",
            ),
            considered=len(enriched),
        )

    def compare(
        self, candidates: Sequence[ExperimentSpec], *, context: SelectionContext
    ) -> dict[str, Any]:
        """What each strategy would have chosen, for a reproducible comparison (§10).

        This is how "the selector helps" becomes measurable on a fixed environment:
        every strategy ranks the SAME validated candidates, so a difference in the
        chosen experiment is a difference in the strategy and nothing else.
        """
        rows: dict[str, Any] = {}
        for strategy in (self.strategy, *self.strategies):
            outcome = ExperimentSelector(strategy=strategy, gate=self.gate).select(
                candidates, context=context
            )
            rows[strategy.name] = {
                "chosen": (outcome.chosen.experiment.experiment_id if outcome.chosen else ""),
                "action": (outcome.chosen.experiment.action if outcome.chosen else ""),
                "considered": outcome.considered,
                "rejected": len(outcome.rejected),
                "information_value": (outcome.chosen.information_value if outcome.chosen else None),
                "curiosity": outcome.chosen.curiosity if outcome.chosen else None,
            }
        return rows


@dataclass(frozen=True, slots=True)
class StrategyComparison:
    """The result of running the same objective under two strategies."""

    environment_id: str
    rows: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"environment_id": self.environment_id, "rows": dict(self.rows)}
