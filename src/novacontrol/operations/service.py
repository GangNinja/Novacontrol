"""The persistent-operations service: the tick, and the authority checks.

This is the layer that decides whether stored work may RUN. It owns no
execution path: it calls the executor it was given, which is the application's
own request pipeline. It owns no scheduler either: the application's ticker
calls :meth:`OperationsService.tick`, and recurring work is expressed with the
existing :class:`~novacontrol.automation.schedule.Schedule` values.

Four rules are enforced here, at the only door that can start work:

* **a level is a ceiling.** ``OBSERVE`` and ``RECOMMEND`` never execute.
* **the permission layer decides.** Every task's scope is checked with the
  application's own :class:`PermissionManager`; an unapproved irreversible action
  is refused there, not by a second risk table.
* **sensitive work waits for a person.** A task marked sensitive, or one the
  permission layer says needs confirmation, is parked ``WAITING_APPROVAL`` and
  is reported. Approval is per task and recorded with its scope.
* **an unverified run is a failure.** The executor's ``verified`` flag is
  required to complete a task; a call that returned is not a result.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping

from novacontrol.operations.learning import OperationsLearning
from novacontrol.operations.manager import OperationsManager
from novacontrol.operations.models import (
    AutonomyLevel,
    Milestone,
    NotificationKind,
    Objective,
    OperationTask,
    TaskOutcome,
    TaskState,
    utcnow,
)
from novacontrol.operations.monitor import NotificationCenter, OperationsMonitor
from novacontrol.reliability.permissions import PermissionManager

#: The scope a task with no more specific declaration runs under. The
#: application declares it beside the code that performs the work.
DEFAULT_TASK_SCOPE = "operations.task.run"

Executor = Callable[[Any], Awaitable[TaskOutcome]]
Reassessor = Callable[[Any], Awaitable["tuple[bool, str]"]]
Publisher = Callable[..., None]
Persister = Callable[[], None]


class OperationsService:
    """Objective lifecycle, autonomous ticks, monitoring and notification."""

    def __init__(
        self,
        manager: OperationsManager | None = None,
        *,
        notifications: NotificationCenter | None = None,
        monitor: OperationsMonitor | None = None,
        permissions: PermissionManager | None = None,
        execute_task: Executor | None = None,
        reassess_task: Reassessor | None = None,
        publish: Publisher | None = None,
        persist: Persister | None = None,
        enabled: bool = True,
        max_tasks_per_tick: int = 3,
    ) -> None:
        self.manager = manager or OperationsManager()
        self.notifications = notifications or NotificationCenter()
        self.monitor = monitor or OperationsMonitor()
        self.permissions = permissions
        self._execute_task = execute_task
        self._reassess_task = reassess_task
        self._publish = publish
        self._persist = persist
        self.enabled = bool(enabled)
        self.max_tasks_per_tick = max(1, int(max_tasks_per_tick))
        # Phase 27 integration: an observer of what really happened, never a
        # second planner. Its output is confidence-labelled and advisory.
        self.learning = OperationsLearning()
        self.ticks = 0
        self.last_tick_at: datetime | None = None
        self.last_signals: tuple[Any, ...] = ()

    # -- configuration ---------------------------------------------------------

    def subscribe(
        self,
        *,
        execute_task: Executor | None = None,
        reassess_task: Reassessor | None = None,
        publish: Publisher | None = None,
        persist: Persister | None = None,
    ) -> None:
        """Wire the application seams after construction (boot order)."""
        if execute_task is not None:
            self._execute_task = execute_task
        if reassess_task is not None:
            self._reassess_task = reassess_task
        if publish is not None:
            self._publish = publish
        if persist is not None:
            self._persist = persist

    # -- objective surfaces ----------------------------------------------------

    def create_objective(self, *args: Any, **kwargs: Any) -> Objective:
        objective = self.manager.create_objective(*args, **kwargs)
        self._announce_created(objective)
        self._save()
        return objective

    def update_objective(self, objective_id: str, **changes: Any) -> Objective:
        objective = self.manager.update_objective(objective_id, **changes)
        self._emit("operations.objective_updated", objective_id=objective.id, status=objective.status.value)
        self._save()
        return objective

    def add_task(
        self, objective_id: str, *args: Any, **kwargs: Any
    ) -> tuple[Objective, OperationTask]:
        objective, task = self.manager.add_task(objective_id, *args, **kwargs)
        self._emit(
            "operations.task_added",
            objective_id=objective.id,
            task_id=task.id,
            state=task.state.value,
        )
        self._save()
        return objective, task

    def add_milestone(
        self, objective_id: str, *args: Any, **kwargs: Any
    ) -> tuple[Objective, Milestone]:
        objective, milestone = self.manager.add_milestone(objective_id, *args, **kwargs)
        self._emit(
            "operations.milestone_added",
            objective_id=objective.id,
            milestone_id=milestone.id,
        )
        self._save()
        return objective, milestone

    def pause_objective(self, objective_id: str, *, reason: str = "") -> Objective:
        objective = self.manager.pause_objective(objective_id, reason=reason)
        self._emit(
            "operations.objective_paused",
            objective_id=objective.id,
            reason=objective.paused_reason,
        )
        self._save()
        return objective

    def resume_objective(self, objective_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        objective, review = self.manager.resume_objective(objective_id, now=now)
        self._emit(
            "operations.objective_resumed",
            objective_id=objective.id,
            needs_reassessment=len(review),
        )
        self.notifications.notify(
            NotificationKind.OBJECTIVE_RESUMED,
            "Objective resumed",
            (
                f"{objective.title!r} is active again."
                + (
                    f" {len(review)} interrupted task(s) will be re-assessed before "
                    "they run again."
                    if review
                    else ""
                )
            ),
            objective_id=objective.id,
            dedupe_key=f"resumed:{objective.id}:{len(objective.tasks)}",
        )
        self._save()
        return {
            **objective.to_dict(),
            "needs_reassessment": list(review),
        }

    def cancel_objective(self, objective_id: str, *, reason: str = "") -> Objective:
        objective = self.manager.cancel_objective(objective_id, reason=reason)
        self._emit(
            "operations.objective_cancelled",
            objective_id=objective.id,
            reason=reason,
        )
        self._save()
        return objective

    def delete_objective(self, objective_id: str) -> Objective:
        objective = self.manager.delete_objective(objective_id)
        self._save()
        return objective

    def decide_task(
        self,
        objective_id: str,
        task_id: str,
        *,
        action: str,
        by: str = "operator",
        reason: str = "",
        now: datetime | None = None,
    ) -> OperationTask:
        if action == "approve":
            task = self.manager.approve_task(objective_id, task_id, by=by, now=now)
            self._emit(
                "operations.task_approved",
                objective_id=objective_id,
                task_id=task.id,
                approved_by=by,
                scope=task.scope,
            )
        elif action == "reject":
            task = self.manager.reject_task(
                objective_id, task_id, reason=reason, now=now
            )
            self._emit(
                "operations.task_rejected",
                objective_id=objective_id,
                task_id=task.id,
                reason=reason,
            )
        elif action == "retry":
            task = self.manager.retry_task(objective_id, task_id, now=now)
            self._emit(
                "operations.task_retried",
                objective_id=objective_id,
                task_id=task.id,
            )
        elif action == "cancel":
            task = self.manager.cancel_task(objective_id, task_id, now=now)
            self._emit(
                "operations.task_cancelled",
                objective_id=objective_id,
                task_id=task.id,
            )
        else:
            raise ValueError(f"Unknown task action: {action}")
        self._save()
        return task

    # -- execution -------------------------------------------------------------

    def autonomy_allows(self, objective: Any, task: Any) -> tuple[bool, str]:
        """Whether this objective's level permits this task, and why not."""
        level = AutonomyLevel(objective.autonomy)
        if level is AutonomyLevel.OBSERVE:
            return False, "the objective is set to observe only"
        if level is AutonomyLevel.RECOMMEND:
            return False, "the objective is set to recommend only"
        # Sensitivity ALWAYS asks a person, at every executing level: a level is
        # a ceiling on what runs by itself, never a standing grant for an
        # irreversible action.
        if task.sensitive and not task.approved:
            return False, "the task is sensitive and has not been approved"
        if self.permissions is None:
            return False, (
                "no permission layer is wired, so no task is started: \"no gate\" "
                "is not permission"
            )
        decision = self.permissions.check(action=task.scope, approved=task.approved)
        if not decision.allow:
            return False, decision.reason
        return True, decision.reason

    async def run_task(self, objective_id: str, task_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Run one task under the objective's autonomy and the permission layer."""
        clock = now or utcnow()
        objective = self.manager.refresh(objective_id, now=clock)
        task = objective.task(task_id)
        level = AutonomyLevel(objective.autonomy)
        if not level.runs_work:
            # Observe and recommend are not weaker ways of running work: they are
            # answers in their own right, and they leave the task untouched.
            return {
                "status": (
                    "observed"
                    if level is AutonomyLevel.OBSERVE
                    else "recommended"
                ),
                "reason": (
                    "the objective is set to observe only"
                    if level is AutonomyLevel.OBSERVE
                    else "the objective is set to recommend only"
                ),
                "task": task.to_dict(),
            }
        if task.state is not TaskState.READY:
            # A direct call must not rewrite work that is already running,
            # waiting for a decision or closed. The state machine is the guard;
            # this is the service refusing before it reaches it.
            return {
                "status": "refused",
                "reason": f"the task is {task.state.value}, so it cannot start",
                "task": task.to_dict(),
            }

        if self.permissions is None:
            blocked = self.manager.block_task(
                objective_id,
                task_id,
                reason=(
                    "no permission layer is wired, so no task is started: \"no gate\" "
                    "is not permission"
                ),
                now=clock,
            )
            self._emit(
                "operations.task_blocked",
                objective_id=objective_id,
                task_id=task_id,
                reason=blocked.error,
            )
            self._save()
            return {
                "status": "blocked",
                "reason": blocked.error,
                "task": blocked.to_dict(),
            }

        allowed, reason = self.autonomy_allows(objective, task)
        if not allowed:
            if task.approved:
                # The person already approved THIS task, but the permission
                # layer still refuses it. Parking it again would ask for the
                # same decision forever, so it is blocked with the real reason.
                blocked = self.manager.block_task(
                    objective_id,
                    task_id,
                    reason=f"approval did not satisfy the permission layer: {reason}",
                    now=clock,
                )
                self._emit(
                    "operations.task_blocked",
                    objective_id=objective_id,
                    task_id=task_id,
                    reason=blocked.error,
                )
                self._save()
                return {
                    "status": "blocked",
                    "reason": blocked.error,
                    "task": blocked.to_dict(),
                }
            waiting = self.manager.wait_for_approval(
                objective_id, task_id, reason=reason, now=clock
            )
            self._emit(
                "operations.task_waiting_approval",
                objective_id=objective_id,
                task_id=task_id,
                reason=reason,
            )
            self._save()
            return {"status": "waiting_approval", "task": waiting.to_dict(), "reason": reason}

        if self._execute_task is None:
            self.manager.block_task(
                objective_id,
                task_id,
                reason="no executor is wired, so nothing was started",
                now=clock,
            )
            self._save()
            return {
                "status": "blocked",
                "task": self.manager.get(objective_id).task(task_id).to_dict(),
                "reason": "no executor is wired",
            }

        needs_reassessment = bool(task.metadata.get("resume_required"))
        if needs_reassessment:
            if self._reassess_task is None:
                self.manager.block_task(
                    objective_id,
                    task_id,
                    reason=(
                        "the task was interrupted and this build cannot re-assess its "
                        "conditions, so it was not replayed"
                    ),
                    now=clock,
                )
                self._save()
                return {
                    "status": "blocked",
                    "reason": "resume requires a reassessment hook",
                    "task": self.manager.get(objective_id).task(task_id).to_dict(),
                }
            still_valid, why = await self._reassess_task(task)
            if not still_valid:
                blocked = self.manager.block_task(
                    objective_id, task_id, reason=f"conditions changed: {why}", now=clock
                )
                self._emit(
                    "operations.task_blocked",
                    objective_id=objective_id,
                    task_id=task_id,
                    reason=blocked.error,
                )
                self._save()
                return {
                    "status": "blocked",
                    "reason": why,
                    "task": blocked.to_dict(),
                }

        try:
            running = self.manager.begin_task(
                objective_id,
                task_id,
                reassessed=needs_reassessment,
                now=clock,
            )
        except ValueError as exc:
            return {
                "status": "refused",
                "reason": str(exc),
                "task": self.manager.get(objective_id).task(task_id).to_dict(),
            }
        self._emit(
            "operations.task_started",
            objective_id=objective_id,
            task_id=task_id,
            attempt=running.attempts,
        )
        try:
            outcome = await self._execute_task(running)
        except Exception as exc:  # an executor that raised is a failed task
            outcome = TaskOutcome(
                verified=False,
                error=f"{type(exc).__name__}: {exc}",
                detail=f"{type(exc).__name__}: {exc}",
            )

        if outcome.needs_approval:
            waiting = self.manager.wait_for_approval(
                objective_id,
                task_id,
                reason=outcome.detail or "the action needs approval",
                now=clock,
            )
            self._emit(
                "operations.task_waiting_approval",
                objective_id=objective_id,
                task_id=task_id,
                reason=waiting.error,
            )
            self._save()
            return {
                "status": "waiting_approval",
                "reason": waiting.error,
                "task": waiting.to_dict(),
            }

        if not outcome.verified:
            failed = self.manager.fail_task(
                objective_id,
                task_id,
                error=outcome.error or outcome.detail or "the outcome was not verified",
                now=clock,
            )
            self._emit(
                "operations.task_failed",
                objective_id=objective_id,
                task_id=task_id,
                error=failed.error,
                attempt=failed.attempts,
            )
            self._save()
            return {"status": "failed", "task": failed.to_dict(), "reason": failed.error}

        # Record the steps the executor VERIFIED before closing the task. This is
        # the checkpoint a later resume trusts, so it is written from evidence,
        # never from the plan.
        for step in outcome.steps:
            try:
                self.manager.record_step(
                    objective_id,
                    task_id,
                    step,
                    verified=True,
                    evidence=f"verified by executor: {step}",
                    now=clock,
                )
            except ValueError:
                continue
        try:
            completed, spawned = self.manager.complete_task(
                objective_id,
                task_id,
                verified=True,
                result=outcome.detail,
                evidence=outcome.evidence,
                # The tick's clock decides the next occurrence, so a simulated
                # tick and a live one schedule from the same instant the work
                # was accounted against.
                now=clock,
            )
        except ValueError as exc:
            failed = self.manager.fail_task(
                objective_id, task_id, error=str(exc), now=clock
            )
            self._save()
            return {"status": "failed", "task": failed.to_dict(), "reason": str(exc)}

        self._emit(
            "operations.task_completed",
            objective_id=objective_id,
            task_id=task_id,
            verified=True,
            spawned_task_id=spawned.id if spawned else "",
        )
        refreshed = self.manager.get(objective_id)
        self._save()
        return {
            "status": "completed",
            "task": completed.to_dict(),
            "spawned": spawned.to_dict() if spawned else None,
            "objective": refreshed.to_dict(),
        }

    async def run_objective(self, objective_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Execute the ready work of one objective, bounded per call.

        A call runs at most ``max_tasks_per_tick`` tasks so a large objective
        cannot occupy the event loop indefinitely; the next tick continues.
        """
        clock = now or utcnow()
        results: list[dict[str, Any]] = []
        # Follow the dependency chain: completing one task can make the next one
        # ready, so the loop re-reads eligibility after every run instead of
        # taking one snapshot and stopping halfway down a project.
        for _ in range(self.max_tasks_per_tick):
            self.manager.refresh(objective_id, now=clock)
            ready = self.manager.ready_tasks(objective_id)
            if not ready:
                break
            results.append(await self.run_task(objective_id, ready[0].id, now=clock))
        return {
            "objective_id": objective_id,
            "ran": len(results),
            "results": results,
            "objective": self.manager.get(objective_id).to_dict(),
        }

    async def tick(self, *, now: datetime | None = None) -> dict[str, Any]:
        """One autonomous pass: refresh, run eligible work, monitor, notify."""
        clock = now or utcnow()
        self.ticks += 1
        self.last_tick_at = clock
        if not self.enabled:
            return {"enabled": False, "ran": 0, "notifications": 0, "signals": []}

        ran: list[dict[str, Any]] = []
        for objective in self.manager.active_objectives():
            self.manager.refresh(objective.id, now=clock)
            current = self.manager.get(objective.id)
            if current.status.value != "active":
                continue
            if not AutonomyLevel(current.autonomy).runs_work:
                continue
            ready = self.manager.ready_tasks(objective.id)[: self.max_tasks_per_tick]
            for task in ready:
                ran.append(await self.run_task(objective.id, task.id, now=clock))

        objectives = self.manager.objectives()
        signals = self.monitor.scan(objectives, now=clock)
        self.last_signals = signals
        stored = self.monitor.notifications_for(signals, self.notifications, now=clock)
        for notification in stored:
            self._emit(
                "operations.notification",
                notification_id=notification.id,
                kind=notification.kind.value,
                objective_id=notification.objective_id,
                task_id=notification.task_id,
            )
        self._save()
        return {
            "enabled": True,
            "ran": len(ran),
            "results": ran,
            "notifications": len(stored),
            "signals": [signal.to_dict() for signal in signals],
        }

    # -- reading ---------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "ticks": self.ticks,
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else "",
            "report": self.manager.report(),
            "notifications": self.notifications.report(),
            "objectives": [objective.to_dict() for objective in self.manager.objectives()],
            "monitoring": [signal.to_dict() for signal in self.last_signals],
            "learning": self.learning.summarise(self.manager.objectives()),
            "default_task_scope": DEFAULT_TASK_SCOPE,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "objectives": self.manager.to_dict()["objectives"],
            "notifications": self.notifications.to_dict(),
            "enabled": self.enabled,
            "ticks": self.ticks,
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any] | None,
        **kwargs: Any,
    ) -> OperationsService:
        data = payload or {}
        manager = OperationsManager.from_dict({"objectives": data.get("objectives", ())})
        notifications = NotificationCenter.from_dict(data.get("notifications"))
        # The snapshot's own switch wins when it has one; otherwise the caller's
        # configuration does. Either way ``enabled`` is passed once.
        stored_enabled = data.get("enabled")
        if stored_enabled is not None:
            kwargs.pop("enabled", None)
        service = cls(
            manager,
            notifications=notifications,
            enabled=bool(
                stored_enabled if stored_enabled is not None else kwargs.pop("enabled", True)
            ),
            **kwargs,
        )
        service.ticks = int(data.get("ticks", 0))
        return service

    # -- internals -------------------------------------------------------------

    def _save(self) -> None:
        if self._persist is None:
            return
        try:
            self._persist()
        except Exception:
            # Persistence is the application's bootstrap store, which already
            # refuses to raise; a failure here must not fail the work that made
            # the state, or a successful task would be reported as failed.
            return

    def _emit(self, type_: str, **payload: Any) -> None:
        if self._publish is None:
            return
        try:
            self._publish(type_, **payload)
        except Exception:
            return

    def _announce_created(self, objective: Any) -> None:
        self._emit(
            "operations.objective_created",
            objective_id=objective.id,
            kind=objective.kind.value,
            autonomy=objective.autonomy.value,
            tasks=len(objective.tasks),
        )
