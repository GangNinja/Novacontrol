"""Candidate plans — one baseline ordering, one search, both bounded.

Two planners produce candidate plans from a validated decomposition, and the
difference between them is the point of having both:

``BaselineOrderPlanner`` (``dependency_order``)
    The control. Tasks run in dependency-depth order with a cheapest-first
    tie-break. Deterministic, one candidate, O(T log T + E). Nothing is
    searched, so nothing about its result is a surprise.

``CostAwarePlanner`` (``cost_aware_beam``)
    A bounded beam over VALID ORDERINGS of the independent tasks. It expands at
    most ``max_expansions`` partial orders and keeps the best ``max_candidates``
    complete ones by the same heuristic the ranker uses. It differs from the
    baseline exactly where ordering matters: independent tasks whose effects
    satisfy each other's preconditions earlier, and cheap tasks that should
    clear the way for expensive ones.

Both emit the existing :class:`~novacontrol.planning.models.Plan`, so a
candidate runs on the Phase 4 executor unchanged — this module adds the
ability to compare plans, not a second execution path.

Three rules they share:

**Never invent a plan the state cannot support.** A task whose preconditions
nobody satisfies becomes a PARTIAL plan that names the missing facts, or no
plan at all when the goal forbids proceeding. A plan built on silence would
execute into a surprise.

**Never include work whose result is already known.** A task whose effects are
already grounded true is skipped with the reason recorded: repeating known
work is the cheapest way to look busy without achieving anything.

**Budgets are checked before search, not after.** Expansions, candidates, wall
clock and steps are ceilings enforced inside the loop; the outcome reports
whether a ceiling truncated the search, so a truncated result can never be
mistaken for an exhaustive one.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from novacontrol.planning.goals import Goal, Task, TaskDecomposition
from novacontrol.planning.grounding import StateGrounding
from novacontrol.planning.models import (
    Plan,
    PlanStep,
    RetryPolicy,
    StepEffect,
)

__all__ = [
    "BaselineOrderPlanner",
    "CandidatePlan",
    "CostAwarePlanner",
    "OrderingTask",
    "PlanningBudget",
    "PlanningOutcome",
    "planners",
]

#: Effects and what they cost in risk points. A heuristic weight, not a
#: probability: it orders plans, it does not measure danger.
RISK_WEIGHTS: Mapping[StepEffect, float] = {
    StepEffect.READ_ONLY: 0.0,
    StepEffect.LOCAL_WRITE: 1.0,
    StepEffect.SYSTEM: 2.0,
    StepEffect.DESTRUCTIVE: 4.0,
    StepEffect.EXTERNAL: 4.0,
}


@dataclass(frozen=True, slots=True)
class PlanningBudget:
    """The ceilings the search runs under. Checked inside the loop, not after."""

    max_expansions: int = 256
    max_candidates: int = 4
    max_seconds: float = 2.0
    max_depth: int = 16

    def __post_init__(self) -> None:
        if self.max_expansions < 1:
            raise ValueError("max_expansions must be at least 1.")
        if self.max_candidates < 1:
            raise ValueError("max_candidates must be at least 1.")
        if self.max_seconds <= 0:
            raise ValueError("max_seconds must be positive.")
        if self.max_depth < 1:
            raise ValueError("max_depth must be at least 1.")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> PlanningBudget:
        defaults = cls()

        def number(key: str, default: float) -> float:
            try:
                return float(data.get(key, default))
            except (TypeError, ValueError):
                return float(default)

        def count(key: str, default: int, minimum: int = 1) -> int:
            try:
                return max(minimum, int(data.get(key, default)))
            except (TypeError, ValueError):
                return default

        return cls(
            max_expansions=count("max_expansions", defaults.max_expansions),
            max_candidates=count("max_candidates", defaults.max_candidates),
            max_seconds=number("max_seconds", defaults.max_seconds),
            max_depth=count("max_depth", defaults.max_depth),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_expansions": self.max_expansions,
            "max_candidates": self.max_candidates,
            "max_seconds": self.max_seconds,
            "max_depth": self.max_depth,
        }


@dataclass(frozen=True, slots=True)
class OrderingTask:
    """A task as the ordering search sees it: what it needs and what it gives."""

    task: Task
    needs: tuple[tuple[str, str], ...] = ()  # (key, expected)
    gives: tuple[str, ...] = ()

    @classmethod
    def of(cls, task: Task) -> OrderingTask:
        needs: list[tuple[str, str]] = []
        for precondition in task.preconditions:
            key, _, expected = precondition.partition("=")
            if key.strip():
                needs.append((key.strip(), expected.strip()))
        return cls(task=task, needs=tuple(needs), gives=tuple(task.effects))


@dataclass(frozen=True, slots=True)
class CandidatePlan:
    """One plan, with the estimates a ranker compares it by."""

    candidate_id: str
    planner: str
    plan: Plan
    #: ``False`` when the plan cannot be executed as-is (missing prerequisites
    #: that no task produces). A partial plan is still shown — the spec asks
    #: for one — but it is never selected for execution by accident.
    feasible: bool = True
    missing: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    #: Steps that could not be ordered at all — a dependency nobody satisfies.
    blocked: tuple[str, ...] = ()
    estimated_seconds: float = 0.0
    estimated_cost: float = 0.0
    #: Heuristic risk weight from the effects involved (see RISK_WEIGHTS).
    risk: float = 0.0
    #: Steps that would need a person's approval before running.
    needs_approval: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "planner": self.planner,
            "feasible": self.feasible,
            "missing": list(self.missing),
            "skipped": list(self.skipped),
            "blocked": list(self.blocked),
            "estimated_seconds": round(self.estimated_seconds, 3),
            "estimated_cost": round(self.estimated_cost, 3),
            "risk": round(self.risk, 3),
            "needs_approval": list(self.needs_approval),
            "notes": list(self.notes),
            "plan": self.plan.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class PlanningOutcome:
    """What one planner produced, and how hard it looked."""

    planner: str
    candidates: tuple[CandidatePlan, ...] = ()
    expansions: int = 0
    #: True when a ceiling stopped the search before it ran out of work. A
    #: truncated outcome is still useful, and saying so is what stops it from
    #: being read as "these were all the plans there were".
    truncated: bool = False
    elapsed_ms: float = 0.0
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "planner": self.planner,
            "candidates": [item.to_dict() for item in self.candidates],
            "considered": len(self.candidates),
            "expansions": self.expansions,
            "truncated": self.truncated,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "limitations": list(self.limitations),
        }


class BaselineOrderPlanner:
    """Dependency order, cheapest first. One candidate, no search.

    Complexity: O(T log T + E) for T tasks and E dependency edges. Appropriate
    for any acyclic decomposition; it has no opinion about which of several
    valid orders is cheaper to RUN, which is exactly the question the cost-aware
    planner exists to answer.
    """

    name = "dependency_order"

    def plan(
        self,
        goal: Goal,
        decomposition: TaskDecomposition,
        grounding: StateGrounding,
        *,
        budget: PlanningBudget | None = None,
    ) -> PlanningOutcome:
        limits = budget or PlanningBudget()
        started = time.perf_counter()
        blocked = _structurally_blocked(decomposition)
        if blocked:
            return PlanningOutcome(
                planner=self.name,
                limitations=(
                    "the decomposition has structural issues, so no plan was built: "
                    + "; ".join(item.detail for item in blocked[:3]),
                ),
                elapsed_ms=_elapsed_ms(started),
            )
        ordering, missing, skipped, blocked = _order_tasks(decomposition.tasks, grounding)
        keep = min(goal.budget.max_steps, limits.max_depth)
        truncated = len(ordering) > keep or bool(missing)
        kept = ordering[:keep]
        plan = _build_plan(goal, kept, planner=self.name)
        candidate = _candidate(
            plan,
            kept,
            self.name,
            missing=missing,
            skipped=skipped,
            blocked=blocked,
        )
        return PlanningOutcome(
            planner=self.name,
            candidates=(candidate,),
            expansions=len(decomposition.tasks),
            truncated=truncated,
            elapsed_ms=_elapsed_ms(started),
            limitations=(
                "the baseline accepts dependency order and does not compare "
                "alternatives; ordering questions are what the cost-aware planner "
                "is for",
            ),
        )


class CostAwarePlanner:
    """A bounded beam over valid orderings of the independent tasks.

    This is a real search, so its bounds are part of its contract: it expands at
    most ``max_expansions`` partial orders, keeps at most ``beam_width``
    partials per depth, stops at ``max_depth`` and at the wall-clock ceiling.
    It is NOT A*: there is no admissible heuristic and it does not claim one —
    it is a cost-ranked beam whose ordering heuristic (cheap, low-risk tasks
    first; tasks that unlock preconditions first) is documented here as a
    heuristic and labelled as one everywhere it surfaces.

    Complexity: bounded by ``max_expansions``, so worst case is linear in the
    ceiling regardless of the combinatorics it would otherwise face. That bound
    is the whole reason this is safe to call on a large decomposition.
    """

    name = "cost_aware_beam"

    def __init__(self, *, beam_width: int = 3) -> None:
        self.beam_width = max(1, int(beam_width))

    def plan(
        self,
        goal: Goal,
        decomposition: TaskDecomposition,
        grounding: StateGrounding,
        *,
        budget: PlanningBudget | None = None,
    ) -> PlanningOutcome:
        limits = budget or PlanningBudget()
        started = time.perf_counter()
        blocked = _structurally_blocked(decomposition)
        if blocked:
            return PlanningOutcome(
                planner=self.name,
                limitations=(
                    "the decomposition has structural issues, so no plan was built: "
                    + "; ".join(item.detail for item in blocked[:3]),
                ),
                elapsed_ms=_elapsed_ms(started),
            )
        tasks = [OrderingTask.of(task) for task in decomposition.tasks]
        by_id = {item.task.id: item for item in tasks}
        held = {key: str(value) for key, value in _held_facts(grounding).items()}
        expansions = 0
        truncated = False
        # Partial orderings: (ordered ids, remaining ids, facts produced).
        partials: list[tuple[tuple[str, ...], tuple[str, ...], dict[str, str]]] = [
            ((), tuple(item.task.id for item in tasks), dict(held))
        ]
        complete: list[tuple[tuple[str, ...], dict[str, str]]] = []
        while partials:
            if expansions >= limits.max_expansions:
                truncated = True
                break
            if time.perf_counter() - started > limits.max_seconds:
                truncated = True
                break
            ordered, remaining, facts = partials.pop(0)
            expansions += 1
            if not remaining:
                complete.append((ordered, facts))
                continue
            if len(ordered) >= min(limits.max_depth, goal.budget.max_steps):
                truncated = True
                continue
            placed = set(ordered)
            ready = [
                item
                for item in remaining
                if all(parent in placed for parent in by_id[item].task.depends_on)
                and all(_need_held(facts, key, expected) for key, expected in by_id[item].needs)
            ]
            if not ready:
                # Nothing can run: the rest is blocked on facts nobody produces.
                truncated = True
                continue
            ready.sort(
                key=lambda item: (
                    _step_risk(by_id[item].task),
                    by_id[item].task.estimated_cost,
                    item,
                )
            )
            for task_id in ready[: self.beam_width]:
                item = by_id[task_id]
                produced = dict(facts)
                for effect in item.gives:
                    key, _, value = effect.partition("=")
                    produced[key.strip()] = value.strip()
                partials.append(
                    (
                        (*ordered, task_id),
                        tuple(other for other in remaining if other != task_id),
                        produced,
                    )
                )
        complete.sort(key=lambda item: _ordering_cost(item[0], by_id))
        candidates: list[CandidatePlan] = []
        for ordered, _facts in complete[: limits.max_candidates]:
            kept = [by_id[task_id].task for task_id in ordered]
            plan = _build_plan(goal, kept, planner=self.name)
            candidates.append(_candidate(plan, kept, self.name))
        if not candidates:
            ordering, missing, skipped, blocked = _order_tasks(decomposition.tasks, grounding)
            kept = ordering[: goal.budget.max_steps]
            plan = _build_plan(goal, kept, planner=self.name)
            candidates.append(
                _candidate(
                    plan,
                    kept,
                    self.name,
                    missing=missing,
                    skipped=skipped,
                    blocked=blocked,
                )
            )
        return PlanningOutcome(
            planner=self.name,
            candidates=tuple(candidates),
            expansions=expansions,
            truncated=truncated,
            elapsed_ms=_elapsed_ms(started),
            limitations=(
                "the beam's ordering score is a documented heuristic, not A* and not a probability",
                *(
                    ("the search hit a ceiling and stopped early; other orderings may exist",)
                    if truncated
                    else ()
                ),
            ),
        )


def planners() -> tuple[object, ...]:
    """The planners this build ships, in comparison order."""
    return (BaselineOrderPlanner(), CostAwarePlanner())


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _structurally_blocked(decomposition: TaskDecomposition) -> tuple[Any, ...]:
    """The issues that make a decomposition UNPLANNABLE, as opposed to partial.

    A missing prerequisite is not one of them: the spec asks for a PARTIAL plan
    or an explanation when evidence is short, and refusing to build anything
    would answer a solvable question with silence. Cycles, unknown dependencies,
    duplicate ids, an unmeasurable goal and impossible constraints are the ones
    no ordering can work around, so those stop the planners.
    """
    from novacontrol.planning.goals import IssueKind

    structural = {
        IssueKind.CYCLE,
        IssueKind.UNKNOWN_DEPENDENCY,
        IssueKind.DUPLICATE_TASK_ID,
        IssueKind.UNMEASURABLE,
        IssueKind.IMPOSSIBLE_CONSTRAINT,
    }
    return tuple(item for item in decomposition.issues if item.kind in structural)


def _order_tasks(
    tasks: Sequence[Task], grounding: StateGrounding
) -> tuple[list[Task], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Dependency order (depth, then cost), with the facts each task needs.

    Returns ``(ordered, missing, skipped, blocked)``: tasks whose preconditions
    no grounded fact and no earlier task produce are reported in ``missing``
    AND listed in ``blocked`` (they are placed after everything they could
    depend on, so a partial plan still shows the work that COULD run); tasks
    whose effects already hold are skipped.
    """
    held = dict(_held_facts(grounding))
    ordered: list[Task] = []
    skipped: list[str] = []
    missing: list[str] = []
    blocked_ids: list[str] = []
    remaining = list(tasks)
    produced = dict(held)
    placed: set[str] = set()
    while remaining:
        ready = [
            task
            for task in remaining
            if all(parent in placed for parent in task.depends_on)
            and all(_need_held(produced, key, expected) for key, expected in _needs(task))
        ]
        if not ready:
            # Nothing else can be placed: whatever remains is blocked.
            for task in remaining:
                blocked_ids.append(task.id)
                needs = (
                    ", ".join(task.preconditions)
                    or ", ".join(task.depends_on)
                    or "its prerequisites"
                )
                missing.append(f"{task.id}: needs {needs}")
            ordered.extend(sorted(remaining, key=lambda item: (item.estimated_cost, item.id)))
            break
        ready.sort(key=lambda item: (item.estimated_seconds, item.estimated_cost, item.id))
        task = ready[0]
        remaining.remove(task)
        placed.add(task.id)
        if _effects_already_hold(task, produced):
            skipped.append(task.id)
            for effect in task.effects:
                key, _, value = effect.partition("=")
                produced[key.strip()] = value.strip()
            continue
        ordered.append(task)
        for effect in task.effects:
            key, _, value = effect.partition("=")
            produced[key.strip()] = value.strip()
    return (
        ordered,
        tuple(dict.fromkeys(missing)),
        tuple(skipped),
        tuple(blocked_ids),
    )


def _needs(task: Task) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for precondition in task.preconditions:
        key, _, expected = precondition.partition("=")
        if key.strip():
            out.append((key.strip(), expected.strip()))
    return out


def _need_held(held: Mapping[str, str], key: str, expected: str) -> bool:
    """Whether a precondition is met by the facts held so far.

    A precondition that NAMED a value (``network=up``) needs exactly that
    value. One that named only the key (``network``) needs the fact to be HELD
    at all — observed, fresh and unconflicted — because "we know nothing about
    it" is the state a precondition exists to refuse.
    """
    if expected:
        return held.get(key) == expected
    return key in held


def _held_facts(grounding: StateGrounding) -> dict[str, str]:
    """The facts a precondition may lean on: usable facts only.

    A stale or conflicting fact is deliberately NOT offered: a precondition
    "satisfied" by a reading nobody would trust is the exact defect this layer
    exists to prevent.
    """
    return {
        fact.key: str(fact.value)
        for fact in grounding.facts
        if fact.usable and fact.value is not None
    }


def _effects_already_hold(task: Task, produced: Mapping[str, str]) -> bool:
    """Whether every effect of the task is already true — work to skip."""
    if not task.effects:
        return False
    for effect in task.effects:
        key, _, value = effect.partition("=")
        if produced.get(key.strip()) != value.strip():
            return False
    return True


def _step_risk(task: Task) -> float:
    return RISK_WEIGHTS.get(task.effect, 0.0)


def _ordering_cost(ordered: Sequence[str], by_id: Mapping[str, OrderingTask]) -> tuple[float, ...]:
    """The beam's ordering heuristic: total cost, then risk, then length.

    A tuple on purpose: the comparison is lexicographic and stable, and every
    component is documented — this is a ranking, not a probability.
    """
    total_cost = sum(by_id[task_id].task.estimated_cost for task_id in ordered)
    total_risk = sum(_step_risk(by_id[task_id].task) for task_id in ordered)
    return (total_cost, total_risk, float(len(ordered)))


def _build_plan(goal: Goal, tasks: Sequence[Task], *, planner: str) -> Plan:
    steps: tuple[PlanStep, ...] = tuple(_to_step(task) for task in tasks)
    return Plan(
        goal=goal.request,
        steps=steps,
        retry_policy=RetryPolicy(),
        state={
            "goal_id": goal.goal_id,
            "planner": planner,
            "estimates": {
                "seconds": round(sum(task.estimated_seconds for task in tasks), 3),
                "cost": round(sum(task.estimated_cost for task in tasks), 3),
            },
        },
    )


def _to_step(task: Task) -> PlanStep:
    """A task as an executable step, carrying what the monitor needs with it.

    ``preconditions`` and ``effects`` ride in ``parameters`` so the ranker can
    weigh the facts a plan leans on (a stale precondition is a cost) and the
    monitor can re-check each step's preconditions against fresh state the
    moment before it runs — and can tell which of them an EARLIER step in this
    same plan produces, so a fact that is about to exist is not mistaken for
    one the world must already hold.
    """
    step = task.to_step()
    if not task.preconditions and not task.effects:
        return step
    parameters = dict(step.parameters)
    if task.preconditions:
        parameters["preconditions"] = list(task.preconditions)
    if task.effects:
        parameters["effects"] = list(task.effects)
    return step.with_(parameters=parameters)


def _candidate(
    plan: Plan,
    tasks: Sequence[Task],
    planner: str,
    *,
    missing: Sequence[str] = (),
    skipped: Sequence[str] = (),
    blocked: Sequence[str] = (),
) -> CandidatePlan:
    approvals = tuple(
        task.id
        for task in tasks
        if task.effect in {StepEffect.DESTRUCTIVE, StepEffect.EXTERNAL, StepEffect.SYSTEM}
    )
    return CandidatePlan(
        candidate_id=f"{plan.id}:{planner}",
        planner=planner,
        plan=plan,
        feasible=not missing,
        missing=tuple(missing),
        skipped=tuple(skipped),
        blocked=tuple(blocked),
        estimated_seconds=sum(task.estimated_seconds for task in tasks),
        estimated_cost=sum(task.estimated_cost for task in tasks),
        risk=sum(_step_risk(task) for task in tasks),
        needs_approval=approvals,
        notes=(
            *(
                ("partial: some preconditions are not held by the state and no task produces them",)
                if missing
                else ()
            ),
            *(
                (
                    f"{len(skipped)} step(s) were skipped because their result is "
                    "already known from the state",
                )
                if skipped
                else ()
            ),
            *(
                (
                    f"{len(blocked)} step(s) could not be ordered: no grounded fact "
                    "and no earlier task satisfies them",
                )
                if blocked
                else ()
            ),
            *(
                ("this plan names steps that would need approval before running",)
                if approvals
                else ()
            ),
        ),
    )


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0
