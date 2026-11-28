"""Phase 30 — persistent autonomy and personal operations.

These tests exercise the state machine and the service directly, with a real
:class:`PermissionManager` (not a stub) so the authority checks are the same ones
production uses. The application and HTTP wiring is covered separately by the
live verification in ``docs/PHASE30_REPORT.md``.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta

from novacontrol.automation.schedule import Schedule, ScheduleKind
from novacontrol.operations import (
    DEFAULT_TASK_SCOPE,
    AutonomyLevel,
    NotificationCenter,
    NotificationKind,
    ObjectiveKind,
    ObjectiveStatus,
    OperationsManager,
    OperationsMonitor,
    OperationsService,
    SignalKind,
    TaskKind,
    TaskOutcome,
    TaskState,
)
from novacontrol.operations.learning import OperationsLearning
from novacontrol.operations.models import Objective, OperationTask
from novacontrol.reliability.permissions import PermissionManager, RiskLevel

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def permissions() -> PermissionManager:
    manager = PermissionManager()
    manager.declare(
        DEFAULT_TASK_SCOPE,
        risk_level=RiskLevel.LOW,
        requires_confirmation=False,
        reversible=True,
        destructive=False,
        external_side_effect=False,
    )
    return manager


def objective_with_tasks(
    *,
    autonomy: AutonomyLevel = AutonomyLevel.EXECUTE,
    deadline: datetime | None = None,
) -> tuple[OperationsManager, Objective, str, str]:
    manager = OperationsManager()
    objective = manager.create_objective(
        "Thesis",
        "Submit the final thesis",
        kind=ObjectiveKind.ACADEMIC,
        autonomy=autonomy,
        deadline=deadline,
        completion_criteria=("draft approved",),
        now=NOW,
    )
    objective, first = manager.add_task(
        objective.id,
        "Draft chapter",
        "write the chapter",
        steps=("outline", "write"),
        due_at=NOW + timedelta(hours=2),
        now=NOW,
    )
    objective, second = manager.add_task(
        objective.id,
        "Review chapter",
        "review the chapter",
        depends_on=(first.id,),
        now=NOW,
    )
    return manager, manager.get(objective.id), first.id, second.id


class ObjectiveLifecycleTests(unittest.TestCase):
    def test_creation_requires_a_real_outcome(self) -> None:
        manager = OperationsManager()
        with self.assertRaises(ValueError):
            manager.create_objective("Only a title", "   ")
        created = manager.create_objective("Thesis", "Submit it", now=NOW)
        self.assertEqual(created.status, ObjectiveStatus.ACTIVE)
        self.assertEqual(created.progress, 0.0)
        self.assertIn(created.id, [item.id for item in manager.objectives()])

    def test_update_priority_deadline_and_rejection_of_unknown_fields(self) -> None:
        manager = OperationsManager()
        created = manager.create_objective("Thesis", "Submit it", now=NOW)
        updated = manager.update_objective(
            created.id,
            priority=90,
            deadline=NOW + timedelta(days=3),
        )
        self.assertEqual(updated.priority, 90)
        self.assertEqual(updated.deadline, NOW + timedelta(days=3))
        with self.assertRaises(ValueError):
            manager.update_objective(created.id, progress=1.0)

    def test_dependency_gates_work_and_progress_is_derived(self) -> None:
        manager, objective, first_id, second_id = objective_with_tasks()
        self.assertEqual(objective.task(first_id).state, TaskState.READY)
        self.assertEqual(objective.task(second_id).state, TaskState.PENDING)
        self.assertEqual(objective.progress, 0.0)

        manager.begin_task(objective.id, first_id, now=NOW)
        self.assertEqual(manager.get(objective.id).progress, 0.0)  # started is not done
        for step in ("outline", "write"):
            manager.record_step(
                objective.id, first_id, step, verified=True, evidence=f"{step}.md", now=NOW
            )
        manager.complete_task(
            objective.id, first_id, verified=True, result="draft done", now=NOW
        )
        refreshed = manager.refresh(objective.id, now=NOW)
        self.assertEqual(refreshed.task(second_id).state, TaskState.READY)
        self.assertAlmostEqual(refreshed.progress, 0.5)

    def test_completion_requires_verification_and_all_steps(self) -> None:
        manager, objective, first_id, _second = objective_with_tasks()
        manager.begin_task(objective.id, first_id, now=NOW)
        with self.assertRaises(ValueError):
            manager.complete_task(objective.id, first_id, verified=False, now=NOW)
        with self.assertRaises(ValueError):
            manager.complete_task(objective.id, first_id, verified=True, now=NOW)

    def test_milestone_completes_only_with_its_tasks(self) -> None:
        manager, objective, first_id, second_id = objective_with_tasks()
        objective, milestone = manager.add_milestone(
            objective.id, "Chapter ready", task_ids=(first_id, second_id), now=NOW
        )
        self.assertIsNone(manager.get(objective.id).milestones[0].verified_at)
        manager.begin_task(objective.id, first_id, now=NOW)
        for step in ("outline", "write"):
            manager.record_step(objective.id, first_id, step, verified=True, now=NOW)
        manager.complete_task(objective.id, first_id, verified=True, now=NOW)
        self.assertIsNone(manager.get(objective.id).milestones[0].verified_at)

    def test_cancel_closes_remaining_work(self) -> None:
        manager, objective, first_id, second_id = objective_with_tasks()
        cancelled = manager.cancel_objective(objective.id, reason="no longer needed")
        self.assertEqual(cancelled.status, ObjectiveStatus.CANCELLED)
        self.assertEqual(cancelled.task(first_id).state, TaskState.CANCELLED)
        self.assertEqual(cancelled.task(second_id).state, TaskState.CANCELLED)

    def test_dependencies_must_exist_and_cycles_are_refused(self) -> None:
        manager = OperationsManager()
        objective = manager.create_objective("Loop", "not hang", now=NOW)
        objective, first = manager.add_task(objective.id, "A", "a", now=NOW)
        with self.assertRaises(ValueError):
            manager.add_task(
                objective.id, "B", "b", depends_on=("missing-task",), now=NOW
            )
        # Dependencies can only name tasks that already exist, so a cycle cannot
        # be introduced through the public door. The guard is still tested
        # directly, because a future bulk-import path would reach it.
        first_task = OperationTask(
            title="A", instruction="a", objective_id=objective.id, id="a"
        )
        second_task = OperationTask(
            title="B", instruction="b", objective_id=objective.id, id="b", depends_on=("a",)
        )
        with self.assertRaises(ValueError):
            OperationsManager._assert_acyclic(
                [first_task.with_(depends_on=("b",)), second_task]
            )


class LearningEvidenceTests(unittest.TestCase):
    def test_evidence_requires_samples_and_stays_advisory(self) -> None:
        manager = OperationsManager()
        objective = manager.create_objective("Reports", "send them", now=NOW)
        tasks = [
            manager.add_task(objective.id, f"Send {index}", "send it", now=NOW)[1]
            for index in range(4)
        ]
        for task in tasks:
            manager.begin_task(objective.id, task.id, now=NOW)
            manager.complete_task(
                objective.id,
                task.id,
                verified=True,
                now=NOW + timedelta(minutes=2),
            )
        summary = OperationsLearning().summarise(manager.objectives())
        evidence = summary["kinds"][0]
        self.assertEqual(evidence["completed"], 4)
        self.assertEqual(evidence["confidence"], "low")
        self.assertEqual(evidence["average_duration_seconds"], 120.0)
        self.assertIn("never changes a schedule", summary["policy"])

    def test_one_run_is_not_a_preference(self) -> None:
        manager = OperationsManager()
        objective = manager.create_objective("Once", "do it", now=NOW)
        objective, task = manager.add_task(objective.id, "Do", "do it", now=NOW)
        manager.begin_task(objective.id, task.id, now=NOW)
        manager.complete_task(objective.id, task.id, verified=True, now=NOW)
        summary = OperationsLearning().summarise(manager.objectives())
        self.assertEqual(summary["kinds"][0]["confidence"], "none")
        self.assertEqual(summary["advisory"], [])


class PersistenceTests(unittest.TestCase):
    def test_round_trip_preserves_checkpoints_and_progress(self) -> None:
        manager, objective, first_id, _second = objective_with_tasks()
        manager.begin_task(objective.id, first_id, now=NOW)
        manager.record_step(
            objective.id, first_id, "outline", verified=True, evidence="outline v2", now=NOW
        )
        payload = manager.to_dict()
        restored = OperationsManager.from_dict(payload)
        task = restored.get(objective.id).task(first_id)
        # A stored RUNNING task is recovered as READY with a mandatory review;
        # that is what keeps an ACTIVE objective from stalling after a restart.
        self.assertEqual(task.state, TaskState.READY)
        self.assertTrue(task.metadata.get("resume_required"))
        self.assertEqual(task.completed_steps, ("outline",))
        self.assertEqual(task.last_verified_state, "outline v2")
        self.assertEqual(task.checkpoint["completed_steps"], ["outline"])

    def test_damaged_objective_is_skipped_not_fatal(self) -> None:
        manager, objective, _first, _second = objective_with_tasks()
        payload = manager.to_dict()
        payload["objectives"].append({"title": "broken"})
        restored = OperationsManager.from_dict(payload)
        self.assertEqual(len(restored.objectives()), 1)

    def test_duplicate_completion_is_refused(self) -> None:
        manager, objective, first_id, _second = objective_with_tasks()
        manager.begin_task(objective.id, first_id, now=NOW)
        manager.record_step(objective.id, first_id, "outline", verified=True, now=NOW)
        manager.record_step(objective.id, first_id, "write", verified=True, now=NOW)
        manager.complete_task(objective.id, first_id, verified=True, now=NOW)
        with self.assertRaises(ValueError):
            manager.complete_task(objective.id, first_id, verified=True, now=NOW)

    def test_interrupted_task_resumes_with_verified_progress_and_a_review(self) -> None:
        manager, objective, first_id, _second = objective_with_tasks()
        manager.begin_task(objective.id, first_id, now=NOW)
        manager.record_step(objective.id, first_id, "outline", verified=True, now=NOW)

        restarted = OperationsManager.from_dict(manager.to_dict())
        restored, review = restarted.resume_objective(objective.id, now=NOW)
        task = restored.task(first_id)
        self.assertEqual(review, (first_id,))
        self.assertEqual(task.state, TaskState.READY)
        self.assertEqual(task.completed_steps, ("outline",))
        self.assertTrue(task.metadata["resume_required"])
        with self.assertRaises(ValueError):
            restarted.begin_task(objective.id, first_id, now=NOW)
        resumed = restarted.begin_task(
            objective.id, first_id, reassessed=True, now=NOW
        )
        self.assertEqual(resumed.state, TaskState.RUNNING)
        self.assertNotIn("resume_required", resumed.metadata)


class SchedulingTests(unittest.TestCase):
    def test_future_scheduled_task_is_not_ready_early(self) -> None:
        manager = OperationsManager()
        objective = manager.create_objective("Report", "send it", now=NOW)
        objective, task = manager.add_task(
            objective.id,
            "Send",
            "send the report",
            scheduled_for=NOW + timedelta(hours=2),
            now=NOW,
        )
        self.assertEqual(manager.get(objective.id).task(task.id).state, TaskState.PENDING)
        later = manager.refresh(objective.id, now=NOW + timedelta(hours=3))
        self.assertEqual(later.task(task.id).state, TaskState.READY)

    def test_recurring_task_spawns_exactly_one_successor(self) -> None:
        manager = OperationsManager()
        objective = manager.create_objective("Status", "weekly status", now=NOW)
        objective, task = manager.add_task(
            objective.id,
            "Weekly status",
            "summarise progress",
            recurrence=Schedule(kind=ScheduleKind.INTERVAL, every_seconds=3600),
            now=NOW,
        )
        manager.begin_task(objective.id, task.id, now=NOW)
        done, spawned = manager.complete_task(
            objective.id, task.id, verified=True, now=NOW
        )
        self.assertEqual(done.state, TaskState.COMPLETED)
        self.assertIsNotNone(spawned)
        assert spawned is not None
        self.assertEqual(spawned.scheduled_for, NOW + timedelta(hours=1))
        self.assertEqual(spawned.state, TaskState.PENDING)
        self.assertEqual(spawned.metadata["occurrence_of"], task.id)
        self.assertEqual(len(manager.get(objective.id).tasks), 2)

    def test_missed_interval_is_compressed_not_replayed(self) -> None:
        schedule = Schedule(kind=ScheduleKind.INTERVAL, every_seconds=3600)
        now = NOW + timedelta(hours=6, minutes=30)
        upcoming = schedule.next_after(now, previous=NOW)
        self.assertEqual(upcoming, NOW + timedelta(hours=7))

    def test_deadline_change_and_timezone_validation(self) -> None:
        manager = OperationsManager()
        objective = manager.create_objective("Report", "send it", now=NOW)
        changed = manager.update_objective(
            objective.id, deadline=NOW + timedelta(days=1)
        )
        self.assertEqual(changed.deadline, NOW + timedelta(days=1))
        with self.assertRaises(ValueError):
            Objective(
                title="Naive",
                outcome="never",
                deadline=datetime(2026, 11, 10, 12, 0),  # no tzinfo
            )


class AutonomyAndApprovalTests(unittest.TestCase):
    def _service(
        self,
        manager: OperationsManager,
        *,
        outcome: TaskOutcome | None = None,
        autocall: list[str] | None = None,
    ) -> OperationsService:
        calls = autocall if autocall is not None else []

        async def execute(task) -> TaskOutcome:
            calls.append(task.id)
            return outcome or TaskOutcome(
                verified=True, detail="done", evidence=("evidence",)
            )

        async def reassess(_task) -> tuple[bool, str]:
            return True, "still valid"

        return OperationsService(
            manager,
            permissions=permissions(),
            execute_task=execute,
            reassess_task=reassess,
        )

    def test_observe_and_recommend_never_execute(self) -> None:
        for level, expected in (
            (AutonomyLevel.OBSERVE, "observed"),
            (AutonomyLevel.RECOMMEND, "recommended"),
        ):
            calls: list[str] = []
            manager = OperationsManager()
            objective = manager.create_objective(
                "Quiet", "watch only", autonomy=level, now=NOW
            )
            objective, task = manager.add_task(
                objective.id, "Watch", "watch the thing", now=NOW
            )
            service = self._service(manager, autocall=calls)
            result = asyncio.run(service.run_task(objective.id, task.id, now=NOW))
            self.assertEqual(result["status"], expected)
            self.assertEqual(calls, [])
            self.assertEqual(service.manager.get(objective.id).task(task.id).state, TaskState.READY)

    def test_execute_runs_through_the_permission_layer(self) -> None:
        calls: list[str] = []
        manager, objective, first_id, _second = objective_with_tasks()
        service = self._service(manager, autocall=calls)
        result = asyncio.run(service.run_task(objective.id, first_id, now=NOW))
        # The task has planned steps the fake executor does not claim, so the
        # service records a failure rather than a completion: that is the point.
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(calls), 1)
        self.assertIn("incomplete", result["reason"].lower())

    def test_sensitive_task_waits_then_runs_once_approved(self) -> None:
        calls: list[str] = []
        manager = OperationsManager()
        objective = manager.create_objective(
            "Inbox", "clear the inbox", autonomy=AutonomyLevel.EXECUTE, now=NOW
        )
        objective, task = manager.add_task(
            objective.id,
            "Send reply",
            "send the reply",
            sensitive=True,
            now=NOW,
        )
        service = self._service(manager, autocall=calls)
        blocked = asyncio.run(service.run_task(objective.id, task.id, now=NOW))
        self.assertEqual(blocked["status"], "waiting_approval")
        self.assertEqual(calls, [])

        service.decide_task(objective.id, task.id, action="approve", now=NOW)
        run = asyncio.run(service.run_task(objective.id, task.id, now=NOW))
        self.assertEqual(run["status"], "completed")
        self.assertEqual(len(calls), 1)
        self.assertTrue(service.manager.get(objective.id).task(task.id).approved)
        self.assertEqual(
            service.manager.get(objective.id).task(task.id).approved_by, "operator"
        )

    def test_rejection_is_not_execution(self) -> None:
        calls: list[str] = []
        manager = OperationsManager()
        objective = manager.create_objective(
            "Inbox", "clear", autonomy=AutonomyLevel.APPROVAL, now=NOW
        )
        objective, task = manager.add_task(
            objective.id, "Delete", "delete the archive", sensitive=True, now=NOW
        )
        service = self._service(manager, autocall=calls)
        service.decide_task(
            objective.id, task.id, action="reject", reason="not now", now=NOW
        )
        state = service.manager.get(objective.id).task(task.id)
        self.assertEqual(state.state, TaskState.CANCELLED)
        self.assertEqual(calls, [])

    def test_unverified_outcome_fails_and_never_completes(self) -> None:
        manager = OperationsManager()
        objective = manager.create_objective(
            "Report", "send it", autonomy=AutonomyLevel.EXECUTE, now=NOW
        )
        objective, task = manager.add_task(objective.id, "Send", "send it", now=NOW)
        service = self._service(
            manager, outcome=TaskOutcome(verified=False, detail="the call returned")
        )
        result = asyncio.run(service.run_task(objective.id, task.id, now=NOW))
        self.assertEqual(result["status"], "failed")
        stored = service.manager.get(objective.id)
        self.assertEqual(stored.task(task.id).state, TaskState.FAILED)
        self.assertEqual(stored.progress, 0.0)

    def test_executor_that_raises_is_a_failure(self) -> None:
        manager = OperationsManager()
        objective = manager.create_objective(
            "Report", "send it", autonomy=AutonomyLevel.EXECUTE, now=NOW
        )
        objective, task = manager.add_task(objective.id, "Send", "send it", now=NOW)

        async def explode(_task):
            raise RuntimeError("worker died")

        service = OperationsService(
            manager, permissions=permissions(), execute_task=explode
        )
        result = asyncio.run(service.run_task(objective.id, task.id, now=NOW))
        self.assertEqual(result["status"], "failed")
        self.assertIn("worker died", result["reason"])

    def test_needs_approval_outcome_parks_without_completing(self) -> None:
        manager = OperationsManager()
        objective = manager.create_objective(
            "Desktop", "open the app", autonomy=AutonomyLevel.EXECUTE, now=NOW
        )
        objective, task = manager.add_task(
            objective.id, "Open", "open the app", kind=TaskKind.COMPUTER, now=NOW
        )
        service = self._service(
            manager,
            outcome=TaskOutcome(
                verified=False,
                needs_approval=True,
                detail="the desktop action needs approval",
            ),
        )
        result = asyncio.run(service.run_task(objective.id, task.id, now=NOW))
        self.assertEqual(result["status"], "waiting_approval")
        self.assertEqual(
            service.manager.get(objective.id).task(task.id).state,
            TaskState.WAITING_APPROVAL,
        )

    def test_retry_is_bounded(self) -> None:
        manager = OperationsManager()
        objective = manager.create_objective("Report", "send", now=NOW)
        objective, task = manager.add_task(
            objective.id, "Send", "send it", max_attempts=1, now=NOW
        )
        manager.begin_task(objective.id, task.id, now=NOW)
        manager.fail_task(objective.id, task.id, error="nope", now=NOW)
        with self.assertRaises(ValueError):
            manager.retry_task(objective.id, task.id, now=NOW)


class MonitoringTests(unittest.TestCase):
    def test_deadline_approaching_and_overdue(self) -> None:
        monitor = OperationsMonitor(deadline_window=timedelta(hours=24))
        manager = OperationsManager()
        soon = manager.create_objective(
            "Soon", "deliver", deadline=NOW + timedelta(hours=5), now=NOW
        )
        signals = monitor.scan([manager.get(soon.id)], now=NOW)
        self.assertIn(SignalKind.DEADLINE_APPROACHING, [s.kind for s in signals])

        late = manager.create_objective(
            "Late", "deliver", deadline=NOW - timedelta(hours=2), now=NOW
        )
        signals = monitor.scan([manager.get(late.id)], now=NOW)
        self.assertIn(SignalKind.OVERDUE, [s.kind for s in signals])

    def test_stalled_task_is_detected(self) -> None:
        monitor = OperationsMonitor(stall_after=timedelta(hours=6))
        manager = OperationsManager()
        objective = manager.create_objective("Stuck", "finish", now=NOW)
        manager.add_task(objective.id, "Wait", "wait for input", now=NOW)
        signals = monitor.scan(
            [manager.get(objective.id)], now=NOW + timedelta(hours=12)
        )
        self.assertIn(SignalKind.STALLED, [s.kind for s in signals])

    def test_no_change_is_a_reported_conclusion(self) -> None:
        monitor = OperationsMonitor()
        manager = OperationsManager()
        objective = manager.create_objective("Quiet", "observe", now=NOW)
        manager.add_task(
            objective.id,
            "Later",
            "do it later",
            scheduled_for=NOW + timedelta(days=2),
            now=NOW,
        )
        signals = monitor.scan([manager.get(objective.id)], now=NOW)
        self.assertEqual([signal.kind for signal in signals], [SignalKind.NO_CHANGE])

    def test_notifications_deduplicate_and_respect_preferences(self) -> None:
        monitor = OperationsMonitor(deadline_window=timedelta(hours=24))
        manager = OperationsManager()
        objective = manager.create_objective(
            "Soon", "deliver", deadline=NOW + timedelta(hours=2), now=NOW
        )
        signals = monitor.scan([manager.get(objective.id)], now=NOW)
        center = NotificationCenter()
        first = monitor.notifications_for(signals, center, now=NOW)
        second = monitor.notifications_for(signals, center, now=NOW + timedelta(minutes=1))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, ())  # duplicate suppressed, not re-sent

        muted = NotificationCenter(muted=True)
        self.assertEqual(monitor.notifications_for(signals, muted, now=NOW), ())
        self.assertEqual(muted.unread_count, 0)

        disabled = NotificationCenter(
            enabled_kinds=[NotificationKind.OBJECTIVE_COMPLETED.value]
        )
        self.assertEqual(monitor.notifications_for(signals, disabled, now=NOW), ())

    def test_completion_notification_reflects_real_state(self) -> None:
        monitor = OperationsMonitor()
        manager = OperationsManager()
        objective = manager.create_objective("Done", "finish it", now=NOW)
        objective, task = manager.add_task(objective.id, "Do", "do it", now=NOW)
        center = NotificationCenter()
        before = monitor.scan([manager.get(objective.id)], now=NOW)
        self.assertEqual(
            monitor.notifications_for(before, center, now=NOW), ()
        )  # an active task with nothing due is not a "completion"
        manager.begin_task(objective.id, task.id, now=NOW)
        manager.complete_task(objective.id, task.id, verified=True, now=NOW)
        after = monitor.scan([manager.get(objective.id)], now=NOW)
        stored = monitor.notifications_for(after, center, now=NOW)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].kind, NotificationKind.OBJECTIVE_COMPLETED)

    def test_preferences_round_trip(self) -> None:
        center = NotificationCenter()
        center.configure(
            enabled_kinds=[NotificationKind.OVERDUE.value],
            dedupe_window_seconds=60,
            muted=True,
        )
        restored = NotificationCenter.from_dict(center.to_dict())
        self.assertTrue(restored.muted)
        self.assertEqual(restored.enabled_kinds, {NotificationKind.OVERDUE.value})
        self.assertEqual(restored.dedupe_window_seconds, 60)


class ServiceTickTests(unittest.TestCase):
    def _service(self, manager: OperationsManager, *, verified: bool = True):
        calls: list[str] = []

        async def execute(task) -> TaskOutcome:
            calls.append(task.id)
            return TaskOutcome(verified=verified, detail="done", evidence=("e",))

        async def reassess(_task) -> tuple[bool, str]:
            return True, "still valid"

        return (
            OperationsService(
                manager,
                permissions=permissions(),
                execute_task=execute,
                reassess_task=reassess,
            ),
            calls,
        )

    def test_tick_runs_ready_work_once(self) -> None:
        manager = OperationsManager()
        objective = manager.create_objective(
            "Report", "send it", autonomy=AutonomyLevel.EXECUTE, now=NOW
        )
        manager.add_task(objective.id, "Send", "send it", now=NOW)
        service, calls = self._service(manager)
        first = asyncio.run(service.tick(now=NOW))
        self.assertEqual(first["ran"], 1)
        self.assertEqual(first["results"][0]["status"], "completed")
        second = asyncio.run(service.tick(now=NOW + timedelta(seconds=10)))
        self.assertEqual(second["ran"], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(service.manager.get(objective.id).progress, 1.0)
        self.assertEqual(
            service.manager.get(objective.id).status, ObjectiveStatus.COMPLETED
        )

    def test_paused_objective_does_not_execute(self) -> None:
        manager = OperationsManager()
        objective = manager.create_objective(
            "Report", "send it", autonomy=AutonomyLevel.EXECUTE, now=NOW
        )
        manager.add_task(objective.id, "Send", "send it", now=NOW)
        manager.pause_objective(objective.id, reason="hold")
        service, calls = self._service(manager)
        result = asyncio.run(service.tick(now=NOW))
        self.assertEqual(result["ran"], 0)
        self.assertEqual(calls, [])

    def test_resume_reassesses_before_running(self) -> None:
        manager = OperationsManager()
        objective = manager.create_objective(
            "Report", "send it", autonomy=AutonomyLevel.EXECUTE, now=NOW
        )
        objective, task = manager.add_task(objective.id, "Send", "send it", now=NOW)
        manager.begin_task(objective.id, task.id, now=NOW)
        restarted = OperationsManager.from_dict(manager.to_dict())
        restarted.resume_objective(objective.id, now=NOW + timedelta(minutes=1))

        calls: list[str] = []

        async def execute(running) -> TaskOutcome:
            calls.append(running.id)
            return TaskOutcome(verified=True, detail="done", evidence=("e",))

        async def changed(_task) -> tuple[bool, str]:
            return False, "the inbox is no longer reachable"

        service = OperationsService(
            restarted,
            permissions=permissions(),
            execute_task=execute,
            reassess_task=changed,
        )
        result = asyncio.run(
            service.run_task(objective.id, task.id, now=NOW + timedelta(minutes=2))
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(calls, [])
        self.assertEqual(
            service.manager.get(objective.id).task(task.id).state, TaskState.BLOCKED
        )

    def test_reassessment_that_passes_runs_the_task(self) -> None:
        manager = OperationsManager()
        objective = manager.create_objective(
            "Report", "send it", autonomy=AutonomyLevel.EXECUTE, now=NOW
        )
        objective, task = manager.add_task(objective.id, "Send", "send it", now=NOW)
        manager.begin_task(objective.id, task.id, now=NOW)
        restarted = OperationsManager.from_dict(manager.to_dict())
        restarted.resume_objective(objective.id, now=NOW + timedelta(minutes=1))
        service, calls = self._service(restarted)
        result = asyncio.run(
            service.run_task(objective.id, task.id, now=NOW + timedelta(minutes=2))
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(calls), 1)

    def test_no_permission_layer_means_no_execution(self) -> None:
        manager = OperationsManager()
        objective = manager.create_objective(
            "Report", "send it", autonomy=AutonomyLevel.EXECUTE, now=NOW
        )
        objective, task = manager.add_task(objective.id, "Send", "send it", now=NOW)
        calls: list[str] = []

        async def execute(running) -> TaskOutcome:
            calls.append(running.id)
            return TaskOutcome(verified=True)

        service = OperationsService(manager, execute_task=execute)
        result = asyncio.run(service.run_task(objective.id, task.id, now=NOW))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("no permission layer", result["reason"])
        self.assertEqual(calls, [])
        self.assertEqual(
            manager.get(objective.id).task(task.id).state, TaskState.BLOCKED
        )

    def test_state_survives_service_round_trip(self) -> None:
        manager = OperationsManager()
        objective = manager.create_objective("Report", "send it", now=NOW)
        manager.add_task(objective.id, "Send", "send it", now=NOW)
        service, _calls = self._service(manager)
        asyncio.run(service.tick(now=NOW))
        restored = OperationsService.from_dict(
            service.to_dict(), permissions=permissions()
        )
        self.assertEqual(len(restored.manager.objectives()), 1)
        self.assertEqual(restored.manager.get(objective.id).progress, 1.0)
        # Completion really happened, so exactly one completion notification was
        # stored — and it survived the round trip.
        self.assertEqual(restored.notifications.unread_count, 1)
        self.assertEqual(
            restored.notifications.list()[0].kind,
            NotificationKind.OBJECTIVE_COMPLETED,
        )


if __name__ == "__main__":
    unittest.main()


class InterruptedRunRecoveryTests(unittest.TestCase):
    def test_a_running_task_is_recovered_for_reassessment_on_load(self) -> None:
        manager, objective, first_id, _ = objective_with_tasks()
        manager.begin_task(objective.id, first_id, now=NOW)
        loaded = OperationsManager.from_dict(manager.to_dict())
        task = loaded.get(objective.id).task(first_id)
        self.assertEqual(task.state, TaskState.READY)
        self.assertTrue(task.metadata.get("resume_required"))
        with self.assertRaises(ValueError):
            loaded.begin_task(objective.id, first_id, now=NOW)
        resumed = loaded.begin_task(
            objective.id, first_id, reassessed=True, now=NOW
        )
        self.assertEqual(resumed.state, TaskState.RUNNING)
        self.assertFalse(resumed.metadata.get("resume_required"))


class TransitionGuardTests(unittest.TestCase):
    def test_approval_and_rejection_require_waiting_work(self) -> None:
        manager, objective, first_id, _ = objective_with_tasks()
        with self.assertRaises(ValueError):
            manager.approve_task(objective.id, first_id, now=NOW)
        with self.assertRaises(ValueError):
            manager.reject_task(objective.id, first_id, now=NOW)
        manager.begin_task(objective.id, first_id, now=NOW)
        manager.wait_for_approval(objective.id, first_id, reason="needs a person", now=NOW)
        manager.reject_task(objective.id, first_id, now=NOW)
        with self.assertRaises(ValueError):
            manager.approve_task(objective.id, first_id, now=NOW)

    def test_closed_tasks_cannot_be_blocked_waited_failed_or_cancelled(self) -> None:
        manager, objective, first_id, _ = objective_with_tasks()
        manager.begin_task(objective.id, first_id, now=NOW)
        manager.record_step(objective.id, first_id, "outline", verified=True, now=NOW)
        manager.record_step(objective.id, first_id, "write", verified=True, now=NOW)
        manager.complete_task(objective.id, first_id, verified=True, now=NOW)
        with self.assertRaises(ValueError):
            manager.wait_for_approval(objective.id, first_id, reason="x", now=NOW)
        with self.assertRaises(ValueError):
            manager.block_task(objective.id, first_id, reason="x", now=NOW)
        with self.assertRaises(ValueError):
            manager.fail_task(objective.id, first_id, error="x", now=NOW)
        with self.assertRaises(ValueError):
            manager.cancel_task(objective.id, first_id, now=NOW)
        self.assertEqual(
            manager.get(objective.id).task(first_id).state, TaskState.COMPLETED
        )


class PermissionWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_permission_layer_blocks_instead_of_asking(self) -> None:
        manager, objective, first_id, _ = objective_with_tasks()

        async def never_called(task):  # pragma: no cover - must not run
            raise AssertionError("nothing may execute without a permission layer")

        service = OperationsService(manager, execute_task=never_called)
        result = await service.run_task(objective.id, first_id, now=NOW)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(
            manager.get(objective.id).task(first_id).state, TaskState.BLOCKED
        )

    async def test_a_direct_run_does_not_rewrite_completed_work(self) -> None:
        manager, objective, first_id, _ = objective_with_tasks()
        manager.begin_task(objective.id, first_id, now=NOW)
        manager.record_step(objective.id, first_id, "outline", verified=True, now=NOW)
        manager.record_step(objective.id, first_id, "write", verified=True, now=NOW)
        manager.complete_task(objective.id, first_id, verified=True, now=NOW)

        async def never_called(task):  # pragma: no cover - must not run
            raise AssertionError("a closed task must not execute again")

        service = OperationsService(
            manager, permissions=permissions(), execute_task=never_called
        )
        result = await service.run_task(objective.id, first_id, now=NOW)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(
            manager.get(objective.id).task(first_id).state, TaskState.COMPLETED
        )


class MonitorTruthfulnessTests(unittest.TestCase):
    def test_observe_objective_does_not_claim_work_is_authorized(self) -> None:
        manager, _, _, _ = objective_with_tasks(autonomy=AutonomyLevel.OBSERVE)
        signals = OperationsMonitor().scan(manager.objectives(), now=NOW)
        actionable = [
            signal for signal in signals if signal.kind is SignalKind.ACTIONABLE
        ]
        self.assertTrue(actionable)
        self.assertIn("a person must start it", actionable[0].detail)
        self.assertNotIn("authorized", actionable[0].detail)
