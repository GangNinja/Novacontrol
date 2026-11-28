"""The persistent-operations vocabulary (Phase 30).

Three ideas live here, and the boundaries between them are the phase:

* an :class:`Objective` is what the person wants to be true — it carries the
  intended outcome, the deadline, the autonomy the person granted and the
  completion criteria. It is NOT a task and it is never "done" because its
  tasks exist.
* an :class:`OperationTask` is one piece of work toward an objective. It knows
  its dependencies, the steps a person would recognise as the work, the steps
  that have actually been VERIFIED, and the checkpoint that lets an interrupted
  run resume without replaying side effects.
* a :class:`Notification` is a statement about real stored state. It is created
  from a monitoring signal, never from a timer pretending something happened.

The models are frozen values with explicit ``to_dict``/``from_dict``: the whole
phase's promise is continuity across restarts, so what is persisted has to be
the same shape that is read back, and a damaged record is rejected rather than
silently coerced into a plausible-looking empty one.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping
from uuid import uuid4

from novacontrol.automation.schedule import Schedule


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _dt(value: Any) -> datetime | None:
    """Read an ISO instant, keeping the value timezone-aware.

    A stored naive instant is treated as UTC rather than as local time: the
    alternative is a deadline that moves when the machine's timezone changes.
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class ObjectiveKind(StrEnum):
    """The kinds of ongoing work this build distinguishes.

    The kind is a label the planner and the dashboard use; it never grants
    permission. A ``business`` objective has exactly the authority its autonomy
    level and its tasks' permission scopes describe.
    """

    PERSONAL = "personal"
    ACADEMIC = "academic"
    PROFESSIONAL = "professional"
    RESEARCH = "research"
    DEVELOPMENT = "development"
    BUSINESS = "business"
    RECURRING = "recurring"


class ObjectiveStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class AutonomyLevel(StrEnum):
    """How far the person's standing authorization reaches.

    * ``OBSERVE`` reads and monitors only.
    * ``RECOMMEND`` produces suggestions and executes nothing.
    * ``EXECUTE`` performs work the permission layer already allows without a
      fresh confirmation.
    * ``APPROVAL`` does the same, and pauses any task marked sensitive until a
      person approves that specific task.

    A level is a ceiling, never a grant: ``EXECUTE`` does not make a destructive
    action allowed, because the permission layer is still consulted first.
    """

    OBSERVE = "observe"
    RECOMMEND = "recommend"
    EXECUTE = "execute"
    APPROVAL = "approval"

    @property
    def runs_work(self) -> bool:
        return self in (AutonomyLevel.EXECUTE, AutonomyLevel.APPROVAL)


class TaskKind(StrEnum):
    REQUEST = "request"
    RESEARCH = "research"
    DEVELOPMENT = "development"
    COMPUTER = "computer"
    REVIEW = "review"
    RECURRING = "recurring"


class TaskState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (
            TaskState.COMPLETED,
            TaskState.SKIPPED,
            TaskState.CANCELLED,
        )

    @property
    def unfinished(self) -> bool:
        return not self.terminal


class NotificationKind(StrEnum):
    DEADLINE_APPROACHING = "deadline_approaching"
    OVERDUE = "overdue"
    STALLED = "stalled"
    APPROVAL_REQUIRED = "approval_required"
    TASK_FAILED = "task_failed"
    TASK_BLOCKED = "task_blocked"
    OBJECTIVE_COMPLETED = "objective_completed"
    RECURRING_COMPLETED = "recurring_completed"
    MONITORED_CHANGE = "monitored_change"
    OBJECTIVE_RESUMED = "objective_resumed"


class SignalKind(StrEnum):
    """What a monitoring pass concluded about one unit of work.

    ``NO_CHANGE`` exists so "nothing to say" is a REPORTED answer rather than
    the absence of one: the dashboard can distinguish a quiet objective from a
    monitor that never ran.
    """

    NO_CHANGE = "no_change"
    DEADLINE_APPROACHING = "deadline_approaching"
    OVERDUE = "overdue"
    STALLED = "stalled"
    APPROVAL_REQUIRED = "approval_required"
    FAILED = "failed"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    ACTIONABLE = "actionable"


_SIGNAL_NOTIFICATION: Mapping[SignalKind, NotificationKind] = {
    SignalKind.DEADLINE_APPROACHING: NotificationKind.DEADLINE_APPROACHING,
    SignalKind.OVERDUE: NotificationKind.OVERDUE,
    SignalKind.STALLED: NotificationKind.STALLED,
    SignalKind.APPROVAL_REQUIRED: NotificationKind.APPROVAL_REQUIRED,
    SignalKind.FAILED: NotificationKind.TASK_FAILED,
    SignalKind.BLOCKED: NotificationKind.TASK_BLOCKED,
    SignalKind.COMPLETED: NotificationKind.OBJECTIVE_COMPLETED,
}


@dataclass(frozen=True, slots=True)
class Milestone:
    """One checkpoint a person can recognise in an objective's progress.

    A milestone is completed only when EVERY task it names is completed and
    verified — it is a view over real task state, not a checkbox beside it.
    """

    title: str
    task_ids: tuple[str, ...] = ()
    due_at: datetime | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    verified_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "task_ids": list(self.task_ids),
            "due_at": _iso(self.due_at),
            "verified_at": _iso(self.verified_at),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Milestone:
        return cls(
            title=str(payload["title"]),
            task_ids=tuple(str(item) for item in payload.get("task_ids", ())),
            due_at=_dt(payload.get("due_at")),
            id=str(payload["id"]),
            verified_at=_dt(payload.get("verified_at")),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(frozen=True, slots=True)
class OperationTask:
    """One piece of work, with the state that survives a restart."""

    title: str
    instruction: str
    objective_id: str
    kind: TaskKind = TaskKind.REQUEST
    state: TaskState = TaskState.PENDING
    priority: int = 50
    due_at: datetime | None = None
    scheduled_for: datetime | None = None
    depends_on: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    completed_steps: tuple[str, ...] = ()
    last_verified_state: str = ""
    checkpoint: Mapping[str, Any] = field(default_factory=dict)
    attempts: int = 0
    max_attempts: int = 2
    error: str = ""
    result: str = ""
    evidence: tuple[str, ...] = ()
    verified: bool = False
    approved: bool = False
    approved_by: str = ""
    approval_scope: str = ""
    sensitive: bool = False
    agent: str = ""
    recurrence: Schedule | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("An operation task needs a title.")
        if not self.instruction.strip():
            raise ValueError("An operation task needs the instruction it will run.")
        if not 0 <= self.priority <= 100:
            raise ValueError("Priority is a number from 0 to 100.")
        if self.attempts < 0:
            raise ValueError("Attempt counts cannot be negative.")
        if self.max_attempts < 1:
            raise ValueError("A task must be allowed at least one attempt.")
        if self.id in self.depends_on:
            raise ValueError("A task cannot depend on itself.")

    @property
    def pending_steps(self) -> tuple[str, ...]:
        done = set(self.completed_steps)
        return tuple(step for step in self.steps if step not in done)

    @property
    def resumable(self) -> bool:
        """Whether this task holds verified progress worth resuming."""
        return bool(self.completed_steps or self.last_verified_state)

    @property
    def scope(self) -> str:
        return self.approval_scope or "operations.task.run"

    def with_(self, **changes: Any) -> OperationTask:
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "objective_id": self.objective_id,
            "title": self.title,
            "instruction": self.instruction,
            "kind": self.kind.value,
            "state": self.state.value,
            "priority": self.priority,
            "due_at": _iso(self.due_at),
            "scheduled_for": _iso(self.scheduled_for),
            "depends_on": list(self.depends_on),
            "steps": list(self.steps),
            "completed_steps": list(self.completed_steps),
            "pending_steps": list(self.pending_steps),
            "last_verified_state": self.last_verified_state,
            "checkpoint": dict(self.checkpoint),
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "error": self.error,
            "result": self.result,
            "evidence": list(self.evidence),
            "verified": self.verified,
            "approved": self.approved,
            "approved_by": self.approved_by,
            "approval_scope": self.approval_scope,
            "sensitive": self.sensitive,
            "agent": self.agent,
            "recurrence": self.recurrence.to_dict() if self.recurrence else None,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> OperationTask:
        raw_recurrence = payload.get("recurrence")
        return cls(
            title=str(payload["title"]),
            instruction=str(payload["instruction"]),
            objective_id=str(payload["objective_id"]),
            kind=TaskKind(str(payload.get("kind", TaskKind.REQUEST.value))),
            state=TaskState(str(payload.get("state", TaskState.PENDING.value))),
            priority=int(payload.get("priority", 50)),
            due_at=_dt(payload.get("due_at")),
            scheduled_for=_dt(payload.get("scheduled_for")),
            depends_on=tuple(str(item) for item in payload.get("depends_on", ())),
            steps=tuple(str(item) for item in payload.get("steps", ())),
            completed_steps=tuple(
                str(item) for item in payload.get("completed_steps", ())
            ),
            last_verified_state=str(payload.get("last_verified_state", "")),
            checkpoint=dict(payload.get("checkpoint", {})),
            attempts=int(payload.get("attempts", 0)),
            max_attempts=int(payload.get("max_attempts", 2)),
            error=str(payload.get("error", "")),
            result=str(payload.get("result", "")),
            evidence=tuple(str(item) for item in payload.get("evidence", ())),
            verified=bool(payload.get("verified", False)),
            approved=bool(payload.get("approved", False)),
            approved_by=str(payload.get("approved_by", "")),
            approval_scope=str(payload.get("approval_scope", "")),
            sensitive=bool(payload.get("sensitive", False)),
            agent=str(payload.get("agent", "")),
            recurrence=(
                Schedule.from_dict(dict(raw_recurrence))
                if isinstance(raw_recurrence, Mapping)
                else None
            ),
            created_at=_dt(payload.get("created_at")) or utcnow(),
            updated_at=_dt(payload.get("updated_at")) or utcnow(),
            started_at=_dt(payload.get("started_at")),
            completed_at=_dt(payload.get("completed_at")),
            metadata=dict(payload.get("metadata", {})),
            id=str(payload["id"]),
        )


@dataclass(frozen=True, slots=True)
class Objective:
    """A long-running outcome, its milestones and its work."""

    title: str
    outcome: str
    kind: ObjectiveKind = ObjectiveKind.PERSONAL
    status: ObjectiveStatus = ObjectiveStatus.ACTIVE
    autonomy: AutonomyLevel = AutonomyLevel.APPROVAL
    priority: int = 50
    deadline: datetime | None = None
    owner: str = "operator"
    completion_criteria: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    milestones: tuple[Milestone, ...] = ()
    tasks: tuple[OperationTask, ...] = ()
    evidence: tuple[str, ...] = ()
    paused_reason: str = ""
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    completed_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("An objective needs a title.")
        if not self.outcome.strip():
            raise ValueError(
                "An objective needs the outcome it is trying to achieve; a title "
                "alone does not say what done looks like."
            )
        if not 0 <= self.priority <= 100:
            raise ValueError("Priority is a number from 0 to 100.")
        if self.deadline is not None and self.deadline.tzinfo is None:
            raise ValueError("A deadline must be timezone-aware.")
        known = {task.id for task in self.tasks}
        for task in self.tasks:
            missing = [dep for dep in task.depends_on if dep not in known]
            if missing:
                raise ValueError(
                    f"Task {task.title!r} depends on unknown tasks: {missing}"
                )

    # -- progress (always derived, never stored separately) --------------------

    @property
    def completed_tasks(self) -> tuple[OperationTask, ...]:
        return tuple(t for t in self.tasks if t.state is TaskState.COMPLETED)

    @property
    def progress(self) -> float:
        if not self.tasks:
            return 0.0
        return round(len(self.completed_tasks) / len(self.tasks), 4)

    @property
    def counts(self) -> dict[str, int]:
        counts = {state.value: 0 for state in TaskState}
        for task in self.tasks:
            counts[task.state.value] += 1
        return counts

    @property
    def next_due(self) -> datetime | None:
        candidates = [
            task.due_at or task.scheduled_for
            for task in self.tasks
            if task.state.unfinished
        ]
        present = [value for value in candidates if value is not None]
        return min(present) if present else None

    @property
    def closed(self) -> bool:
        return self.status in (
            ObjectiveStatus.COMPLETED,
            ObjectiveStatus.CANCELLED,
        )

    def task(self, task_id: str) -> OperationTask:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise KeyError(f"Task not found in objective {self.id}: {task_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "outcome": self.outcome,
            "kind": self.kind.value,
            "status": self.status.value,
            "autonomy": self.autonomy.value,
            "priority": self.priority,
            "deadline": _iso(self.deadline),
            "owner": self.owner,
            "completion_criteria": list(self.completion_criteria),
            "tags": list(self.tags),
            "milestones": [milestone.to_dict() for milestone in self.milestones],
            "tasks": [task.to_dict() for task in self.tasks],
            "evidence": list(self.evidence),
            "paused_reason": self.paused_reason,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "completed_at": _iso(self.completed_at),
            "metadata": dict(self.metadata),
            "progress": self.progress,
            "counts": self.counts,
            "next_due": _iso(self.next_due),
            "closed": self.closed,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Objective:
        return cls(
            title=str(payload["title"]),
            outcome=str(payload["outcome"]),
            kind=ObjectiveKind(str(payload.get("kind", ObjectiveKind.PERSONAL.value))),
            status=ObjectiveStatus(
                str(payload.get("status", ObjectiveStatus.ACTIVE.value))
            ),
            autonomy=AutonomyLevel(
                str(payload.get("autonomy", AutonomyLevel.APPROVAL.value))
            ),
            priority=int(payload.get("priority", 50)),
            deadline=_dt(payload.get("deadline")),
            owner=str(payload.get("owner", "operator")),
            completion_criteria=tuple(
                str(item) for item in payload.get("completion_criteria", ())
            ),
            tags=tuple(str(item) for item in payload.get("tags", ())),
            milestones=tuple(
                Milestone.from_dict(item)
                for item in payload.get("milestones", ())
                if isinstance(item, Mapping)
            ),
            tasks=tuple(
                OperationTask.from_dict(item)
                for item in payload.get("tasks", ())
                if isinstance(item, Mapping)
            ),
            evidence=tuple(str(item) for item in payload.get("evidence", ())),
            paused_reason=str(payload.get("paused_reason", "")),
            created_at=_dt(payload.get("created_at")) or utcnow(),
            updated_at=_dt(payload.get("updated_at")) or utcnow(),
            completed_at=_dt(payload.get("completed_at")),
            metadata=dict(payload.get("metadata", {})),
            id=str(payload["id"]),
        )


@dataclass(frozen=True, slots=True)
class MonitoringSignal:
    """One conclusion drawn from stored state by one monitoring pass."""

    kind: SignalKind
    objective_id: str
    detail: str
    task_id: str = ""
    severity: str = "info"
    actionable: bool = False
    dedupe_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        notification_kind = _SIGNAL_NOTIFICATION.get(self.kind)
        return {
            "kind": self.kind.value,
            "objective_id": self.objective_id,
            "task_id": self.task_id,
            "detail": self.detail,
            "severity": self.severity,
            "actionable": self.actionable,
            "dedupe_key": self.dedupe_key,
            "notification_kind": notification_kind.value if notification_kind else "",
        }


@dataclass(frozen=True, slots=True)
class Notification:
    kind: NotificationKind
    title: str
    body: str
    objective_id: str = ""
    task_id: str = ""
    severity: str = "info"
    dedupe_key: str = ""
    evidence: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utcnow)
    read: bool = False
    read_at: datetime | None = None
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "title": self.title,
            "body": self.body,
            "objective_id": self.objective_id,
            "task_id": self.task_id,
            "severity": self.severity,
            "dedupe_key": self.dedupe_key,
            "evidence": list(self.evidence),
            "created_at": _iso(self.created_at),
            "read": self.read,
            "read_at": _iso(self.read_at),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Notification:
        return cls(
            kind=NotificationKind(str(payload["kind"])),
            title=str(payload["title"]),
            body=str(payload.get("body", "")),
            objective_id=str(payload.get("objective_id", "")),
            task_id=str(payload.get("task_id", "")),
            severity=str(payload.get("severity", "info")),
            dedupe_key=str(payload.get("dedupe_key", "")),
            evidence=tuple(str(item) for item in payload.get("evidence", ())),
            created_at=_dt(payload.get("created_at")) or utcnow(),
            read=bool(payload.get("read", False)),
            read_at=_dt(payload.get("read_at")),
            id=str(payload["id"]),
        )


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """What an executor reports about one attempt.

    ``verified`` is separate from ``detail`` for the phase's central reason: a
    call returning is not success. An unverified outcome is a FAILED task, and
    an outcome asking for approval is a WAITING_APPROVAL task — never a
    completion.
    """

    verified: bool
    detail: str = ""
    evidence: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    needs_approval: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "detail": self.detail,
            "evidence": list(self.evidence),
            "steps": list(self.steps),
            "needs_approval": self.needs_approval,
            "error": self.error,
        }
