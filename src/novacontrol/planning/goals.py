"""Goals and their decomposition — what a plan is FOR, before any plan exists.

A ``Goal`` is the user's request plus everything that makes it checkable: the
conditions that say when it is done, the constraints it runs under, the budget
it may spend, and where it came from. A ``Task`` is one decomposed piece of it,
carrying the inputs, expected outputs, preconditions, dependencies, required
capability, estimated cost and verification procedure the specification asks
for. Both are data: nothing here plans, predicts or executes.

Two rules shape everything in this module.

**Uncertainty is preserved, never resolved by invention.** A goal arrives with
the fields the caller actually supplied. Missing success conditions are
reported as a defect of the goal (``unmeasurable``) rather than filled in with
a guess, and a decomposition whose issues are unresolved REFUSES to become a
plan — a caller that asked for something unverifiable gets that fact back
instead of a plan that claims to verify it.

**Validation is a report, not an exception.** ``decompose`` returns every
issue it found — cycles, unknown dependencies, unproducible preconditions,
impossible constraints, unmeasurable completion — with the task ids involved,
because "why can this goal not be planned?" is a question a caller must be
able to answer, and a raised ``ValueError`` answers only "it cannot".

The contracts are shaped to become :class:`~novacontrol.planning.models.Plan`
steps one-to-one (``PlanStep`` is the execution vocabulary), so the planning
layer adds goals and tasks without adding a second step model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from novacontrol.planning.models import (
    PlanStep,
    StepEffect,
    VerificationMethod,
    VerificationSpec,
    _plain,
)

__all__ = [
    "Goal",
    "GoalBudget",
    "GoalStatus",
    "IssueKind",
    "SuccessCondition",
    "Task",
    "TaskDecomposition",
    "TaskIssue",
    "coerce_value",
    "decompose",
]

#: A goal id is a label, so it must be safe to read in a URL and a store key.
_ID_PREFIX = "goal"


def coerce_value(text: str) -> Any:
    """A ``key=value`` string as the value an observation would actually carry.

    Preconditions, effects and success conditions are written as TEXT
    (``tree.clean=True``), while a grounded fact is whatever JSON value the world
    model stored (``True``). This is the ONE place that turns the text side into
    the typed side, so a planning comparison cannot invent its own reading: the
    estimation layer uses it for a declared effect, and grounding uses it when a
    precondition is compared against a real observation.

    Only booleans and numbers are translated; everything else stays the string it
    was written as.
    """
    lowered = str(text).strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"none", "null"}:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        pass
    try:
        return float(text)
    except (TypeError, ValueError):
        return text


def _verification_of(raw: Any) -> VerificationSpec:
    """Read a task's check from a mapping, a bare name, or nothing at all.

    A task that arrives as DATA (an API body, a stored record) cannot build a
    ``VerificationSpec`` object, so this is the door that lets it still state how
    it will be checked: ``{"method": "callable", "target": "metric_read"}``, or
    the bare name ``"metric_read"`` for the common case of a registered check.
    Anything unreadable stays ``NONE`` — an unspecified check is reported as
    unverified by the executor, which is the honest reading and much better than
    a check invented from a guess.
    """
    if isinstance(raw, VerificationSpec):
        return raw
    if isinstance(raw, str):
        name = raw.strip()
        if not name:
            return VerificationSpec()
        return VerificationSpec(
            method=VerificationMethod.CALLABLE,
            target=name,
            description=f"the named check {name!r}",
        )
    if isinstance(raw, Mapping):
        method_text = str(raw.get("method", "") or "").strip()
        try:
            method = VerificationMethod(method_text) if method_text else VerificationMethod.NONE
        except ValueError:
            method = VerificationMethod.NONE
        if method is VerificationMethod.NONE:
            return VerificationSpec()
        return VerificationSpec(
            method=method,
            target=str(raw.get("target", "") or ""),
            description=str(raw.get("description", "") or ""),
            expect=raw.get("expect"),
        )
    return VerificationSpec()


def new_goal_id() -> str:
    return f"{_ID_PREFIX}_{uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class GoalStatus(StrEnum):
    """Where a goal is in its life. Only COMPLETED claims done-ness."""

    OPEN = "open"
    #: Tasks were produced; no plan has been selected yet.
    PLANNED = "planned"
    #: A plan is executing right now.
    RUNNING = "running"
    #: Execution stopped for something a retry cannot fix (missing capability,
    #: denied permission, state the plan depended on having moved).
    BLOCKED = "blocked"
    #: Every required success condition was VERIFIED. Nothing else is completion.
    COMPLETED = "completed"
    #: The work ran and the check disagreed, or a step failed unrecoverably.
    FAILED = "failed"
    #: The request cannot be planned as asked; the missing information is named.
    NEEDS_CLARIFICATION = "needs_clarification"
    CANCELLED = "cancelled"


class IssueKind(StrEnum):
    """Why a decomposition cannot (yet) become a plan."""

    UNKNOWN_DEPENDENCY = "unknown_dependency"
    CYCLE = "cycle"
    #: A precondition no task produces and the grounded state does not hold.
    MISSING_PREREQUISITE = "missing_prerequisite"
    DUPLICATE_TASK_ID = "duplicate_task_id"
    #: No success condition is measurable, so nothing could confirm completion.
    UNMEASURABLE = "unmeasurable"
    #: A constraint rules out every task that could contribute (e.g. "no
    #: writes" for a goal whose only path writes).
    IMPOSSIBLE_CONSTRAINT = "impossible_constraint"
    #: A task names a capability this build does not declare.
    UNKNOWN_CAPABILITY = "unknown_capability"


@dataclass(frozen=True, slots=True)
class TaskIssue:
    """One thing wrong with a decomposition, with the tasks it concerns."""

    kind: IssueKind
    detail: str
    task_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "detail": self.detail,
            "task_ids": list(self.task_ids),
        }


@dataclass(frozen=True, slots=True)
class SuccessCondition:
    """When the goal counts as done, stated so a check can answer it.

    ``key``/``expected`` are the value the check compares against, and
    ``method`` says HOW it will be checked. A condition with no ``expected``
    cannot be evaluated — that is what makes a goal ``unmeasurable`` — and no
    default is invented here: the caller knows what done means, this build does
    not.
    """

    key: str
    expected: Any = None
    method: VerificationMethod = VerificationMethod.STATE_OBSERVED
    description: str = ""
    #: A condition that may fail without failing the goal (a preference).
    required: bool = True

    @property
    def measurable(self) -> bool:
        return bool(self.key.strip()) and self.expected is not None

    def satisfied_by(self, observed: Mapping[str, Any]) -> bool | None:
        """Whether an observation settles this condition.

        ``None`` means "this observation cannot say" — never ``False``: an
        unanswerable question is not a failed condition.
        """
        if not self.measurable or self.key not in observed:
            return None
        return bool(observed[self.key] == self.expected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "expected": _plain(self.expected),
            "method": self.method.value,
            "description": self.description,
            "required": self.required,
            "measurable": self.measurable,
        }


@dataclass(frozen=True, slots=True)
class GoalBudget:
    """What one goal may spend. Every ceiling is checked before it is needed."""

    max_seconds: float = 60.0
    max_steps: int = 16
    max_replans: int = 2
    max_candidates: int = 4

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1.")
        if self.max_replans < 0:
            raise ValueError("max_replans must not be negative.")
        if self.max_candidates < 1:
            raise ValueError("max_candidates must be at least 1.")
        if self.max_seconds <= 0:
            raise ValueError("max_seconds must be positive.")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> GoalBudget:
        defaults = cls()

        def number(key: str, default: float) -> float:
            value = data.get(key, default)
            try:
                return float(value)
            except (TypeError, ValueError):
                return float(default)

        def count(key: str, default: int, *, minimum: int = 1) -> int:
            try:
                return max(minimum, int(data.get(key, default)))
            except (TypeError, ValueError):
                return default

        return cls(
            max_seconds=number("max_seconds", defaults.max_seconds),
            max_steps=count("max_steps", defaults.max_steps),
            max_replans=count("max_replans", defaults.max_replans, minimum=0),
            max_candidates=count("max_candidates", defaults.max_candidates),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_seconds": self.max_seconds,
            "max_steps": self.max_steps,
            "max_replans": self.max_replans,
            "max_candidates": self.max_candidates,
        }


@dataclass(frozen=True, slots=True)
class Task:
    """One decomposed piece of a goal, in the execution vocabulary.

    The fields mirror :class:`~novacontrol.planning.models.PlanStep` because a
    task BECOMES one; the additions (``estimated_seconds``, ``estimated_cost``,
    ``verification`` as words) are the goal-level estimates a ranker needs
    before execution starts.
    """

    id: str
    title: str
    description: str = ""
    action: str = ""
    tool: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    #: Facts this task needs to already hold, named by success-condition style
    #: keys ("network=up"). Checked against the grounded state before it runs.
    preconditions: tuple[str, ...] = ()
    #: What this task leaves behind, as ``key=value`` strings a later task's
    #: precondition can name. This is what makes dependency order checkable
    #: instead of merely declared.
    effects: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    #: Capability names this task needs. A name no build declares is reported
    #: by ``decompose`` rather than discovered at execution time.
    capabilities: tuple[str, ...] = ()
    estimated_seconds: float = 1.0
    estimated_cost: float = 1.0
    effect: StepEffect = StepEffect.READ_ONLY
    verification: VerificationSpec = field(default_factory=VerificationSpec)
    verification_procedure: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("A task needs an id: dependencies name it.")
        if not self.title.strip():
            raise ValueError("A task needs a title.")

    @property
    def step_id(self) -> str:
        return self.id

    def to_step(self) -> PlanStep:
        """The executable step this task describes — one vocabulary, no copy."""
        return PlanStep(
            id=self.id,
            title=self.title,
            description=self.description or self.title,
            action=self.action,
            tool=self.tool,
            parameters=dict(self.parameters),
            depends_on=self.depends_on,
            expected_result="; ".join(self.expected_outputs) or self.description,
            verification=self.verification,
            effect=self.effect,
            # Only a read-only step may declare itself parallel-safe; the model
            # refuses anything else, so the mapping cannot invent one.
            parallel_safe=self.effect is StepEffect.READ_ONLY,
        )

    def with_(self, **changes: Any) -> Task:
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "action": self.action,
            "tool": self.tool,
            "parameters": {key: _plain(value) for key, value in self.parameters.items()},
            "depends_on": list(self.depends_on),
            "preconditions": list(self.preconditions),
            "effects": list(self.effects),
            "expected_outputs": list(self.expected_outputs),
            "capabilities": list(self.capabilities),
            "estimated_seconds": self.estimated_seconds,
            "estimated_cost": self.estimated_cost,
            "effect": self.effect.value,
            "verification": self.verification.to_dict(),
            "verification_procedure": self.verification_procedure,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Task:
        """Read a task back tolerantly; a field of the wrong type is ignored."""

        def texts(key: str) -> tuple[str, ...]:
            raw = data.get(key)
            if isinstance(raw, str):
                return (raw,) if raw.strip() else ()
            if isinstance(raw, Sequence):
                return tuple(str(item) for item in raw if str(item).strip())
            return ()

        effect_text = str(data.get("effect", "") or "").strip()
        try:
            effect = StepEffect(effect_text) if effect_text else StepEffect.READ_ONLY
        except ValueError:
            effect = StepEffect.READ_ONLY
        verification = _verification_of(data.get("verification"))
        parameters = data.get("parameters")
        return cls(
            id=str(data.get("id", "") or ""),
            title=str(data.get("title", "") or ""),
            description=str(data.get("description", "") or ""),
            action=str(data.get("action", "") or ""),
            tool=str(data.get("tool", "") or ""),
            parameters=dict(parameters) if isinstance(parameters, Mapping) else {},
            depends_on=texts("depends_on"),
            preconditions=texts("preconditions"),
            effects=texts("effects"),
            expected_outputs=texts("expected_outputs"),
            capabilities=texts("capabilities"),
            estimated_seconds=_number(data.get("estimated_seconds"), 1.0),
            estimated_cost=_number(data.get("estimated_cost"), 1.0),
            effect=effect,
            verification=verification,
            verification_procedure=str(data.get("verification_procedure", "") or ""),
        )


@dataclass(frozen=True, slots=True)
class Goal:
    """A bounded request: what done means, what it may cost, and where it came from.

    ``normalized`` is the lower-cased, whitespace-collapsed request — the same
    goal stated twice is the same goal for planning purposes. ``provenance``
    records where the request came from ("chat", "api", "schedule"), which is
    what lets a later audit ask who wanted this.
    """

    # The request comes first so ``Goal("open the editor")`` reads the way a
    # caller says it; the id follows and generates itself when not supplied.
    request: str = ""
    normalized: str = ""
    goal_id: str = field(default_factory=new_goal_id)
    success_conditions: tuple[SuccessCondition, ...] = ()
    #: Facts the caller asserts about the start. They are ASSERTIONS until the
    #: grounding layer confirms them against the world model.
    initial_state: Mapping[str, Any] = field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    budget: GoalBudget = field(default_factory=GoalBudget)
    #: Permission names this goal's tasks may ask for (``filesystem:write``…).
    required_permissions: tuple[str, ...] = ()
    approval_conditions: tuple[str, ...] = ()
    deadline_seconds: float | None = None
    failure_conditions: tuple[str, ...] = ()
    cancellation_conditions: tuple[str, ...] = ()
    #: A goal that is part of a larger one names its parent here.
    parent_goal_id: str = ""
    provenance: str = ""
    correlation_id: str = ""
    status: GoalStatus = GoalStatus.OPEN
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.request.strip():
            raise ValueError("A goal needs the request that produced it.")
        if not self.normalized:
            object.__setattr__(self, "normalized", normalize_request(self.request))

    @property
    def measurable(self) -> bool:
        """True when at least one REQUIRED condition can be evaluated."""
        return any(item.measurable for item in self.success_conditions if item.required)

    def satisfied_by(self, observed: Mapping[str, Any]) -> bool | None:
        """Whether an observation settles the goal.

        ``None`` when any required condition cannot be answered from it: an
        observation that says nothing about the goal is not a failure of the
        goal, and rounding either way is the lie this method exists to prevent.
        """
        verdict: bool = True
        for condition in self.success_conditions:
            if not condition.required:
                continue
            answer = condition.satisfied_by(observed)
            if answer is None:
                return None
            if not answer:
                verdict = False
        return verdict

    def with_(self, **changes: Any) -> Goal:
        changes.setdefault("updated_at", now_iso())
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "request": self.request,
            "normalized": self.normalized,
            "success_conditions": [item.to_dict() for item in self.success_conditions],
            "initial_state": {key: _plain(value) for key, value in self.initial_state.items()},
            "constraints": list(self.constraints),
            "exclusions": list(self.exclusions),
            "budget": self.budget.to_dict(),
            "required_permissions": list(self.required_permissions),
            "approval_conditions": list(self.approval_conditions),
            "deadline_seconds": self.deadline_seconds,
            "failure_conditions": list(self.failure_conditions),
            "cancellation_conditions": list(self.cancellation_conditions),
            "parent_goal_id": self.parent_goal_id,
            "provenance": self.provenance,
            "correlation_id": self.correlation_id,
            "status": self.status.value,
            "measurable": self.measurable,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Goal:
        """Read a goal back; unreadable rows are skipped rather than fatal."""
        conditions = tuple(
            SuccessCondition(
                key=str(item.get("key", "") or ""),
                expected=item.get("expected"),
                description=str(item.get("description", "") or ""),
                required=bool(item.get("required", True)),
            )
            for item in _rows(data, "success_conditions")
        )
        budget = data.get("budget")
        status_text = str(data.get("status", "") or "").strip()
        try:
            status = GoalStatus(status_text) if status_text else GoalStatus.OPEN
        except ValueError:
            status = GoalStatus.OPEN
        deadline = data.get("deadline_seconds")
        initial = data.get("initial_state")
        return cls(
            goal_id=str(data.get("goal_id", "") or "") or new_goal_id(),
            request=str(data.get("request", "") or ""),
            normalized=str(data.get("normalized", "") or ""),
            success_conditions=conditions,
            initial_state=dict(initial) if isinstance(initial, Mapping) else {},
            constraints=_texts(data, "constraints"),
            exclusions=_texts(data, "exclusions"),
            budget=GoalBudget.from_mapping(budget) if isinstance(budget, Mapping) else GoalBudget(),
            required_permissions=_texts(data, "required_permissions"),
            approval_conditions=_texts(data, "approval_conditions"),
            deadline_seconds=_optional_number(deadline),
            failure_conditions=_texts(data, "failure_conditions"),
            cancellation_conditions=_texts(data, "cancellation_conditions"),
            parent_goal_id=str(data.get("parent_goal_id", "") or ""),
            provenance=str(data.get("provenance", "") or ""),
            correlation_id=str(data.get("correlation_id", "") or ""),
            status=status,
            created_at=str(data.get("created_at", "") or "") or now_iso(),
            updated_at=str(data.get("updated_at", "") or "") or now_iso(),
        )


@dataclass(frozen=True, slots=True)
class TaskDecomposition:
    """A goal's tasks, and everything that is wrong with them.

    ``ok`` is True only when there are no issues at all, so a caller cannot
    build a plan from a decomposition without first reading its problems. The
    issues carry task ids, so "which step is the problem?" is answerable.
    """

    goal_id: str
    tasks: tuple[Task, ...] = ()
    issues: tuple[TaskIssue, ...] = ()
    #: How the tasks were produced ("goal_request", "caller_supplied", …).
    source: str = "caller_supplied"

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def measurable(self) -> bool:
        return not any(item.kind is IssueKind.UNMEASURABLE for item in self.issues)

    def issues_of(self, kind: IssueKind) -> tuple[TaskIssue, ...]:
        return tuple(item for item in self.issues if item.kind is kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "source": self.source,
            "ok": self.ok,
            "tasks": [task.to_dict() for task in self.tasks],
            "issues": [item.to_dict() for item in self.issues],
        }


def decompose(
    goal: Goal,
    tasks: Sequence[Task] = (),
    *,
    known_capabilities: Sequence[str] = (),
    held_facts: Mapping[str, Any] | None = None,
) -> TaskDecomposition:
    """Validate a goal's decomposition and report every issue it has.

    ``held_facts`` is what the grounded state already makes true (task
    preconditions named there are satisfied, not missing). ``known_capabilities``
    is what this build can actually do; an empty sequence means "not checked",
    which is honest — a build that has not said what it can cannot claim a
    capability is unknown.
    """
    issues: list[TaskIssue] = []
    by_id = {task.id: task for task in tasks}
    held = dict(held_facts or {})

    if not goal.measurable:
        issues.append(
            TaskIssue(
                kind=IssueKind.UNMEASURABLE,
                detail=(
                    "the goal states no measurable success condition, so completion "
                    "could never be verified; name what 'done' looks like"
                ),
            )
        )

    seen: list[str] = []
    for task in tasks:
        if task.id in seen:
            issues.append(
                TaskIssue(
                    kind=IssueKind.DUPLICATE_TASK_ID,
                    detail=f"task id {task.id!r} is used more than once",
                    task_ids=(task.id,),
                )
            )
        seen.append(task.id)
        for parent in task.depends_on:
            if parent not in by_id:
                issues.append(
                    TaskIssue(
                        kind=IssueKind.UNKNOWN_DEPENDENCY,
                        detail=f"task {task.id!r} depends on {parent!r}, which does not exist",
                        task_ids=(task.id, parent),
                    )
                )
        for precondition in task.preconditions:
            key, _, expected = precondition.partition("=")
            if not key.strip():
                continue
            if key in held:
                if expected and str(held[key]) != expected:
                    issues.append(
                        TaskIssue(
                            kind=IssueKind.MISSING_PREREQUISITE,
                            detail=(
                                f"task {task.id!r} needs {precondition!r} but the state "
                                f"holds {key}={held[key]!r}"
                            ),
                            task_ids=(task.id,),
                        )
                    )
                continue
            producers = [
                other.id for other in tasks if other.id != task.id and precondition in other.effects
            ]
            if not producers:
                issues.append(
                    TaskIssue(
                        kind=IssueKind.MISSING_PREREQUISITE,
                        detail=(
                            f"task {task.id!r} needs {precondition!r}, which no task "
                            "produces and the state does not hold"
                        ),
                        task_ids=(task.id,),
                    )
                )
        if known_capabilities:
            for capability in task.capabilities:
                if capability not in known_capabilities:
                    issues.append(
                        TaskIssue(
                            kind=IssueKind.UNKNOWN_CAPABILITY,
                            detail=(
                                f"task {task.id!r} needs capability {capability!r}, "
                                "which this build does not declare"
                            ),
                            task_ids=(task.id,),
                        )
                    )
        for exclusion in goal.exclusions:
            needle = exclusion.strip().lower()
            if not needle:
                continue
            if needle in task.effect.value.replace("_", " "):
                issues.append(
                    TaskIssue(
                        kind=IssueKind.IMPOSSIBLE_CONSTRAINT,
                        detail=(
                            f"goal excludes {exclusion!r} but task {task.id!r} is "
                            f"{task.effect.value}"
                        ),
                        task_ids=(task.id,),
                    )
                )

    cycle = _find_cycle(tasks)
    if cycle:
        issues.append(
            TaskIssue(
                kind=IssueKind.CYCLE,
                detail="dependency cycle: " + " -> ".join(cycle),
                task_ids=tuple(cycle),
            )
        )
    if tasks and len(tasks) > goal.budget.max_steps:
        issues.append(
            TaskIssue(
                kind=IssueKind.IMPOSSIBLE_CONSTRAINT,
                detail=(
                    f"the goal's budget allows {goal.budget.max_steps} step(s) but "
                    f"{len(tasks)} were decomposed"
                ),
            )
        )
    return TaskDecomposition(goal_id=goal.goal_id, tasks=tuple(tasks), issues=tuple(issues))


def _find_cycle(tasks: Sequence[Task]) -> list[str]:
    """One dependency cycle as an id path, or ``[]`` when the graph is acyclic."""
    graph = {
        task.id: tuple(parent for parent in task.depends_on if parent in {t.id for t in tasks})
        for task in tasks
    }
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str]:
        state[node] = 1
        stack.append(node)
        for parent in graph.get(node, ()):
            if state.get(parent, 0) == 1:
                return stack[stack.index(parent) :] + [parent]
            if state.get(parent, 0) == 0:
                found = visit(parent)
                if found:
                    return found
        state[node] = 2
        stack.pop()
        return []

    for task in tasks:
        if state.get(task.id, 0) == 0:
            found = visit(task.id)
            if found:
                return found
    return []


def normalize_request(text: str) -> str:
    """The same request, stated comparably: lower-cased, collapsed whitespace."""
    return " ".join(str(text).strip().lower().split())


def _rows(data: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    raw = data.get(key)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _texts(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    raw = data.get(key)
    if isinstance(raw, str):
        return (raw,) if raw.strip() else ()
    if isinstance(raw, Sequence):
        return tuple(str(item) for item in raw if str(item).strip())
    return ()


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _optional_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
