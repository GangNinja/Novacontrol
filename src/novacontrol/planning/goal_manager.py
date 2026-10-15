"""Goal manager — the one place a goal becomes a plan, runs, and is judged.

This module is an ORCHESTRATOR, and the distinction matters: everything it
does is delegated to a component that already existed. Decomposition is
:func:`~novacontrol.planning.goals.decompose`, grounding is
:class:`~novacontrol.planning.grounding.StateGroundingService`, candidates come
from the two planners, selection is :class:`~novacontrol.planning.ranking.PlanRanker`,
authorization is :class:`~novacontrol.planning.policy.ActionPolicy`, execution is
the application's own :class:`~novacontrol.planning.executor.WorkflowExecutor`,
and reassessment is :class:`~novacontrol.planning.monitor.PlanMonitor`. There is
no second planner, no second loop and no second permission check here — this is
the order they are called in, and the state that remembers what happened.

Three rules, each of which is a way a planning layer typically starts lying:

**A plan is not an execution.** ``plan()`` produces candidates and names one;
``execute()`` is the only method that touches the machine, and it is the only
one whose report claims steps ran. A caller that stops at ``plan()`` has a
documented intention and nothing else, and the report says exactly that.

**Preconditions are re-checked against FRESH state immediately before the step
runs**, through a gate in front of the application's own step runner — the one
door to real work. The gate never becomes a second executor: it raises
``PermissionError``, which the existing executor already reads as a refusal
(``DENIED``, never retried), so a step the state no longer supports is a
decision rather than a failure, and the executor keeps owning retries,
verification and recovery.

**Completion is a verified condition, never the end of the steps.** A run whose
last step returned is not done; it is done when the goal's required success
conditions were checked against an observation and held. When nothing can
observe them, the verdict is ``unknown`` — reported, not rounded up.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from inspect import isawaitable
from typing import Any

from novacontrol.planning.candidates import (
    BaselineOrderPlanner,
    CandidatePlan,
    CostAwarePlanner,
    PlanningBudget,
    PlanningOutcome,
)
from novacontrol.planning.estimation import OutcomeEstimator
from novacontrol.planning.executor import StepHandler, WorkflowExecutor
from novacontrol.planning.goals import (
    Goal,
    GoalStatus,
    SuccessCondition,
    Task,
    TaskDecomposition,
    decompose,
)
from novacontrol.planning.grounding import StateGrounding, StateGroundingService
from novacontrol.planning.models import Plan, WorkflowResult
from novacontrol.planning.monitor import (
    CompletionVerdict,
    PlanMonitor,
    ReplanDecision,
    RevalidationReport,
)
from novacontrol.planning.policy import ActionPolicy, PolicyReport
from novacontrol.planning.ranking import PlanRanker

__all__ = [
    "GoalManager",
    "GoalRun",
    "JsonGoalRepository",
    "PlanReport",
]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_stamp(text: str) -> datetime | None:
    """A stored timestamp, or None when it cannot be read. Never raises."""
    try:
        return datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def _deadline_at(goal: Goal) -> datetime | None:
    """The absolute moment a goal's duration limit ends, when it stated one."""
    if goal.deadline_seconds is None:
        return None
    created = _parse_stamp(goal.created_at)
    if created is None:
        return None
    return created + timedelta(seconds=float(goal.deadline_seconds))


def _seconds_until(goal: Goal) -> float | None:
    """Seconds left before a goal's deadline; negative once it has passed."""
    deadline = _deadline_at(goal)
    if deadline is None:
        return None
    return (deadline - datetime.now(UTC)).total_seconds()


def _condition_key(text: Any) -> str:
    """The fact key inside a ``key=value`` condition; "" when there is none."""
    key, _, _ = str(text).partition("=")
    return key.strip()


@dataclass(frozen=True, slots=True)
class _RunLimits:
    """When a RUNNING goal must stop: its deadline, and the clock it may spend."""

    #: Absolute moment the goal's duration limit ends, when it stated one.
    deadline_at: datetime | None
    #: Seconds this run may spend, from the goal's budget. Required on purpose:
    #: a default here would silently stop every run it was forgotten for.
    max_seconds: float
    #: When this run began, as a parsed moment (None when unreadable).
    started_at: datetime | None


# --------------------------------------------------------------------------- #
# What one execution produced
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GoalRun:
    """One execution of a goal's selected plan, with everything that was checked."""

    goal_id: str
    plan_id: str
    #: ``completed`` only when the goal's conditions were VERIFIED. Also
    #: ``blocked`` (a precondition refused the step), ``denied`` (the policy
    #: refused the plan), ``needs_observation``, ``failed``, ``partial``.
    status: str
    reason: str = ""
    workflow: WorkflowResult | None = None
    #: Per-step revalidation, in the order the steps ran.
    revalidations: tuple[RevalidationReport, ...] = ()
    completion: CompletionVerdict | None = None
    started_at: str = ""
    finished_at: str = ""
    #: Steps that ran without anything verifying them (from the executor).
    unverified_steps: tuple[str, ...] = ()

    @property
    def executed(self) -> bool:
        return self.workflow is not None and self.workflow.executed

    @property
    def verified(self) -> bool:
        return self.completion is not None and self.completion.verified

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "plan_id": self.plan_id,
            "status": self.status,
            "reason": self.reason,
            "executed": self.executed,
            "verified": self.verified,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "unverified_steps": list(self.unverified_steps),
            "revalidations": [item.to_dict() for item in self.revalidations],
            "completion": self.completion.to_dict() if self.completion else None,
            "workflow": self.workflow.to_dict() if self.workflow else None,
        }


@dataclass(frozen=True, slots=True)
class PlanReport:
    """What planning one goal produced: candidates, the comparison, the pick."""

    goal_id: str
    grounding: StateGrounding | None = None
    decomposition: TaskDecomposition | None = None
    outcomes: tuple[PlanningOutcome, ...] = ()
    comparison: Mapping[str, Any] = field(default_factory=dict)
    selected: CandidatePlan | None = None
    policy: PolicyReport | None = None
    estimates: Mapping[str, Any] = field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    #: Set when nothing could be planned, and why.
    refusal: str = ""

    @property
    def planned(self) -> bool:
        return self.selected is not None and not self.refusal

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "planned": self.planned,
            "refusal": self.refusal,
            "grounding": self.grounding.as_dict() if self.grounding else None,
            "decomposition": self.decomposition.to_dict() if self.decomposition else None,
            # ``planners`` is what each planner PRODUCED (with its expansions and
            # whether a ceiling truncated it); ``candidates`` is every candidate
            # plan, flattened. Two different things, two different keys — one key
            # holding the first while being named after the second is how a caller
            # ends up reading ``candidate_id`` off a planner row.
            "planners": [item.to_dict() for item in self.outcomes],
            "candidates": [
                candidate.to_dict() for outcome in self.outcomes for candidate in outcome.candidates
            ],
            "comparison": dict(self.comparison),
            "selected": self.selected.to_dict() if self.selected else None,
            "policy": self.policy.to_dict() if self.policy else None,
            "estimates": dict(self.estimates),
            "limitations": list(self.limitations),
        }


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


class JsonGoalRepository:
    """Goals over the application's JSON state store — one key per goal."""

    def __init__(self, store: Any, *, prefix: str = "goal") -> None:
        self.store = store
        self.prefix = prefix

    def key(self, goal_id: str) -> str:
        return f"{self.prefix}_{_sanitize(goal_id)}"

    def load(self, goal_id: str) -> dict[str, Any]:
        try:
            payload = self.store.read(self.key(goal_id))
        except Exception:  # noqa: BLE001 - an unreadable store is an empty one
            return {}
        return dict(payload) if isinstance(payload, Mapping) else {}

    def save(self, goal_id: str, payload: Mapping[str, Any]) -> bool:
        try:
            self.store.write(self.key(goal_id), dict(payload))
        except Exception:  # noqa: BLE001 - persistence must not fail the work
            return False
        return True

    def clear(self, goal_id: str) -> bool:
        try:
            self.store.write(self.key(goal_id), {})
        except Exception:  # noqa: BLE001
            return False
        return True

    def index(self) -> tuple[str, ...]:
        try:
            payload = self.store.read("goal_index")
        except Exception:  # noqa: BLE001
            return ()
        raw = payload.get("goals") if isinstance(payload, Mapping) else None
        if not isinstance(raw, list):
            return ()
        out: list[str] = []
        for item in raw:
            key = str(item or "").strip()
            if key and key not in out:
                out.append(key)
        return tuple(out)

    def write_index(self, goal_ids: Sequence[str]) -> bool:
        try:
            self.store.write("goal_index", {"goals": list(goal_ids)})
        except Exception:  # noqa: BLE001
            return False
        return True


def _sanitize(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "-" for char in str(value))[:64]


# --------------------------------------------------------------------------- #
# The manager
# --------------------------------------------------------------------------- #


class GoalManager:
    """Ground → decompose → plan → rank → authorize → execute → verify → reassess.

    Every collaborator is optional and the defaults are honest ones: no world
    model means every fact is an assertion, no executor means ``execute()``
    refuses rather than pretending, and no permission layer means consequential
    steps still ask before they run.
    """

    def __init__(
        self,
        *,
        executor: WorkflowExecutor | None = None,
        grounding: StateGroundingService | None = None,
        estimator: OutcomeEstimator | None = None,
        ranker: PlanRanker | None = None,
        policy: ActionPolicy | None = None,
        permissions: Any = None,
        repository: Any = None,
        observer: Any = None,
        budget: PlanningBudget | None = None,
        stale_after_seconds: float | None = None,
    ) -> None:
        self._executor = executor
        self._grounding_service = grounding or StateGroundingService()
        if stale_after_seconds is not None and grounding is None:
            self._grounding_service = StateGroundingService(stale_after_seconds=stale_after_seconds)
        self._estimator = estimator or OutcomeEstimator()
        self._ranker = ranker or PlanRanker()
        self._policy = policy or ActionPolicy(permissions=permissions)
        self._repository = repository
        self._observer = observer
        self.budget = budget or PlanningBudget()
        self._planners = (BaselineOrderPlanner(), CostAwarePlanner())
        self._goals: dict[str, Goal] = {}
        self._decompositions: dict[str, TaskDecomposition] = {}
        self._candidates: dict[str, tuple[CandidatePlan, ...]] = {}
        self._selected: dict[str, str] = {}
        self._monitors: dict[str, PlanMonitor] = {}
        self._runs: dict[str, GoalRun] = {}
        self._restore_report: dict[str, Any] = {}

    # -- lifecycle ---------------------------------------------------------------

    @property
    def wired(self) -> dict[str, Any]:
        """What is actually connected — for the status surface."""
        return {
            "executor": self._executor is not None,
            "world_model": bool(getattr(self._grounding_service, "_world", None)),
            "permissions": bool(getattr(self._policy, "_permissions", None)),
            "repository": self._repository is not None,
            "estimator": self._estimator.providers,
        }

    def restore(self) -> dict[str, Any]:
        """Reload stored goals. Never raises: a corrupt record is skipped.

        What comes back is the GOAL — its request, its stated conditions, its
        budgets — and its validated decomposition. What deliberately does NOT come
        back is a selected plan: a plan is a decision made against state this
        process never observed, so restoring one would carry a stale intention into
        a new process. The record of which candidate WAS chosen is reported beside
        it, as a fact about the record rather than as a live selection.
        """
        report: dict[str, Any] = {"restored": 0, "skipped": 0, "ids": []}
        if self._repository is None:
            return report
        for goal_id in self._repository.index():
            payload = self._repository.load(goal_id)
            if not payload:
                report["skipped"] += 1
                continue
            try:
                goal = Goal.from_mapping(payload.get("goal") or payload)
            except Exception:  # noqa: BLE001 - one bad record is not a boot failure
                report["skipped"] += 1
                continue
            self._goals[goal.goal_id] = goal
            raw_decomposition = payload.get("decomposition")
            has_decomposition = isinstance(raw_decomposition, Mapping)
            if has_decomposition:
                self._decompositions[goal.goal_id] = _decomposition_from(raw_decomposition)
            # A BOUNDED row, not the stored payload: this report travels on the
            # status surface, and a goal's request text is the user's words rather
            # than an operational figure. What an operator needs from a boot is
            # which goals came back, in what state, and what was chosen last time.
            previous = payload.get("selected")
            self._restore_report[goal.goal_id] = {
                "goal_id": goal.goal_id,
                "status": str(getattr(goal.status, "value", goal.status)),
                "decomposition": has_decomposition,
                "conditions": len(goal.success_conditions),
                "previously_selected": str(previous) if isinstance(previous, str) else "",
            }
            report["restored"] += 1
            report["ids"].append(goal.goal_id)
        return report

    def save(self, goal_id: str) -> bool:
        """Persist one goal. Returns whether it landed; never raises."""
        goal = self._goals.get(goal_id)
        if goal is None or self._repository is None:
            return False
        payload: dict[str, Any] = {"goal": goal.to_dict()}
        decomposition = self._decompositions.get(goal_id)
        if decomposition is not None:
            payload["decomposition"] = decomposition.to_dict()
        if goal_id in self._selected:
            payload["selected"] = self._selected[goal_id]
        ok = bool(self._repository.save(goal_id, payload))
        if ok:
            ids = list(self._repository.index())
            if goal_id not in ids:
                ids.append(goal_id)
                self._repository.write_index(ids)
        return ok

    # -- creating and decomposing --------------------------------------------------

    def create(
        self,
        request: str,
        *,
        success_conditions: Sequence[SuccessCondition] = (),
        initial_state: Mapping[str, Any] | None = None,
        constraints: Sequence[str] = (),
        exclusions: Sequence[str] = (),
        required_permissions: Sequence[str] = (),
        approval_conditions: Sequence[str] = (),
        deadline_seconds: float | None = None,
        failure_conditions: Sequence[str] = (),
        cancellation_conditions: Sequence[str] = (),
        provenance: str = "",
        correlation_id: str = "",
        max_steps: int | None = None,
        max_replans: int | None = None,
        max_seconds: float | None = None,
    ) -> Goal:
        """Record a goal exactly as stated — missing fields stay missing."""
        from novacontrol.planning.goals import GoalBudget

        budget_kwargs: dict[str, Any] = {}
        if max_steps is not None:
            budget_kwargs["max_steps"] = int(max_steps)
        if max_replans is not None:
            budget_kwargs["max_replans"] = int(max_replans)
        if max_seconds is not None:
            budget_kwargs["max_seconds"] = float(max_seconds)
        goal = Goal(
            request=str(request),
            success_conditions=tuple(success_conditions),
            initial_state=dict(initial_state or {}),
            constraints=tuple(constraints),
            exclusions=tuple(exclusions),
            budget=GoalBudget(**budget_kwargs) if budget_kwargs else GoalBudget(),
            required_permissions=tuple(required_permissions),
            approval_conditions=tuple(approval_conditions),
            deadline_seconds=deadline_seconds,
            failure_conditions=tuple(failure_conditions),
            cancellation_conditions=tuple(cancellation_conditions),
            provenance=provenance,
            correlation_id=correlation_id,
        )
        self._goals[goal.goal_id] = goal
        self._monitors[goal.goal_id] = PlanMonitor(
            grounding_service=self._grounding_service,
            estimator=self._estimator,
            max_replans=goal.budget.max_replans,
            observer=self._observer,
        )
        self._announce(
            "goal.created",
            {"goal_id": goal.goal_id, "correlation_id": correlation_id},
        )
        self.save(goal.goal_id)
        return goal

    def goals(self) -> tuple[Goal, ...]:
        return tuple(self._goals.values())

    def get(self, goal_id: str) -> Goal | None:
        return self._goals.get(str(goal_id))

    def cancel(self, goal_id: str) -> Goal | None:
        goal = self._goals.get(str(goal_id))
        if goal is None:
            return None
        updated = goal.with_(status=GoalStatus.CANCELLED)
        self._goals[goal_id] = updated
        self._announce("goal.cancelled", {"goal_id": goal_id})
        self.save(goal_id)
        return updated

    def decompose(
        self,
        goal_id: str,
        tasks: Sequence[Task],
        *,
        known_capabilities: Sequence[str] = (),
        held_facts: Mapping[str, Any] | None = None,
    ) -> TaskDecomposition:
        """Validate a decomposition and remember it. Issues are reported, not raised."""
        goal = self._require(goal_id)
        result = decompose(
            goal,
            tasks,
            known_capabilities=known_capabilities,
            held_facts=held_facts,
        )
        self._decompositions[goal_id] = result
        self._goals[goal_id] = goal.with_(status=GoalStatus.PLANNED)
        self._announce(
            "goal.decomposed",
            {
                "goal_id": goal_id,
                "tasks": len(result.tasks),
                "issues": len(result.issues),
            },
        )
        self.save(goal_id)
        return result

    # -- planning ------------------------------------------------------------------

    def plan(
        self,
        goal_id: str,
        *,
        tasks: Sequence[Task] | None = None,
        keys: Sequence[str] = (),
        budget: PlanningBudget | None = None,
        known_capabilities: Sequence[str] = (),
    ) -> PlanReport:
        """Ground, plan with both planners, rank, authorize — and name one candidate.

        Produces CANDIDATES and nothing else: no step runs from here. A
        decomposition with structural issues yields a refusal naming them, and a
        decomposition whose preconditions no state supports yields an infeasible
        candidate that the ranker refuses to select.
        """
        goal = self._require(goal_id)
        limits = budget or self.budget
        # A goal the caller cancelled, or one whose own limit has already passed, is
        # not planned for: producing candidates for work nobody will run is how a
        # refusal gets read as progress.
        stopped = self._start_refusal(goal)
        if stopped is not None:
            self._announce("goal.plan_refused", {"goal_id": goal_id})
            return PlanReport(goal_id=goal_id, refusal=stopped[1])
        # A plan() call on a goal that already has a selected plan (or a run) is a
        # REPLAN, and it is counted HERE rather than left to the caller: the
        # ceiling exists to stop the loop, so the place that would start another
        # attempt is the place that has to count it. An exhausted ceiling returns a
        # refusal saying so, rather than quietly planning the same thing again.
        if goal_id in self._selected or goal_id in self._runs:
            monitor = self._monitor(goal_id)
            ceiling = min(monitor.max_replans, goal.budget.max_replans)
            if monitor.replans_used >= ceiling:
                self._announce("goal.plan_refused", {"goal_id": goal_id})
                return PlanReport(
                    goal_id=goal_id,
                    refusal=(
                        f"the replanning ceiling is reached ({monitor.replans_used}/"
                        f"{ceiling}); another attempt at the same goal is not a plan"
                    ),
                )
            monitor.record_replan()
        decomposition: TaskDecomposition | None
        if tasks is not None:
            decomposition = self.decompose(goal_id, tasks, known_capabilities=known_capabilities)
        else:
            decomposition = self._decompositions.get(goal_id)
        if decomposition is None:
            self._announce("goal.plan_refused", {"goal_id": goal_id})
            return PlanReport(
                goal_id=goal_id,
                refusal="this goal has no decomposition yet; supply tasks first",
            )

        grounding = self._ground(goal, keys=keys, extra=decomposition)
        outcomes = tuple(
            planner.plan(goal, decomposition, grounding, budget=limits)
            for planner in self._planners
        )
        candidates = tuple(candidate for outcome in outcomes for candidate in outcome.candidates)
        self._candidates[goal_id] = candidates
        if not candidates:
            refusal = (
                "; ".join(limitation for outcome in outcomes for limitation in outcome.limitations)
                or "no planner produced a candidate"
            )
            self._announce("goal.plan_refused", {"goal_id": goal_id})
            return PlanReport(
                goal_id=goal_id,
                grounding=grounding,
                decomposition=decomposition,
                outcomes=outcomes,
                refusal=refusal,
                limitations=tuple(
                    limitation for outcome in outcomes for limitation in outcome.limitations
                ),
            )

        comparison = self._ranker.compare(candidates, grounding)
        selected, score = self._ranker.select(candidates, grounding)
        if selected is None:
            report = PlanReport(
                goal_id=goal_id,
                grounding=grounding,
                decomposition=decomposition,
                outcomes=outcomes,
                comparison=comparison,
                policy=self._policy.evaluate_tasks(decomposition.tasks, goal=goal),
                refusal=(
                    "every candidate is infeasible against the grounded state: "
                    + "; ".join(item for candidate in candidates for item in candidate.missing[:2])
                ),
                limitations=tuple(
                    limitation for outcome in outcomes for limitation in outcome.limitations
                ),
            )
            self._announce("goal.plan_refused", {"goal_id": goal_id})
            return report

        self._selected[goal_id] = selected.candidate_id
        self._announce(
            "goal.plan_selected",
            {
                "goal_id": goal_id,
                "candidate_id": selected.candidate_id,
                "planner": selected.planner,
                "steps": len(selected.plan.steps),
                "feasible": selected.feasible,
            },
        )
        self.save(goal_id)
        estimate = self._estimator.estimate_plan(
            [
                task
                for task in decomposition.tasks
                if task.id in {s.id for s in selected.plan.steps}
            ],
            scope=goal.normalized,
        )
        # The seconds budget is compared against the plan's OWN estimate here and
        # enforced against the wall clock during the run. Reporting it at plan time
        # is what lets a caller see the mismatch before asking for a run, instead of
        # discovering it as a truncated one.
        budget_notes: list[str] = []
        if selected.estimated_seconds > goal.budget.max_seconds:
            budget_notes.append(
                f"the plan's own estimate is {selected.estimated_seconds:.3f}s and the "
                f"goal's budget is {goal.budget.max_seconds:.3f}s, so execution will be "
                "stopped by the budget if the estimate holds"
            )
        return PlanReport(
            goal_id=goal_id,
            grounding=grounding,
            decomposition=decomposition,
            outcomes=outcomes,
            comparison=comparison,
            selected=selected,
            policy=self._policy.evaluate_plan(selected.plan, goal=goal),
            estimates=estimate.to_dict(),
            limitations=(
                tuple(limitation for outcome in outcomes for limitation in outcome.limitations)
                + tuple(budget_notes)
            ),
        )

    def candidates(self, goal_id: str) -> tuple[CandidatePlan, ...]:
        return tuple(self._candidates.get(str(goal_id), ()))

    def selected(self, goal_id: str) -> CandidatePlan | None:
        """The candidate a plan() call named, if this goal has one."""
        candidate_id = self._selected.get(str(goal_id))
        if not candidate_id:
            return None
        for candidate in self._candidates.get(str(goal_id), ()):
            if candidate.candidate_id == candidate_id:
                return candidate
        return None

    # -- execution -----------------------------------------------------------------

    async def execute(
        self,
        goal_id: str,
        *,
        approved: Sequence[str] = (),
        keys: Sequence[str] = (),
    ) -> GoalRun:
        """Run the selected plan on the application's executor, gated per step.

        Nothing runs when the policy refuses the whole plan, when no candidate
        was selected, or when no executor is wired. Each step is re-checked
        against fresh state immediately before it runs; a step whose
        preconditions no longer hold is DENIED by the executor rather than
        attempted.
        """
        goal = self._require(goal_id)
        started = _now()
        candidate = self.selected(goal_id)
        # A goal the caller STOPPED, or one whose own limit has already passed, is not
        # started. These are the caller's stated stopping conditions and their stated
        # requirement: "the caller said stop" is not a preference this layer may
        # quietly outvote, and a limit nobody enforces is a promise the code does not
        # keep. The refusal is a Result, not an exception: nothing ran.
        stopped_at_start = self._start_refusal(goal)
        if stopped_at_start is not None:
            status, reason = stopped_at_start
            return self._record(
                GoalRun(
                    goal_id=goal_id,
                    plan_id=candidate.plan.id if candidate is not None else "",
                    status=status,
                    reason=reason,
                    started_at=started,
                    finished_at=_now(),
                )
            )
        if candidate is None:
            return self._record(
                GoalRun(
                    goal_id=goal_id,
                    plan_id="",
                    status="blocked",
                    reason="no candidate plan has been selected for this goal; plan it first",
                    started_at=started,
                    finished_at=_now(),
                )
            )
        if self._executor is None:
            return self._record(
                GoalRun(
                    goal_id=goal_id,
                    plan_id=candidate.plan.id,
                    status="blocked",
                    reason=(
                        "no executor is wired, so a plan cannot become work here; "
                        "the candidate is documented and nothing was attempted"
                    ),
                    started_at=started,
                    finished_at=_now(),
                )
            )

        if not candidate.plan.steps:
            # A plan with NO steps is the skip path, not a refusal: every task's
            # effect was already grounded true, so there is nothing to run. Calling
            # this a policy denial would read as "a step was refused" when no step
            # existed. The completion check decides instead — a goal whose
            # conditions already hold is complete and needed no work; anything
            # else is blocked with the reason naming what is still missing.
            observed = self._observe(goal, keys=keys)
            completion = self._monitor(goal_id).completion(goal, observed)
            notes = "; ".join(candidate.notes) or (
                "every task's effect is already grounded true, so nothing needs to run"
            )
            status = "completed" if completion.verified else "blocked"
            reason = (
                f"the selected plan needed no steps: {notes}"
                if completion.verified
                else (
                    "the selected plan needed no steps and the goal is not verified: "
                    f"{notes}; {completion.reason}"
                )
            )
            run = self._record(
                GoalRun(
                    goal_id=goal_id,
                    plan_id=candidate.plan.id,
                    status=status,
                    reason=reason,
                    completion=completion,
                    started_at=started,
                    finished_at=_now(),
                )
            )
            self._goals[goal_id] = goal.with_(
                status=(GoalStatus.COMPLETED if completion.verified else _goal_status(status))
            )
            self.save(goal_id)
            return run

        report = self._policy.evaluate_plan(candidate.plan, goal=goal, approved=approved)
        if not report.runnable:
            return self._record(
                GoalRun(
                    goal_id=goal_id,
                    plan_id=candidate.plan.id,
                    status="denied",
                    reason=(
                        "the action policy refused every step: "
                        + "; ".join(
                            f"{item.step_id}: {item.reason}" for item in report.verdicts[:3]
                        )
                    ),
                    started_at=started,
                    finished_at=_now(),
                )
            )

        revalidations: list[RevalidationReport] = []
        #: Why a step was stopped, in the order the stops happened. A time limit and
        #: a contradicted precondition are different causes of "it did not go", so
        #: the run reports which one came first rather than one label for both.
        stops: list[Mapping[str, Any]] = []
        limits = self._run_limits(goal, started)
        executor = self._gated_executor(candidate.plan, goal_id, revalidations, limits, stops)
        self._goals[goal_id] = goal.with_(status=GoalStatus.RUNNING)
        workflow = await executor.execute(candidate.plan)
        observed = self._observe(goal, keys=keys)
        completion = self._monitor(goal_id).completion(goal, observed)
        status = _status_for(workflow, completion, revalidations, stops)
        if not stops:
            # The run reached its own end — now ask the clock. A run that finished
            # PAST a stated limit is not reported as simply "completed": the verdict
            # stays attached (the completion check said what it said) and the label
            # names the breach, because "completed" would read as "inside its
            # limits", and it was not. Nothing earlier stopped it, so nothing else
            # is being overruled by saying so.
            overrun = self._limits_stop(limits)
            if overrun is not None:
                stops = [{"kind": overrun[0], "reason": overrun[1]}]
                status = "deadline_exceeded" if overrun[0] == "deadline" else "budget_exhausted"
        run = self._record(
            GoalRun(
                goal_id=goal_id,
                plan_id=candidate.plan.id,
                status=status,
                reason=_run_reason(workflow, completion, status, revalidations, stops),
                workflow=workflow,
                revalidations=tuple(revalidations),
                completion=completion,
                started_at=started,
                finished_at=_now(),
                unverified_steps=tuple(workflow.unverified_steps),
            )
        )
        # A verified completion keeps the goal COMPLETED even when the RUN overran a
        # limit: the goal's own conditions held, and the breach is recorded on the
        # run that breached them rather than by demoting the goal's outcome.
        self._goals[goal_id] = goal.with_(
            status=(GoalStatus.COMPLETED if completion.verified else _goal_status(status))
        )
        self.save(goal_id)
        return run

    # -- reassessment ---------------------------------------------------------------

    def reassess(
        self, goal_id: str, *, observed: Mapping[str, Any] | None = None
    ) -> ReplanDecision:
        """Whether this goal should be replanned — under the goal's own ceiling."""
        goal = self._require(goal_id)
        run = self._runs.get(goal_id)
        decision = self._monitor(goal_id).reassess(
            workflow=run.workflow if run else None,
            goal=goal,
            failed_steps=(),
        )
        self._announce(
            "goal.reassessed",
            {
                "goal_id": goal_id,
                "should_replan": decision.should_replan,
                "replans_used": decision.replans_used,
                "exhausted": decision.exhausted,
            },
        )
        return decision

    def record_replan(self, goal_id: str) -> int:
        """Count one replan against the goal's ceiling."""
        return self._monitor(self._require(goal_id).goal_id).record_replan()

    # -- reading ---------------------------------------------------------------------

    def run(self, goal_id: str) -> GoalRun | None:
        return self._runs.get(str(goal_id))

    def view(self, goal_id: str) -> dict[str, Any]:
        """Everything this manager knows about one goal, for a caller or a UI."""
        goal = self._require(goal_id)
        candidate = self.selected(goal_id)
        run = self._runs.get(goal_id)
        return {
            "goal": goal.to_dict(),
            "decomposition": (
                self._decompositions[goal_id].to_dict() if goal_id in self._decompositions else None
            ),
            "candidates": [item.to_dict() for item in self.candidates(goal_id)],
            "selected": candidate.candidate_id if candidate else None,
            "plan": candidate.plan.to_dict() if candidate else None,
            "run": run.to_dict() if run else None,
            "monitor": {
                "replans_used": self._monitor(goal_id).replans_used,
                "max_replans": goal.budget.max_replans,
            },
            "wired": self.wired,
        }

    def status(self) -> dict[str, Any]:
        """A bounded, content-light summary of the whole planning surface."""
        runs = [run.to_dict() for run in self._runs.values()]
        return {
            "goals": len(self._goals),
            "planned": sum(1 for goal_id in self._goals if goal_id in self._selected),
            "completed": sum(1 for run in self._runs.values() if run.verified),
            "failed": sum(1 for run in self._runs.values() if run.status in {"failed", "denied"}),
            "unknown": sum(1 for run in self._runs.values() if run.status == "needs_observation"),
            "replans_used": sum(m.replans_used for m in self._monitors.values()),
            "ids": list(self._goals),
            "runs": runs[-5:],
            "wired": self.wired,
            "restored": dict(self._restore_report),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "goals": {key: value.to_dict() for key, value in self._goals.items()},
            "selected": dict(self._selected),
        }

    def from_dict(self, data: Mapping[str, Any]) -> None:
        """Adopt stored goals without touching the repository."""
        raw_goals = data.get("goals")
        if isinstance(raw_goals, Mapping):
            for goal_id, payload in raw_goals.items():
                try:
                    self._goals[str(goal_id)] = Goal.from_mapping(
                        payload if isinstance(payload, Mapping) else {}
                    )
                except Exception:  # noqa: BLE001
                    continue
        raw_selected = data.get("selected")
        if isinstance(raw_selected, Mapping):
            for goal_id, candidate_id in raw_selected.items():
                self._selected[str(goal_id)] = str(candidate_id)

    # -- internals ---------------------------------------------------------------------

    def _require(self, goal_id: str) -> Goal:
        goal = self._goals.get(str(goal_id))
        if goal is None:
            raise KeyError(f"no goal with id {goal_id!r}")
        return goal

    def _monitor(self, goal_id: str) -> PlanMonitor:
        monitor = self._monitors.get(goal_id)
        if monitor is None:
            monitor = PlanMonitor(
                grounding_service=self._grounding_service,
                estimator=self._estimator,
                observer=self._observer,
            )
            self._monitors[goal_id] = monitor
        return monitor

    def _ground(
        self,
        goal: Goal,
        *,
        keys: Sequence[str] = (),
        tasks: Plan | None = None,
        extra: TaskDecomposition | None = None,
    ) -> StateGrounding:
        """Ground the keys this plan's preconditions and conditions lean on."""
        wanted: list[str] = list(keys)
        wanted.extend(str(key) for key in goal.initial_state)
        for condition in goal.success_conditions:
            wanted.append(condition.key)
        for task in extra.tasks if extra is not None else ():
            wanted.extend(_keys_of(task.preconditions))
            # The EFFECT keys too. "Never include work whose result is already
            # known" can only be decided if the state was read for what the work
            # would produce, so the effect keys have to be grounded as well.
            wanted.extend(_keys_of(task.effects))
        if tasks is not None:
            for step in tasks.steps:
                wanted.extend(_keys_of(_preconditions_of(step)))
        return self._grounding_service.ground(
            tuple(dict.fromkeys(item for item in wanted if item)),
            asserted=goal.initial_state,
        )

    def _observe(self, goal: Goal, *, keys: Sequence[str] = ()) -> dict[str, Any]:
        """The state read AFTER execution, for the completion check only."""
        wanted = tuple(
            dict.fromkeys(
                [
                    *(str(key) for key in keys),
                    *(condition.key for condition in goal.success_conditions),
                    # The failure conditions too: "did the thing we said must not
                    # happen happen?" can only be answered from state that was read.
                    *(_condition_key(text) for text in goal.failure_conditions),
                ]
            )
        )
        grounding = self._grounding_service.ground(wanted, asserted=goal.initial_state)
        observed: dict[str, Any] = {}
        for fact in grounding.facts:
            if fact.usable:
                observed[fact.key] = fact.value
        return observed

    # -- the goal's own limits ----------------------------------------------------

    def _start_refusal(self, goal: Goal) -> tuple[str, str] | None:
        """Why this goal must not START, or None when it may.

        Three of the goal's own statements decide this, and each is checked rather
        than left to whoever reads the record: a cancelled goal, a goal whose
        deadline has already passed, and a goal that requires a permission nothing
        in this build can grant. Each refusal is a status plus a reason naming the
        statement it came from, because "it did not run" is not an answer.
        """
        if goal.status is GoalStatus.CANCELLED:
            return (
                "cancelled",
                "this goal was cancelled, so no step was attempted and no plan was "
                "produced: cancelling is a stopping condition, not a preference",
            )
        remaining = _seconds_until(goal)
        if remaining is not None and remaining <= 0:
            return (
                "deadline_exceeded",
                f"the goal's deadline passed {abs(remaining):.3f}s ago, so nothing was "
                "started: a plan that cannot begin inside the limit is not a plan",
            )
        unmet = self._unmet_permissions(goal)
        if unmet:
            return (
                "denied",
                "this goal requires "
                + ", ".join(str(item) for item in goal.required_permissions)
                + ", and "
                + unmet
                + "; nothing was started",
            )
        return None

    def _unmet_permissions(self, goal: Goal) -> str:
        """Why the goal's required permissions cannot be checked, or "".

        A stated requirement is either checkable or it is a refusal — never
        silently ignored. With no layer wired, nothing can check it, and "no gate"
        is not permission (the same stance the action policy takes for a
        consequential step). With a layer that declares no scopes at all, there is
        no claim to contradict: the per-step policy still gates every step, so the
        requirement is reported as unchecked by staying silent here rather than by
        refusing work the layer never had an opinion about.
        """
        stated = {str(item) for item in goal.required_permissions if str(item).strip()}
        if not stated:
            return ""
        layer = getattr(self._policy, "_permissions", None)
        if layer is None:
            return "no permission layer is wired, so the requirement cannot be checked"
        declared: set[str] = set()
        try:
            for name in layer.declared():
                declaration = layer.declared_for(str(name))
                for scope in getattr(declaration, "permissions", ()) or ():
                    declared.add(str(getattr(scope, "value", scope)))
        except Exception:  # noqa: BLE001 - a layer that cannot answer is unknown
            return ""
        if not declared:
            return ""
        missing = sorted(stated - declared)
        if not missing:
            return ""
        return (
            "the permission layer does not declare "
            + ", ".join(missing)
            + ", so the requirement cannot be granted"
        )

    @staticmethod
    def _run_limits(goal: Goal, started: str) -> _RunLimits:
        """The two limits a running goal must respect, as absolute values."""
        return _RunLimits(
            deadline_at=_deadline_at(goal),
            max_seconds=float(goal.budget.max_seconds),
            started_at=_parse_stamp(started),
        )

    @staticmethod
    def _limits_stop(limits: _RunLimits) -> tuple[str, str] | None:
        """Which limit, if either, has already passed — as a bare FACT.

        The kind (``deadline`` / ``budget``) and a sentence stating the limit and
        what it measured. No claim about steps is made here: this is asked both
        before a step and after the run, so the caller is the one that knows whether
        anything was skipped.
        """
        now = datetime.now(UTC)
        if limits.deadline_at is not None and now >= limits.deadline_at:
            over = (now - limits.deadline_at).total_seconds()
            return ("deadline", f"the goal's deadline passed {over:.3f}s ago")
        if limits.started_at is not None:
            spent = (now - limits.started_at).total_seconds()
            if spent > limits.max_seconds:
                return (
                    "budget",
                    f"this run has spent {spent:.3f}s of the goal's "
                    f"{limits.max_seconds:.3f}s budget",
                )
        return None

    def _gated_executor(
        self,
        plan: Plan,
        goal_id: str,
        revalidations: list[RevalidationReport],
        limits: _RunLimits,
        stops: list[Mapping[str, Any]],
    ) -> WorkflowExecutor:
        """The application's executor, with a fresh-state and time gate in front of it.

        The gate is NOT a second executor: it checks the step against the goal's own
        limits and re-checks the step's own preconditions, then calls the same step
        runner. Refusing raises ``PermissionError``, which the executor already
        reads as a refusal — no retry, no rephrase, and the run reports ``DENIED``
        rather than a failure. Every stop is recorded in order, so the run can say
        WHICH limit or precondition ended it.
        """
        base = self._executor
        if base is None:  # pragma: no cover - guarded by the caller
            raise RuntimeError("no executor is wired")
        plan_keys: dict[str, set[str]] = {
            step.id: set(_keys_of(_preconditions_of(step))) for step in plan.steps
        }
        #: What each step DECLARED it would leave behind. Declared effects are
        #: what made the ordering checkable at plan time, so the gate has to honour
        #: them too: reading only the step's raw output keys blocked every dependent
        #: step forever, because a fact a completed step produced by DESIGN was not
        #: in its returned mapping.
        plan_effects: dict[str, set[str]] = {
            step.id: set(_keys_of(_declared_effects(step))) for step in plan.steps
        }
        handler: StepHandler = base.step_handler

        async def gated(step: Any, context: Any) -> Any:
            """Re-check this step's preconditions, then call the SAME step runner.

            The set of facts an earlier step actually PRODUCED is read from the
            executor's own ``StepContext.outputs`` — the record of steps that
            finished — rather than from state kept in this closure. That is both
            simpler and more honest: a fact is "about to exist" only when a step
            that already ran reported it, and a step that was denied or failed
            reports nothing. A finished step's DECLARED effects count as produced
            too, because that is the contract the plan was ordered by.
            """
            step_id = str(getattr(step, "id", "") or "")
            finished: set[str] = set()
            for done_id, output in dict(context.outputs or {}).items():
                finished.update(str(key) for key in dict(output))
                finished.update(plan_effects.get(str(done_id), set()))
            needed = {key for key in plan_keys.get(step_id, set()) if key not in finished}
            # The goal's own limits come FIRST: a run past its deadline or its budget
            # must not start another step whatever the state says, and reporting a
            # state refusal for a step that was out of time would blame the state for
            # a decision time made.
            expired = self._limits_stop(limits)
            if expired is not None:
                over = expired[1] + ", so no further step was started"
                stops.append({"kind": expired[0], "reason": over, "step_id": step_id})
                raise PermissionError(over)
            report = self._monitor(goal_id).revalidate(
                step,
                self._grounding_service.ground(tuple(sorted(needed))),
                only=sorted(needed),
            )
            revalidations.append(report)
            if not report.proceed:
                # The stop carries the gate's OWN verdict rather than one "state"
                # label for both: a precondition the state CONTRADICTED (``refuse``)
                # and one it cannot settle (``observe``) are different findings and
                # want different labels on the run — folding them into one kind
                # reported every unconfirmable step as a state refusal.
                stops.append(
                    {
                        "kind": "state" if report.refused else "needs_observation",
                        "reason": report.reason,
                        "step_id": report.step_id,
                    }
                )
                self._announce(
                    "goal.step_blocked",
                    {
                        "goal_id": goal_id,
                        "step_id": report.step_id,
                        "verdict": report.verdict,
                    },
                )
                # A refusal is not a failure: the executor already reads this as
                # DENIED and does not retry it.
                raise PermissionError(report.reason)
            output = handler(step, context)
            if isawaitable(output):
                output = await output
            return output

        return WorkflowExecutor(
            step_handler=gated,
            verifier=base.verifier,
            retry_policy=base.retry_policy,
            verification_policy=base.verification_policy,
            advisor=base.advisor,
            confirmation=base.confirmation,
            observer=base.observer,
            announcer=base.announcer,
            max_wave_size=base.max_wave_size,
        )

    def _record(self, run: GoalRun) -> GoalRun:
        self._runs[run.goal_id] = run
        self._announce(
            "goal.executed",
            {"goal_id": run.goal_id, "status": run.status, "verified": run.verified},
        )
        return run

    def _announce(self, kind: str, payload: Mapping[str, Any]) -> None:
        if self._observer is None:
            return
        try:
            self._observer(kind, dict(payload))
        except Exception:  # noqa: BLE001 - an announcement is not the work
            return


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _keys_of(preconditions: Sequence[str]) -> tuple[str, ...]:
    out: list[str] = []
    for item in preconditions:
        key, _, _ = str(item).partition("=")
        if key.strip():
            out.append(key.strip())
    return tuple(out)


def _declared_effects(step: Any) -> tuple[str, ...]:
    """The ``key=value`` effects a step declares it leaves behind.

    The plan builder keeps a task's effects in the step's parameters (they are
    what the ordering search reasoned about), so this is where the run-time gate
    reads the same statement back.
    """
    parameters = getattr(step, "parameters", {}) or {}
    raw = parameters.get("effects")
    if isinstance(raw, str):
        return (raw,) if raw.strip() else ()
    if isinstance(raw, Sequence):
        return tuple(str(item) for item in raw if str(item).strip())
    return ()


def _preconditions_of(step: Any) -> tuple[str, ...]:
    direct = getattr(step, "preconditions", None)
    if isinstance(direct, Sequence) and not isinstance(direct, str):
        return tuple(str(item) for item in direct)
    raw = (getattr(step, "parameters", {}) or {}).get("preconditions")
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, Sequence):
        return tuple(str(item) for item in raw)
    return ()


def _status_for(
    workflow: WorkflowResult,
    completion: CompletionVerdict,
    revalidations: Sequence[RevalidationReport],
    stops: Sequence[Mapping[str, Any]] = (),
) -> str:
    """The honest label for one run: verified, out of time, blocked or failed.

    The gate's own verdict outranks the completion check when they disagree:
    a step the state REFUSED never ran, so "the conditions cannot be observed"
    is a consequence of that refusal rather than the headline.
    """
    # The FIRST stop decides the label, because it is the one that ended the run:
    # a goal past its deadline reads ``deadline_exceeded``, one past its seconds
    # budget reads ``budget_exhausted``, and a step the state actively contradicted
    # reads ``blocked``. A caller that cannot tell those apart cannot tell "we ran
    # out of time" from "the world moved", which want different next steps.
    if stops:
        kind = str(stops[0].get("kind", ""))
        if kind == "deadline":
            return "deadline_exceeded"
        if kind == "budget":
            return "budget_exhausted"
        if kind == "needs_observation":
            return "needs_observation"
        return "blocked"
    # The gate leads whenever it stopped a step. A step the state REFUSED, or
    # could not confirm, never ran — so a success condition that holds afterwards
    # describes the goal ALREADY holding rather than the plan having achieved it.
    # Reporting "completed" there would hide the refusal, which is precisely the
    # difference between "the conditions hold" and "the plan did it".
    if any(item.refused for item in revalidations):
        return "blocked"
    if any(item.needs_observation for item in revalidations):
        return "needs_observation"
    if completion.verified and workflow.executed and not workflow.unverified_steps:
        return "completed"
    if completion.cannot_tell:
        return "needs_observation"
    if workflow.status.value == "denied":
        return "denied"
    if not workflow.executed:
        return "blocked"
    if completion.status == "failed" or workflow.errors:
        return "failed"
    return "partial"


def _run_reason(
    workflow: WorkflowResult,
    completion: CompletionVerdict,
    status: str,
    revalidations: Sequence[RevalidationReport] = (),
    stops: Sequence[Mapping[str, Any]] = (),
) -> str:
    if status == "completed":
        return completion.reason or "every required success condition was verified"
    # Whoever stopped the run leads: the goal's own limit when time or budget ran
    # out, otherwise the gate's state verdict. A success condition that holds
    # afterwards then describes the goal ALREADY holding rather than the plan
    # having achieved it, so it is reported as context rather than as the headline.
    parts: list[str] = []
    if stops:
        first = str(stops[0].get("reason", ""))
        if first:
            parts.append(first)
    elif revalidations:
        gate = next((item for item in revalidations if not item.proceed), None)
        if gate is not None:
            parts.append(gate.reason)
    if completion.reason and completion.reason not in parts:
        parts.append(completion.reason)
    if workflow.summary:
        parts.append(workflow.summary)
    if workflow.unverified_steps:
        parts.append(
            f"{len(workflow.unverified_steps)} step(s) ran without a check: "
            + ", ".join(workflow.unverified_steps[:3])
        )
    return "; ".join(parts) or "the run ended without a verified success condition"


def _goal_status(status: str) -> GoalStatus:
    """The goal's own life-state after a run with this status.

    ``cancelled`` maps to itself: a run that refused to start because the caller
    cancelled must not move the goal out of CANCELLED. The two time limits map to
    BLOCKED like every other "stopped for something a retry cannot fix" — a
    deadline that has passed is not a failure of the work, and it is not a state a
    plain retry clears either.
    """
    return {
        "completed": GoalStatus.COMPLETED,
        "failed": GoalStatus.FAILED,
        "denied": GoalStatus.BLOCKED,
        "blocked": GoalStatus.BLOCKED,
        "needs_observation": GoalStatus.BLOCKED,
        "cancelled": GoalStatus.CANCELLED,
        "deadline_exceeded": GoalStatus.BLOCKED,
        "budget_exhausted": GoalStatus.BLOCKED,
    }.get(status, GoalStatus.RUNNING)


def _decomposition_from(payload: Mapping[str, Any]) -> TaskDecomposition:
    from novacontrol.planning.goals import IssueKind, TaskIssue

    raw_tasks = payload.get("tasks")
    tasks = (
        tuple(Task.from_mapping(item) for item in raw_tasks if isinstance(item, Mapping))
        if isinstance(raw_tasks, list)
        else ()
    )
    raw_issues = payload.get("issues")
    issues: list[TaskIssue] = []
    if isinstance(raw_issues, list):
        for item in raw_issues:
            if not isinstance(item, Mapping):
                continue
            try:
                kind = IssueKind(str(item.get("kind", "") or ""))
            except ValueError:
                continue
            issues.append(
                TaskIssue(
                    kind=kind,
                    detail=str(item.get("detail", "") or ""),
                    task_ids=tuple(str(x) for x in item.get("task_ids", ()) or ()),
                )
            )
    return TaskDecomposition(
        goal_id=str(payload.get("goal_id", "") or ""),
        tasks=tasks,
        issues=tuple(issues),
        source=str(payload.get("source", "caller_supplied") or "caller_supplied"),
    )
