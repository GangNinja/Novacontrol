from __future__ import annotations

import unittest
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from novacontrol.application import NovaControlApplication
from novacontrol.automation import (
    AUTOMATION_RUN_SCOPE,
    MIN_INTERVAL_SECONDS,
    AutomationCondition,
    AutomationEngine,
    AutomationKind,
    AutomationOutcome,
    AutomationRunStatus,
    AutomationStatus,
    ConditionKind,
    Schedule,
    ScheduleKind,
    parse_schedule,
    strip_schedule,
)
from novacontrol.planning import PlanCompiler, StepEffect
from novacontrol.reliability.permissions import PermissionManager, RiskLevel

CLOCK = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)


def _daily_at(nine_am: time = time(9, 0)) -> Schedule:
    return Schedule(kind=ScheduleKind.DAILY, time_of_day=nine_am)


def _once_in(minutes: int) -> Schedule:
    return Schedule(kind=ScheduleKind.ONCE, at=CLOCK + timedelta(minutes=minutes))


def _risk() -> PermissionManager:
    """A permission layer that gates ``automation.run`` like the app does."""
    manager = PermissionManager()
    manager.declare(
        AUTOMATION_RUN_SCOPE,
        risk_level=RiskLevel.HIGH,
        requires_confirmation=True,
        reversible=True,
        external_side_effect=False,
    )
    return manager


class ScheduleParsingTests(unittest.TestCase):
    """A schedule is computed, never guessed."""

    def test_named_weekday_parses_to_a_weekly_schedule(self) -> None:
        schedule = parse_schedule("every Monday at 9", now=CLOCK)

        assert schedule is not None
        self.assertIs(schedule.kind, ScheduleKind.WEEKLY)
        self.assertEqual(schedule.weekday, 0)
        self.assertEqual(schedule.time_of_day, time(9, 0))
        self.assertEqual(schedule.describe(), "every monday at 09:00")

    def test_daily_phrases_default_to_a_morning_time(self) -> None:
        for phrase in ("daily", "every day", "each day"):
            with self.subTest(phrase=phrase):
                schedule = parse_schedule(f"{phrase} back up the project", now=CLOCK)
                assert schedule is not None
                self.assertIs(schedule.kind, ScheduleKind.DAILY)
                self.assertEqual(schedule.time_of_day, time(9, 0))

    def test_interval_phrases_carry_their_period(self) -> None:
        schedule = parse_schedule("every 30 minutes check the build", now=CLOCK)

        assert schedule is not None
        self.assertIs(schedule.kind, ScheduleKind.INTERVAL)
        self.assertEqual(schedule.every_seconds, 1800.0)

    def test_relative_phrase_is_one_occurrence_in_the_future(self) -> None:
        schedule = parse_schedule("in 2 hours rotate the logs", now=CLOCK)

        assert schedule is not None
        self.assertIs(schedule.kind, ScheduleKind.ONCE)
        self.assertEqual(schedule.at, CLOCK + timedelta(hours=2))

    def test_a_sentence_that_states_no_time_schedules_nothing(self) -> None:
        for phrase in (
            "when should I run my backup?",
            "what time does my backup run?",
            "remind me about the report",
        ):
            with self.subTest(phrase=phrase):
                self.assertIsNone(parse_schedule(phrase, now=CLOCK))

    def test_a_sub_second_interval_is_refused(self) -> None:
        self.assertIsNone(parse_schedule("every 0.5 seconds", now=CLOCK))
        self.assertEqual(MIN_INTERVAL_SECONDS, 1.0)

    def test_strip_schedule_keeps_the_request_and_drops_the_phrase(self) -> None:
        self.assertEqual(
            strip_schedule("Remind me at 6 PM to push my project."), "push my project"
        )
        self.assertEqual(
            strip_schedule("Every Monday at 9 generate a project report."),
            "generate a project report",
        )

    def test_strip_schedule_falls_back_when_nothing_would_remain(self) -> None:
        # Stripping an empty request is worse than keeping a consumed phrase.
        self.assertEqual(strip_schedule("every day at 9"), "every day at 9")
        self.assertEqual(strip_schedule(""), "")

    def test_next_occurrence_is_strictly_in_the_future(self) -> None:
        daily = _daily_at()
        self.assertEqual(
            daily.next_after(CLOCK), datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
        )
        self.assertEqual(
            daily.next_after(datetime(2026, 9, 29, 3, 0, tzinfo=UTC)),
            datetime(2026, 9, 29, 9, 0, tzinfo=UTC),
        )

    def test_weekly_next_occurrence_lands_on_the_named_weekday(self) -> None:
        weekly = Schedule(kind=ScheduleKind.WEEKLY, weekday=0, time_of_day=time(9, 0))
        # CLOCK is a Tuesday; the next Monday is six days away.
        self.assertEqual(
            weekly.next_after(CLOCK), datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
        )

    def test_interval_compresses_missed_occurrences_into_one(self) -> None:
        interval = Schedule(kind=ScheduleKind.INTERVAL, every_seconds=300.0)
        # A machine asleep for an hour gets ONE next run, not twelve.
        self.assertEqual(
            interval.next_after(CLOCK), datetime(2026, 9, 29, 10, 5, tzinfo=UTC)
        )
        self.assertEqual(
            interval.next_after(CLOCK, previous=CLOCK - timedelta(hours=1)),
            datetime(2026, 9, 29, 10, 5, tzinfo=UTC),
        )

    def test_passed_one_time_schedule_has_no_further_occurrence(self) -> None:
        self.assertIsNone(_once_in(-5).next_after(CLOCK))

    def test_schedule_round_trips_through_its_dict(self) -> None:
        for schedule in (
            _daily_at(),
            _once_in(30),
            Schedule(kind=ScheduleKind.INTERVAL, every_seconds=90.0),
            Schedule(kind=ScheduleKind.WEEKLY, weekday=2, time_of_day=time(14, 30)),
        ):
            with self.subTest(schedule=schedule.describe()):
                self.assertEqual(Schedule.from_dict(schedule.to_dict()), schedule)

    def test_a_schedule_refuses_an_ambiguous_construction(self) -> None:
        with self.assertRaises(ValueError):
            Schedule(kind=ScheduleKind.ONCE, at=datetime(2026, 9, 29, 10, 0))
        with self.assertRaises(ValueError):
            Schedule(kind=ScheduleKind.INTERVAL, every_seconds=0.25)
        with self.assertRaises(ValueError):
            Schedule(kind=ScheduleKind.WEEKLY, time_of_day=time(9, 0))


class AutomationEngineTests(unittest.IsolatedAsyncioTestCase):
    """The engine owns WHEN; the injected runner owns WHAT."""

    def setUp(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def _engine(self, **kwargs) -> AutomationEngine:
        kwargs.setdefault("publish", self._publish)
        return AutomationEngine(**kwargs)

    def _publish(self, type_, **payload) -> None:
        self.events.append((str(getattr(type_, "value", type_)), payload))

    async def test_creation_stores_the_request_and_waits_for_approval(self) -> None:
        engine = self._engine()
        task = engine.create("open notepad", schedule=_daily_at(), now=CLOCK)

        self.assertEqual(task.automation_id, task.id)
        self.assertEqual(task.request, "open notepad")
        self.assertIs(task.kind, AutomationKind.RECURRING)
        self.assertIs(task.status, AutomationStatus.PENDING_APPROVAL)
        self.assertFalse(task.enabled)
        self.assertEqual(task.next_run_at, datetime(2026, 9, 30, 9, 0, tzinfo=UTC))
        self.assertEqual(task.failure_count, 0)
        self.assertEqual(task.created_at, CLOCK)
        self.assertIn("automation.created", [name for name, _ in self.events])

    async def test_approved_creation_is_armed_immediately(self) -> None:
        engine = self._engine()
        task = engine.create(
            "open notepad",
            schedule=_daily_at(),
            requires_approval=False,
            approved=True,
            now=CLOCK,
        )

        self.assertIs(task.status, AutomationStatus.SCHEDULED)
        self.assertTrue(task.enabled)
        self.assertTrue(task.approved)

    async def test_a_past_one_time_schedule_is_refused_at_creation(self) -> None:
        engine = self._engine()
        with self.assertRaises(ValueError):
            engine.create("rotate logs", schedule=_once_in(-1), now=CLOCK)
        self.assertEqual(engine.tasks(), ())

    async def test_a_conditional_request_is_a_conditional_automation(self) -> None:
        engine = self._engine()
        task = engine.create(
            "run the tests",
            schedule=_daily_at(),
            condition=AutomationCondition(ConditionKind.PATH_EXISTS, "pyproject.toml"),
            now=CLOCK,
        )

        self.assertIs(task.kind, AutomationKind.CONDITIONAL)

    async def test_approval_is_the_only_way_a_pending_task_is_armed(self) -> None:
        engine = self._engine()
        task = engine.create("open notepad", schedule=_daily_at(), now=CLOCK)

        with self.assertRaises(PermissionError):
            engine.enable(task.id, now=CLOCK)

        approved = engine.approve(task.id, by="tester", now=CLOCK)
        self.assertTrue(approved.enabled)
        self.assertTrue(approved.approved)
        self.assertEqual(approved.approved_by, "tester")
        self.assertIs(approved.status, AutomationStatus.SCHEDULED)

    async def test_cancellation_stops_a_task_for_good(self) -> None:
        engine = self._engine()
        task = engine.create(
            "open notepad", schedule=_daily_at(), approved=True, now=CLOCK
        )

        cancelled = engine.cancel(task.id)

        self.assertIs(cancelled.status, AutomationStatus.CANCELLED)
        self.assertFalse(cancelled.enabled)
        self.assertIsNone(cancelled.next_run_at)
        self.assertEqual(engine.due(CLOCK + timedelta(days=1)), ())
        # Idempotent: cancelling twice is not an error.
        self.assertIs(engine.cancel(task.id).status, AutomationStatus.CANCELLED)

    async def test_disable_and_delete(self) -> None:
        engine = self._engine()
        task = engine.create(
            "open notepad", schedule=_daily_at(), approved=True, now=CLOCK
        )

        disabled = engine.disable(task.id)
        self.assertIs(disabled.status, AutomationStatus.DISABLED)
        self.assertFalse(disabled.enabled)

        self.assertTrue(engine.delete(task.id))
        self.assertFalse(engine.delete(task.id))
        self.assertEqual(engine.tasks(), ())

    async def test_only_armed_tasks_that_are_due_are_due(self) -> None:
        engine = self._engine()
        pending = engine.create("waits", schedule=_daily_at(), now=CLOCK)
        armed = engine.create(
            "runs", schedule=_daily_at(), approved=True, now=CLOCK
        )

        due = engine.due(datetime(2026, 9, 30, 12, 0, tzinfo=UTC))
        self.assertEqual([task.id for task in due], [armed.id])
        self.assertNotIn(pending.id, [task.id for task in due])

    async def test_a_completed_recurring_run_re_arms_for_its_next_occurrence(self) -> None:
        engine = self._engine(permissions=_risk())

        async def runner(_task):
            return AutomationOutcome(AutomationRunStatus.COMPLETED, detail="done", verified=True)

        task = engine.create("runs", schedule=_daily_at(), approved=True, now=CLOCK)
        due_at = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
        runs = await engine.run_due(runner, now=due_at)

        self.assertEqual(len(runs), 1)
        self.assertIs(runs[0].status, AutomationRunStatus.COMPLETED)
        self.assertTrue(runs[0].verified)
        stored = engine.get(task.id)
        self.assertIs(stored.status, AutomationStatus.SCHEDULED)
        self.assertTrue(stored.enabled)
        self.assertEqual(stored.run_count, 1)
        self.assertEqual(stored.last_run_at, due_at)
        self.assertEqual(stored.next_run_at, datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
        self.assertEqual(len(stored.runs), 1)

    async def test_a_completed_one_time_run_finishes_and_disarms(self) -> None:
        engine = self._engine(permissions=_risk())

        async def runner(_task):
            return AutomationOutcome(AutomationRunStatus.COMPLETED)

        task = engine.create("rotate logs", schedule=_once_in(5), approved=True, now=CLOCK)
        await engine.run_due(runner, now=CLOCK + timedelta(minutes=10))

        stored = engine.get(task.id)
        self.assertIs(stored.status, AutomationStatus.COMPLETED)
        self.assertFalse(stored.enabled)
        self.assertIsNone(stored.next_run_at)

    async def test_missed_occurrences_are_not_replayed_in_a_burst(self) -> None:
        engine = self._engine(permissions=_risk())
        calls = 0

        async def runner(_task):
            nonlocal calls
            calls += 1
            return AutomationOutcome(AutomationRunStatus.COMPLETED)

        engine.create(
            "hourly", schedule=Schedule(kind=ScheduleKind.INTERVAL, every_seconds=3600.0),
            approved=True, now=CLOCK,
        )
        # Six hours later, the machine runs its tick once.
        await engine.run_due(runner, now=CLOCK + timedelta(hours=6))

        self.assertEqual(calls, 1)

    async def test_a_failure_is_counted_and_a_broken_task_disables_itself(self) -> None:
        engine = self._engine(permissions=_risk(), max_failures=2)

        async def failing(_task):
            return AutomationOutcome(
                AutomationRunStatus.FAILED, detail="boom", failure_kind="boom"
            )

        task = engine.create(
            "hourly", schedule=Schedule(kind=ScheduleKind.INTERVAL, every_seconds=60.0),
            approved=True, now=CLOCK,
        )

        first = await engine.run_now(task.id, failing, now=CLOCK)
        self.assertIs(first.status, AutomationRunStatus.FAILED)
        under_limit = engine.get(task.id)
        self.assertEqual(under_limit.failure_count, 1)
        self.assertTrue(under_limit.enabled)

        await engine.run_now(task.id, failing, now=CLOCK + timedelta(minutes=1))
        broken = engine.get(task.id)
        self.assertEqual(broken.failure_count, 2)
        self.assertIs(broken.status, AutomationStatus.FAILED)
        self.assertFalse(broken.enabled)
        self.assertIsNone(broken.next_run_at)

    async def test_a_runner_that_raises_is_a_recorded_failure_not_a_crash(self) -> None:
        engine = self._engine(permissions=_risk())

        async def exploding(_task):
            raise RuntimeError("the runner broke")

        task = engine.create("runs", schedule=_daily_at(), approved=True, now=CLOCK)
        run = await engine.run_now(task.id, exploding, now=CLOCK)

        self.assertIs(run.status, AutomationRunStatus.FAILED)
        self.assertIn("the runner broke", run.detail)
        self.assertEqual(run.failure_kind, "RuntimeError")
        self.assertEqual(engine.get(task.id).failure_count, 1)

    async def test_nothing_runs_through_an_unapproved_permission_gate(self) -> None:
        engine = self._engine(permissions=_risk())

        async def runner(_task):  # pragma: no cover - must never be reached
            raise AssertionError("an unapproved automation ran")

        task = engine.create("runs", schedule=_daily_at(), now=CLOCK)
        run = await engine.run_now(task.id, runner, now=CLOCK)

        self.assertIs(run.status, AutomationRunStatus.DENIED)
        self.assertIn("confirmation", run.detail.lower())
        stored = engine.get(task.id)
        self.assertEqual(stored.failure_count, 0)
        self.assertFalse(stored.enabled)
        self.assertIs(stored.status, AutomationStatus.PENDING_APPROVAL)

    async def test_no_permission_layer_is_a_refusal_not_permission(self) -> None:
        engine = self._engine()  # no permissions wired

        async def runner(_task):  # pragma: no cover - must never be reached
            raise AssertionError("work ran with no permission layer")

        task = engine.create("runs", schedule=_daily_at(), approved=True, now=CLOCK)
        run = await engine.run_now(task.id, runner, now=CLOCK)

        self.assertIs(run.status, AutomationRunStatus.DENIED)
        self.assertIn("no gate", run.detail)

    async def test_an_approved_task_runs_through_the_gate(self) -> None:
        engine = self._engine(permissions=_risk())
        ran: list[str] = []

        async def runner(task):
            ran.append(task.id)
            return AutomationOutcome(AutomationRunStatus.COMPLETED, verified=True)

        task = engine.create("runs", schedule=_daily_at(), now=CLOCK)
        engine.approve(task.id, now=CLOCK)
        run = await engine.run_now(task.id, runner, now=CLOCK)

        self.assertEqual(ran, [task.id])
        self.assertIs(run.status, AutomationRunStatus.COMPLETED)
        self.assertTrue(run.permission.get("allow", False))

    async def test_an_unmet_condition_skips_the_run_without_a_failure(self) -> None:
        engine = self._engine(
            permissions=_risk(), evaluate_condition=lambda _c: (False, "not today")
        )

        async def runner(_task):  # pragma: no cover - must never be reached
            raise AssertionError("a skipped automation ran")

        task = engine.create(
            "runs",
            schedule=_daily_at(),
            condition=AutomationCondition(ConditionKind.TESTS_PASS),
            approved=True,
            now=CLOCK,
        )
        run = await engine.run_now(task.id, runner, now=CLOCK)

        self.assertIs(run.status, AutomationRunStatus.SKIPPED)
        stored = engine.get(task.id)
        self.assertEqual(stored.failure_count, 0)
        self.assertTrue(stored.enabled)
        self.assertIs(stored.status, AutomationStatus.SCHEDULED)

    async def test_a_met_condition_lets_the_run_proceed(self) -> None:
        engine = self._engine(
            permissions=_risk(), evaluate_condition=lambda _c: (True, "green")
        )

        async def runner(_task):
            return AutomationOutcome(AutomationRunStatus.COMPLETED)

        task = engine.create(
            "runs",
            schedule=_daily_at(),
            condition=AutomationCondition(ConditionKind.TESTS_PASS),
            approved=True,
            now=CLOCK,
        )
        run = await engine.run_now(task.id, runner, now=CLOCK)

        self.assertIs(run.status, AutomationRunStatus.COMPLETED)

    async def test_run_history_is_bounded(self) -> None:
        engine = self._engine(permissions=_risk(), run_history=2)

        async def runner(_task):
            return AutomationOutcome(AutomationRunStatus.COMPLETED)

        task = engine.create(
            "hourly", schedule=Schedule(kind=ScheduleKind.INTERVAL, every_seconds=60.0),
            approved=True, now=CLOCK,
        )
        for index in range(3):
            await engine.run_now(task.id, runner, now=CLOCK + timedelta(minutes=index))

        stored = engine.get(task.id)
        self.assertEqual(stored.run_count, 3)
        self.assertEqual(len(stored.runs), 2)

    async def test_find_returns_every_match_rather_than_guessing(self) -> None:
        engine = self._engine()
        engine.create("back up the project", schedule=_daily_at(), now=CLOCK)
        engine.create("back up the database", schedule=_daily_at(), now=CLOCK)

        self.assertEqual(len(engine.find("back up")), 2)
        self.assertEqual(len(engine.find("database")), 1)
        self.assertEqual(engine.find("nothing like it"), ())

    async def test_report_summarizes_what_is_stored(self) -> None:
        engine = self._engine(max_failures=4)
        engine.create("waits", schedule=_daily_at(), now=CLOCK)
        engine.create("runs", schedule=_daily_at(), approved=True, now=CLOCK)

        report = engine.report()
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["enabled"], 1)
        self.assertEqual(report["pending_approval"], 1)
        self.assertEqual(report["max_failures"], 4)
        self.assertEqual(report["run_scope"], AUTOMATION_RUN_SCOPE)
        self.assertIsNotNone(report["next_due"])

    async def test_engine_state_round_trips_and_skips_damaged_records(self) -> None:
        engine = self._engine()
        engine.create("waits", schedule=_daily_at(), now=CLOCK)
        engine.create("runs", schedule=_once_in(30), approved=True, now=CLOCK)
        payload = engine.to_dict()

        payload["tasks"].append({"request": "damaged"})  # unreadable, must be skipped
        restored = AutomationEngine.from_dict(payload, publish=self._publish)

        self.assertEqual(len(restored.tasks()), 2)
        self.assertEqual(
            {task.request for task in restored.tasks()}, {"waits", "runs"}
        )
        once = next(
            task for task in restored.tasks() if task.request == "runs"
        )
        self.assertIs(once.kind, AutomationKind.ONCE)
        self.assertIs(restored.get(once.id).status, AutomationStatus.SCHEDULED)


class SchedulePlanTests(unittest.TestCase):
    """A scheduling request is a plan with a confirmation-gated write step."""

    def _plan(self, goal: str, capability: str):
        return PlanCompiler().compile(goal, decision={"selected_capability": capability})

    def test_a_scheduling_capability_compiles_to_a_schedule_step(self) -> None:
        goal = "every Monday at 9 generate a project report"
        plan = self._plan(goal, "create_automation")

        self.assertFalse(plan.needs_clarification)
        self.assertEqual([step.id for step in plan.steps], ["schedule-automation"])
        step = plan.steps[0]
        self.assertEqual(step.action, "schedule_task")
        self.assertEqual(step.parameters["request"], goal)
        # Storing a task changes what this machine will do on its own later, so
        # it is a write and the executor requires confirmation for it.
        self.assertIs(step.effect, StepEffect.LOCAL_WRITE)
        self.assertTrue(step.expected_result)

    def test_a_run_capability_compiles_to_a_run_step(self) -> None:
        plan = self._plan("run my backup", "run_automation")

        self.assertEqual([step.id for step in plan.steps], ["run-automation"])
        step = plan.steps[0]
        self.assertEqual(step.action, "run_automation")
        self.assertEqual(step.parameters["request"], "run my backup")
        self.assertIs(step.effect, StepEffect.LOCAL_WRITE)

    def test_an_unrelated_capability_does_not_become_an_automation(self) -> None:
        plan = self._plan("open notepad", "desktop_automation")

        self.assertNotIn("schedule_task", [step.action for step in plan.steps])
        self.assertNotIn("run_automation", [step.action for step in plan.steps])


class AutomationApplicationTests(unittest.IsolatedAsyncioTestCase):
    """The application's scheduling surfaces, end to end where it is cheap."""

    async def test_scheduling_without_a_stated_time_is_refused(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = NovaControlApplication(data_dir=temp_dir)
            try:
                with self.assertRaises(ValueError):
                    app.schedule_automation("push my project")
            finally:
                await app.stop()

    async def test_a_spoken_schedule_is_stored_pending_and_not_run(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = NovaControlApplication(data_dir=temp_dir)
            try:
                stored = app.schedule_automation(
                    "Remind me at 6 PM to push my project."
                )

                self.assertEqual(stored["request"], "push my project")
                self.assertEqual(stored["schedule"]["kind"], "once")
                self.assertIs(stored["status"], "pending_approval")
                self.assertFalse(stored["enabled"])
                self.assertTrue(stored["requires_approval"])
                self.assertEqual(stored["permissions"], [AUTOMATION_RUN_SCOPE])
                self.assertIsNone(stored["last_run_at"])
            finally:
                await app.stop()

    async def test_a_spoken_schedule_mention_plans_but_does_not_execute(self) -> None:
        """The clause the phase is really about, driven through handle_request.

        A sentence that merely MENTIONS a schedule must travel the ordinary path
        — intent detection, decision, planning — and the work it mentions must
        not run. What it produces is a stored automation, disarmed, waiting for
        approval; nothing was pushed and no run was recorded.
        """
        with TemporaryDirectory() as temp_dir:
            app = NovaControlApplication(data_dir=temp_dir)
            await app.start()
            try:
                response = await app.handle_request(
                    "Remind me at 6 PM to push my project."
                )

                self.assertEqual(response.route, "planning")
                tasks = app.automation_tasks()
                self.assertEqual(len(tasks), 1)
                stored = tasks[0]
                self.assertEqual(stored["request"], "push my project")
                self.assertEqual(stored["schedule"]["kind"], "once")
                self.assertEqual(stored["status"], "pending_approval")
                self.assertFalse(stored["enabled"])
                self.assertIsNone(stored["last_run_at"])
                self.assertEqual(stored["run_count"], 0)
                # The request itself was audited as a request; no row claims an
                # automation ran, because none did.
                rows = app.audit_entries()
                self.assertTrue(rows)
                self.assertTrue(all(row["source"] == "request" for row in rows))
            finally:
                await app.stop()

    async def test_enable_approve_cancel_and_disable_surfaces(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = NovaControlApplication(data_dir=temp_dir)
            try:
                stored = app.schedule_automation("push my project", schedule=_daily_at())
                automation_id = stored["automation_id"]

                with self.assertRaises(PermissionError):
                    app.enable_automation(automation_id)

                self.assertTrue(app.approve_automation(automation_id)["enabled"])
                self.assertIs(
                    app.disable_automation(automation_id)["status"], "disabled"
                )
                self.assertTrue(app.enable_automation(automation_id)["enabled"])
                self.assertIs(app.cancel_automation(automation_id)["status"], "cancelled")
                self.assertEqual(app.automation_tasks()[0]["status"], "cancelled")
            finally:
                await app.stop()

    async def test_a_scheduled_request_runs_through_the_ordinary_request_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = NovaControlApplication(data_dir=temp_dir)
            await app.start()
            try:
                stored = app.schedule_automation(
                    "open notepad",
                    schedule=parse_schedule("in 10 minutes"),
                    authorize=True,
                )
                automation_id = stored["automation_id"]

                run = await app.run_automation(automation_id)

                self.assertIs(run["status"], "completed")
                self.assertEqual(run["automation_id"], automation_id)
                tasks = {task["automation_id"]: task for task in app.automation_tasks()}
                self.assertEqual(tasks[automation_id]["run_count"], 1)
                self.assertIs(
                    tasks[automation_id]["last_outcome"], "completed"
                )
                # The run's request was audited under the automation, not as a
                # standalone request: the thread of work is followable.
                records = app.audit_entries(10)
                automation_rows = [
                    row for row in records if row["automation_id"] == automation_id
                ]
                self.assertTrue(automation_rows, records)
                self.assertEqual(automation_rows[-1]["source"], "automation")
            finally:
                await app.stop()

    async def test_due_automations_do_not_run_when_the_scheduler_is_off(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = NovaControlApplication(data_dir=temp_dir)
            await app.start()
            try:
                app.schedule_automation(
                    "open notepad",
                    schedule=parse_schedule("in 10 minutes"),
                    authorize=True,
                )
                app.settings.update(automation_enabled=False)

                self.assertFalse(app.automation_enabled())
                self.assertEqual(await app.run_due_automations(now=CLOCK), [])
            finally:
                await app.stop()

    async def test_due_automations_run_an_armed_task_once(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = NovaControlApplication(data_dir=temp_dir)
            await app.start()
            try:
                stored = app.schedule_automation(
                    "open notepad",
                    schedule=parse_schedule("in 10 minutes"),
                    authorize=True,
                )
                automation_id = stored["automation_id"]

                runs = await app.run_due_automations(
                    now=datetime.fromisoformat(stored["next_run_at"]) + timedelta(seconds=1)
                )

                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0]["status"], "completed")
                tasks = {task["automation_id"]: task for task in app.automation_tasks()}
                self.assertEqual(tasks[automation_id]["run_count"], 1)
                # The one-time task has consumed its instant.
                self.assertEqual(
                    await app.run_due_automations(now=CLOCK + timedelta(days=1)), []
                )
            finally:
                await app.stop()

    async def test_the_app_evaluates_a_condition_before_a_scheduled_run(self) -> None:
        """The live evaluator, not an injected one: a path that is not there yet."""
        with TemporaryDirectory() as temp_dir:
            app = NovaControlApplication(data_dir=temp_dir)
            await app.start()
            try:
                marker = Path(temp_dir) / "ready.flag"
                stored = app.schedule_automation(
                    "open notepad",
                    schedule=_daily_at(),
                    condition=AutomationCondition(ConditionKind.PATH_EXISTS, str(marker)),
                    authorize=True,
                )
                first_due = datetime.fromisoformat(stored["next_run_at"]) + timedelta(seconds=1)

                # Nothing has created the file, so the run is skipped — not
                # failed — and the task stays armed for its next occurrence.
                skipped = await app.run_due_automations(now=first_due)
                self.assertEqual(len(skipped), 1)
                self.assertIs(skipped[0]["status"], "skipped")
                self.assertEqual(app.automation_tasks()[0]["failure_count"], 0)

                # Once the file exists, the next occurrence runs.
                marker.write_text("ready", encoding="utf-8")
                runs = await app.run_due_automations(now=first_due + timedelta(days=1))
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0]["status"], "completed")
            finally:
                await app.stop()

    async def test_the_ticker_is_running_and_counts_its_ticks(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = NovaControlApplication(data_dir=temp_dir)
            await app.start()
            try:
                stored = app.schedule_automation(
                    "open notepad",
                    schedule=parse_schedule("in 10 minutes"),
                    authorize=True,
                )

                # The background loop exists, has not fired at boot, and a tick
                # runs what is due and is reflected in the reported state.
                started = app.automation_status()
                self.assertTrue(started["running"])
                self.assertEqual(started["ticks"], 0)

                runs = await app._automation_tick(
                    now=datetime.fromisoformat(stored["next_run_at"]) + timedelta(seconds=1)
                )

                self.assertEqual(len(runs), 1)
                status = app.automation_status()
                self.assertEqual(status["ticks"], 1)
                self.assertEqual(status["tasks"][0]["run_count"], 1)
            finally:
                await app.stop()

    async def test_an_automation_already_holding_a_schedule_never_re_schedules_itself(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = NovaControlApplication(data_dir=temp_dir)
            await app.start()
            try:
                # A request that still states a schedule would create another
                # automation every time it ran; the run reports that rather than
                # looping.
                stored = app.schedule_automation(
                    "push my project", schedule=_daily_at(), authorize=True
                )
                tasks = {task["automation_id"]: task for task in app.automation_tasks()}
                automation_id = stored["automation_id"]
                # Force the stored request to carry a schedule, as a hand-edited
                # state file could.
                from novacontrol.automation import AutomationTask

                task = app.automation_engine.get(automation_id)
                app.automation_engine._tasks[automation_id] = AutomationTask.from_dict(
                    {**task.to_dict(), "request": "in 5 minutes push my project"}
                )
                outcome = await app._run_automation_task(
                    app.automation_engine.get(automation_id)
                )

                self.assertIs(outcome.status, AutomationRunStatus.SKIPPED)
                self.assertEqual(
                    len(app.automation_engine.tasks()), len(tasks)
                )
            finally:
                await app.stop()

    async def test_a_failed_run_is_counted_on_the_task(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = NovaControlApplication(data_dir=temp_dir)
            await app.start()
            try:
                stored = app.schedule_automation(
                    "open notepad", schedule=_daily_at(), authorize=True
                )
                automation_id = stored["automation_id"]

                async def failing(_task):
                    return AutomationOutcome(
                        AutomationRunStatus.FAILED, detail="no", failure_kind="no"
                    )

                app.automation_engine.max_failures = 1
                await app.automation_engine.run_now(automation_id, failing, now=CLOCK)

                task = app.automation_engine.get(automation_id)
                self.assertEqual(task.failure_count, 1)
                self.assertIs(task.status, AutomationStatus.FAILED)
            finally:
                await app.stop()


if __name__ == "__main__":
    unittest.main()
