"""Outcome estimation — what each step is EXPECTED to do, and who says so.

Estimating an outcome is only honest when the estimate can name its source, so
every estimate carries a ``basis``:

``world_model``
    Phase 22's prediction service answered for this action. The strongest
    source this build has, and the rarest.
``learned_rule``
    A Phase 23 rule covers the action IN THIS SCOPE. Provenance is the rule id
    and its status (``provisional`` or ``confirmed``); a retired or
    contradicted rule is never consulted, and a rule from another scope is not
    borrowed.
``rule_based``
    The task's own declared effects — the author of the task said what it does.
    Deterministic, and labelled as a declaration rather than a prediction.
``unavailable``
    Nothing could say. The estimate is empty and says so; no plausible-sounding
    default is written in its place.

Everything here is PREDICTION. Observed outcomes come from execution, and the
monitor compares the two; this module never lets a prediction stand in for an
observation, which is the specific way a planning layer starts lying about
what it has accomplished.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from novacontrol.planning.goals import Task, coerce_value

__all__ = [
    "EstimateBasis",
    "OutcomeEstimator",
    "PlanEstimate",
    "StepEstimate",
]


class EstimateBasis(StrEnum):
    """Where an estimate came from. Never upgraded to sound more confident."""

    WORLD_MODEL = "world_model"
    LEARNED_RULE = "learned_rule"
    RULE_BASED = "rule_based"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class StepEstimate:
    """One step's expected effect, with its provenance."""

    task_id: str
    expected: Mapping[str, Any]
    basis: EstimateBasis = EstimateBasis.UNAVAILABLE
    #: None when nothing measured confidence. A declared effect has no
    #: probability behind it, and inventing one is the fabrication this build
    #: refuses.
    confidence: float | None = None
    source: str = ""
    note: str = ""
    limitations: tuple[str, ...] = ()

    @property
    def predicted(self) -> bool:
        """True only when something beyond the task's own words predicted it."""
        return self.basis in {EstimateBasis.WORLD_MODEL, EstimateBasis.LEARNED_RULE}

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "expected": dict(self.expected),
            "basis": self.basis.value,
            "confidence": self.confidence,
            "source": self.source,
            "note": self.note,
            "predicted": self.predicted,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class PlanEstimate:
    """Every step's estimate, plus the honest summary of what could be said."""

    steps: tuple[StepEstimate, ...] = ()
    limitations: tuple[str, ...] = ()

    @property
    def predicted_steps(self) -> tuple[str, ...]:
        return tuple(item.task_id for item in self.steps if item.predicted)

    @property
    def unavailable_steps(self) -> tuple[str, ...]:
        return tuple(item.task_id for item in self.steps if item.basis is EstimateBasis.UNAVAILABLE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [item.to_dict() for item in self.steps],
            "predicted_steps": list(self.predicted_steps),
            "unavailable_steps": list(self.unavailable_steps),
            "limitations": list(self.limitations),
        }


#: Optional collaborators, both injectable so the estimator works in a test
#: with no world model and no learning engine at all.
WorldPredictor = Callable[[str], Mapping[str, Any] | None]


class OutcomeEstimator:
    """Estimates step outcomes from the strongest source that actually exists.

    ``world_predictor`` is a callable from action name to predicted state
    (the application wires a thin adapter over Phase 22's prediction service);
    ``learned_rules`` is anything with ``predict(action, scope=...)`` returning
    ``(outcome, rule)`` — Phase 23's ``RuleBook`` — or ``None``. With neither,
    every estimate degrades to the task's declared effects, and the report says
    so rather than implying a model was consulted.
    """

    def __init__(
        self,
        *,
        world_predictor: WorldPredictor | None = None,
        learned_rules: Any = None,
    ) -> None:
        self._world_predictor = world_predictor
        self._rules = learned_rules

    @property
    def providers(self) -> dict[str, Any]:
        """Which estimation sources are wired — for the status surface."""
        return {
            "world_model": self._world_predictor is not None,
            "learned_rules": self._rules is not None,
            "declared_effects": True,
        }

    def estimate_task(self, task: Task, *, scope: str = "") -> StepEstimate:
        """The expected effect of ONE task, from the best source available."""
        ruled = self._from_learned_rule(task, scope=scope)
        if ruled is not None:
            return ruled
        modelled = self._from_world_model(task)
        if modelled is not None:
            return modelled
        return self._from_declared_effects(task)

    def estimate_plan(self, tasks: Sequence[Task], *, scope: str = "") -> PlanEstimate:
        estimates = tuple(self.estimate_task(task, scope=scope) for task in tasks)
        unavailable = tuple(
            item.task_id for item in estimates if item.basis is EstimateBasis.UNAVAILABLE
        )
        limitations: list[str] = []
        if self._world_predictor is None and self._rules is None:
            limitations.append(
                "no world-model predictor and no learned rules are wired, so every "
                "estimate below is the task's own declared effect — a declaration, "
                "not a prediction"
            )
        if unavailable:
            limitations.append("no source could estimate: " + ", ".join(unavailable[:4]))
        return PlanEstimate(steps=estimates, limitations=tuple(limitations))

    # -- sources ---------------------------------------------------------------

    def _from_learned_rule(self, task: Task, *, scope: str) -> StepEstimate | None:
        if self._rules is None or not task.action:
            return None
        try:
            found = self._rules.predict(task.action, scope=scope)
        except Exception:  # noqa: BLE001 - a lookup that raises is "no rule"
            return None
        if not found:
            return None
        outcome, rule = found
        status = str(getattr(rule, "status", ""))
        if status in {"retired"}:
            # A retired rule is never a basis for an estimate, whatever the
            # lookup returned.
            return StepEstimate(
                task_id=task.id,
                expected={},
                basis=EstimateBasis.UNAVAILABLE,
                source=str(getattr(rule, "rule_id", "")),
                note=f"the learned rule for this action is {status}, so it was not used",
            )
        scope_holds = getattr(rule, "applies_to", None)
        if callable(scope_holds) and scope and not scope_holds(scope):
            return StepEstimate(
                task_id=task.id,
                expected={},
                basis=EstimateBasis.UNAVAILABLE,
                source=str(getattr(rule, "rule_id", "")),
                note=(
                    f"the learned rule was made in scope {getattr(rule, 'scope', '')!r} "
                    f"and does not apply to {scope!r}"
                ),
            )
        return StepEstimate(
            task_id=task.id,
            expected=dict(outcome),
            basis=EstimateBasis.LEARNED_RULE,
            confidence=None,
            source=str(getattr(rule, "rule_id", "")),
            note=f"learned rule, status {status or 'unknown'}",
            limitations=(
                "a learned rule is evidence about the scope it was learned in; "
                "it is not a guarantee here",
            ),
        )

    def _from_world_model(self, task: Task) -> StepEstimate | None:
        if self._world_predictor is None or not task.action:
            return None
        try:
            predicted = self._world_predictor(task.action)
        except Exception:  # noqa: BLE001 - a predictor that raises is unavailable
            return None
        if not isinstance(predicted, Mapping) or not predicted:
            return None
        return StepEstimate(
            task_id=task.id,
            expected=dict(predicted),
            basis=EstimateBasis.WORLD_MODEL,
            confidence=None,
            source="world_model",
            note="predicted by the world model's transition projection",
            limitations=(
                "a projection from past observations; the observed result is what "
                "execution reports",
            ),
        )

    def _from_declared_effects(self, task: Task) -> StepEstimate:
        expected: dict[str, Any] = {}
        for effect in task.effects:
            key, _, value = effect.partition("=")
            if key.strip():
                expected[key.strip()] = coerce_value(value.strip())
        if not expected:
            return StepEstimate(
                task_id=task.id,
                expected={},
                basis=EstimateBasis.UNAVAILABLE,
                note="the task declares no effect and no provider could estimate one",
            )
        return StepEstimate(
            task_id=task.id,
            expected=expected,
            basis=EstimateBasis.RULE_BASED,
            confidence=None,
            source=f"task:{task.id}",
            note="the task's own declared effect — a declaration, not a prediction",
            limitations=(
                "a declaration is what the task AUTHOR said would happen; this "
                "build has not observed it here",
            ),
        )
