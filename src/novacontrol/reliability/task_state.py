"""TaskStateMachine: the states a task may be in, and the moves between them.

A task used to have a status that anything could set. That is how a cancelled
task ends up reporting COMPLETED: nothing rejected the transition, so the last
writer won. This module makes the legal moves DATA — one table, checked on every
change — and makes an illegal one an error rather than a silent overwrite.

The states, and why each is distinct from its neighbours:

    PENDING     created, nothing decided yet
    PLANNING    a plan is being built
    RUNNING     a step is executing, or the loop is between steps
    WAITING     blocked on something outside the task (a tool, a person)
    VERIFYING   the step ran; the result is being checked
    RECOVERING  a failure was diagnosed and a recovery is being chosen
    RETRYING    a decision was made to run again
    PAUSED      a person asked it to hold; the state is PRESERVED, not lost
    CANCELLED   a person asked it to stop, and it did so at a safe point
    COMPLETED   the work finished and was verified
    FAILED      it stopped without completing, for a reported reason

VERIFYING and RECOVERING are not decoration: a task that is checking its result
is not the same task as one that is executing, and Phase 9's event stream can
only be truthful about verification if the state says when it is happening.

Every state carries the fields the specification asks for — the id, the parent
id, the previous state, the timestamps, the current step, the retry count, the
error, the cancellation and pause flags — and every change can be OBSERVED
through an injected observer, which is the seam Phase 9's event bus plugs into.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class TaskState(StrEnum):
    """Where a task is. The vocabulary above, as values a report can carry."""

    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING = "waiting"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    RETRYING = "retrying"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


#: States a task cannot leave. Nothing may follow them, so no transition out of
#: one is legal — which is what stops a cancelled task being marked complete.
TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.CANCELLED, TaskState.COMPLETED, TaskState.FAILED}
)

#: The legal moves. FAILED and CANCELLED appear in every non-terminal row on
#: purpose: a crash and a cancellation can happen at any point, and pretending
#: otherwise would make the machine reject the two transitions that matter most
#: in production. Everything else is there because the workflow genuinely goes
#: that way — RUNNING -> PAUSED because a person asked, VERIFYING -> RETRYING
#: because a check failed, and so on.
ALLOWED_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset(
        {TaskState.PLANNING, TaskState.CANCELLED, TaskState.FAILED}
    ),
    TaskState.PLANNING: frozenset(
        {
            TaskState.RUNNING,
            TaskState.WAITING,
            TaskState.PAUSED,
            TaskState.COMPLETED,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.VERIFYING,
            TaskState.WAITING,
            TaskState.RETRYING,
            TaskState.RECOVERING,
            TaskState.PAUSED,
            TaskState.COMPLETED,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    TaskState.WAITING: frozenset(
        {
            TaskState.RUNNING,
            TaskState.PLANNING,
            TaskState.PAUSED,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    TaskState.VERIFYING: frozenset(
        {
            TaskState.RUNNING,
            TaskState.RETRYING,
            TaskState.RECOVERING,
            TaskState.WAITING,
            TaskState.PAUSED,
            TaskState.COMPLETED,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    TaskState.RECOVERING: frozenset(
        {
            TaskState.RETRYING,
            TaskState.RUNNING,
            TaskState.PLANNING,
            TaskState.WAITING,
            TaskState.PAUSED,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    TaskState.RETRYING: frozenset(
        {
            TaskState.RUNNING,
            TaskState.VERIFYING,
            TaskState.RECOVERING,
            TaskState.PAUSED,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    TaskState.PAUSED: frozenset(
        {
            TaskState.RUNNING,
            TaskState.PLANNING,
            TaskState.WAITING,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    TaskState.CANCELLED: frozenset(),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
}

#: States a task may be paused FROM — anything that is doing work.
PAUSABLE_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.PLANNING,
        TaskState.RUNNING,
        TaskState.WAITING,
        TaskState.VERIFYING,
        TaskState.RECOVERING,
        TaskState.RETRYING,
    }
)


class InvalidTransitionError(ValueError):
    """A state change that the machine refused, with the reason it refused."""

    def __init__(
        self, task_id: str, current: TaskState, requested: TaskState, allowed: Sequence[TaskState]
    ) -> None:
        self.task_id = task_id
        self.current = current
        self.requested = requested
        self.allowed = tuple(allowed)
        moves = ", ".join(sorted(state.value for state in self.allowed)) or "none"
        super().__init__(
            f"Task {task_id or '(unnamed)'} cannot go from {current.value} to "
            f"{requested.value}; allowed from {current.value}: {moves}."
        )


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    """One task's state and everything the specification asks it to carry."""

    task_id: str
    current_state: TaskState
    previous_state: TaskState | None = None
    parent_task_id: str = ""
    current_step: str = ""
    retry_count: int = 0
    error: str = ""
    cancellation_requested: bool = False
    pause_requested: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def terminal(self) -> bool:
        return self.current_state in TERMINAL_STATES

    @property
    def paused(self) -> bool:
        return self.current_state is TaskState.PAUSED

    def with_(self, **changes: Any) -> TaskSnapshot:
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "parent_task_id": self.parent_task_id,
            "current_state": self.current_state.value,
            "previous_state": self.previous_state.value if self.previous_state else None,
            "current_step": self.current_step,
            "retry_count": self.retry_count,
            "error": self.error,
            "cancellation_requested": self.cancellation_requested,
            "pause_requested": self.pause_requested,
            "metadata": dict(self.metadata),
            "terminal": self.terminal,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


#: Told about every state change: (snapshot, previous state). Injected rather
#: than imported so this module stays free of the event bus, and so Phase 9 can
#: publish these without a second change here.
StateObserver = Callable[[TaskSnapshot, TaskState], None]


class TaskStateMachine:
    """One task's states, with every move checked against the table above."""

    def __init__(
        self,
        *,
        task_id: str | None = None,
        parent_task_id: str = "",
        state: TaskState = TaskState.PENDING,
        metadata: Mapping[str, Any] | None = None,
        observer: StateObserver | None = None,
    ) -> None:
        self._task_id = task_id or uuid4().hex
        now = datetime.now(UTC)
        self._snapshot = TaskSnapshot(
            task_id=self._task_id,
            parent_task_id=parent_task_id,
            current_state=state,
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        self._history: list[TaskState] = [state]
        self._observer = observer

    # -- reading ---------------------------------------------------------------

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def state(self) -> TaskState:
        return self._snapshot.current_state

    @property
    def snapshot(self) -> TaskSnapshot:
        return self._snapshot

    @property
    def history(self) -> tuple[TaskState, ...]:
        """Every state this task has been in, oldest first."""
        return tuple(self._history)

    @property
    def terminal(self) -> bool:
        return self._snapshot.terminal

    @property
    def cancellation_requested(self) -> bool:
        return self._snapshot.cancellation_requested

    @property
    def pause_requested(self) -> bool:
        return self._snapshot.pause_requested

    def allowed(self) -> frozenset[TaskState]:
        return ALLOWED_TRANSITIONS[self.state]

    def can_transition(self, target: TaskState) -> bool:
        return target in self.allowed()

    # -- moving ----------------------------------------------------------------

    def transition(
        self,
        target: TaskState,
        *,
        current_step: str | None = None,
        error: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> TaskSnapshot:
        """Move to ``target``, or refuse with :class:`InvalidTransitionError`.

        Refused rather than logged: a transition nothing performs is a bug in
        the caller, and silently accepting it is how a task ends up COMPLETED
        after being cancelled.
        """
        current = self.state
        if target not in ALLOWED_TRANSITIONS[current]:
            raise InvalidTransitionError(
                self._task_id, current, target, sorted(self.allowed(), key=str)
            )
        merged = dict(self._snapshot.metadata)
        merged.update(metadata or {})
        self._snapshot = self._snapshot.with_(
            previous_state=current,
            current_state=target,
            current_step=self._snapshot.current_step if current_step is None else current_step,
            error=self._snapshot.error if error is None else error,
            metadata=merged,
            updated_at=datetime.now(UTC),
        )
        self._history.append(target)
        self._notify(current)
        return self._snapshot

    def ensure(self, target: TaskState, **changes: Any) -> TaskSnapshot:
        """Move to ``target`` unless already there — for idempotent checkpoints."""
        return self._snapshot if self.state is target else self.transition(target, **changes)

    # -- the moves the workflow actually makes ---------------------------------
    # Named methods rather than bare ``transition(RUNNING)``: the vocabulary is
    # the safety property, so callers cannot reach a state by accident.

    def start_planning(self) -> TaskSnapshot:
        return self.transition(TaskState.PLANNING)

    def start_running(self, *, current_step: str | None = None) -> TaskSnapshot:
        if self.state is TaskState.PAUSED:
            return self.transition(TaskState.RUNNING, current_step=current_step)
        return self.ensure(TaskState.RUNNING, current_step=current_step)

    def wait(self, *, reason: str = "") -> TaskSnapshot:
        return self.transition(
            TaskState.WAITING, metadata={"waiting_reason": reason} if reason else None
        )

    def begin_verification(self, *, current_step: str | None = None) -> TaskSnapshot:
        return self.transition(TaskState.VERIFYING, current_step=current_step)

    def begin_recovery(self, *, reason: str = "") -> TaskSnapshot:
        return self.transition(
            TaskState.RECOVERING, metadata={"recovery_reason": reason} if reason else None
        )

    def begin_retry(self, *, current_step: str | None = None) -> TaskSnapshot:
        """Go back for another attempt, counting it as the specification asks."""
        self.transition(TaskState.RETRYING, current_step=current_step)
        self._snapshot = self._snapshot.with_(
            retry_count=self._snapshot.retry_count + 1, updated_at=datetime.now(UTC)
        )
        return self._snapshot

    def complete(self) -> TaskSnapshot:
        return self.transition(TaskState.COMPLETED)

    def fail(self, error: str = "") -> TaskSnapshot:
        return self.transition(TaskState.FAILED, error=error)

    def pause(self, *, reason: str = "") -> TaskSnapshot:
        """Ask the task to hold: the flag is set AND the state moves.

        Both, because they answer different questions. The STATE says the task
        is not doing work; the FLAG says a pause was requested, which is what a
        runner checks at its next checkpoint even if it has already moved on —
        a pause that only moved the state would not stop a runner that is
        between steps, and a flag without a state would leave the task looking
        busy while it waits.

        A terminal task cannot be paused: there is no work left to hold, so the
        request is refused rather than recorded as if it had meant something.
        """
        if self.terminal:
            raise InvalidTransitionError(self._task_id, self.state, TaskState.PAUSED, ())
        self._snapshot = self._snapshot.with_(
            pause_requested=True, updated_at=datetime.now(UTC)
        )
        if self.state not in PAUSABLE_STATES:
            return self._snapshot
        return self.transition(
            TaskState.PAUSED, metadata={"pause_reason": reason} if reason else None
        )

    def resume(self, *, target: TaskState | None = None) -> TaskSnapshot:
        """Clear the pause and go back to the work that was interrupted.

        The state it returns to is the one the pause STOPPED, not a default:
        a task paused while PLANNING goes back to planning, because sending it
        to RUNNING would run steps that were never planned. ``previous_state``
        is exactly that fact, so it is where this reads it from.

        ``target`` overrides the destination for a caller that knows better
        (resuming into PLANNING from a checkpoint, say).
        """
        self._snapshot = self._snapshot.with_(
            pause_requested=False, updated_at=datetime.now(UTC)
        )
        if self.state is not TaskState.PAUSED:
            return self._snapshot
        return self.transition(target or self._resume_target())

    def _resume_target(self) -> TaskState:
        """Where a resume should land: the interrupted state, or the loop.

        A state that was paused in the middle of a STEP (VERIFYING, RECOVERING,
        RETRYING) is not reachable from PAUSED, and re-entering it here would
        claim the step's phase resumed when the runner is what resumes it — so
        those land in RUNNING and the loop picks the phase back up itself.
        """
        allowed = ALLOWED_TRANSITIONS[TaskState.PAUSED]
        interrupted = self._snapshot.previous_state
        if interrupted is not None and interrupted in allowed:
            return interrupted
        return TaskState.RUNNING

    def request_cancellation(self) -> TaskSnapshot:
        """Record that a person asked to stop, without stopping anything yet.

        Deliberately separate from :meth:`cancel`: a running tool is not killed
        by a flag, so the request is visible at the next safe point and the task
        moves to CANCELLED there.
        """
        self._snapshot = self._snapshot.with_(
            cancellation_requested=True, updated_at=datetime.now(UTC)
        )
        return self._snapshot

    def cancel(self, *, reason: str = "") -> TaskSnapshot:
        """Stop, and stop safely: CANCELLED is only reachable where it is legal.

        A completed or failed task cannot be cancelled — there is nothing left
        to stop — and the transition table refuses it rather than rewriting what
        already happened.
        """
        self._snapshot = self._snapshot.with_(
            cancellation_requested=True, updated_at=datetime.now(UTC)
        )
        return self.transition(
            TaskState.CANCELLED, metadata={"cancel_reason": reason} if reason else None
        )

    def note_step(self, step: str) -> TaskSnapshot:
        self._snapshot = self._snapshot.with_(current_step=step, updated_at=datetime.now(UTC))
        return self._snapshot

    def set_metadata(self, **values: Any) -> TaskSnapshot:
        merged = dict(self._snapshot.metadata)
        merged.update(values)
        self._snapshot = self._snapshot.with_(metadata=merged, updated_at=datetime.now(UTC))
        return self._snapshot

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self._snapshot.to_dict(),
            "history": [state.value for state in self._history],
            "allowed": sorted(state.value for state in self.allowed()),
        }

    def _notify(self, previous: TaskState) -> None:
        if self._observer is None:
            return
        self._observer(self._snapshot, previous)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "PAUSABLE_STATES",
    "TERMINAL_STATES",
    "InvalidTransitionError",
    "StateObserver",
    "TaskSnapshot",
    "TaskState",
    "TaskStateMachine",
]
