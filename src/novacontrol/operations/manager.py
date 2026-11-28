"""Durable objective and task state, and the rules that keep it honest.

The manager is a pure state machine over :class:`Objective` values. It never
executes anything, never calls a model and never reads a clock it was not given:
the service above it decides WHAT to run, and the application decides HOW. What
this module does own is the set of invariants the phase is judged on:

* **completion requires verification.** ``complete_task`` refuses a run that
  claims success without a verified outcome, and refuses a task with unfinished
  planned steps. Creating a task is not doing it.
* **dependencies are real.** A task waits until every task it depends on is
  COMPLETED; a failed or cancelled dependency blocks it instead of leaving it
  looking merely "not started".
* **resume never replays blindly.** A task interrupted while RUNNING is marked
  READY with ``resume_required`` — its verified steps are kept, and it cannot
  start again until the caller has re-assessed current conditions.
* **duplicate triggers are refused by state.** ``begin_task`` only accepts a
  READY task, so a second trigger (a repeated tick, a double click, a resumed
  schedule) finds RUNNING and does nothing.
* **recurrence creates the next occurrence once.** Completing a recurring task
  spawns exactly one successor, keyed by occurrence number, so a replayed
  completion cannot create a second chain.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Sequence
from uuid import uuid4

from novacontrol.automation.schedule import Schedule
from novacontrol.operations.models import (
    AutonomyLevel,
    Milestone,
    Objective,
    ObjectiveKind,
    ObjectiveStatus,
    OperationTask,
    TaskKind,
    TaskState,
    utcnow,
)

_EDITABLE_OBJECTIVE_FIELDS = frozenset(
    {
        "title",
        "outcome",
        "kind",
        "autonomy",
        "priority",
        "deadline",
        "owner",
        "completion_criteria",
        "tags",
        "metadata",
    }
)


class OperationsManager:
    """Stores objectives and moves their tasks through their states."""

    def __init__(self, objectives: Sequence[Objective] = ()) -> None:
        self._objectives: dict[str, Objective] = {}
        for objective in objectives:
            self._objectives[objective.id] = objective

    # -- reading ---------------------------------------------------------------

    def objectives(self) -> tuple[Objective, ...]:
        return tuple(
            sorted(
                self._objectives.values(),
                key=lambda item: (-item.priority, item.created_at, item.id),
            )
        )

    def get(self, objective_id: str) -> Objective:
        try:
            return self._objectives[str(objective_id)]
        except KeyError as exc:
            raise KeyError(f"Objective not found: {objective_id}") from exc

    def find(self, needle: str) -> tuple[Objective, ...]:
        text = str(needle or "").strip().lower()
        if not text:
            return ()
        return tuple(
            objective
            for objective in self.objectives()
            if text in objective.title.lower()
            or text == objective.id.lower()
            or text in objective.outcome.lower()
        )

    def ready_tasks(self, objective_id: str) -> tuple[OperationTask, ...]:
        """Tasks that may start now, highest priority first."""
        objective = self.get(objective_id)
        ready = [task for task in objective.tasks if task.state is TaskState.READY]
        return tuple(sorted(ready, key=lambda task: (-task.priority, task.created_at)))

    def active_objectives(self) -> tuple[Objective, ...]:
        return tuple(
            objective
            for objective in self.objectives()
            if objective.status is ObjectiveStatus.ACTIVE
        )

    def stalled(
        self, *, now: datetime | None = None, after: timedelta = timedelta(hours=24)
    ) -> tuple[OperationTask, ...]:
        clock = now or utcnow()
        return tuple(
            task
            for objective in self.active_objectives()
            for task in objective.tasks
            if task.state in (TaskState.READY, TaskState.RUNNING)
            and clock - task.updated_at >= after
        )

    def overdue(self, *, now: datetime | None = None) -> tuple[OperationTask, ...]:
        clock = now or utcnow()
        return tuple(
            task
            for objective in self.active_objectives()
            for task in objective.tasks
            if task.state.unfinished
            and task.due_at is not None
            and task.due_at < clock
        )

    # -- objective lifecycle ---------------------------------------------------

    def create_objective(
        self,
        title: str,
        outcome: str,
        *,
        kind: ObjectiveKind = ObjectiveKind.PERSONAL,
        autonomy: AutonomyLevel = AutonomyLevel.APPROVAL,
        priority: int = 50,
        deadline: datetime | None = None,
        completion_criteria: Sequence[str] = (),
        tags: Sequence[str] = (),
        owner: str = "operator",
        metadata: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> Objective:
        clock = now or utcnow()
        objective = Objective(
            title=title,
            outcome=outcome,
            kind=kind,
            status=ObjectiveStatus.ACTIVE,
            autonomy=autonomy,
            priority=priority,
            deadline=deadline,
            owner=owner,
            completion_criteria=tuple(str(item) for item in completion_criteria),
            tags=tuple(str(item) for item in tags),
            created_at=clock,
            updated_at=clock,
            metadata=dict(metadata or {}),
        )
        self._objectives[objective.id] = objective
        return objective

    def update_objective(self, objective_id: str, **changes: Any) -> Objective:
        unknown = set(changes) - _EDITABLE_OBJECTIVE_FIELDS
        if unknown:
            raise ValueError(f"Cannot change: {', '.join(sorted(unknown))}")
        objective = self.get(objective_id)
        if objective.closed:
            raise ValueError("A completed or cancelled objective cannot be edited.")
        updated = replace(
            objective,
            **{
                **changes,
                "kind": ObjectiveKind(changes["kind"])
                if "kind" in changes
                else objective.kind,
                "autonomy": AutonomyLevel(changes["autonomy"])
                if "autonomy" in changes
                else objective.autonomy,
                "updated_at": utcnow(),
            },
        )
        self._objectives[updated.id] = updated
        return updated

    def pause_objective(self, objective_id: str, *, reason: str = "") -> Objective:
        objective = self.get(objective_id)
        if objective.closed:
            raise ValueError("A closed objective cannot be paused.")
        updated = replace(
            objective,
            status=ObjectiveStatus.PAUSED,
            paused_reason=reason or "paused by the operator",
            updated_at=utcnow(),
        )
        self._objectives[updated.id] = updated
        return updated

    def resume_objective(
        self, objective_id: str, *, now: datetime | None = None
    ) -> tuple[Objective, tuple[str, ...]]:
        """Reopen a paused objective, and say what must be re-assessed.

        A task that was RUNNING when the process stopped is not resumed in
        place: it becomes READY with ``resume_required`` in its metadata, which
        keeps its verified steps and forces the caller to check conditions
        before running further actions. The return value names those tasks so
        the dashboard can show that a resume is a review, not a replay.
        """
        clock = now or utcnow()
        objective = self.get(objective_id)
        if objective.status is ObjectiveStatus.CANCELLED:
            raise ValueError("A cancelled objective cannot be resumed.")
        if objective.status is ObjectiveStatus.COMPLETED:
            raise ValueError("A completed objective is closed; create a new one.")
        tasks: list[OperationTask] = []
        needs_review: list[str] = []
        for task in objective.tasks:
            if task.state is TaskState.RUNNING:
                metadata = dict(task.metadata)
                metadata["resume_required"] = True
                metadata["interrupted_at"] = clock.isoformat()
                tasks.append(
                    task.with_(
                        state=TaskState.READY,
                        updated_at=clock,
                        metadata=metadata,
                    )
                )
                needs_review.append(task.id)
            elif bool(task.metadata.get("resume_required")):
                # from_dict already recovered this stored RUNNING task to READY;
                # it still needs the same review before it runs again.
                tasks.append(task)
                needs_review.append(task.id)
            else:
                tasks.append(task)
        updated = replace(
            objective,
            status=ObjectiveStatus.ACTIVE,
            paused_reason="",
            tasks=tuple(tasks),
            updated_at=clock,
        )
        self._objectives[updated.id] = updated
        return updated, tuple(needs_review)

    def cancel_objective(self, objective_id: str, *, reason: str = "") -> Objective:
        objective = self.get(objective_id)
        if objective.status is ObjectiveStatus.COMPLETED:
            raise ValueError("A completed objective cannot be cancelled.")
        stamp = utcnow()
        tasks = tuple(
            task.with_(
                state=TaskState.CANCELLED,
                updated_at=stamp,
                error=reason or task.error,
            )
            if task.state.unfinished
            else task
            for task in objective.tasks
        )
        updated = replace(
            objective,
            status=ObjectiveStatus.CANCELLED,
            tasks=tasks,
            paused_reason=reason,
            updated_at=stamp,
        )
        self._objectives[updated.id] = updated
        return updated

    def delete_objective(self, objective_id: str) -> Objective:
        try:
            return self._objectives.pop(str(objective_id))
        except KeyError as exc:
            raise KeyError(f"Objective not found: {objective_id}") from exc

    # -- work definition -------------------------------------------------------

    def add_task(
        self,
        objective_id: str,
        title: str,
        instruction: str,
        *,
        kind: TaskKind = TaskKind.REQUEST,
        priority: int = 50,
        due_at: datetime | None = None,
        scheduled_for: datetime | None = None,
        depends_on: Sequence[str] = (),
        steps: Sequence[str] = (),
        approval_scope: str = "",
        sensitive: bool = False,
        agent: str = "",
        recurrence: Schedule | None = None,
        metadata: Mapping[str, Any] | None = None,
        max_attempts: int = 2,
        now: datetime | None = None,
    ) -> tuple[Objective, OperationTask]:
        clock = now or utcnow()
        objective = self.get(objective_id)
        if objective.closed:
            raise ValueError("Closed objectives do not accept new work.")
        if due_at is not None and due_at.tzinfo is None:
            raise ValueError("A due time must be timezone-aware.")
        known = {task.id for task in objective.tasks}
        dependencies = tuple(str(item) for item in depends_on)
        missing = [dep for dep in dependencies if dep not in known]
        if missing:
            raise ValueError(f"Unknown dependencies: {', '.join(missing)}")
        task = OperationTask(
            title=title,
            instruction=instruction,
            objective_id=objective.id,
            kind=kind,
            state=TaskState.PENDING,
            priority=priority,
            due_at=due_at,
            scheduled_for=scheduled_for,
            depends_on=dependencies,
            steps=tuple(str(step) for step in steps),
            approval_scope=approval_scope,
            sensitive=sensitive,
            agent=agent,
            recurrence=recurrence,
            max_attempts=max_attempts,
            created_at=clock,
            updated_at=clock,
            metadata=dict(metadata or {}),
        )
        tasks = objective.tasks + (task,)
        self._assert_acyclic(tasks)
        updated = replace(objective, tasks=tasks, updated_at=clock)
        self._objectives[updated.id] = updated
        refreshed = self.refresh(objective.id, now=clock)
        return refreshed, refreshed.task(task.id)

    def add_milestone(
        self,
        objective_id: str,
        title: str,
        *,
        task_ids: Sequence[str] = (),
        due_at: datetime | None = None,
        now: datetime | None = None,
    ) -> tuple[Objective, Milestone]:
        clock = now or utcnow()
        objective = self.get(objective_id)
        if objective.closed:
            raise ValueError("Closed objectives do not accept new milestones.")
        known = {task.id for task in objective.tasks}
        missing = [task_id for task_id in task_ids if task_id not in known]
        if missing:
            raise ValueError(f"Milestone names unknown tasks: {', '.join(missing)}")
        milestone = Milestone(
            title=title,
            task_ids=tuple(str(item) for item in task_ids),
            due_at=due_at,
        )
        updated = replace(
            objective,
            milestones=objective.milestones + (milestone,),
            updated_at=clock,
        )
        self._objectives[updated.id] = updated
        return updated, milestone

    # -- derived state ---------------------------------------------------------

    def refresh(self, objective_id: str, *, now: datetime | None = None) -> Objective:
        """Recompute eligibility, milestone completion and objective completion.

        This is the only place state is derived from dependencies, and it is
        idempotent: calling it twice at the same instant changes nothing.
        """
        clock = now or utcnow()
        objective = self.get(objective_id)
        if objective.closed:
            return objective
        by_id = {task.id: task for task in objective.tasks}
        refreshed: list[OperationTask] = []
        for task in objective.tasks:
            # FAILED, BLOCKED and WAITING_APPROVAL are STICKY: they are states a
            # person has to resolve, so a refresh must not quietly turn one back
            # into runnable work (that is how a broken task retries forever).
            if task.state in (
                TaskState.FAILED,
                TaskState.BLOCKED,
                TaskState.WAITING_APPROVAL,
                TaskState.RUNNING,
            ) or task.state.terminal:
                refreshed.append(task)
                continue
            blocking = [
                by_id[dep]
                for dep in task.depends_on
                if dep in by_id
                and by_id[dep].state
                in (
                    TaskState.FAILED,
                    TaskState.BLOCKED,
                    TaskState.CANCELLED,
                )
            ]
            if blocking:
                reason = "; ".join(
                    f"{item.title!r} is {item.state.value}" for item in blocking
                )
                refreshed.append(
                    task.with_(
                        state=TaskState.BLOCKED,
                        error=reason,
                        updated_at=clock,
                    )
                )
                continue
            waiting = [
                dep
                for dep in task.depends_on
                if dep in by_id and by_id[dep].state is not TaskState.COMPLETED
            ]
            if waiting or (
                task.scheduled_for is not None and task.scheduled_for > clock
            ):
                refreshed.append(_state(task, TaskState.PENDING, clock))
                continue
            refreshed.append(_state(task, TaskState.READY, clock))

        milestones = tuple(
            replace(
                milestone,
                verified_at=(
                    clock
                    if milestone.task_ids
                    and all(
                        by_id.get(task_id) is not None
                        and by_id[task_id].state is TaskState.COMPLETED
                        for task_id in milestone.task_ids
                    )
                    else milestone.verified_at
                ),
            )
            for milestone in objective.milestones
        )

        status = objective.status
        completed_at = objective.completed_at
        if refreshed and all(task.state is TaskState.COMPLETED for task in refreshed):
            status = ObjectiveStatus.COMPLETED
            completed_at = completed_at or clock
        # A rejected task cancels THAT task, not the objective: an objective
        # with one completed piece of work and one rejected piece is still an
        # active objective whose remaining progress a person can see.

        updated = replace(
            objective,
            tasks=tuple(refreshed),
            milestones=milestones,
            status=status,
            completed_at=completed_at,
            updated_at=clock,
        )
        self._objectives[updated.id] = updated
        return updated

    # -- task lifecycle --------------------------------------------------------

    def begin_task(
        self,
        objective_id: str,
        task_id: str,
        *,
        reassessed: bool = False,
        now: datetime | None = None,
    ) -> OperationTask:
        """Move a READY task to RUNNING, or refuse with the reason.

        Two refusals matter. A task with ``resume_required`` will not start until
        the caller passes ``reassessed=True`` — the phase forbids blindly
        replaying actions after an interruption. And a task that is not READY
        (already RUNNING, completed, waiting) is refused by state, which is what
        makes a duplicate trigger harmless.
        """
        clock = now or utcnow()
        objective = self.refresh(objective_id, now=clock)
        if objective.status is not ObjectiveStatus.ACTIVE:
            raise ValueError(
                f"Objective {objective.title!r} is {objective.status.value}, so its "
                "work cannot start."
            )
        task = objective.task(task_id)
        if task.state is TaskState.RUNNING:
            raise ValueError("That task is already running; a duplicate trigger was ignored.")
        if task.state is not TaskState.READY:
            raise ValueError(
                f"Task {task.title!r} is {task.state.value}, so it cannot start."
            )
        if bool(task.metadata.get("resume_required")) and not reassessed:
            raise ValueError(
                "This task was interrupted, so its conditions must be re-assessed "
                "before it runs again."
            )
        updated_task = task.with_(
            state=TaskState.RUNNING,
            attempts=task.attempts + 1,
            started_at=task.started_at or clock,
            updated_at=clock,
            error="",
        )
        metadata = dict(updated_task.metadata)
        metadata.pop("resume_required", None)
        updated_task = updated_task.with_(metadata=metadata)
        self._replace_task(objective, updated_task)
        return updated_task

    def record_step(
        self,
        objective_id: str,
        task_id: str,
        step: str,
        *,
        verified: bool,
        evidence: str = "",
        now: datetime | None = None,
    ) -> OperationTask:
        """Record one VERIFIED step and its checkpoint.

        An unverified step is refused: the checkpoint is the record the resume
        path trusts, so storing an unproven step would make the next resume skip
        work that never actually happened.
        """
        if not verified:
            raise ValueError(
                "Only a verified step can be checkpointed; an attempt is not a result."
            )
        clock = now or utcnow()
        objective = self.get(objective_id)
        task = objective.task(task_id)
        if step not in task.steps:
            raise ValueError(f"Unknown step for this task: {step}")
        completed = task.completed_steps
        if step not in completed:
            completed = completed + (step,)
        checkpoint = dict(task.checkpoint)
        checkpoint["completed_steps"] = list(completed)
        checkpoint["last_verified_state"] = evidence or step
        checkpoint["verified_at"] = clock.isoformat()
        updated_task = task.with_(
            completed_steps=completed,
            last_verified_state=evidence or step,
            checkpoint=checkpoint,
            evidence=task.evidence + ((evidence,) if evidence else ()),
            updated_at=clock,
        )
        self._replace_task(objective, updated_task)
        return updated_task

    def complete_task(
        self,
        objective_id: str,
        task_id: str,
        *,
        verified: bool,
        result: str = "",
        evidence: Sequence[str] = (),
        now: datetime | None = None,
    ) -> tuple[OperationTask, OperationTask | None]:
        """Close a task, requiring a verified outcome and no pending steps."""
        clock = now or utcnow()
        if not verified:
            raise ValueError(
                "A task is not complete until its outcome is verified; a returned "
                "call is not evidence."
            )
        objective = self.get(objective_id)
        task = objective.task(task_id)
        if task.state is TaskState.COMPLETED:
            raise ValueError("That task is already complete; a duplicate completion was ignored.")
        if task.state is TaskState.CANCELLED:
            raise ValueError("A cancelled task cannot be completed.")
        if task.steps:
            pending = task.pending_steps
            if pending:
                raise ValueError(
                    "Planned steps are still incomplete: " + ", ".join(pending)
                )
        completed_task = task.with_(
            state=TaskState.COMPLETED,
            verified=True,
            result=result or task.result,
            evidence=task.evidence + tuple(str(item) for item in evidence),
            error="",
            completed_at=clock,
            updated_at=clock,
        )
        spawned: OperationTask | None = None
        if task.recurrence is not None:
            next_at = task.recurrence.next_after(clock)
            if next_at is not None:
                occurrence = int(task.metadata.get("occurrence", 1)) + 1
                if not any(
                    other.metadata.get("occurrence_of") == task.id
                    for other in objective.tasks
                ):
                    spawned = replace(
                        completed_task,
                        id=uuid4().hex,
                        state=TaskState.PENDING,
                        scheduled_for=next_at,
                        attempts=0,
                        completed_steps=(),
                        last_verified_state="",
                        checkpoint={},
                        result="",
                        evidence=(),
                        verified=False,
                        completed_at=None,
                        started_at=None,
                        created_at=clock,
                        updated_at=clock,
                        metadata={
                            **dict(completed_task.metadata),
                            "occurrence": occurrence,
                            "occurrence_of": task.id,
                        },
                    )
        tasks = tuple(
            completed_task if item.id == task.id else item for item in objective.tasks
        )
        if spawned is not None:
            tasks = tasks + (spawned,)
        updated = replace(objective, tasks=tasks, updated_at=clock)
        self._objectives[updated.id] = updated
        self.refresh(updated.id, now=clock)
        return completed_task, spawned

    def fail_task(
        self,
        objective_id: str,
        task_id: str,
        *,
        error: str,
        now: datetime | None = None,
    ) -> OperationTask:
        clock = now or utcnow()
        objective = self.get(objective_id)
        task = objective.task(task_id)
        if task.state in (TaskState.COMPLETED, TaskState.CANCELLED):
            raise ValueError(
                f"Task {task.title!r} is {task.state.value}; a closed task cannot fail."
            )
        updated_task = task.with_(
            state=TaskState.FAILED,
            error=error or "the task failed",
            verified=False,
            updated_at=clock,
        )
        self._replace_task(objective, updated_task)
        self.refresh(objective_id, now=clock)
        return updated_task

    def block_task(
        self,
        objective_id: str,
        task_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> OperationTask:
        clock = now or utcnow()
        objective = self.get(objective_id)
        current = objective.task(task_id)
        if current.state in (TaskState.COMPLETED, TaskState.CANCELLED):
            raise ValueError(
                f"Task {current.title!r} is {current.state.value}; a closed task "
                "cannot be blocked."
            )
        updated_task = current.with_(
            state=TaskState.BLOCKED,
            error=reason,
            updated_at=clock,
        )
        self._replace_task(objective, updated_task)
        return updated_task

    def wait_for_approval(
        self,
        objective_id: str,
        task_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> OperationTask:
        clock = now or utcnow()
        objective = self.get(objective_id)
        current = objective.task(task_id)
        if current.state not in (TaskState.READY, TaskState.RUNNING):
            raise ValueError(
                f"Task {current.title!r} is {current.state.value}; only ready or "
                "running work can wait for approval."
            )
        updated_task = current.with_(
            state=TaskState.WAITING_APPROVAL,
            error=reason,
            updated_at=clock,
        )
        self._replace_task(objective, updated_task)
        return updated_task

    def approve_task(
        self,
        objective_id: str,
        task_id: str,
        *,
        by: str = "operator",
        now: datetime | None = None,
    ) -> OperationTask:
        """Approve THIS task, for this objective, and return it to the queue.

        Approval is recorded with its scope and the approver, never as a global
        flag: the task remembers what was authorized and by whom.
        """
        clock = now or utcnow()
        objective = self.get(objective_id)
        task = objective.task(task_id)
        if task.state is not TaskState.WAITING_APPROVAL:
            raise ValueError(
                "Only a task waiting for approval can be approved; this one is "
                f"{task.state.value}."
            )
        updated_task = task.with_(
            approved=True,
            approved_by=by,
            approval_scope=task.scope,
            state=TaskState.READY,
            error="",
            updated_at=clock,
        )
        self._replace_task(objective, updated_task)
        return self.refresh(objective_id, now=clock).task(task_id)

    def reject_task(
        self,
        objective_id: str,
        task_id: str,
        *,
        reason: str = "",
        now: datetime | None = None,
    ) -> OperationTask:
        clock = now or utcnow()
        objective = self.get(objective_id)
        task = objective.task(task_id)
        if task.state is not TaskState.WAITING_APPROVAL and not (
            task.state is TaskState.READY and task.sensitive and not task.approved
        ):
            raise ValueError(
                "Only a task waiting for approval (or a queued sensitive task) "
                f"can be rejected; this one is {task.state.value}."
            )
        updated_task = task.with_(
            state=TaskState.CANCELLED,
            approved=False,
            error=reason or "rejected by the operator",
            updated_at=clock,
        )
        self._replace_task(objective, updated_task)
        self.refresh(objective_id, now=clock)
        return updated_task

    def retry_task(
        self,
        objective_id: str,
        task_id: str,
        *,
        now: datetime | None = None,
    ) -> OperationTask:
        """Requeue a failed or blocked task when it has attempts left.

        A sensitive task loses its approval on retry: the approval was for the
        attempt that failed, and repeating an irreversible action is a new
        decision.
        """
        clock = now or utcnow()
        objective = self.get(objective_id)
        task = objective.task(task_id)
        if task.state not in (TaskState.FAILED, TaskState.BLOCKED):
            raise ValueError(
                f"Only a failed or blocked task can be retried; this one is "
                f"{task.state.value}."
            )
        if task.attempts >= task.max_attempts:
            raise ValueError(
                f"This task has used its {task.max_attempts} attempts; it needs a "
                "person to decide what happens next."
            )
        updated_task = task.with_(
            state=TaskState.READY,
            error="",
            verified=False,
            approved=False if task.sensitive else task.approved,
            updated_at=clock,
        )
        self._replace_task(objective, updated_task)
        return self.refresh(objective_id, now=clock).task(task_id)

    def cancel_task(
        self, objective_id: str, task_id: str, *, now: datetime | None = None
    ) -> OperationTask:
        clock = now or utcnow()
        objective = self.get(objective_id)
        task = objective.task(task_id)
        if task.state is TaskState.COMPLETED:
            raise ValueError("A completed task cannot be cancelled.")
        updated_task = task.with_(
            state=TaskState.CANCELLED, updated_at=clock
        )
        self._replace_task(objective, updated_task)
        self.refresh(objective_id, now=clock)
        return updated_task

    # -- persistence -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"objectives": [objective.to_dict() for objective in self.objectives()]}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> OperationsManager:
        clock = utcnow()
        objectives: list[Objective] = []
        for item in (payload or {}).get("objectives", ()):
            if not isinstance(item, Mapping):
                continue
            try:
                objectives.append(
                    _recover_interrupted(Objective.from_dict(item), clock)
                )
            except (KeyError, ValueError, TypeError):
                # One damaged objective must not take the whole board down.
                continue
        return cls(objectives)

    def report(self) -> dict[str, Any]:
        objectives = self.objectives()
        tasks = [task for objective in objectives for task in objective.tasks]
        return {
            "objectives": len(objectives),
            "active": sum(
                1 for item in objectives if item.status is ObjectiveStatus.ACTIVE
            ),
            "paused": sum(
                1 for item in objectives if item.status is ObjectiveStatus.PAUSED
            ),
            "completed": sum(
                1 for item in objectives if item.status is ObjectiveStatus.COMPLETED
            ),
            "cancelled": sum(
                1 for item in objectives if item.status is ObjectiveStatus.CANCELLED
            ),
            "tasks": len(tasks),
            "ready": sum(1 for task in tasks if task.state is TaskState.READY),
            "running": sum(1 for task in tasks if task.state is TaskState.RUNNING),
            "waiting_approval": sum(
                1 for task in tasks if task.state is TaskState.WAITING_APPROVAL
            ),
            "failed": sum(1 for task in tasks if task.state is TaskState.FAILED),
            "blocked": sum(1 for task in tasks if task.state is TaskState.BLOCKED),
            "completed_tasks": sum(
                1 for task in tasks if task.state is TaskState.COMPLETED
            ),
        }

    # -- internals -------------------------------------------------------------

    def _replace_task(self, objective: Objective, task: OperationTask) -> Objective:
        tasks = tuple(task if item.id == task.id else item for item in objective.tasks)
        updated = replace(objective, tasks=tasks, updated_at=task.updated_at)
        self._objectives[updated.id] = updated
        return updated

    @staticmethod
    def _assert_acyclic(tasks: Sequence[OperationTask]) -> None:
        graph = {task.id: task.depends_on for task in tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def walk(node: str) -> None:
            if node in visited:
                return
            if node in visiting:
                raise ValueError("That dependency would create a cycle.")
            visiting.add(node)
            for neighbour in graph.get(node, ()):
                walk(neighbour)
            visiting.discard(node)
            visited.add(node)

        for node in graph:
            walk(node)


def _recover_interrupted(objective: Objective, clock: datetime) -> Objective:
    """Turn a stored RUNNING task into reviewable work.

    A process that stops mid-task leaves RUNNING behind. Without this, an
    objective that was ACTIVE at the restart would keep that task RUNNING
    forever and never run it again: exactly the silent stall Phase 30 exists to
    prevent. The task keeps its verified checkpoints and becomes READY with
    ``resume_required``, so the service still reassesses conditions before it
    executes anything.
    """
    if not any(task.state is TaskState.RUNNING for task in objective.tasks):
        return objective
    tasks = tuple(
        task.with_(
            state=TaskState.READY,
            updated_at=clock,
            metadata={
                **dict(task.metadata),
                "resume_required": True,
                "interrupted_at": clock.isoformat(),
            },
        )
        if task.state is TaskState.RUNNING
        else task
        for task in objective.tasks
    )
    return replace(objective, tasks=tasks, updated_at=clock)


def _state(task: OperationTask, state: TaskState, clock: datetime) -> OperationTask:
    """Move a task to ``state``, touching ``updated_at`` only on a real change.

    A refresh that rewrote the timestamp every tick would make every waiting task
    look freshly touched, and stalled-task detection would never fire.
    """
    if task.state is state:
        return task
    return replace(task, state=state, updated_at=clock)


def _coerce_deadline(value: Any) -> datetime | None:
    """Small helper used by the API layer for optional ISO deadlines."""
    if value in (None, ""):
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
