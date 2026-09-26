"""Phase 9.1: the lifecycle vocabulary, published from the live path.

An event system is only worth having if the events actually fire, so nothing
here tests the bus in isolation (``tests/test_events.py`` does that). These
tests drive the APPLICATION — a request, a plan, a task being paused, a tool
being called, a check being made — and read the stream afterwards.

Two properties are checked over and over, because they are the two that turn a
log into an event system:

  * every event of one request answers ``what happened?`` — it carries the
    request's own correlation id, so a whole thread of work can be assembled
    from the history without guessing;
  * a broken WATCHER cannot break the work. A handler that raises is reported
    and isolated, and the request that announced itself completes anyway.
"""

from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from typing import Any

from novacontrol.application import NovaControlApplication
from novacontrol.core.events import Event, EventType
from novacontrol.core.security import ApprovalDecision, PermissionScope
from novacontrol.planning import (
    Plan,
    PlanStep,
    PlanStepStatus,
    RetryPolicy,
    StepEffect,
    VerificationMethod,
    VerificationSpec,
)
from novacontrol.tools import FunctionTool, ToolRequest, ToolSchema

#: A real 1x1 PNG: the vision path must be given an image, not a file that
#: happens to have the right extension.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _Recorder:
    """A wildcard subscriber that keeps every event it was shown."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [event.type for event in self.events]

    def of(self, type_: EventType | str) -> list[Event]:
        wanted = str(type_)
        return [event for event in self.events if event.type == wanted]

    def payloads(self, type_: EventType | str) -> list[dict[str, Any]]:
        return [dict(event.payload) for event in self.of(type_)]


class LifecycleEventTests(unittest.IsolatedAsyncioTestCase):
    """9.1 through the application: subscription, correlation, history."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = NovaControlApplication(data_dir=Path(self._tmp.name))
        await self.app.start()
        self.recorder = _Recorder()
        await self.app.event_bus.subscribe("*", self.recorder)

    async def asyncTearDown(self) -> None:
        await self.app.stop()
        self._tmp.cleanup()

    # -- a request's whole path -------------------------------------------------

    async def test_a_request_announces_its_own_path_under_one_correlation_id(self) -> None:
        response = await self.app.handle_request("how much RAM is free?")

        self.assertEqual(response.route, "system")
        types = self.recorder.types()
        for expected in (
            EventType.TASK_STARTED.value,
            EventType.INTENT_DETECTED.value,
            EventType.CONTEXT_RESOLVED.value,
            EventType.DECISION_CREATED.value,
            EventType.TASK_COMPLETED.value,
        ):
            self.assertIn(expected, types)

        # Everything this request caused carries the request's own id: that is
        # what makes the history a thread rather than a pile.
        caused = [
            event
            for event in self.recorder.events
            if event.type
            in (
                EventType.TASK_STARTED.value,
                EventType.INTENT_DETECTED.value,
                EventType.CONTEXT_RESOLVED.value,
                EventType.DECISION_CREATED.value,
                EventType.TASK_COMPLETED.value,
            )
        ]
        self.assertEqual({event.correlation_id for event in caused}, {caused[0].correlation_id})

    async def test_the_intent_event_carries_what_was_understood(self) -> None:
        await self.app.handle_request("how much RAM is free?")

        intent = self.recorder.payloads(EventType.INTENT_DETECTED)[0]
        self.assertTrue(intent["intent"])
        self.assertEqual(intent["strategy"], "fast_path")
        self.assertGreater(intent["confidence"], 0.0)
        self.assertFalse(intent["has_image"])

    async def test_the_decision_event_carries_the_route_it_chose(self) -> None:
        await self.app.handle_request("how much RAM is free?")

        decision = self.recorder.payloads(EventType.DECISION_CREATED)[0]
        self.assertEqual(decision["route"], "system_tools")
        self.assertEqual(decision["decision_type"], "deterministic")
        self.assertEqual(decision["capability"], "memory_status")

    async def test_the_tool_a_request_would_reach_is_announced_before_anything_runs(self) -> None:
        await self.app.handle_request("how much RAM is free?")
        await asyncio.sleep(0)  # a scheduled publish runs on the next tick

        selected = self.recorder.payloads(EventType.TOOL_SELECTED)
        self.assertTrue(selected)
        self.assertEqual(selected[0]["tool"], "system_monitor")
        # The provenance rides as ``selection_source``: ``source`` is the
        # envelope's own field and cannot be a payload key at all.
        self.assertIn(selected[0]["selection_source"], ("declared", "registered", "executor"))
        self.assertEqual(
            [item["tool"] for item in self.recorder.payloads(EventType.TOOL_STARTED)],
            [],
        )

    async def test_work_the_request_starts_carries_the_requests_own_id(self) -> None:
        """Depth costs nothing: a tool call a handler makes is THAT request's work.

        Injected the way the plan tests inject a step handler: the request path
        is the real one, and the work inside it belongs to this test.
        """

        async def handler(_app: Any, _request: Any, _text: str) -> tuple[str, dict[str, Any]]:
            await _app.tool_executor.execute(
                ToolRequest(tool_name="machine_facts", arguments={}, reason="a test asked")
            )
            return "probe", {"summary": "the request did its work"}

        # A COPY, assigned to the instance: ``_HANDLERS`` is a class attribute,
        # so mutating it in place would hand this test's handler to every other
        # application in the process (see tests/test_model_pipeline_phase7.py).
        self.app._HANDLERS = dict.fromkeys(self.app._HANDLERS, handler)

        await self.app.handle_request("do the work")
        await asyncio.sleep(0)

        request_ids = {event.correlation_id for event in self.recorder.of(EventType.INTENT_DETECTED)}
        tool_ids = {event.correlation_id for event in self.recorder.of(EventType.TOOL_STARTED)}
        self.assertTrue(tool_ids)
        self.assertEqual(tool_ids, request_ids)

    async def test_a_payload_field_may_not_shadow_the_envelopes_own_fields(self) -> None:
        """The mistake that made ``tool.selected`` silently never fire."""
        with self.assertRaises(TypeError):
            self.app._announce_soon(
                EventType.TOOL_SELECTED, tool="machine_facts", source="declared"
            )

    async def test_the_history_answers_what_just_happened(self) -> None:
        await self.app.handle_request("how much RAM is free?")

        recent = self.app.event_bus.recent(limit=3)

        self.assertTrue(recent)
        self.assertEqual(recent[-1].type, EventType.TASK_COMPLETED.value)
        self.assertEqual(
            [event.type for event in self.app.event_bus.recent(type_="task.completed")],
            [EventType.TASK_COMPLETED.value],
        )

    # -- a broken watcher -------------------------------------------------------

    async def test_a_failing_watcher_cannot_fail_the_request(self) -> None:
        """The specification's own hazard: one bad handler must not take the system."""
        seen: list[str] = []

        async def explode(_event: Event) -> None:
            raise RuntimeError("this watcher is broken")

        async def still_here(event: Event) -> None:
            seen.append(event.type)

        await self.app.event_bus.subscribe(EventType.DECISION_CREATED.value, explode)
        await self.app.event_bus.subscribe(EventType.TASK_COMPLETED.value, still_here)

        response = await self.app.handle_request("how much RAM is free?")

        self.assertEqual(response.route, "system")
        self.assertIn(EventType.TASK_COMPLETED.value, seen)
        # ...and the failure is still on the record, for whoever is watching.
        self.assertIn(EventType.DECISION_CREATED.value, self.recorder.types())

    async def test_the_bus_reports_a_broken_handler_rather_than_swallowing_it(self) -> None:
        async def explode(_event: Event) -> None:
            raise RuntimeError("this watcher is broken")

        await self.app.event_bus.subscribe(EventType.INTENT_DETECTED.value, explode)

        result = await self.app.event_bus.publish(
            Event.of(EventType.INTENT_DETECTED.value, intent="demo")
        )

        self.assertEqual(result.delivered, 1)  # the recorder
        self.assertTrue(result.failures)
        self.assertIn("this watcher is broken", str(result.failures[0].error))

    # -- plans ------------------------------------------------------------------

    async def test_a_plan_is_announced_with_its_steps(self) -> None:
        plan = self.app.plan_for("find my project and run the tests and explain why they fail")
        await asyncio.sleep(0)  # a scheduled publish runs on the next tick

        announced = self.recorder.payloads(EventType.PLAN_CREATED)[0]
        self.assertEqual(announced["goal"], plan.goal)
        self.assertEqual(announced["steps"], tuple(step.id for step in plan.steps))
        self.assertEqual(announced["titles"], tuple(step.title for step in plan.steps))

    async def test_a_running_plan_announces_its_checks_and_its_recovery(self) -> None:
        attempts: list[int] = []

        async def flaky(step: PlanStep, _context: Any) -> dict[str, Any]:
            attempts.append(1)
            # The first attempt locates nothing (so the check fails), the retry
            # locates the directory (so the same check passes).
            return (
                {"path": "/tmp/project", "summary": "found it", "changed": True}
                if len(attempts) > 1
                else {"path": "", "summary": "nothing there", "changed": False}
            )

        step = PlanStep(
            id="locate-project",
            title="Locate the project",
            description="Find the project directory.",
            action="locate_project",
            effect=StepEffect.READ_ONLY,
            verification=VerificationSpec(
                method=VerificationMethod.STATE_OBSERVED,
                expect=True,
                description="the project was located",
            ),
        )
        self.app.workflow_executor.step_handler = flaky
        self.app.workflow_executor.retry_policy = RetryPolicy(max_attempts=2)

        # The first attempt's check fails (the handler returns no "changed"
        # state), so the advisor retries and that recovery is announced too.
        result = await self.app.workflow_executor.execute(Plan(goal="a goal", steps=(step,)))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(result.step_results["locate-project"].status, PlanStepStatus.COMPLETED)

        self.assertEqual(
            self.recorder.payloads(EventType.VERIFICATION_STARTED)[0]["step_id"], "locate-project"
        )
        statuses = [item["status"] for item in self.recorder.payloads(EventType.VERIFICATION_COMPLETED)]
        self.assertEqual(statuses, ["fail", "pass"])
        recovery = self.recorder.payloads(EventType.RECOVERY_STARTED)
        self.assertEqual(recovery[0]["step_id"], "locate-project")
        self.assertEqual(recovery[0]["action"], "retry")
        self.assertEqual(
            self.recorder.payloads(EventType.RECOVERY_COMPLETED)[0]["outcome"], "recovered"
        )

    # -- tasks ------------------------------------------------------------------

    async def test_a_task_control_command_is_announced_from_the_transition(self) -> None:
        control = self.app.task_control
        machine = control.open_task(task_id="watched-1")
        machine.start_planning()
        machine.start_running()

        control.pause("watched-1")
        await asyncio.sleep(0)
        control.resume("watched-1")
        await asyncio.sleep(0)

        self.assertEqual(
            [item["task_id"] for item in self.recorder.payloads(EventType.TASK_STARTED)],
            ["watched-1"],
        )
        paused = self.recorder.payloads(EventType.TASK_PAUSED)[0]
        self.assertEqual(paused["task_id"], "watched-1")
        self.assertEqual(paused["previous"], "running")
        # A resume is a resume: the transition out of PAUSED must not be
        # reported as the task starting over.
        resumed = self.recorder.payloads(EventType.TASK_RESUMED)[0]
        self.assertEqual(resumed["task_id"], "watched-1")
        self.assertEqual(resumed["previous"], "paused")

    async def test_a_task_cancelled_before_it_ran_says_so_at_once(self) -> None:
        self.app.task_control.open_task(task_id="watched-2")

        self.app.task_control.cancel("watched-2", reason="the caller changed its mind")
        await asyncio.sleep(0)

        cancelled = self.recorder.payloads(EventType.TASK_CANCELLED)[0]
        self.assertEqual(cancelled["task_id"], "watched-2")
        self.assertEqual(cancelled["state"], "cancelled")

    async def test_a_running_task_is_announced_cancelled_when_it_actually_stops(self) -> None:
        """A cancellation is a REQUEST until the work yields, and the event says so."""
        control = self.app.task_control
        machine = control.open_task(task_id="watched-4")
        machine.start_planning()
        machine.start_running()

        control.cancel("watched-4", reason="stop")
        await asyncio.sleep(0)
        self.assertEqual(self.recorder.payloads(EventType.TASK_CANCELLED), [])

        with self.assertRaises(Exception):
            await control.checkpoint("watched-4", step="between steps")
        await asyncio.sleep(0)

        cancelled = self.recorder.payloads(EventType.TASK_CANCELLED)[0]
        self.assertEqual(cancelled["task_id"], "watched-4")
        self.assertEqual(cancelled["state"], "cancelled")

    async def test_a_task_that_could_not_be_started_is_not_announced(self) -> None:
        """Only transitions publish: opening a task is not work beginning."""
        machine = self.app.task_control.open_task(task_id="watched-3")

        self.assertFalse(machine.terminal)
        await asyncio.sleep(0)
        self.assertEqual(self.recorder.payloads(EventType.TASK_STARTED), [])
        self.assertEqual(self.recorder.payloads(EventType.TASK_COMPLETED), [])

    # -- tools ------------------------------------------------------------------

    async def test_a_tool_call_announces_what_it_did(self) -> None:
        result = await self.app.tool_executor.execute(
            ToolRequest(tool_name="machine_facts", arguments={}, reason="a test asked")
        )

        self.assertEqual(result.status.value, "completed")
        started = self.recorder.payloads(EventType.TOOL_STARTED)[0]
        self.assertEqual(started["tool"], "machine_facts")
        completed = self.recorder.payloads(EventType.TOOL_COMPLETED)[0]
        self.assertEqual(completed["tool"], "machine_facts")
        self.assertEqual(completed["status"], "completed")

    async def test_a_tool_that_is_not_approved_is_announced_as_a_refusal(self) -> None:
        """A denial is not a completion: a watcher must not read it as work done."""
        self._register_probe("needs_approval", required_permissions=(PermissionScope.SHELL_EXECUTE,))
        self.app.tool_executor.approval_gateway = _RefusingGateway()

        result = await self.app.tool_executor.execute(
            ToolRequest(tool_name="needs_approval", arguments={})
        )

        self.assertEqual(result.status.value, "denied")
        self.assertEqual(self.recorder.payloads(EventType.TOOL_COMPLETED), [])
        failed = self.recorder.payloads(EventType.TOOL_FAILED)[0]
        self.assertEqual(failed["tool"], "needs_approval")
        self.assertEqual(failed["status"], "denied")
        self.assertIn("not this time", failed["error"])

    async def test_a_tool_that_raises_is_announced_with_its_error(self) -> None:
        def boom(_arguments: Any) -> None:
            raise ValueError("the tool broke")

        self._register_probe("fragile_probe", handler=boom)

        result = await self.app.tool_executor.execute(
            ToolRequest(tool_name="fragile_probe", arguments={})
        )

        self.assertEqual(result.status.value, "failed")
        failed = self.recorder.payloads(EventType.TOOL_FAILED)[-1]
        self.assertEqual(failed["error"], "ValueError: the tool broke")

    def _register_probe(
        self,
        name: str,
        *,
        handler: Any = None,
        required_permissions: tuple[Any, ...] = (),
    ) -> None:
        """A tool this test owns, so the assertions do not depend on the catalogue."""

        async def ok(_arguments: Any) -> dict[str, Any]:
            return {"ok": True}

        self.app.tools.register(
            FunctionTool(
                name,
                ToolSchema(name, "A probe this test registered."),
                handler or ok,
                required_permissions=required_permissions,
            )
        )

    # -- models and senses ------------------------------------------------------

    async def test_reading_an_attached_image_is_announced(self) -> None:
        image = Path(self._tmp.name) / "shot.png"
        image.write_bytes(_PNG)

        await self.app.handle_request("what does this say?", image=str(image))

        self.assertEqual(
            self.recorder.payloads(EventType.VISION_STARTED)[0]["image"], str(image)
        )
        completed = self.recorder.payloads(EventType.VISION_COMPLETED)[0]
        self.assertEqual(completed["image"], str(image))
        self.assertEqual(completed["answered"], False)  # a 1x1 PNG says nothing

    async def test_an_image_that_could_not_be_read_is_announced_as_answered_false(self) -> None:
        missing = Path(self._tmp.name) / "nothing-here.png"

        await self.app.handle_request("what does this say?", image=str(missing))

        completed = self.recorder.payloads(EventType.VISION_COMPLETED)
        self.assertTrue(completed)
        self.assertFalse(completed[0]["answered"])

    async def test_unloading_models_announces_what_was_actually_released(self) -> None:
        # Naming a model that is not loaded keeps this test off any runtime the
        # machine running it may have. The point is the CONTRACT: the
        # announcement follows the release rather than being printed because a
        # call was made, so what is announced is exactly what the manager said
        # it released.
        report = await self.app.unload_model("a-model-that-is-not-loaded")

        self.assertEqual(report["models"], [])
        self.assertEqual(self.recorder.payloads(EventType.MODEL_UNLOADED), [])


class _RefusingGateway:
    """An approval gateway that answers no, and says why."""

    async def request_approval(self, request: Any) -> Any:
        return ApprovalDecision(
            request_id=request.id,
            approved=False,
            decided_by="test",
            reason="not this time",
        )


if __name__ == "__main__":
    unittest.main()
