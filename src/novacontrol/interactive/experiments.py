"""Experiments: the bounded intervention, specified before anything runs.

An experiment here is a DECLARATION, not a stored closure. It names which hypotheses
it is testing and what each of them predicts, the environment and the one action it
will take, its preconditions, permissions and observation method, the ceilings it may
not exceed, and what counts as success, failure and "cannot tell".

Two consequences are deliberate:

**Predictions are written down first, per hypothesis.** ``predicted_by_hypothesis``
is what makes an experiment able to *distinguish* explanations rather than merely
produce output, and it is why a prediction cannot be reconstructed after the fact to
match whatever happened.

**A signature is a content hash, not a counter.** Two experiments over the same
action, the same parameters and the same hypotheses have the same signature, so
"this has already been run" is a fact about the work and not about how many times a
loop iterated.

Nothing here executes anything: this module decides whether an experiment is worth
proposing and whether it may run, and refuses with an explanation otherwise.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from novacontrol.interactive.environments import Environment
from novacontrol.interactive.hypotheses import predictions_differ
from novacontrol.interactive.models import (
    MAX_EXPERIMENTS_PER_OBJECTIVE,
    ActionSpec,
    BudgetUsage,
    ExperimentSpec,
    ExperimentStatus,
    Hypothesis,
    HypothesisStatus,
    LearningBudget,
    LearningObjective,
    MatchingPolicy,
    as_text,
    now_iso,
)

__all__ = [
    "DUPLICATE_INTERVENTION_REASON",
    "DispositionPolicy",
    "ExperimentLedger",
    "candidate_experiments",
    "experiment_signature",
    "intervention_key",
    "validate_experiment",
]


class DispositionPolicy:
    """What may be proposed and what may run — the rules a report can quote.

    These are policy, not capability: a deployment that wants to allow a destructive
    experiment in a sandbox raises ``allow_destructive`` and states it. The defaults
    are the careful ones, and "repeat an experiment whose answer is still current" is
    refused by default because that is how a bounded loop becomes an expensive one.
    """

    def __init__(
        self,
        *,
        allow_destructive: bool = False,
        allow_repeat_when_answered: bool = False,
        max_cost_multiple_of_budget: float = 1.0,
        require_distinguishing: bool = True,
        min_hypotheses: int = 1,
    ) -> None:
        self.allow_destructive = bool(allow_destructive)
        self.allow_repeat_when_answered = bool(allow_repeat_when_answered)
        self.max_cost_multiple_of_budget = max(1.0, float(max_cost_multiple_of_budget))
        self.require_distinguishing = bool(require_distinguishing)
        self.min_hypotheses = max(1, int(min_hypotheses))

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow_destructive": self.allow_destructive,
            "allow_repeat_when_answered": self.allow_repeat_when_answered,
            "max_cost_multiple_of_budget": self.max_cost_multiple_of_budget,
            "require_distinguishing": self.require_distinguishing,
            "min_hypotheses": self.min_hypotheses,
        }


def experiment_signature(
    *,
    environment_id: str,
    action: str,
    parameters: Mapping[str, Any],
    hypothesis_ids: Sequence[str],
) -> str:
    """A stable content hash for an experiment — the duplicate check's whole basis."""
    payload = {
        "environment_id": as_text(environment_id),
        "action": as_text(action),
        "parameters": {str(key): parameters[key] for key in sorted(parameters)},
        "hypothesis_ids": sorted(as_text(item) for item in hypothesis_ids),
    }
    digest = hashlib.sha1(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"exp_{digest[:12]}"


def intervention_key(*, environment_id: str, action: str, parameters: Mapping[str, Any]) -> str:
    """The identity of an INTERVENTION — what was done, where, with what settings.

    This is the duplicate question, and it is deliberately coarser than
    :func:`experiment_signature`: two experiments that act on the same environment
    with the same action and the same parameters are the same intervention, however
    the set of hypotheses being tested has changed in between. Running it a second
    time observes the same thing again, so the evidence is already in hand.
    """
    payload = {
        "environment_id": as_text(environment_id),
        "action": as_text(action),
        "parameters": {str(key): parameters[key] for key in sorted(parameters)},
    }
    digest = hashlib.sha1(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"int_{digest[:12]}"


def _criteria(
    action: str,
    predicted_by_hypothesis: Mapping[str, Mapping[str, Any]],
    matching: MatchingPolicy,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """The success/failure/inconclusive criteria, in words a person can check."""
    success = tuple(
        f"the observation matches {hypothesis_id}'s prediction for {action!r} "
        f"({dict(predicted)}) within tolerance {matching.numeric_tolerance}"
        for hypothesis_id, predicted in predicted_by_hypothesis.items()
    )
    failure = tuple(
        f"the observation matches a DIFFERENT hypothesis' prediction than "
        f"{hypothesis_id}'s for {action!r}"
        for hypothesis_id in predicted_by_hypothesis
    )
    predicted_keys = sorted({key for value in predicted_by_hypothesis.values() for key in value})
    inconclusive = (
        f"the observation is incomplete but the keys {predicted_keys} "
        f"are not covered, or the environment reported an error",
    )
    return success, failure, inconclusive


def candidate_experiments(
    objective: LearningObjective,
    hypotheses: Sequence[Hypothesis],
    environment: Environment,
    *,
    budget: LearningBudget,
    matching: MatchingPolicy,
    policy: DispositionPolicy,
    parameters_by_action: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[ExperimentSpec, ...]:
    """One candidate per exposed action × the hypotheses it could tell apart.

    A single hypothesis still yields a candidate (its prediction can be confirmed or
    contradicted); two hypotheses that predict the SAME thing for an action yield one
    too, carrying the reason it is not distinguishing — the preview should show what
    was considered and rejected, not only what was chosen.

    A SUPPORTED hypothesis stays testable on purpose: support is a threshold, and a
    rule learned from one observation would be a rule nobody checked. What stops a
    supported hypothesis from being re-run to no purpose is the duplicate rule — the
    same intervention is refused once its verdict has settled — so a re-test has to
    be a DIFFERENT intervention (a declared replicate), which is exactly the
    verification the phase asks for.
    """
    tested = tuple(
        item
        for item in hypotheses
        if item.predicted and (item.open_status or item.status is HypothesisStatus.SUPPORTED)
    )
    if len(tested) < policy.min_hypotheses:
        return ()
    parameters_by_action = parameters_by_action or {}
    candidates: list[ExperimentSpec] = []
    for spec in environment.actions():
        selected = _testable_for(spec, tested)
        if not selected:
            continue
        predicted = {item.hypothesis_id: dict(item.predicted) for item in selected}
        distinguishing = _distinguishing(predicted, matching)
        parameters = dict(parameters_by_action.get(spec.name, {}))
        success, failure, inconclusive = _criteria(spec.name, predicted, matching)
        candidates.append(
            ExperimentSpec(
                experiment_id=experiment_signature(
                    environment_id=environment.environment_id,
                    action=spec.name,
                    parameters=parameters,
                    hypothesis_ids=tuple(predicted),
                ),
                objective_id=objective.objective_id,
                hypothesis_ids=tuple(predicted),
                environment_id=environment.environment_id,
                action=spec.name,
                parameters=parameters,
                predicted_by_hypothesis=predicted,
                preconditions=spec.preconditions,
                permissions=spec.permissions,
                observation_method="environment.observe",
                estimated_seconds=min(spec.estimated_seconds, budget.max_seconds_per_experiment),
                estimated_cost_units=1.0,
                max_actions=budget.max_actions_per_experiment,
                max_seconds=budget.max_seconds_per_experiment,
                max_retries=budget.max_retries,
                success_criteria=success,
                failure_criteria=failure,
                inconclusive_criteria=inconclusive,
                cancellation_conditions=(
                    "the objective is cancelled",
                    "a budget ceiling is reached",
                    "the environment is no longer available",
                ),
                destructive=spec.destructive,
                reversible=spec.reversible,
                simulator=spec.simulator,
                status=ExperimentStatus.PROPOSED,
                created_at=now_iso(),
                updated_at=now_iso(),
                rejection_reason="" if distinguishing else _NON_DISTINGUISHING,
            )
        )
    return tuple(candidates)


_NON_DISTINGUISHING = (
    "every hypothesis being tested predicts the same observation for this action, "
    "so running it cannot tell them apart"
)

#: The stable opening of the refusal a duplicate intervention gets, so a caller can
#: tell "answered already" apart from every other reason without matching prose.
DUPLICATE_INTERVENTION_REASON = (
    "this intervention has already been completed and its evidence is still current, "
    "so repeating it would spend the budget without adding information; a declared "
    "replicate parameter is what makes a deliberate re-run a different intervention"
)


def _testable_for(spec: ActionSpec, hypotheses: Sequence[Hypothesis]) -> tuple[Hypothesis, ...]:
    """Which open hypotheses an action could speak to (all of them, here).

    An action is a candidate test for every open hypothesis with a prediction: the
    predictions are what decide distinguishability, not a keyword match between the
    hypothesis text and the action name, which would be a guess dressed as a filter.
    """
    del spec
    return tuple(hypotheses)


def _distinguishing(
    predicted_by_hypothesis: Mapping[str, Mapping[str, Any]], matching: MatchingPolicy
) -> bool:
    """Whether two of these predictions would be told apart by an observation."""
    values = list(predicted_by_hypothesis.values())
    if len(values) < 2:
        return True
    first = values[0]
    for other in values[1:]:
        if predictions_differ(first, other, tolerance=matching.numeric_tolerance):
            return True
    return False


def validate_experiment(
    spec: ExperimentSpec,
    *,
    objective: LearningObjective,
    environment: Environment | None,
    policy: DispositionPolicy,
    usage: BudgetUsage,
    budget: LearningBudget,
    matching: MatchingPolicy,
    answered_interventions: Sequence[str] = (),
    approval: bool = False,
) -> tuple[bool, str]:
    """Whether this experiment may run, and the reason when it may not.

    Every rejection happens HERE, before an action is taken: a budget or permission
    check that runs after the intervention is a report, not a control (§11/§18).

    ``answered_interventions`` holds the interventions that already produced a
    SETTLED verdict. An intervention whose evidence was inconclusive is not in it, so
    a bounded re-observation of a partial reading is allowed — and the same
    intervention is never repeated once it has settled something.
    """
    if environment is None:
        return False, f"no environment {spec.environment_id!r} is registered"
    if objective.environment_id and spec.environment_id != objective.environment_id:
        return False, (
            f"experiment targets {spec.environment_id!r}, but objective "
            f"{objective.objective_id!r} is scoped to {objective.environment_id!r}"
        )
    if usage.experiments >= budget.max_experiments:
        return False, (
            f"the objective has already run {usage.experiments} experiment(s), which is its "
            f"ceiling ({budget.max_experiments})"
        )
    if usage.seconds >= budget.max_seconds_total:
        return False, (
            f"the objective has spent {usage.seconds:.2f}s of its {budget.max_seconds_total:.2f}s"
        )
    if usage.cost_units + spec.estimated_cost_units > budget.max_cost_units:
        return False, (
            f"this experiment's estimated cost ({spec.estimated_cost_units:.2f}) would take the "
            f"objective past its ceiling ({budget.max_cost_units:.2f})"
        )
    if spec.estimated_cost_units > policy.max_cost_multiple_of_budget * budget.max_cost_units:
        return False, "the experiment's estimated cost is out of proportion to the objective"
    if spec.estimated_seconds > budget.max_seconds_per_experiment:
        return False, (
            f"the experiment estimates {spec.estimated_seconds:.2f}s, more than the per-experiment "
            f"ceiling ({budget.max_seconds_per_experiment:.2f}s)"
        )
    if spec.max_actions > budget.max_actions_per_experiment:
        return False, (
            f"the experiment allows {spec.max_actions} action(s), more than the per-experiment "
            f"ceiling ({budget.max_actions_per_experiment})"
        )
    if spec.destructive and not policy.allow_destructive:
        return False, (
            "destructive experiments are refused by policy: a destructive action is "
            "never approved, so no approval would change this"
        )
    if spec.needs_approval and not approval:
        return False, (
            "this experiment is not a simulator action (or is destructive), so it needs "
            "explicit approval before it may run; nothing was attempted"
        )
    ok, reason = environment.validate(spec.action, spec.parameters)
    if not ok:
        return False, reason
    if policy.require_distinguishing and _is_non_distinguishing(spec, matching):
        return False, _NON_DISTINGUISHING
    key = intervention_key(
        environment_id=spec.environment_id, action=spec.action, parameters=spec.parameters
    )
    if key in set(answered_interventions) and not policy.allow_repeat_when_answered:
        return False, (DUPLICATE_INTERVENTION_REASON)
    return True, ""


def _is_non_distinguishing(spec: ExperimentSpec, matching: MatchingPolicy) -> bool:
    return not _distinguishing(spec.predicted_by_hypothesis, matching)


class ExperimentLedger:
    """Every experiment this process knows about, and what became of each one."""

    def __init__(self) -> None:
        self._specs: dict[str, ExperimentSpec] = {}
        self._order: list[str] = []

    def get(self, experiment_id: str) -> ExperimentSpec | None:
        return self._specs.get(as_text(experiment_id))

    def all(self) -> tuple[ExperimentSpec, ...]:
        return tuple(self._specs[key] for key in self._order)

    def for_objective(self, objective_id: str) -> tuple[ExperimentSpec, ...]:
        target = as_text(objective_id)
        return tuple(item for item in self.all() if item.objective_id == target)

    def add(self, spec: ExperimentSpec) -> ExperimentSpec:
        """Record a candidate. Re-recording the same signature replaces the row."""
        if spec.experiment_id in self._specs:
            updated = replace(spec, created_at=self._specs[spec.experiment_id].created_at)
            self._specs[updated.experiment_id] = updated
            return updated
        count = len(self.for_objective(spec.objective_id))
        if count >= MAX_EXPERIMENTS_PER_OBJECTIVE:
            raise ValueError(
                f"objective {spec.objective_id!r} already holds {count} experiments, which is "
                f"the ceiling ({MAX_EXPERIMENTS_PER_OBJECTIVE})"
            )
        self._specs[spec.experiment_id] = spec
        self._order.append(spec.experiment_id)
        return spec

    def set_status(
        self, experiment_id: str, status: ExperimentStatus, *, reason: str = ""
    ) -> ExperimentSpec:
        """Move one experiment's status, and never back to a claim it did not earn."""
        current = self.get(experiment_id)
        if current is None:
            raise KeyError(f"no experiment {experiment_id!r} in this ledger")
        updated = replace(
            current,
            status=status,
            rejection_reason=reason or current.rejection_reason,
            attempts=current.attempts + (1 if status is ExperimentStatus.RUNNING else 0),
            updated_at=now_iso(),
        )
        self._specs[updated.experiment_id] = updated
        return updated

    def answered_interventions(self, objective_id: str) -> tuple[str, ...]:
        """Interventions for this objective whose evidence was FULLY OBSERVED.

        A run that observed nothing — a failed, blocked, timed-out or interrupted
        attempt — did not answer anything, so its intervention stays available.
        Whether a settled verdict came out of it is the manager's record to keep, not
        this ledger's: the run is where the comparison lives.
        """
        return tuple(
            intervention_key(
                environment_id=item.environment_id,
                action=item.action,
                parameters=item.parameters,
            )
            for item in self.for_objective(objective_id)
            if item.status is ExperimentStatus.COMPLETED
        )

    def restore(self, specs: Sequence[ExperimentSpec]) -> tuple[int, int]:
        """Rebuild the ledger; a ``running`` row becomes INTERRUPTED (§13).

        An experiment that was in flight when the process stopped cannot be assumed
        to have finished: marking it interrupted is what stops a restart from
        reporting a result nobody observed. Returns (restored, interrupted).
        """
        restored = 0
        interrupted = 0
        for item in specs:
            if not item.experiment_id:
                continue
            spec = item
            if item.status in {ExperimentStatus.RUNNING, ExperimentStatus.APPROVED}:
                spec = replace(
                    item,
                    status=ExperimentStatus.INTERRUPTED,
                    rejection_reason=(
                        "the process stopped while this experiment was in flight; its outcome "
                        "was never observed, so it is not reported as completed"
                    ),
                    updated_at=now_iso(),
                )
                interrupted += 1
            self._specs[spec.experiment_id] = spec
            if spec.experiment_id not in self._order:
                self._order.append(spec.experiment_id)
            restored += 1
        return restored, interrupted

    def counts(self) -> dict[str, int]:
        """How many experiments are in each status — the metrics table reads this."""
        counts: dict[str, int] = {member.value: 0 for member in ExperimentStatus}
        for item in self.all():
            counts[item.status.value] += 1
        return counts
