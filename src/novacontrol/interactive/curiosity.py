"""Curiosity — bounded, explained, and never a reward the build trains on.

Curiosity here is a ranking aid: it says which *useful discovery* an experiment is
most likely to produce, and it deliberately rewards understanding rather than
novelty. The components are:

``uncertainty_reduction``  how unsettled the tested hypotheses are
``unknown_transition``     1.0 when the action's outcome is not already known in scope
``competing_resolution``   how many hypothesis pairs one action could separate
``prediction_gap``         how far the current belief is from "settled by evidence"
``limitation_discovery``   whether the objective itself is about finding a boundary

and the penalties, which exist because unbounded curiosity is what §9 forbids:

``unsafe``       destructive or irreversible actions
``repetitive``   an experiment whose signature has already produced current evidence
``unverified``   a candidate with no predictions, so nothing could be checked
``resource``     the estimated cost against the remaining budget, and extra actions
``unbounded``    an experiment allowed more actions than the policy's ceiling

**This is not a validated intrinsic reward.** It is a weighted sum over declared
quantities, used to order candidates; it is never added to Phase 15's trajectories or
Phase 18's reward data, and no model is trained on it. ``kind`` says ``heuristic``
and the limitations say so in the payload as well as here, because a scalar with a
persuasive name is how an arbitrary weight becomes a scientific claim.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from novacontrol.interactive.hypotheses import predictions_differ
from novacontrol.interactive.models import (
    BudgetUsage,
    CuriosityScore,
    ExperimentSpec,
    Hypothesis,
    LearningBudget,
    MatchingPolicy,
    clamp,
)

__all__ = [
    "CuriosityWeights",
    "curiosity_score",
]

_DEFAULT_WEIGHTS: Mapping[str, float] = {
    "uncertainty_reduction": 0.30,
    "unknown_transition": 0.25,
    "competing_resolution": 0.25,
    "prediction_gap": 0.15,
    "limitation_discovery": 0.05,
}

_DEFAULT_PENALTIES: Mapping[str, float] = {
    "unsafe": 0.50,
    "repetitive": 0.40,
    "unverified": 0.30,
    "resource": 0.20,
    "unbounded": 0.20,
}

_LIMITATIONS: tuple[str, ...] = (
    "a bounded heuristic ranking aid, not a validated intrinsic reward",
    "it is never fed to Phase 15 trajectories, Phase 18 rewards or any training run",
    "it rewards useful discovery; it says nothing about the novelty of an action itself",
)


class CuriosityWeights:
    """The components, penalties and limitations of the curiosity score."""

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


def curiosity_score(
    spec: ExperimentSpec,
    hypotheses: Sequence[Hypothesis],
    *,
    usage: BudgetUsage,
    budget: LearningBudget,
    matching: MatchingPolicy,
    answered: bool = False,
    known_actions: Sequence[str] = (),
    weights: CuriosityWeights | None = None,
) -> CuriosityScore:
    """How much this experiment is expected to teach, and what that number is made of."""
    weights = weights or CuriosityWeights()
    tested = tuple(item for item in hypotheses if item.hypothesis_id in set(spec.hypothesis_ids))
    if not tested:
        tested = tuple(hypotheses)
    uncertainty = sum(item.uncertainty for item in tested) / len(tested) if tested else 0.0
    unknown_transition = 0.0 if spec.action in set(known_actions) else 1.0
    pairs = 0
    differing = 0
    for index, left in enumerate(tested):
        for right in tested[index + 1 :]:
            pairs += 1
            if predictions_differ(
                left.predicted, right.predicted, tolerance=matching.numeric_tolerance
            ):
                differing += 1
    competing = (differing / pairs) if pairs else 0.5
    settled = [
        abs((item.confidence if item.confidence is not None else 0.5) - 0.5) * 2.0
        for item in tested
    ]
    prediction_gap = 1.0 - (sum(settled) / len(settled) if settled else 0.0)
    found_boundary = any("boundary" in item.statement.lower() for item in tested)
    limitation_discovery = 1.0 if found_boundary else 0.0
    components = {
        "uncertainty_reduction": uncertainty,
        "unknown_transition": unknown_transition,
        "competing_resolution": competing,
        "prediction_gap": prediction_gap,
        "limitation_discovery": limitation_discovery,
    }
    cost_ratio = (
        (usage.cost_units + spec.estimated_cost_units) / budget.max_cost_units
        if budget.max_cost_units
        else 1.0
    )
    penalties = {
        "unsafe": 1.0 if (spec.destructive or not spec.reversible) else 0.0,
        "repetitive": 1.0 if answered else 0.0,
        "unverified": 0.0 if spec.predicted_by_hypothesis else 1.0,
        "resource": clamp(cost_ratio, 0.0, 1.0),
        "unbounded": 1.0 if spec.max_actions > budget.max_actions_per_experiment else 0.0,
    }
    score = sum(weights.weights.get(key, 0.0) * value for key, value in components.items())
    score -= sum(weights.penalties.get(key, 0.0) * value for key, value in penalties.items())
    return CuriosityScore(
        score=clamp(score, 0.0, 1.0),
        kind="heuristic",
        components=components,
        penalties=penalties,
        limitations=weights.limitations,
    )
