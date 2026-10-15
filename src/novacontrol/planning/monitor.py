"""Execution monitoring — re-check before running, compare after, and decide
whether to replan.

The planner's state was read at PLANNING time. This module is the part that
refuses to let that stale reading become an action: it re-reads the world
immediately before a step runs, compares what execution OBSERVED against what
was PREDICTED, and decides — under a ceiling — whether the plan still holds.

Four jobs, each with one honest answer:

**Revalidate a step's preconditions against FRESH state.** Three outcomes, not
two: ``proceed`` (the facts hold), ``observe`` (the state is unknown, stale or
conflicted — take a safe observation or ask), and ``refuse`` (a fact actively
contradicts what the plan assumed). Conflating "cannot tell" with "no" is how a
planner either barrels into a surprise or stops at every unknown; conflating it
with "yes" is worse.

**Compare predicted against observed.** Estimates come from
:class:`~novacontrol.planning.estimation.EstimateBasis` and carry their basis;
execution reports what actually happened. The comparison records BOTH and
never rewrites the prediction to match — a prediction that is quietly corrected
after the fact can no longer be used to learn whether prediction was any good.

**Decide whether to replan, under a budget.** A deviation is not automatically
a reason to replan: replanning costs time, and a plan that fails the same way
twice needs a different plan or a person, not a third identical attempt.
``max_replans`` comes from the goal's own budget.

**Answer "is it done?" only from verified conditions.** ``Goal.satisfied_by``
already owns that logic (``None`` means "cannot tell"); the monitor reports its
verdict with the observations it was based on, and does not round ``None`` into
``True``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.planning.estimation import EstimateBasis, StepEstimate
from novacontrol.planning.goals import Goal, SuccessCondition, Task, coerce_value
from novacontrol.planning.grounding import GroundedFact, StateGrounding
from novacontrol.planning.models import PlanStep, WorkflowResult

__all__ = [
    "CompletionVerdict",
    "Deviation",
    "ObservationReport",
    "PlanMonitor",
    "PreconditionCheck",
    "RevalidationReport",
    "ReplanDecision",
]


# --------------------------------------------------------------------------- #
# Revalidation: the state as it is NOW, before the step runs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PreconditionCheck:
    """One precondition against fresh state — including "cannot say"."""

    key: str
    expected: str
    #: ``True`` holds, ``False`` actively contradicts, ``None`` cannot tell.
    holds: bool | None
    reason: str = ""
    observed_value: Any = None
    basis: str = ""
    stale: bool = False
    conflicting: bool = False
    evidence: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if self.holds is True:
            return "held"
        if self.holds is False:
            return "contradicted"
        return "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "expected": self.expected,
            "holds": self.holds,
            "status": self.status,
            "reason": self.reason,
            "observed_value": self.observed_value,
            "basis": self.basis,
            "stale": self.stale,
            "conflicting": self.conflicting,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class RevalidationReport:
    """What re-reading the state said about one step, and what to do about it."""

    step_id: str
    checks: tuple[PreconditionCheck, ...] = ()
    #: ``proceed`` | ``observe`` | ``refuse``
    verdict: str = "proceed"
    reason: str = ""
    as_of: str = ""
    world_available: bool = True

    @property
    def proceed(self) -> bool:
        return self.verdict == "proceed"

    @property
    def needs_observation(self) -> bool:
        return self.verdict == "observe"

    @property
    def refused(self) -> bool:
        return self.verdict == "refuse"

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "verdict": self.verdict,
            "proceed": self.proceed,
            "reason": self.reason,
            "as_of": self.as_of,
            "world_available": self.world_available,
            "checks": [item.to_dict() for item in self.checks],
        }


# --------------------------------------------------------------------------- #
# Observation: what was predicted vs what was seen
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Deviation:
    """One key where prediction and observation disagree."""

    key: str
    predicted: Any
    observed: Any
    #: Which source predicted it (``world_model`` / ``learned_rule`` /
    #: ``rule_based`` / ``unavailable``).
    basis: str = ""

    @property
    def predicted_at_all(self) -> bool:
        return self.basis in {
            EstimateBasis.WORLD_MODEL.value,
            EstimateBasis.LEARNED_RULE.value,
            EstimateBasis.RULE_BASED.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "predicted": self.predicted,
            "observed": self.observed,
            "basis": self.basis,
            "predicted_at_all": self.predicted_at_all,
        }


@dataclass(frozen=True, slots=True)
class ObservationReport:
    """What execution observed, set beside what was estimated.

    ``unpredicted`` keys are not failures: they were never predicted, so there
    is nothing to disagree with. Recording them separately is what stops an
    unpredicted outcome from being counted as a correct prediction.
    """

    step_id: str
    estimate: StepEstimate | None = None
    observed: Mapping[str, Any] = field(default_factory=dict)
    deviations: tuple[Deviation, ...] = ()
    unpredicted: tuple[str, ...] = ()
    agreement: str = "unknown"  # "agree" | "disagree" | "unpredicted"

    @property
    def predicted_keys(self) -> tuple[str, ...]:
        if self.estimate is None:
            return ()
        return tuple(str(key) for key in self.estimate.expected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "agreement": self.agreement,
            "observed": dict(self.observed),
            "estimate": self.estimate.to_dict() if self.estimate else None,
            "deviations": [item.to_dict() for item in self.deviations],
            "unpredicted": list(self.unpredicted),
        }


# --------------------------------------------------------------------------- #
# Replanning and completion
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ReplanDecision:
    """Whether to replan, why, and how much of the budget is left."""

    should_replan: bool
    reason: str
    replans_used: int = 0
    max_replans: int = 0
    #: True when the ceiling itself stopped the decision.
    exhausted: bool = False

    @property
    def remaining(self) -> int:
        return max(0, self.max_replans - self.replans_used)

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_replan": self.should_replan,
            "reason": self.reason,
            "replans_used": self.replans_used,
            "max_replans": self.max_replans,
            "remaining": self.remaining,
            "exhausted": self.exhausted,
        }


@dataclass(frozen=True, slots=True)
class CompletionVerdict:
    """Done, not done, or cannot tell — with what said so."""

    status: str  # "completed" | "failed" | "unknown"
    reason: str = ""
    conditions: tuple[dict[str, Any], ...] = ()
    #: The observations the verdict was computed from (keys only: a verdict is
    #: traceable to evidence, not a copy of the state).
    evidence: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return self.status == "completed"

    @property
    def cannot_tell(self) -> bool:
        return self.status == "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "verified": self.verified,
            "cannot_tell": self.cannot_tell,
            "reason": self.reason,
            "conditions": [dict(item) for item in self.conditions],
            "evidence": list(self.evidence),
        }


class PlanMonitor:
    """Re-checks, compares, and decides — always under a ceiling.

    ``grounding_service`` is what re-reads the state; with none wired, every
    revalidation reports ``observe`` rather than ``proceed``, because a monitor
    that cannot see the world must not certify a step based on it.
    """

    def __init__(
        self,
        *,
        grounding_service: Any = None,
        estimator: Any = None,
        max_replans: int = 2,
        observer: Any = None,
    ) -> None:
        self._grounding = grounding_service
        self._estimator = estimator
        self.max_replans = max(0, int(max_replans))
        self._observer = observer
        self.replans_used = 0
        self.deviations: list[Deviation] = []

    # -- revalidation -----------------------------------------------------------

    def preconditions_of(self, step: PlanStep | Task) -> tuple[str, ...]:
        """The precondition strings a step carries (tasks keep them in fields)."""
        direct = getattr(step, "preconditions", None)
        if isinstance(direct, Sequence) and not isinstance(direct, str):
            return tuple(str(item) for item in direct)
        parameters = getattr(step, "parameters", {}) or {}
        raw = parameters.get("preconditions")
        if raw is None:
            return ()
        if isinstance(raw, str):
            return (raw,)
        if isinstance(raw, Sequence):
            return tuple(str(item) for item in raw)
        return ()

    def revalidate(
        self,
        step: PlanStep | Task,
        grounding: StateGrounding | None = None,
        *,
        only: Sequence[str] | None = None,
    ) -> RevalidationReport:
        """Re-read the state for this step's preconditions, right before running.

        ``only`` restricts the check to a subset of the step's preconditions —
        the gate uses it to skip the keys an EARLIER step in the same plan
        produces, so a fact that is about to exist is not mistaken for one the
        world must already hold. A key outside ``only`` is not checked, and the
        report says which ones were.

        A step with no preconditions proceeds: there is nothing to contradict.
        A step whose state cannot be read at all reports ``observe`` — an
        unreadable world is not a green light.
        """
        step_id = str(getattr(step, "id", "") or "")
        preconditions = self.preconditions_of(step)
        if only is not None:
            keep = {str(item).strip() for item in only}
            preconditions = tuple(
                item for item in preconditions if item.partition("=")[0].strip() in keep
            )
        if not preconditions:
            return RevalidationReport(
                step_id=step_id,
                verdict="proceed",
                reason="the step declares no preconditions, so there is nothing to re-check",
            )

        if only is not None and not preconditions:
            return RevalidationReport(
                step_id=step_id,
                verdict="proceed",
                reason=(
                    "every precondition this step assumes is produced earlier in "
                    "this same plan, so there is nothing for the world to confirm"
                ),
            )

        if grounding is None:
            return RevalidationReport(
                step_id=step_id,
                verdict="observe",
                reason=(
                    "no state could be read before this step, so its preconditions "
                    "are unverified; take an observation or ask before acting"
                ),
                world_available=False,
                checks=tuple(
                    PreconditionCheck(
                        key=key,
                        expected=expected,
                        holds=None,
                        reason="no grounding was available",
                    )
                    for key, expected in (_split(item) for item in preconditions)
                ),
            )

        checks: list[PreconditionCheck] = []
        for raw in preconditions:
            key, expected = _split(raw)
            fact = grounding.fact(key)
            checks.append(_check(key, expected, fact, grounding))

        contradicted = [item for item in checks if item.holds is False]
        unknown = [item for item in checks if item.holds is None]
        if contradicted:
            return RevalidationReport(
                step_id=step_id,
                checks=tuple(checks),
                verdict="refuse",
                reason=(
                    "the state now contradicts a precondition this step assumes: "
                    + "; ".join(
                        f"{item.key} expected {item.expected!r} but holds {item.observed_value!r}"
                        for item in contradicted[:3]
                    )
                ),
                as_of=grounding.as_of,
                world_available=grounding.world_available,
            )
        if unknown:
            return RevalidationReport(
                step_id=step_id,
                checks=tuple(checks),
                verdict="observe",
                reason=(
                    "the state cannot confirm a precondition this step assumes: "
                    + "; ".join(f"{item.key} ({item.reason})" for item in unknown[:3])
                ),
                as_of=grounding.as_of,
                world_available=grounding.world_available,
            )
        return RevalidationReport(
            step_id=step_id,
            checks=tuple(checks),
            verdict="proceed",
            reason="every precondition this step assumes is held by fresh state",
            as_of=grounding.as_of,
            world_available=grounding.world_available,
        )

    # -- observation vs prediction ---------------------------------------------

    def observe(
        self,
        step_id: str,
        observed: Mapping[str, Any],
        estimate: StepEstimate | None = None,
    ) -> ObservationReport:
        """Compare what was predicted with what was seen — keeping both."""
        if estimate is None and self._estimator is not None:
            estimate = getattr(self._estimator, "_current_estimate", None)
        expected: Mapping[str, Any] = estimate.expected if estimate else {}
        basis = estimate.basis.value if estimate else EstimateBasis.UNAVAILABLE.value
        deviations: list[Deviation] = []
        unpredicted: list[str] = []
        for key, value in dict(observed).items():
            if key not in expected:
                unpredicted.append(key)
                continue
            if expected[key] != value:
                deviations.append(
                    Deviation(key=key, predicted=expected[key], observed=value, basis=basis)
                )
        predicted_keys = [key for key in expected if key not in dict(observed)]
        if deviations:
            agreement = "disagree"
        elif expected and not unpredicted:
            agreement = "agree"
        else:
            agreement = "unpredicted"
        self.deviations.extend(deviations)
        self._announce(
            "monitor.observed",
            {"step_id": step_id, "agreement": agreement, "deviations": len(deviations)},
        )
        return ObservationReport(
            step_id=step_id,
            estimate=estimate,
            observed=dict(observed),
            deviations=tuple(deviations),
            unpredicted=tuple(unpredicted + predicted_keys),
            agreement=agreement,
        )

    # -- replanning -------------------------------------------------------------

    def reassess(
        self,
        *,
        workflow: WorkflowResult | None,
        goal: Goal,
        failed_steps: Sequence[str] = (),
    ) -> ReplanDecision:
        """Whether the plan should be rebuilt — and whether the budget allows it.

        Replanning is warranted by a WORKING failure: the workflow executed but
        some step did not verify or did not complete. A plan that never ran, or
        that completed and verified, needs no replan — and the ceiling is
        checked first so a run that is already out of budget says
        ``exhausted=True`` instead of quietly planning again.
        """
        ceiling = (
            self.max_replans
            if goal is None
            else min(self.max_replans, int(goal.budget.max_replans))
        )
        if self.replans_used >= ceiling:
            return ReplanDecision(
                should_replan=False,
                reason=(
                    f"the replanning ceiling is reached ({self.replans_used}/{ceiling}); "
                    "another identical attempt is not a plan"
                ),
                replans_used=self.replans_used,
                max_replans=ceiling,
                exhausted=True,
            )
        if workflow is None:
            return ReplanDecision(
                should_replan=False,
                reason="nothing executed, so there is no outcome to reassess",
                replans_used=self.replans_used,
                max_replans=ceiling,
            )
        broken = tuple(
            dict.fromkeys(
                list(failed_steps) + list(workflow.errors) + list(workflow.unverified_steps)
            )
        )
        if not broken and workflow.executed and not workflow.unverified_steps:
            return ReplanDecision(
                should_replan=False,
                reason="every step ran and was verified; the plan holds",
                replans_used=self.replans_used,
                max_replans=ceiling,
            )
        if not workflow.executed and not broken:
            return ReplanDecision(
                should_replan=False,
                reason="the workflow did not execute and reported no step to reassess",
                replans_used=self.replans_used,
                max_replans=ceiling,
            )
        return ReplanDecision(
            should_replan=True,
            reason=(
                "the run ended with steps that did not complete or did not verify: "
                + ", ".join(broken[:4])
            ),
            replans_used=self.replans_used,
            max_replans=ceiling,
        )

    def record_replan(self) -> int:
        """Count one replan against the ceiling. Returns the new total."""
        self.replans_used += 1
        self._announce(
            "monitor.replanned",
            {"replans_used": self.replans_used, "max_replans": self.max_replans},
        )
        return self.replans_used

    def reset(self) -> None:
        """New goal, new budget. Deviations are kept: they are history."""
        self.replans_used = 0

    # -- completion -------------------------------------------------------------

    def completion(self, goal: Goal, observed: Mapping[str, Any]) -> CompletionVerdict:
        """``completed``, ``failed`` or ``unknown`` — read from verified conditions.

        ``unknown`` is a real answer: an observation that says nothing about a
        required condition cannot be rounded into success, and an unmeasurable
        goal cannot be completed by running out of steps.
        """
        evidence = tuple(str(key) for key in dict(observed))
        # A STATED FAILURE CONDITION is checked first, and it is decisive: a goal
        # that says "disk.full means this failed" and then observes disk.full has
        # failed, whatever its success conditions say. Reporting "completed" there
        # is the one confusion a success-only check can never catch, so the failure
        # conditions are evaluated before the success ones.
        failure_rows = _failure_condition_rows(goal, observed)
        broken = [str(row["key"]) for row in failure_rows if row["answer"] is True]
        if broken:
            return CompletionVerdict(
                status="failed",
                reason=(
                    "a stated failure condition was observed and held: " + ", ".join(broken[:4])
                ),
                conditions=tuple(failure_rows),
                evidence=evidence,
            )
        if not goal.success_conditions:
            return CompletionVerdict(
                status="unknown",
                reason=(
                    "the goal states no success condition, so completion cannot be "
                    "verified from any observation"
                ),
            )
        if not goal.measurable:
            return CompletionVerdict(
                status="unknown",
                reason="the goal's required success conditions are not measurable",
            )
        rows: list[dict[str, Any]] = []
        unknown: list[str] = []
        failed: list[str] = []
        for condition in goal.success_conditions:
            answer = condition.satisfied_by(observed)
            rows.append(
                {
                    "key": condition.key,
                    "expected": condition.expected,
                    "required": condition.required,
                    "answer": answer,
                }
            )
            if answer is None:
                if condition.required:
                    unknown.append(condition.key)
            elif not answer and condition.required:
                failed.append(condition.key)
        if failed:
            return CompletionVerdict(
                status="failed",
                reason=(
                    "a required success condition was checked and did not hold: "
                    + ", ".join(failed[:4])
                ),
                conditions=tuple(rows),
                evidence=evidence,
            )
        if unknown:
            return CompletionVerdict(
                status="unknown",
                reason=(
                    "no observation settled a required success condition: " + ", ".join(unknown[:4])
                ),
                conditions=tuple(rows),
                evidence=evidence,
            )
        return CompletionVerdict(
            status="completed",
            reason="every required success condition was checked and held",
            conditions=tuple(rows),
            evidence=evidence,
        )

    # -- internals ---------------------------------------------------------------

    def _announce(self, kind: str, payload: Mapping[str, Any]) -> None:
        if self._observer is None:
            return
        try:
            self._observer(kind, dict(payload))
        except Exception:  # noqa: BLE001 - an announcement is not the work
            return


def _failure_condition_rows(goal: Goal, observed: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A goal's stated failure conditions as ``expectation`` rows.

    A failure condition is written exactly like a success condition
    (``key=value``) and compared by the SAME rules, through the same
    :class:`~novacontrol.planning.goals.SuccessCondition` — a second comparison in
    this file would be a second place for "true" to mean something slightly
    different. ``answer`` is True when the failure condition holds, False when it
    was checked and does not, and None when the observation says nothing about it.
    """
    rows: list[dict[str, Any]] = []
    for text in goal.failure_conditions:
        key, _, raw = str(text).partition("=")
        name = key.strip()
        if not name:
            continue
        condition = SuccessCondition(key=name, expected=coerce_value(raw.strip()))
        rows.append(
            {
                "key": name,
                "expected": condition.expected,
                "required": True,
                "answer": condition.satisfied_by(observed),
                "kind": "failure_condition",
            }
        )
    return rows


def _split(precondition: str) -> tuple[str, str]:
    key, _, expected = str(precondition).partition("=")
    return key.strip(), expected.strip()


def _check(
    key: str,
    expected: str,
    fact: GroundedFact | None,
    grounding: StateGrounding,
) -> PreconditionCheck:
    """One precondition against one fact, preserving "cannot tell"."""
    if fact is None:
        return PreconditionCheck(
            key=key,
            expected=expected,
            holds=None,
            reason="the state holds no fact under this key",
        )
    answer = fact.holds(expected if expected else None)
    if fact.usable and answer is True:
        return PreconditionCheck(
            key=key,
            expected=expected,
            holds=True,
            reason="observed and fresh",
            observed_value=fact.value,
            basis=fact.basis.value,
            stale=fact.stale,
            conflicting=fact.conflicting,
            evidence=fact.evidence,
        )
    if fact.usable and answer is False:
        return PreconditionCheck(
            key=key,
            expected=expected,
            holds=False,
            reason="the state holds a different value than the step assumes",
            observed_value=fact.value,
            basis=fact.basis.value,
            stale=fact.stale,
            conflicting=fact.conflicting,
            evidence=fact.evidence,
        )
    if fact.conflicting:
        reason = "two sources disagree about this key"
    elif fact.stale:
        reason = f"the fact is older than {grounding.stale_after_seconds:.0f}s"
    elif fact.basis.value in {"predicted", "unknown"}:
        reason = f"the belief is {fact.basis.value}, not an observation"
    else:
        reason = "the fact cannot carry a decision"
    return PreconditionCheck(
        key=key,
        expected=expected,
        holds=None,
        reason=reason,
        observed_value=fact.value,
        basis=fact.basis.value,
        stale=fact.stale,
        conflicting=fact.conflicting,
        evidence=fact.evidence,
    )
