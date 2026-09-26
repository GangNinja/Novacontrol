"""Pause, resume and cancel: cooperative control over running tasks.

Three rules shape this module, and they are the difference between stopping a
task and breaking one:

  * **A cancellation is a request, not a kill.** The flag is set immediately and
    honoured at the task's next CHECKPOINT — the place a runner already yields
    between steps. Nothing terminates a tool mid-flight, because a half-done
    write is worse than a finished one, and ``stop`` on a task that is inside an
    irreversible operation must not be a way to interrupt it halfway.
  * **State is preserved.** Pausing keeps the state machine, the current step
    and the checkpoint, so a resume continues from where the work was rather
    than starting it again.
  * **A tool that CAN stop cooperatively registers how.** ``register_cancel_hook``
    is for the operations that genuinely support being interrupted (a long
    download, a browser walk); they are told, and everything else is simply not
    started again.

The commands are matched as whole phrases, deterministically. "stop this task"
is a cancellation; "stop the music" is not a task command, and a matcher that
pattern-matched "stop" would make every request a way to kill the running job.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from inspect import isawaitable
from typing import Any

from novacontrol.reliability.task_state import (
    InvalidTransitionError,
    StateObserver,
    TaskSnapshot,
    TaskState,
    TaskStateMachine,
)

#: Told that a cancellable operation should stop. May be sync or async.
CancelHook = Callable[[str], "None | Awaitable[None]"]


class TaskCommand(StrEnum):
    """What a person asked for, once their words were read."""

    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    STATUS = "status"


#: The phrases that mean each command. Whole phrases only, because a task
#: command must be deliberate: "stop the music" shares the word and not the
#: meaning, and guessing which "stop" was meant is how a running job dies by
#: accident.
_COMMAND_PHRASES: tuple[tuple[TaskCommand, tuple[str, ...]], ...] = (
    (
        TaskCommand.CANCEL,
        (
            "cancel",
            "cancel it",
            "cancel this",
            "cancel this task",
            "cancel the task",
            "stop",
            "stop it",
            "stop this",
            "stop this task",
            "stop the task",
            "stop the current task",
            "abort",
            "abort it",
            "abort this task",
            "never mind",
            "forget it",
        ),
    ),
    (
        TaskCommand.PAUSE,
        (
            "pause",
            "pause it",
            "pause this",
            "pause this task",
            "pause the task",
            "hold on",
            "hold this task",
        ),
    ),
    (
        TaskCommand.RESUME,
        (
            "resume",
            "resume it",
            "resume this",
            "resume this task",
            "resume the task",
            "continue",
            "continue this task",
            "carry on",
            "unpause",
        ),
    ),
    (
        TaskCommand.STATUS,
        ("status", "task status", "what is it doing", "what's it doing"),
    ),
)

_PUNCTUATION = re.compile(r"[.!?,;:]+$")


def parse_task_command(text: str) -> TaskCommand | None:
    """The command in ``text``, or None when it is not a task command.

    Whole-phrase matching after a conservative normalisation (case, surrounding
    whitespace, trailing punctuation). No substring matching: "stop the music"
    must not cancel a task, and a person who wants to stop work says so.
    """
    lowered = _PUNCTUATION.sub("", " ".join(str(text or "").split()).lower())
    if not lowered:
        return None
    stripped = lowered.removeprefix("please ").strip()
    for command, phrases in _COMMAND_PHRASES:
        if stripped in phrases:
            return command
    return None


class TaskCancelled(Exception):
    """Raised at a checkpoint after a cancellation request was honoured.

    Raised rather than returned so a runner cannot ignore it by accident: the
    except clause that catches it is the place the caller says what it does with
    the state it kept.
    """

    def __init__(self, task_id: str, step: str = "") -> None:
        self.task_id = task_id
        self.step = step
        super().__init__(
            f"Task {task_id or '(unnamed)'} was cancelled"
            + (f" at {step!r}" if step else "")
            + "; it stopped at a safe point and its state was preserved."
        )


@dataclass(frozen=True, slots=True)
class TaskControlResult:
    """What a control command did, in a shape a UI or API can report."""

    command: TaskCommand
    task_id: str
    accepted: bool
    state: TaskState
    summary: str
    checkpoint: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command.value,
            "task_id": self.task_id,
            "accepted": self.accepted,
            "state": self.state.value,
            "summary": self.summary,
            "checkpoint": dict(self.checkpoint),
        }


@dataclass(slots=True)
class _Task:
    machine: TaskStateMachine
    checkpoint: dict[str, Any] = field(default_factory=dict)
    cancel_hooks: list[CancelHook] = field(default_factory=list)
    paused_event: asyncio.Event = field(default_factory=asyncio.Event)


class TaskController:
    """Owns the running tasks' state machines, and the control commands."""

    def __init__(self, *, observer: StateObserver | None = None) -> None:
        self._observer = observer
        self._tasks: dict[str, _Task] = {}
        self._order: list[str] = []

    # -- tasks -----------------------------------------------------------------

    def open_task(
        self,
        *,
        task_id: str | None = None,
        parent_task_id: str = "",
        state: TaskState = TaskState.PENDING,
        metadata: Mapping[str, Any] | None = None,
    ) -> TaskStateMachine:
        """Start tracking a task, and return its machine."""
        machine = TaskStateMachine(
            task_id=task_id,
            parent_task_id=parent_task_id,
            state=state,
            metadata=metadata,
            observer=self._observer,
        )
        self._tasks[machine.task_id] = _Task(machine=machine)
        self._order.append(machine.task_id)
        return machine

    def machine(self, task_id: str) -> TaskStateMachine:
        return self._entry(task_id).machine

    def snapshot(self, task_id: str) -> TaskSnapshot:
        return self._entry(task_id).machine.snapshot

    def tasks(self) -> tuple[TaskSnapshot, ...]:
        return tuple(self._tasks[task_id].machine.snapshot for task_id in self._order)

    def running(self) -> tuple[TaskSnapshot, ...]:
        """Tasks that are not in a terminal state — what a control command acts on."""
        return tuple(snapshot for snapshot in self.tasks() if not snapshot.terminal)

    def active_task_id(self) -> str:
        """The task a bare "pause"/"cancel" refers to: the newest unfinished one."""
        for task_id in reversed(self._order):
            if not self._tasks[task_id].machine.terminal:
                return task_id
        return self._order[-1] if self._order else ""

    def forget(self, task_id: str) -> None:
        """Stop tracking one task (its state is kept by the caller)."""
        self._tasks.pop(task_id, None)
        if task_id in self._order:
            self._order.remove(task_id)

    # -- checkpoints -----------------------------------------------------------

    def save_checkpoint(self, task_id: str, **values: Any) -> Mapping[str, Any]:
        """Record where the work got to, so a resume can continue from there."""
        entry = self._entry(task_id)
        entry.checkpoint.update(values)
        entry.machine.set_metadata(checkpoint=dict(entry.checkpoint))
        return dict(entry.checkpoint)

    def checkpoint_of(self, task_id: str) -> Mapping[str, Any]:
        return dict(self._entry(task_id).checkpoint)

    def register_cancel_hook(self, task_id: str, hook: CancelHook) -> None:
        """Register how a cancellable operation should be told to stop.

        Only for operations that genuinely support interruption. Everything else
        is stopped by not being started again at the next checkpoint.
        """
        self._entry(task_id).cancel_hooks.append(hook)

    async def checkpoint(self, task_id: str, *, step: str = "") -> TaskState:
        """Where a running task yields: honours a pending pause or cancellation.

        Called by the runner between steps. A cancellation raises
        :class:`TaskCancelled` after moving the task to CANCELLED, and a pause
        waits here until :meth:`resume` is called — which is what keeps the
        state, the step and the checkpoint intact across the interruption.
        """
        entry = self._entry(task_id)
        machine = entry.machine
        if step:
            machine.note_step(step)
        if machine.terminal:
            return machine.state
        if machine.cancellation_requested:
            hooks = list(entry.cancel_hooks)
            machine.ensure(TaskState.RUNNING)
            for hook in hooks:
                produced = hook(task_id)
                if isawaitable(produced):
                    await produced
            machine.cancel(reason=f"cancelled at {step}" if step else "cancelled")
            raise TaskCancelled(task_id, step)
        if machine.pause_requested:
            machine.pause(reason=f"paused at {step}" if step else "paused")
            await entry.paused_event.wait()
            # Woken by a resume OR by a cancellation. A cancellation while the
            # task was parked has to STOP the runner: it may no longer start
            # the next step, and asking the machine to run on would raise a
            # transition error instead — telling the caller its state is wrong
            # rather than that it was cancelled. Nothing was in flight while it
            # was parked, so there is no cancellation hook to run here.
            if machine.cancellation_requested:
                raise TaskCancelled(task_id, step)
            if machine.terminal:
                return machine.state
        machine.start_running(current_step=step or None)
        return machine.state

    # -- commands --------------------------------------------------------------

    def pause(self, task_id: str, *, reason: str = "") -> TaskControlResult:
        entry = self._entry(task_id)
        try:
            snapshot = entry.machine.pause(reason=reason)
        except InvalidTransitionError as exc:
            return TaskControlResult(
                TaskCommand.PAUSE, task_id, False, entry.machine.state, str(exc)
            )
        return TaskControlResult(
            TaskCommand.PAUSE,
            task_id,
            True,
            snapshot.current_state,
            f"Task is holding at {snapshot.current_step or 'its current step'}; "
            "its state was kept and it will continue from there.",
            checkpoint=dict(entry.checkpoint),
        )

    def resume(self, task_id: str, *, target: TaskState | None = None) -> TaskControlResult:
        entry = self._entry(task_id)
        machine = entry.machine
        if machine.cancellation_requested:
            # A pending cancellation is the one thing a resume cannot override.
            # The task is on its way to stopping, so accepting the command would
            # promise a continuation that is not going to happen.
            return TaskControlResult(
                TaskCommand.RESUME,
                task_id,
                False,
                machine.state,
                "Task has a cancellation pending, so it was not resumed; it will stop "
                "at its next checkpoint.",
                checkpoint=dict(entry.checkpoint),
            )
        was_paused = machine.state is TaskState.PAUSED or machine.pause_requested
        try:
            machine.resume(target=target)
        except InvalidTransitionError as exc:
            return TaskControlResult(
                TaskCommand.RESUME, task_id, False, machine.state, str(exc)
            )
        # Release anyone waiting at a checkpoint. Set AFTER the state moved, so
        # a woken runner never sees itself still marked paused.
        entry.paused_event.set()
        entry.paused_event = asyncio.Event()
        if not was_paused:
            # Reported rather than dressed up as a resume: saying "resumed from
            # its checkpoint" about a task that never stopped is the kind of
            # small lie a status readout must not tell.
            return TaskControlResult(
                TaskCommand.RESUME,
                task_id,
                True,
                machine.state,
                f"Task was not paused; it is still {machine.state.value}.",
                checkpoint=dict(entry.checkpoint),
            )
        # NOT forced into RUNNING: a task paused while planning resumes into
        # planning, and the runner picks the phase back up from there.
        return TaskControlResult(
            TaskCommand.RESUME,
            task_id,
            True,
            machine.state,
            (
                f"Task resumed into {machine.state.value}"
                + (
                    f" at {entry.checkpoint['step']}."
                    if entry.checkpoint.get("step")
                    else "."
                )
            ),
            checkpoint=dict(entry.checkpoint),
        )

    def cancel(self, task_id: str, *, reason: str = "") -> TaskControlResult:
        """Request a stop, and stop now only where stopping is actually safe.

        A task that is RUNNING is asked to stop and continues until its next
        checkpoint: nothing here kills work in flight, because a half-finished
        write is worse than a finished one. A task that is already parked — 
        PENDING, or PAUSED at a checkpoint — has nothing in flight, so it moves
        to CANCELLED immediately instead of waiting for a runner that may never
        look again.
        """
        entry = self._entry(task_id)
        machine = entry.machine
        if machine.terminal:
            return TaskControlResult(
                TaskCommand.CANCEL,
                task_id,
                False,
                machine.state,
                f"Task is already {machine.state.value}, so there is nothing left to cancel.",
            )
        machine.request_cancellation()
        # Wake anything waiting at a checkpoint: it will see the request and
        # stop there, which is the point in the run where stopping is safe.
        entry.paused_event.set()
        if machine.state in (TaskState.PENDING, TaskState.PAUSED):
            machine.cancel(reason=reason or "cancelled before it could run")
            return TaskControlResult(
                TaskCommand.CANCEL,
                task_id,
                True,
                TaskState.CANCELLED,
                "Task was cancelled at a safe point; nothing was left half-done.",
                checkpoint=dict(entry.checkpoint),
            )
        return TaskControlResult(
            TaskCommand.CANCEL,
            task_id,
            True,
            machine.state,
            "Cancellation requested; the task will stop at its next safe point.",
            checkpoint=dict(entry.checkpoint),
        )

    def status(self, task_id: str) -> TaskControlResult:
        snapshot = self.snapshot(task_id)
        return TaskControlResult(
            TaskCommand.STATUS,
            task_id,
            True,
            snapshot.current_state,
            f"Task is {snapshot.current_state.value}"
            + (f" at {snapshot.current_step}" if snapshot.current_step else "")
            + ".",
            checkpoint=self.checkpoint_of(task_id),
        )

    def handle_command(self, text: str, *, task_id: str = "") -> TaskControlResult | None:
        """Apply a spoken/typed control command to a task.

        Returns None when the text is not a task command, so the caller can
        route it to whatever else the words might mean. When it IS a command,
        the result says whether it was accepted, which is what a UI reports
        instead of silently appearing to work.
        """
        command = parse_task_command(text)
        if command is None:
            return None
        target = task_id or self.active_task_id()
        if not target:
            return TaskControlResult(
                command,
                "",
                False,
                TaskState.PENDING,
                "There is no task to control.",
            )
        if command is TaskCommand.PAUSE:
            return self.pause(target)
        if command is TaskCommand.RESUME:
            return self.resume(target)
        if command is TaskCommand.CANCEL:
            return self.cancel(target)
        return self.status(target)

    # -- internals -------------------------------------------------------------

    def _entry(self, task_id: str) -> _Task:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise KeyError(f"Task is not tracked: {task_id}") from exc


__all__ = [
    "CancelHook",
    "TaskCancelled",
    "TaskCommand",
    "TaskControlResult",
    "TaskController",
    "parse_task_command",
]
