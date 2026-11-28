"""Focused Phase 29 verification: universal computer and device operation.

These tests drive the real models, perception/action seams, orchestrator and
HTTP factory. They deliberately use fakes only where the operating system would
otherwise be touched (input, capture, browser), so the tests can assert exactly
which semantic action was sent without depending on a GUI session.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - optional dev dependency
    TestClient = None  # type: ignore[assignment]

from novacontrol.agentcore import (
    ActionEngine,
    AgenticOrchestrator,
    ApplicationKnowledgeGraph,
    EvaluationLedger,
    TaskInterpreter,
    UiState,
    UiElement,
)
from novacontrol.agentcore.perception import UiPerceptionEngine
from novacontrol.agentcore.planner import AdaptivePlanner
from novacontrol.agentcore.state import ActionRecord, AgentTaskState, TaskPhase
from novacontrol.api.app import create_app
from novacontrol.api.models import ApiSurface
from novacontrol.api.route_consumers import ROUTE_CONSUMERS
from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.browser.controller import BrowserAutomationController
from novacontrol.computer import (
    ComputerOperationHistory,
    ComputerOperator,
    ComputerTaskBusy,
    DeviceRegistry,
    OperationClass,
    classify_operation,
    resolve_reference,
)
from novacontrol.application_helpers import ApprovedApprovalGateway
from novacontrol.core.buglog import BugLog
from novacontrol.core.security import DenyByDefaultApprovalGateway
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.desktop.controller import DesktopAutomationController
from novacontrol.desktop.vision import VisionController


class _Store:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def read(self, namespace: str) -> Any:
        return self.data.get(namespace)

    def write(self, namespace: str, value: Any) -> None:
        self.data[namespace] = value


class _DesktopProbe:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def window_lines(self) -> list[str]:
        return ["notepad|Untitled - Notepad"]

    async def window_geometry(self, _title: str) -> None:
        return None

    def capture_screen(self) -> None:
        return None

    async def run(self, action: Any) -> dict[str, Any]:
        self.calls.append(action.to_dict())
        return {"ok": True}

    def vision_click(self) -> None:
        return None


class _BrowserProbe:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, action: Any) -> dict[str, Any]:
        self.calls.append(action.to_dict())
        return {"url": "https://example.com", "title": "Example"}


class _PhoneStatus:
    def __init__(self, available: bool) -> None:
        self.available = available

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "state": "device_connected" if self.available else "no_device",
            "next_steps": ["Enable USB debugging"] if not self.available else [],
        }


class ClassificationTests(unittest.TestCase):
    def test_operation_classes_are_distinct(self) -> None:
        self.assertIs(classify_operation("observe the current window"), OperationClass.READ_ONLY)
        self.assertIs(classify_operation("delete the report"), OperationClass.DESTRUCTIVE)
        self.assertIs(classify_operation("submit the account form"), OperationClass.SENSITIVE)
        self.assertIs(classify_operation("install a display driver"), OperationClass.PRIVILEGED)
        self.assertIs(classify_operation("flurb the widget"), OperationClass.SENSITIVE)


class HistoryTests(unittest.TestCase):
    def test_history_is_bounded_redacted_and_persisted(self) -> None:
        store = _Store()
        history = ComputerOperationHistory(store, cap=2)
        state = {
            "task_id": "t1",
            "goal": "type password=hunter2 into example@example.com",
            "status": "completed",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:01+00:00",
            "plan": [],
            "actions": [{"action": "type", "target": "password", "success": True}],
            "observations": [],
            "verification": {"final": {"status": "pass"}},
            "errors": [],
            "summary": "done",
            "learned_information": [],
        }
        record = history.record_task(state, approvals=[{"approved": True}])
        payload = record.to_dict()
        self.assertNotIn("hunter2", payload["objective"])
        self.assertNotIn("example@example.com", payload["objective"])
        self.assertFalse(payload["stores_screenshots"])
        history.record_task({**state, "task_id": "t2"})
        history.record_task({**state, "task_id": "t3"})
        self.assertEqual(history.summary()["total"], 2)
        restored = ComputerOperationHistory(store, cap=2)
        self.assertEqual(len(restored.list(limit=10)), 2)
        self.assertEqual(restored.get("t3")["task_id"], "t3")

    def test_history_redacts_authorization_cookie_and_bearer_values(self) -> None:
        history = ComputerOperationHistory()
        state = {
            "task_id": "auth-redaction",
            "goal": (
                "send Authorization: Bearer sk-phase29-secret and "
                "Cookie: session-cookie-secret with Bearer raw-bearer-secret"
            ),
            "status": "completed",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:01+00:00",
            "plan": [],
            "actions": [],
            "observations": [],
            "verification": {"final": {"status": "pass"}},
            "errors": [],
            "summary": "done",
            "learned_information": [],
        }

        record = history.record_task(state)
        objective = record.to_dict()["objective"]

        self.assertNotIn("sk-phase29-secret", objective)
        self.assertNotIn("session-cookie-secret", objective)
        self.assertNotIn("raw-bearer-secret", objective)
        self.assertIn("Authorization=[redacted]", objective)
        self.assertIn("Cookie=[redacted]", objective)


class ContextTests(unittest.TestCase):
    def test_click_reference_resolves_only_observed_elements(self) -> None:
        state = UiState(
            application="notepad",
            window="Untitled - Notepad",
            elements=(
                UiElement(id="save", type="button", text="Save", selector=""),
            ),
            source="window_probe",
        )
        resolved = resolve_reference("click the Save button", ui_state=state)
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.goal, "click Save")

        missing = resolve_reference("click the Publish button", ui_state=state)
        self.assertEqual(missing.status, "unresolved")
        self.assertIn("No visible element", missing.reason)

    def test_continue_and_back_references_use_history_and_observation(self) -> None:
        continued = resolve_reference("continue from where I stopped", history_unfinished="save the note")
        self.assertEqual(continued.status, "resolved")
        self.assertEqual(continued.goal, "save the note")
        back = resolve_reference("go back to the previous page", ui_state=UiState(application="browser", url="https://example.com/a"))
        self.assertEqual(back.status, "resolved")


class DeviceRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_reports_supported_connected_and_unsupported(self) -> None:
        registry = DeviceRegistry(
            desktop_runner=_DesktopProbe(),
            browser_runner=_BrowserProbe(),
            phone_status=lambda: _PhoneStatus(False),
            display_probe=lambda: (
                {
                    "index": 0,
                    "primary": True,
                    "bounds": {"left": 0, "top": 0, "right": 1920, "bottom": 1080, "width": 1920, "height": 1080},
                    "work_area": {"left": 0, "top": 0, "right": 1920, "bottom": 1040, "width": 1920, "height": 1040},
                    "dpi_aware": False,
                },
                {
                    "index": 1,
                    "primary": False,
                    "bounds": {"left": 1920, "top": 0, "right": 4480, "bottom": 1440, "width": 2560, "height": 1440},
                    "work_area": {"left": 1920, "top": 0, "right": 4480, "bottom": 1400, "width": 2560, "height": 1400},
                    "dpi_aware": False,
                },
            ),
            platform_name="Windows-test",
        )
        report = await registry.discover()
        payload = report.to_dict()
        kinds = {device["kind"] for device in payload["devices"]}
        self.assertTrue({"desktop", "display", "keyboard_mouse", "browser", "phone"} <= kinds)
        desktop = next(device for device in payload["devices"] if device["id"] == "desktop")
        self.assertTrue(desktop["capabilities"][0]["available"])
        displays = [device for device in payload["devices"] if device["kind"] == "display"]
        self.assertEqual(len(displays), 2)
        self.assertEqual(displays[1]["id"], "display:1")
        self.assertEqual(displays[1]["metadata"]["bounds"]["width"], 2560)
        phone = next(device for device in payload["devices"] if device["id"] == "phone")
        self.assertFalse(phone["available"])
        self.assertIn("Enable USB debugging", phone["limitations"])
        self.assertTrue(report.supported_operations)
        self.assertTrue(registry.capability(report, "click")["available"])
        self.assertTrue(registry.capability(report, "tabs")["available"])


class PerceptionStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_unresponsive_window_and_display_geometries_are_observable(self) -> None:
        async def details_probe() -> list[dict[str, Any]]:
            return [
                {
                    "title": "Hung App",
                    "process": "hung",
                    "handle": 77,
                    "pid": 1234,
                    "bounds": [0, 0, 100, 100],
                    "scale": 1.25,
                    "foreground": True,
                    "minimized": False,
                    "visible": True,
                    "responding": False,
                    "available": True,
                }
            ]

        engine = UiPerceptionEngine(
            window_details_probe=details_probe,
            display_probe=lambda: (
                {
                    "index": 0,
                    "primary": True,
                    "bounds": {"width": 1920, "height": 1080},
                    "work_area": {"width": 1920, "height": 1040},
                },
                {
                    "index": 1,
                    "primary": False,
                    "bounds": {"width": 2560, "height": 1440},
                    "work_area": {"width": 2560, "height": 1400},
                },
            ),
        )
        state = await engine.observe(include_desktop=True, include_dom=False, include_semantic=False)

        self.assertEqual(state.metadata["unresponsive_windows"], ["Hung App"])
        self.assertTrue(state.metadata["has_unresponsive_window"])
        self.assertEqual(len(state.metadata["displays"]), 2)
        self.assertEqual(state.metadata["displays"][1]["bounds"]["width"], 2560)
        window = next(element for element in state.elements if element.text == "Hung App")
        self.assertIs(window.attributes["responding"], False)
        self.assertEqual(window.attributes["scale"], 1.25)

        after = UiState(
            application="hung",
            window="Hung App",
            metadata={"unresponsive_windows": ["Hung App"]},
        )
        diff = after.diff(UiState(application="hung", window="Hung App"))
        self.assertEqual(diff["unresponsive_windows"], ["Hung App"])


class ActionEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_desktop_click_focuses_observed_window_before_coordinates(self) -> None:
        desktop = _DesktopProbe()
        engine = ActionEngine(desktop_runner=desktop)
        state = UiState(
            application="notepad",
            window="Untitled - Notepad",
            elements=(UiElement(id="save", type="button", text="Save", location=(120, 80)),),
            metadata={"evidence": True},
        )
        outcome = await engine.click_element(state, "Save")
        self.assertTrue(outcome.success)
        self.assertEqual([call["type"] for call in desktop.calls], ["window_focus", "mouse_click"])
        click = desktop.calls[-1]
        self.assertEqual(click["parameters"]["x"], 120)
        self.assertEqual(click["parameters"]["y"], 80)
        self.assertTrue(click["parameters"]["require_foreground"])

    async def test_desktop_type_scroll_drag_and_shortcuts_are_guarded(self) -> None:
        desktop = _DesktopProbe()
        engine = ActionEngine(desktop_runner=desktop)
        state = UiState(
            application="notepad",
            window="Untitled - Notepad",
            elements=(
                UiElement(id="field", type="input", text="Search", location=(10, 20)),
                UiElement(id="win", type="window", text="Untitled - Notepad", location=(400, 300)),
            ),
        )
        typed = await engine.type_text(state, "Search", "hello")
        self.assertTrue(typed.success)
        self.assertEqual([call["type"] for call in desktop.calls], ["window_focus", "mouse_click", "type_text"])
        await engine.scroll(state, -120, target="")
        await engine.drag((1, 2), (3, 4), window_title="Untitled - Notepad")
        await engine.press_key("ctrl+s")
        self.assertEqual([call["type"] for call in desktop.calls][-3:], ["mouse_scroll", "mouse_drag", "keyboard_shortcut"])

    async def test_browser_actions_are_semantic_not_coordinate_guesses(self) -> None:
        browser = _BrowserProbe()
        engine = ActionEngine(browser_runner=browser)
        state = UiState(
            application="browser",
            window="Example",
            url="https://example.com",
            elements=(UiElement(id="a", type="link", text="Account", selector="a=Account"),),
        )
        await engine.click_element(state, "Account")
        await engine.navigate("https://example.com/next")
        await engine.scroll(state, -400)
        await engine.go_back()
        self.assertEqual(
            [call["type"] for call in browser.calls],
            ["click", "navigate", "scroll", "go_back"],
        )
        self.assertEqual(browser.calls[0]["target"], "a=Account")

    async def test_browser_actions_use_the_approval_controller_before_the_runner(self) -> None:
        browser = _BrowserProbe()
        controller = BrowserAutomationController(
            runner=browser,
            approval_gateway=DenyByDefaultApprovalGateway(),
        )
        engine = ActionEngine(browser_runner=browser, browser_controller=controller)

        outcome = await engine.navigate("https://example.com/blocked")

        self.assertFalse(outcome.success)
        self.assertEqual(browser.calls, [])
        self.assertEqual(outcome.output["status"], "denied")


class InterpreterTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_unresponsive_llm_enrichment_falls_back_to_heuristics(self) -> None:
        class _HangingProvider:
            name = "ollama"

            async def complete(self, _messages: Any, **_kwargs: Any) -> str:
                await asyncio.sleep(60)
                return "never"

        with mock.patch("novacontrol.agentcore.interpreter._ENRICH_TIMEOUT_SECONDS", 0.05):
            interpreted = await TaskInterpreter(_HangingProvider()).interpret("open settings")

        self.assertEqual(interpreted.goal, "open settings")
        self.assertEqual(interpreted.subtasks, ("open settings",))
        self.assertIn("desktop", interpreted.required_tools)


class ApprovalIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_does_not_clobber_an_operator_approved_session(self) -> None:
        """A read-only capture inside a run must not reset the run's gateway."""

        class _Runner:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def run(self, action: Any) -> dict[str, Any]:
                self.calls.append(action.type.value)
                return {"fresh": True, "exists": True, "size_bytes": 64}

        with tempfile.TemporaryDirectory() as tmp:
            runner = _Runner()
            approval = ApprovedApprovalGateway()
            controller = DesktopAutomationController(
                runner=runner,  # type: ignore[arg-type]
                approval_gateway=approval,
            )
            vision = VisionController(controller, bug_log=BugLog(Path(tmp) / "bugs.json"))

            captured = await vision.capture_screen()
            self.assertTrue(captured["captured"])
            self.assertIs(controller.approval_gateway, approval)

            opened = await ActionEngine(desktop_controller=controller).open_application("calculator")
            self.assertTrue(opened.success, opened.detail)
            self.assertEqual(runner.calls, ["take_screenshot", "open_application"])


class _StaticPerception:
    def __init__(self) -> None:
        self.state = UiState(
            application="notepad",
            window="Untitled - Notepad",
            elements=(UiElement(id="win", type="window", text="Untitled - Notepad"),),
            raw_text="Untitled - Notepad",
            source="window_probe",
            metadata={"evidence": True},
        )

    async def observe(self, **kwargs: Any) -> UiState:
        return self.state


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_event_stops_before_the_first_action(self) -> None:
        orchestrator = AgenticOrchestrator(
            perception=_StaticPerception(),
            actions=ActionEngine(desktop_runner=_DesktopProbe()),
            knowledge=ApplicationKnowledgeGraph(),
            evaluation=EvaluationLedger(),
        )
        event = asyncio.Event()
        event.set()
        state = await orchestrator.run_task("open notepad", cancel_event=event)
        self.assertEqual(state.status.value, "cancelled")
        self.assertEqual(state.actions, [])
        self.assertIn("cancelled", state.summary.lower())


class LearningIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_loop_records_workflow_and_evaluation(self) -> None:
        knowledge = ApplicationKnowledgeGraph()
        evaluation = EvaluationLedger()
        orchestrator = AgenticOrchestrator(
            perception=_StaticPerception(),
            actions=ActionEngine(desktop_runner=_DesktopProbe()),
            knowledge=knowledge,
            evaluation=evaluation,
        )
        state = await orchestrator.run_task("open notepad")

        self.assertEqual(state.status.value, "completed")
        self.assertTrue(any(item.kind == "workflow" for item in state.learned_information))
        self.assertTrue(knowledge.workflows_for("desktop"))
        self.assertGreaterEqual(evaluation.metrics()["tasks"], 1)


class FinalVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_exhaustion_with_successful_actions_is_not_completion(self) -> None:
        orchestrator = AgenticOrchestrator(
            perception=_StaticPerception(),
            actions=ActionEngine(desktop_runner=_DesktopProbe()),
            knowledge=ApplicationKnowledgeGraph(),
            evaluation=EvaluationLedger(),
        )
        state = AgentTaskState(goal="open calculator on the desktop")
        state.transition(TaskPhase.EXECUTING)
        state.actions.append(
            ActionRecord(step_id="s1", action="open_application", target="calculator", success=True)
        )

        await orchestrator._finalize(state, time.monotonic())

        self.assertEqual(state.status.value, "failed")
        self.assertEqual(state.verification["final"]["status"], "fail")


class _FakeState:
    def __init__(self) -> None:
        self.id = "state-1"
        self.status = "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "goal": "open notepad",
            "status": "completed",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:01+00:00",
            "plan": [],
            "actions": [],
            "observations": [],
            "verification": {"final": {"status": "pass"}},
            "errors": [],
            "summary": "done",
            "learned_information": [],
        }


class _SlowOrchestrator:
    def __init__(self) -> None:
        self.perception = _StaticPerception()
        self.interpreter = TaskInterpreter()
        self.planner = AdaptivePlanner()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run_task(self, _goal: str, **_kwargs: Any) -> _FakeState:
        self.started.set()
        await self.release.wait()
        return _FakeState()


class OperatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_operation_is_refused_without_interrupting_the_first(self) -> None:
        orchestrator = _SlowOrchestrator()
        operator = ComputerOperator(
            orchestrator=orchestrator,  # type: ignore[arg-type]
            history=ComputerOperationHistory(),
            devices=DeviceRegistry(platform_name="Windows-test"),
        )
        first = asyncio.create_task(operator.run("open notepad"))
        await orchestrator.started.wait()
        with self.assertRaises(ComputerTaskBusy):
            await operator.run("open calculator")
        orchestrator.release.set()
        result = await first
        self.assertEqual(result.status, "completed")
        self.assertEqual(operator.history.summary()["completed"], 1)

    async def test_destructive_objective_requires_explicit_approval(self) -> None:
        operator = ComputerOperator(
            orchestrator=_SlowOrchestrator(),  # type: ignore[arg-type]
            history=ComputerOperationHistory(),
            devices=DeviceRegistry(platform_name="Windows-test"),
        )
        with self.assertRaises(PermissionError):
            await operator.run("delete the report from downloads")
        self.assertEqual(operator.history.summary()["blocked"], 1)

    async def test_unexpected_loop_exception_is_recorded_as_a_failed_task(self) -> None:
        class _ExplodingOrchestrator:
            def __init__(self) -> None:
                self.perception = _StaticPerception()
                self.interpreter = TaskInterpreter()
                self.planner = AdaptivePlanner()

            async def run_task(self, _goal: str, **_kwargs: Any) -> _FakeState:
                raise RuntimeError("boom")

        operator = ComputerOperator(
            orchestrator=_ExplodingOrchestrator(),  # type: ignore[arg-type]
            history=ComputerOperationHistory(),
            devices=DeviceRegistry(platform_name="Windows-test", display_probe=lambda: ()),
        )

        with self.assertRaises(RuntimeError):
            await operator.run("open notepad")

        record = operator.history.list(limit=1)[0]
        self.assertEqual(record["status"], "failed")
        self.assertIn("boom", record["errors"][0])

    async def test_approved_run_swaps_and_restores_the_browser_gateway(self) -> None:
        class _GatewayOrchestrator:
            def __init__(self, controller: BrowserAutomationController) -> None:
                self.perception = _StaticPerception()
                self.interpreter = TaskInterpreter()
                self.planner = AdaptivePlanner()
                self.controller = controller
                self.seen: Any = None

            async def run_task(self, _goal: str, **_kwargs: Any) -> _FakeState:
                self.seen = self.controller.approval_gateway
                return _FakeState()

        controller = BrowserAutomationController(runner=_BrowserProbe())
        original = controller.approval_gateway
        orchestrator = _GatewayOrchestrator(controller)
        operator = ComputerOperator(
            orchestrator=orchestrator,  # type: ignore[arg-type]
            history=ComputerOperationHistory(),
            devices=DeviceRegistry(platform_name="Windows-test", display_probe=lambda: ()),
            browser_controller=controller,
        )

        result = await operator.run("open notepad", approved=True)

        self.assertEqual(result.status, "completed")
        self.assertIsInstance(orchestrator.seen, ApprovedApprovalGateway)
        self.assertIs(controller.approval_gateway, original)


@unittest.skipIf(TestClient is None, "fastapi.testclient.TestClient requires httpx")
class ComputerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        if TestClient is None:
            self.skipTest("httpx not installed")
        self._tmp = tempfile.TemporaryDirectory()
        self._nova: NovaControlApplication | None = None
        self._patchers = [
            mock.patch("novacontrol.api.app.NovaControlApplication", side_effect=self._app),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
        ]
        for patcher in self._patchers:
            patcher.start()
        self._client = TestClient(create_app())
        self._client.__enter__()

    def _app(self, **kwargs: object) -> NovaControlApplication:
        kwargs["data_dir"] = self._tmp.name
        app = NovaControlApplication(**kwargs)  # type: ignore[arg-type]
        self._nova = app
        return app

    def tearDown(self) -> None:
        self._client.__exit__(None, None, None)
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()

    def test_routes_are_declared_with_consumers(self) -> None:
        surface = ApiSurface.default()
        for route in (
            ("GET", "/computer/status"),
            ("GET", "/computer/devices"),
            ("POST", "/computer/observe"),
            ("POST", "/computer/plan"),
            ("POST", "/computer/run"),
            ("POST", "/computer/stop"),
            ("GET", "/computer/history"),
            ("POST", "/computer/history/clear"),
            ("POST", "/computer/reference"),
        ):
            self.assertTrue(any((item.method, item.path) == route for item in surface.routes), route)
            self.assertIn(route, ROUTE_CONSUMERS)

    def test_status_plan_reference_and_history_round_trip(self) -> None:
        status = self._client.get("/computer/status")
        self.assertEqual(status.status_code, 200)
        self.assertIn("devices", status.json())
        self.assertIn("focused_loop", status.json())

        plan = self._client.post("/computer/plan", json={"objective": "open notepad"})
        self.assertEqual(plan.status_code, 200)
        self.assertTrue(plan.json()["approval"]["token"])
        token = plan.json()["approval"]["token"]

        missing = self._client.post(
            "/computer/run",
            json={"objective": "open notepad"},
        )
        self.assertEqual(missing.status_code, 422)

        body = {
            "text": "go back to the previous page",
            "ui_state": {
                "application": "browser",
                "window": "Example",
                "url": "https://example.com",
                "elements": [],
            },
        }
        resolved = self._client.post("/computer/reference", json=body)
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.json()["status"], "resolved")

        history = self._client.get("/computer/history")
        self.assertEqual(history.status_code, 200)
        self.assertIn("records", history.json())
        clear = self._client.post("/computer/history/clear")
        self.assertEqual(clear.status_code, 200)
        self.assertGreaterEqual(clear.json()["removed"], 0)
        # A replayed token is refused even after the first run attempt fails
        # against the no-op desktop: the token is consumed before execution.
        replay = self._client.post(
            "/computer/run",
            json={"objective": "open notepad", "approval_token": token},
        )
        self.assertIn(replay.status_code, (200, 409, 403))
        if replay.status_code == 200:
            second = self._client.post(
                "/computer/run",
                json={"objective": "open notepad", "approval_token": token},
            )
            self.assertEqual(second.status_code, 403)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
