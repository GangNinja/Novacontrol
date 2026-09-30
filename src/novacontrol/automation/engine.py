"""The automation engine: what runs later, and under whose authority.

This module owns WHEN a stored request runs. It deliberately owns nothing about
WHAT that request does — it holds the request text, a schedule, and the state of
scheduling it, and hands the text to a runner it is given. In this application
that runner is ``handle_request``, so a scheduled task is carried out by exactly
the same pipeline as a spoken one: intent detection, the decision engine, the
planner, tool selection, the permission layer, execution and verification. There
is no second execution path here, and no way for this module to perform an
action itself: it can only ask the runner.

Three rules make it safe to leave running:

  * **nothing runs without a verdict.** Every run is put through the permission
    layer first (``automation.run``, declared HIGH and confirmation-requiring by
    the application), so a task that was never approved is refused with a reason
    rather than executed because a clock reached a time. With no permission
    layer wired the answer is the same, because "no gate" is not permission.
  * **only approved tasks are armed.** Creating a task that needs approval
    stores it ``PENDING_APPROVAL`` and NOT enabled, ``enable()`` refuses to arm
    one that was never approved, and approval itself is a separate call — arming
    is not a way to approve.
  * **failures are counted.** A failed run increments the task's failure count,
    and the task disables itself once the configured limit is reached, so a
    broken automation stops announcing itself instead of failing every hour
    forever.

A denial and an unmet condition are :class:`AutomationRunStatus` values that are
NOT failures: neither is the work's fault, and counting either would disable a
healthy automation for something it did not cause. Neither one re-arms the task
on its own either — a refused run disarms it, because a scheduler that keeps
asking is a scheduler whose audit trail fills with the same refusal.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from novacontrol.automation.models import (
    AutomationCondition,
    AutomationKind,
    AutomationRun,
    AutomationRunStatus,
    AutomationStatus,
    AutomationTask,
    ConditionKind,
)
from novacontrol.automation.schedule import Schedule, ScheduleKind
from novacontrol.core.events import EventType
from novacontrol.reliability.permissions import PermissionDecision, PermissionManager

#: The action scope one automation RUN is authorized under. Declared by the
#: application beside the code that carries runs out, so the verdict a task was
#: refused under is a named, auditable thing rather than a bare boolean.
AUTOMATION_RUN_SCOPE = "automation.run"

#: Runs kept per task. Bounded because a recurring task's history would
#: otherwise grow without limit inside the persisted state.
DEFAULT_RUN_HISTORY = 10


@dataclass(frozen=True, slots=True)
class AutomationOutcome:
    """What a runner reports about one attempt — never a bare success flag.

    ``verified`` is separate from ``status`` on purpose: a run can finish without
    proving anything, and the phase's whole point is that "the call returned" is
    not success.
    """

    status: AutomationRunStatus
    detail: str = ""
    verified: bool = False
    correlation_id: str = ""
    failure_kind: str = ""
    recovered: bool = False
    permission: Mapping[str, Any] = field(default_factory=dict)


#: Carries out one due task and reports what happened. Injected, so the engine
#: never learns how work is done.
AutomationRunner = Callable[[AutomationTask], Awaitable[AutomationOutcome]]

#: Evaluates a named condition: (met, why). Injected for the same reason.
ConditionEvaluator = Callable[[AutomationCondition], "tuple[bool, str]"]

#: Publishes one lifecycle event. The signature matches the application's
#: synchronous announce seam, which validates the payload where the mistake is.
Publisher = Callable[..., None]


class AutomationEngine:
    """Stores scheduled requests and runs the ones that are due."""

    def __init__(
        self,
        *,
        permissions: PermissionManager | None = None,
        evaluate_condition: ConditionEvaluator | None = None,
        publish: Publisher | None = None,
        max_failures: int = 3,
        run_history: int = DEFAULT_RUN_HISTORY,
        tasks: Sequence[AutomationTask] = (),
    ) -> None:
        if max_failures < 1:
            raise ValueError("An automation must be allowed at least one failure.")
        self._tasks: dict[str, AutomationTask] = {}
        self.permissions = permissions
        self._evaluate_condition = evaluate_condition
        self._publish = publish
        self.max_failures = max_failures
        self.run_history = max(1, run_history)
        for task in tasks:
            self._tasks[task.id] = task

    # -- state -----------------------------------------------------------------

    def tasks(self) -> tuple[AutomationTask, ...]:
        """Every stored automation, oldest first (stable across a restart)."""
        return tuple(
            sorted(self._tasks.values(), key=lambda task: (task.created_at, task.id))
        )

    def get(self, automation_id: str) -> AutomationTask:
        try:
            return self._tasks[str(automation_id)]
        except KeyError as exc:
            raise KeyError(f"Automation not found: {automation_id}") from exc

    def find(self, needle: str) -> tuple[AutomationTask, ...]:
        """Tasks matching an id, a name, or a fragment of either — for a CLI.

        Ambiguity is returned rather than resolved: picking the first match of a
        vague phrase is how the wrong automation gets cancelled.
        """
        text = str(needle or "").strip().lower()
        if not text:
            return ()
        return tuple(
            task
            for task in self.tasks()
            if text == task.id.lower()
            or text == task.name.lower()
            or text in task.name.lower()
            or text in task.request.lower()
        )

    def due(self, now: datetime | None = None) -> tuple[AutomationTask, ...]:
        """The armed tasks whose next execution has arrived.

        A task that is pending approval, disarmed, cancelled or completed is
        never due — the state on the task is the whole question, which is why
        "why didn't it run?" has an answer in the record.
        """
        clock = now or datetime.now(UTC)
        return tuple(
            task
            for task in self.tasks()
            if task.enabled
            and task.status is AutomationStatus.SCHEDULED
            and task.next_run_at is not None
            and task.next_run_at <= clock
        )

    # -- lifecycle -------------------------------------------------------------

    def create(
        self,
        request: str,
        *,
        schedule: Schedule,
        name: str = "",
        condition: AutomationCondition | None = None,
        permissions: Sequence[str] = (),
        requires_approval: bool = True,
        approved: bool = False,
        approved_by: str = "",
        metadata: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> AutomationTask:
        """Store a request plus its schedule. Runs NOTHING.

        The one thing creation refuses is a schedule with no future occurrence —
        a one-time task whose instant has already passed. Storing it would be
        storing a task that can never run, and reporting it as scheduled would be
        a lie an operator only discovers by waiting.
        """
        clock = now or datetime.now(UTC)
        stated = condition or AutomationCondition()
        if schedule.kind is ScheduleKind.ONCE and schedule.next_after(clock) is None:
            raise ValueError(
                "That time has already passed, so there is no occurrence to schedule."
            )
        kind = (
            AutomationKind.CONDITIONAL
            if stated.kind is not ConditionKind.ALWAYS
            else (
                AutomationKind.ONCE
                if schedule.kind is ScheduleKind.ONCE
                else AutomationKind.RECURRING
            )
        )
        armed = approved or not requires_approval
        task = AutomationTask(
            request=request,
            schedule=schedule,
            name=name,
            kind=kind,
            status=(
                AutomationStatus.SCHEDULED if armed else AutomationStatus.PENDING_APPROVAL
            ),
            enabled=armed,
            next_run_at=schedule.next_after(clock),
            permissions=tuple(str(scope) for scope in permissions),
            requires_approval=requires_approval,
            approved=approved,
            approved_by=approved_by,
            condition=stated,
            created_at=clock,
            metadata=dict(metadata or {}),
        )
        self._tasks[task.id] = task
        self._emit(
            EventType.AUTOMATION_CREATED,
            automation_id=task.id,
            status=task.status.value,
            schedule=task.schedule.describe(),
            next_run_at=_iso(task.next_run_at),
        )
        return task

    def approve(
        self, automation_id: str, *, by: str = "operator", now: datetime | None = None
    ) -> AutomationTask:
        """Authorize a pending task. This is the ONLY way it becomes armed.

        A one-time task whose instant passed while it waited for approval is
        armed to run at the next tick rather than refused: the operator's
        approval is the decision that was missing, and the missed instant is the
        consequence of waiting for it.
        """
        clock = now or datetime.now(UTC)
        task = self.get(automation_id)
        updated = task.with_(
            approved=True,
            approved_by=by,
            enabled=True,
            status=AutomationStatus.SCHEDULED,
            next_run_at=self._next_run(task, clock),
        )
        return self._store(updated, EventType.AUTOMATION_UPDATED, detail="approved")

    def cancel(self, automation_id: str) -> AutomationTask:
        """Stop a task. Cancelled means it never runs again, this process or next."""
        task = self.get(automation_id)
        if task.status is AutomationStatus.CANCELLED:
            return task
        updated = task.with_(
            enabled=False, status=AutomationStatus.CANCELLED, next_run_at=None
        )
        return self._store(updated, EventType.AUTOMATION_CANCELLED)

    def enable(self, automation_id: str, *, now: datetime | None = None) -> AutomationTask:
        """Arm a task again — unless it was never approved, which is not arming."""
        clock = now or datetime.now(UTC)
        task = self.get(automation_id)
        if task.requires_approval and not task.approved:
            raise PermissionError(
                "This automation was never approved, so it cannot be enabled; "
                "approve it first."
            )
        if task.status is AutomationStatus.COMPLETED:
            raise ValueError("This automation has already completed its one-time run.")
        updated = task.with_(
            enabled=True,
            status=AutomationStatus.SCHEDULED,
            next_run_at=self._next_run(task, clock),
        )
        return self._store(updated, EventType.AUTOMATION_UPDATED, detail="enabled")

    def disable(self, automation_id: str) -> AutomationTask:
        task = self.get(automation_id)
        updated = task.with_(enabled=False, status=AutomationStatus.DISABLED)
        return self._store(updated, EventType.AUTOMATION_UPDATED, detail="disabled")

    def delete(self, automation_id: str) -> bool:
        """Forget a task entirely. Returns whether one was there to forget."""
        return self._tasks.pop(str(automation_id), None) is not None

    # -- running ---------------------------------------------------------------

    async def run_due(
        self, runner: AutomationRunner, *, now: datetime | None = None
    ) -> tuple[AutomationRun, ...]:
        """Run every task that is due, oldest schedule first.

        Only ONE run happens per due task per tick, however long the machine was
        asleep: the schedule advances to the next future occurrence instead of
        replaying the ones that were missed, because a backlog of stale runs is a
        stampede wearing a schedule's clothes.
        """
        clock = now or datetime.now(UTC)
        records: list[AutomationRun] = []
        for task in self.due(clock):
            records.append(await self._run_one(task, runner, now=clock))
        return tuple(records)

    async def run_now(
        self, automation_id: str, runner: AutomationRunner, *, now: datetime | None = None
    ) -> AutomationRun:
        """Run one task on demand, under exactly the same permission gate.

        A manual run is not an approval: a caller cannot get work out of an
        unapproved task by asking now instead of waiting.
        """
        clock = now or datetime.now(UTC)
        return await self._run_one(self.get(automation_id), runner, now=clock)

    async def _run_one(
        self, task: AutomationTask, runner: AutomationRunner, *, now: datetime
    ) -> AutomationRun:
        decision = self.authorize(task)
        if decision is None or not decision.allow:
            reason = decision.reason if decision is not None else self.no_gate_reason()
            record = self._record(
                task,
                AutomationRun(
                    status=AutomationRunStatus.DENIED,
                    started_at=now,
                    finished_at=now,
                    detail=reason,
                    permission=decision.to_dict() if decision is not None else {},
                ),
                now=now,
            )
            self._emit(
                EventType.AUTOMATION_DENIED, automation_id=task.id, reason=reason
            )
            return record

        met, why = self._condition_met(task)
        if not met:
            record = self._record(
                task,
                AutomationRun(
                    status=AutomationRunStatus.SKIPPED,
                    started_at=now,
                    finished_at=now,
                    detail=why,
                    permission=decision.to_dict(),
                ),
                now=now,
            )
            self._emit(EventType.AUTOMATION_SKIPPED, automation_id=task.id, reason=why)
            return record

        running = task.with_(status=AutomationStatus.RUNNING)
        self._tasks[running.id] = running
        self._emit(EventType.AUTOMATION_STARTED, automation_id=task.id, run_at=_iso(now))
        started = datetime.now(UTC)
        if started < now:
            started = now
        try:
            outcome = await runner(running)
        except Exception as exc:  # a runner that raised is a failed run, recorded
            outcome = AutomationOutcome(
                status=AutomationRunStatus.FAILED,
                detail=f"{type(exc).__name__}: {exc}",
                failure_kind=type(exc).__name__,
            )
        finished = datetime.now(UTC)
        if finished < started:
            finished = started
        record = AutomationRun(
            status=outcome.status,
            started_at=started,
            finished_at=finished,
            detail=outcome.detail,
            verified=outcome.verified,
            correlation_id=outcome.correlation_id,
            failure_kind=outcome.failure_kind,
            recovered=outcome.recovered,
            permission=dict(outcome.permission) if outcome.permission else decision.to_dict(),
        )
        stored = self._record(running, record, now=now, executed=True)
        if stored.status is AutomationRunStatus.FAILED:
            self._emit(
                EventType.AUTOMATION_FAILED,
                automation_id=task.id,
                run_id=stored.id,
                error=stored.detail,
                failures=self.get(task.id).failure_count,
            )
        else:
            self._emit(
                EventType.AUTOMATION_COMPLETED,
                automation_id=task.id,
                run_id=stored.id,
                status=stored.status.value,
            )
        return stored

    # -- authority -------------------------------------------------------------

    def authorize(self, task: AutomationTask) -> PermissionDecision | None:
        """Ask the permission layer whether THIS task may run now.

        Returns ``None`` only when no permission layer is wired — and that is a
        refusal too (see :meth:`_run_one`), because the engine will not run work
        it cannot show was allowed. A task that requires no approval is
        authorized by construction; one that does is refused until it is
        approved, checked here at the point of running and not only at the point
        of arming, because stored state can be edited.
        """
        if self.permissions is None:
            return None
        return self.permissions.check(
            action=AUTOMATION_RUN_SCOPE,
            approved=bool(task.approved or not task.requires_approval),
        )

    @staticmethod
    def no_gate_reason() -> str:
        return (
            "No permission layer is wired, so this automation was not run: "
            '"no gate" is not permission.'
        )

    def _condition_met(self, task: AutomationTask) -> tuple[bool, str]:
        if task.condition.kind is ConditionKind.ALWAYS:
            return True, "no condition"
        if self._evaluate_condition is None:
            return False, (
                f"Nothing in this build evaluates the condition "
                f"{task.condition.describe()!r}, so the run was skipped rather "
                "than attempted."
            )
        return self._evaluate_condition(task.condition)

    # -- bookkeeping -----------------------------------------------------------

    def _next_run(self, task: AutomationTask, clock: datetime) -> datetime:
        """The next occurrence to store when a task is armed or re-armed.

        A future occurrence wins; a one-time task whose instant has passed fires
        at the next tick, because arming it IS the decision that was missing and
        deferring it by a whole recursion would be a second wait for a decision
        already made.
        """
        upcoming = task.schedule.next_after(clock)
        if upcoming is not None:
            return upcoming
        return clock

    def _record(
        self,
        task: AutomationTask,
        record: AutomationRun,
        *,
        now: datetime,
        executed: bool = False,
    ) -> AutomationRun:
        """Fold one run into the task's state, and store it.

        This is where the phase's state fields are maintained, and where the
        difference between the four outcomes shows:

        * a COMPLETED recurring task stays armed for its next occurrence; a
          COMPLETED one-time task is finished and disarmed;
        * a FAILED recurring task stays armed while it is under the failure
          limit and disarms itself at it; a FAILED one-time task has consumed
          its instant either way, so it is disarmed rather than replayed;
        * a DENIED or SKIPPED run disarms the task and does NOT count as a
          failure — the work never ran, so it did not fail.
        """
        failures = task.failure_count + (1 if record.status is AutomationRunStatus.FAILED else 0)
        once = task.schedule.kind is ScheduleKind.ONCE
        upcoming = None if once else task.schedule.next_after(now)

        if record.status is AutomationRunStatus.FAILED and failures >= self.max_failures:
            status, armed, next_run = AutomationStatus.FAILED, False, None
            detail = (
                f"{record.detail} (disabled after {failures} failed runs)"
                if record.detail
                else f"disabled after {failures} failed runs"
            )
        elif record.status is AutomationRunStatus.COMPLETED:
            status = AutomationStatus.COMPLETED if once else AutomationStatus.SCHEDULED
            armed = not once and task.enabled
            next_run = None if once else upcoming
            detail = record.detail
        elif record.status is AutomationRunStatus.DENIED:
            # A refusal is not a failure, and it is not something to repeat on a
            # timer either: the task is disarmed and waits for a person.
            armed = False
            next_run = None if once else upcoming
            # Waiting for approval is a state a person can act on; a refusal of
            # a task that WAS approved is a policy the operator should look at.
            status = (
                AutomationStatus.PENDING_APPROVAL
                if task.requires_approval and not task.approved
                else AutomationStatus.DISABLED
            )
            detail = record.detail
        else:  # SKIPPED
            armed = not once and task.enabled
            next_run = None if once else upcoming
            status = AutomationStatus.DISABLED if once else AutomationStatus.SCHEDULED
            detail = record.detail

        stored_record = replace(record, detail=detail)
        updated = task.with_(
            status=status,
            enabled=armed,
            next_run_at=next_run,
            last_run_at=now if executed else task.last_run_at,
            failure_count=failures,
            run_count=task.run_count + (1 if executed else 0),
            last_outcome=stored_record.status.value,
            runs=(task.runs + (stored_record,))[-self.run_history :],
        )
        self._tasks[updated.id] = updated
        return stored_record

    def _store(
        self, task: AutomationTask, type_: EventType, *, detail: str = ""
    ) -> AutomationTask:
        self._tasks[task.id] = task
        self._emit(
            type_,
            automation_id=task.id,
            status=task.status.value,
            enabled=task.enabled,
            next_run_at=_iso(task.next_run_at),
            detail=detail,
        )
        return task

    def _emit(self, type_: EventType, **payload: Any) -> None:
        if self._publish is None:
            return
        self._publish(type_, **payload)

    # -- persistence -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"tasks": [task.to_dict() for task in self.tasks()]}

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any] | None,
        *,
        permissions: PermissionManager | None = None,
        evaluate_condition: ConditionEvaluator | None = None,
        publish: Publisher | None = None,
        max_failures: int = 3,
        run_history: int = DEFAULT_RUN_HISTORY,
    ) -> AutomationEngine:
        tasks: list[AutomationTask] = []
        for item in (payload or {}).get("tasks", ()):
            if isinstance(item, Mapping):
                try:
                    tasks.append(AutomationTask.from_dict(item))
                except (KeyError, ValueError, TypeError):
                    # A record this build cannot read is skipped rather than
                    # crashing the boot of everything else: one damaged task
                    # must not take the scheduler down with it.
                    continue
        return cls(
            permissions=permissions,
            evaluate_condition=evaluate_condition,
            publish=publish,
            max_failures=max_failures,
            run_history=run_history,
            tasks=tasks,
        )

    def report(self) -> dict[str, Any]:
        """A read surface: what is stored, and what happens next."""
        tasks = self.tasks()
        upcoming = min(
            (
                task.next_run_at
                for task in tasks
                if task.enabled and task.next_run_at is not None
            ),
            default=None,
        )
        return {
            "total": len(tasks),
            "enabled": sum(1 for task in tasks if task.enabled),
            "pending_approval": sum(
                1 for task in tasks if task.status is AutomationStatus.PENDING_APPROVAL
            ),
            "failed": sum(1 for task in tasks if task.status is AutomationStatus.FAILED),
            "cancelled": sum(
                1 for task in tasks if task.status is AutomationStatus.CANCELLED
            ),
            "max_failures": self.max_failures,
            "run_scope": AUTOMATION_RUN_SCOPE,
            "next_due": upcoming.isoformat() if upcoming is not None else None,
        }


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""
