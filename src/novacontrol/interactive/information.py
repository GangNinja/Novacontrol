"""Information value — a bounded, explained ranking aid, and what it is NOT.

Choosing what to try is a judgement, and this module makes the judgement readable
instead of clever. The score is a weighted sum of quantities that are all declared
elsewhere in the build:

``uncertainty``      the mean uncertainty of the hypotheses the experiment tests
``discrimination``   the fraction of tested hypothesis PAIRS whose predictions differ
``novelty``          whether this experiment has already been run and answered
``relevance``        whether the experiment is in the objective's own scope
``cost``             the experiment's estimated seconds and cost, against the budget
``risk``             destructive, irreversible or externally visible actions

What it is not: **it is not Bayesian information gain.** A real expected-information-
gain calculation needs a prior over hypotheses and a likelihood for each possible
observation, and this build has neither — it has counts of independent evidence.
Calling a weighted sum "information gain" would be the kind of claim §5 forbids, so
``kind`` says ``heuristic`` and the limitations say what is missing. It also is not a
statement about whether the ANSWER matters to a person: only the objective's
relevance component touches that, and crudely.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from novacontrol.interactive.hypotheses import predictions_differ
from novacontrol.interactive.models import (
    BudgetUsage,
    ExperimentSpec,
    Hypothesis,
    InformationValue,
    LearningBudget,
    MatchingPolicy,
    clamp,
)

__all__ = [
    "InformationWeights",
    "discrimination_ratio",
    "information_value",
]

#: The weights, in one place so a report can quote them rather than reverse-
#: engineer them from the numbers.
_DEFAULT_WEIGHTS: Mapping[str, float] = {
    "uncertainty": 0.35,
    "discrimination": 0.35,
    "novelty": 0.15,
    "relevance": 0.15,
}

_DEFAULT_PENALTIES: Mapping[str, float] = {
    "cost": 0.10,
    "risk": 0.20,
}

_LIMITATIONS: tuple[str, ...] = (
    "a weighted heuristic over declared quantities, not Bayesian information gain",
    "it needs no priors, which is also why it cannot be shown to be optimal",
    "it ranks what to try next; it does not say whether the answer matters",
)


class InformationWeights:
    """The weights and penalties of the heuristic, as a declared object."""

    def __init__(
        self,
        *,
        weights: Mapping[str, float] | None = None,
        penalties: Mapping[str, float] | None = None,
        limitations: Sequence[str] = (),
    ) -> None:
        self.weights = {key: float(value) for key, value in (weights or _DEFAULT_WEIGHTS).items()}
        self.penalties = {
            key: float(value) for key, value in (penalties or _DEFAULT_PENALTIES).items()
        }
        self.limitations = tuple(limitations) or _LIMITATIONS

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": dict(self.weights),
            "penalties": dict(self.penalties),
            "limitations": list(self.limitations),
        }


def discrimination_ratio(hypotheses: Sequence[Hypothesis], matching: MatchingPolicy) -> float:
    """The share of hypothesis pairs this experiment's predictions would separate.

    One hypothesis has nothing to be separated from, so the ratio is measured as
    "not zero" rather than inflated to 1.0: a single-prediction test can confirm or
    contradict, and this score is not the place to claim more than that.
    """
    if len(hypotheses) < 2:
        return 0.5
    total = 0
    differing = 0
    for index, left in enumerate(hypotheses):
        for right in hypotheses[index + 1 :]:
            total += 1
            if predictions_differ(
                left.predicted, right.predicted, tolerance=matching.numeric_tolerance
            ):
                differing += 1
    return differing / total if total else 0.0


def information_value(
    spec: ExperimentSpec,
    hypotheses: Sequence[Hypothesis],
    *,
    usage: BudgetUsage,
    budget: LearningBudget,
    matching: MatchingPolicy,
    answered: bool = False,
    objective_scope: str = "",
    weights: InformationWeights | None = None,
) -> InformationValue:
    """The heuristic score for one candidate, with its components attached.

    Every component is in ``[0, 1]`` before weighting, so a score of 0.62 can be
    decomposed by a reader. ``answered=True`` drops novelty to its floor: an
    experiment whose signature has already produced current evidence scores low on
    novelty and would normally be refused before this is even asked.
    """
    weights = weights or InformationWeights()
    tested = tuple(item for item in hypotheses if item.hypothesis_id in set(spec.hypothesis_ids))
    if not tested:
        tested = tuple(hypotheses)
    uncertainty = sum(item.uncertainty for item in tested) / len(tested) if tested else 0.0
    discrimination = discrimination_ratio(tested, matching)
    novelty = 0.15 if answered else 1.0
    relevance = 1.0 if not objective_scope or objective_scope == spec.environment_id else 0.0
    cost_used = usage.cost_units / budget.max_cost_units if budget.max_cost_units else 1.0
    cost_component = clamp(cost_used, 0.0, 1.0)
    seconds_used = spec.estimated_seconds / spec.max_seconds if spec.max_seconds else 1.0
    cost_component = clamp(max(cost_component, seconds_used), 0.0, 1.0)
    risk_component = 0.0
    if spec.destructive:
        risk_component += 0.6
    if not spec.reversible:
        risk_component += 0.3
    if spec.needs_approval:
        risk_component += 0.2
    risk_component = clamp(risk_component, 0.0, 1.0)
    components = {
        "uncertainty": uncertainty,
        "discrimination": discrimination,
        "novelty": novelty,
        "relevance": relevance,
    }
    score = sum(weights.weights.get(key, 0.0) * value for key, value in components.items())
    score -= weights.penalties.get("cost", 0.0) * cost_component
    score -= weights.penalties.get("risk", 0.0) * risk_component
    components["cost_penalty"] = cost_component
    components["risk_penalty"] = risk_component
    return InformationValue(
        score=clamp(score, 0.0, 1.0),
        kind="heuristic",
        components=components,
        limitations=weights.limitations,
    )
