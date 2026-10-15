"""Plan ranking — the documented way two candidate plans are compared.

The ranking is a GATE followed by a WEIGHTED SUM, and the order matters:

1. **Feasibility is not a score.** A plan that cannot run as-is (missing
   prerequisites, or a goal that forbids its effects) is not "a bit worse" —
   it is not selectable. Infeasible plans sort last and are never returned as
   ``selected``. Scoring them would let a cheap broken plan outrank an
   expensive working one, which is the specific way a ranker becomes a hazard.

2. **Then a weighted sum of documented components** — estimated cost, step
   count, risk weight, steps needing approval, and the staleness of the facts
   the plan's preconditions lean on. Lower is better. Every component is a
   measurement or a declared weight; none is a probability, and the total is
   labelled ``heuristic`` everywhere it appears, because a weighted sum of
   heterogeneous units is a ranking and nothing more.

The weights live in :class:`PlanWeights` so a caller can state different
priorities (cost-first, risk-first) without a second ranking implementation —
and the chosen weights are reported with the result, so two rankings can be
explained after the fact.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from novacontrol.planning.candidates import CandidatePlan
from novacontrol.planning.grounding import StateGrounding

__all__ = ["PlanRanker", "PlanScore", "PlanWeights"]


@dataclass(frozen=True, slots=True)
class PlanWeights:
    """What each ranking component is worth. Heuristic weights, stated."""

    cost: float = 1.0
    steps: float = 0.5
    risk: float = 2.0
    approvals: float = 1.5
    staleness: float = 3.0

    def to_dict(self) -> dict[str, float]:
        return {
            "cost": self.cost,
            "steps": self.steps,
            "risk": self.risk,
            "approvals": self.approvals,
            "staleness": self.staleness,
        }


@dataclass(frozen=True, slots=True)
class PlanScore:
    """One candidate's score, with the components that produced it."""

    candidate_id: str
    feasible: bool
    #: ``None`` for an infeasible plan: it has no total to report, and a
    #: number would imply one.
    total: float | None
    components: Mapping[str, float]
    #: The staleness penalty actually charged (facts the plan relies on).
    stale_facts: tuple[str, ...] = ()
    label: str = "heuristic"
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "feasible": self.feasible,
            "total": None if self.total is None else round(self.total, 3),
            "components": {key: round(value, 3) for key, value in self.components.items()},
            "stale_facts": list(self.stale_facts),
            "label": self.label,
            "reasons": list(self.reasons),
        }


class PlanRanker:
    """Ranks candidate plans with the documented gate-and-sum method."""

    def __init__(self, *, weights: PlanWeights | None = None) -> None:
        self.weights = weights or PlanWeights()

    def score(self, candidate: CandidatePlan, grounding: StateGrounding | None = None) -> PlanScore:
        """Score one candidate. Infeasible plans get no total, on purpose."""
        if not candidate.feasible:
            return PlanScore(
                candidate_id=candidate.candidate_id,
                feasible=False,
                total=None,
                components={},
                reasons=("the plan cannot run as written: " + "; ".join(candidate.missing[:3]),),
            )
        stale = self._stale_dependencies(candidate, grounding)
        components = {
            "cost": candidate.estimated_cost * self.weights.cost,
            "steps": float(len(candidate.plan.steps)) * self.weights.steps,
            "risk": candidate.risk * self.weights.risk,
            "approvals": float(len(candidate.needs_approval)) * self.weights.approvals,
            "staleness": float(len(stale)) * self.weights.staleness,
        }
        total = sum(components.values())
        return PlanScore(
            candidate_id=candidate.candidate_id,
            feasible=True,
            total=total,
            components=components,
            stale_facts=stale,
            reasons=(
                *(
                    (
                        f"the plan leans on {len(stale)} stale or conflicted fact(s): "
                        + ", ".join(stale[:3]),
                    )
                    if stale
                    else ()
                ),
                *(
                    (f"{len(candidate.needs_approval)} step(s) would need approval before running",)
                    if candidate.needs_approval
                    else ()
                ),
            ),
        )

    def rank(
        self,
        candidates: Sequence[CandidatePlan],
        grounding: StateGrounding | None = None,
    ) -> tuple[tuple[CandidatePlan, PlanScore], ...]:
        """Every candidate with its score, best first. Feasible ones lead."""
        scored = [(item, self.score(item, grounding)) for item in candidates]
        # The tie-break is on STABLE properties — the planner's name and the step
        # order it produced — never on the candidate id. A candidate id carries a
        # freshly generated plan id, so breaking ties on it made the "deterministic"
        # comparison pick a different winner for the same goal on different runs.
        scored.sort(
            key=lambda pair: (
                not pair[1].feasible,
                pair[1].total if pair[1].total is not None else float("inf"),
                pair[0].planner,
                tuple(step.id for step in pair[0].plan.steps),
            )
        )
        return tuple(scored)

    def select(
        self,
        candidates: Sequence[CandidatePlan],
        grounding: StateGrounding | None = None,
    ) -> tuple[CandidatePlan | None, PlanScore | None]:
        """The best FEASIBLE plan, or ``(None, None)`` when there is none."""
        ranked = self.rank(candidates, grounding)
        if not ranked:
            return None, None
        best, best_score = ranked[0]
        if not best_score.feasible:
            return None, best_score
        return best, best_score

    def compare(
        self,
        candidates: Sequence[CandidatePlan],
        grounding: StateGrounding | None = None,
    ) -> dict[str, Any]:
        """The comparison a caller reads: rows, weights, and the winner."""
        ranked = self.rank(candidates, grounding)
        selected, _ = self.select(candidates, grounding)
        return {
            "rows": [
                {
                    "planner": candidate.planner,
                    "candidate_id": candidate.candidate_id,
                    "score": score.to_dict(),
                    "estimated_seconds": round(candidate.estimated_seconds, 3),
                    "steps": [step.id for step in candidate.plan.steps],
                    "selected": selected is not None
                    and candidate.candidate_id == selected.candidate_id,
                }
                for candidate, score in ranked
            ],
            "weights": self.weights.to_dict(),
            "label": "heuristic ranking over documented components; not a probability",
            "selected": None if selected is None else selected.candidate_id,
        }

    # -- internals -------------------------------------------------------------

    def _stale_dependencies(
        self, candidate: CandidatePlan, grounding: StateGrounding | None
    ) -> tuple[str, ...]:
        """The facts this plan's preconditions rely on that are stale/conflicted.

        Only preconditions count: a stale fact the plan never leans on is
        information, not a penalty. What a plan USES is what makes its
        evidence quality a cost.
        """
        if grounding is None:
            return ()
        used: list[str] = []
        for step in candidate.plan.steps:
            for precondition in _preconditions_of(step.parameters):
                fact = grounding.fact(precondition)
                if fact is not None and not fact.usable and fact.basis.value != "unknown":
                    used.append(precondition)
        return tuple(dict.fromkeys(used))


def _preconditions_of(parameters: Mapping[str, Any]) -> tuple[str, ...]:
    """The precondition KEYS a step carries, as (key) names.

    Steps built from tasks keep their preconditions in ``parameters`` under
    ``preconditions`` (see the plan builder), so a precondition the plan leans
    on is something the ranker can weigh instead of re-deriving.
    """
    raw = parameters.get("preconditions")
    out: list[str] = []
    if isinstance(raw, str):
        raw = (raw,)
    if isinstance(raw, tuple | list):
        for item in raw:
            key, _, _ = str(item).partition("=")
            if key.strip():
                out.append(key.strip())
    return tuple(out)
