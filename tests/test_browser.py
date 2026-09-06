"""Tests for browser automation: controller, approval, audit, events."""

from __future__ import annotations

import unittest
from datetime import datetime

from conftest import AllowGateway
from novacontrol.browser import (
    BrowserActionStatus,
    BrowserAutomationController,
    BrowserAutomationModule,
    PlaywrightBrowserRunner,
)
from novacontrol.core.audit import InMemoryAutomationAuditLog
from novacontrol.core.events import Event, EventBus


class RecordingBrowserRunner:
    def __init__(self):
        self.actions = []

    async def run(self, action):
        self.actions.append(action)
        return {"ran": action.type.value, "target": action.target}


class ClosableBrowserRunner(RecordingBrowserRunner):
    def __init__(self):
        super().__init__()
        self.closed = False

    async def close(self):
        self.closed = True


class FailingBrowserRunner:
    async def run(self, action):
        raise RuntimeError(f"simulated failure for {action.target}")


class BrowserAutomationTests(unittest.IsolatedAsyncioTestCase):

    async def test_default_controller_denies_sensitive_navigation(self) -> None:
        controller = BrowserAutomationController()
        results = await controller.execute_workflow(controller.plan_navigation("https://example.com"))
        self.assertEqual(results[0].status, BrowserActionStatus.DENIED)

    async def test_extraction_runs_without_approval(self) -> None:
        runner = RecordingBrowserRunner()
        controller = BrowserAutomationController(runner=runner)
        results = await controller.execute_workflow(controller.plan_extraction("h1"))
        self.assertEqual(results[0].status, BrowserActionStatus.COMPLETED)
        self.assertEqual(runner.actions[0].parameters["selector"], "h1")

    async def test_approved_navigation_runs(self) -> None:
        runner = RecordingBrowserRunner()
        controller = BrowserAutomationController(approval_gateway=AllowGateway(), runner=runner)
        results = await controller.execute_workflow(controller.plan_navigation("https://example.com"))
        self.assertEqual(results[0].status, BrowserActionStatus.COMPLETED)
        self.assertEqual(runner.actions[0].target, "https://example.com")

    async def test_approved_navigation_leaves_a_timestamped_audit_trace(self) -> None:
        """Desktop-style audit: every approved run is appended with a timestamp."""
        audit = InMemoryAutomationAuditLog()
        runner = RecordingBrowserRunner()
        controller = BrowserAutomationController(
            approval_gateway=AllowGateway(), runner=runner, audit_log=audit,
        )
        results = await controller.execute_workflow(controller.plan_navigation("https://example.com"))
        entries = await audit.read()

        self.assertEqual(results[0].status, BrowserActionStatus.COMPLETED)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].action_id, results[0].action_id)
        self.assertEqual(entries[0].status, "completed")
        # The trace line is timestamped (raises when malformed) and carries output.
        datetime.fromisoformat(entries[0].recorded_at)
        self.assertEqual(entries[0].output["target"], "https://example.com")

    async def test_denied_navigation_is_audited(self) -> None:
        """Even a refused action leaves a trace, matching the desktop contract."""
        audit = InMemoryAutomationAuditLog()
        controller = BrowserAutomationController(runner=RecordingBrowserRunner(), audit_log=audit)
        results = await controller.execute_workflow(controller.plan_navigation("https://example.com"))
        entries = await audit.read()

        self.assertEqual(results[0].status, BrowserActionStatus.DENIED)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].status, "denied")
        datetime.fromisoformat(entries[0].recorded_at)
        self.assertIsNotNone(entries[0].approval_id, "denied trace carries the denial request id")

    async def test_failed_action_is_audited_with_error(self) -> None:
        audit = InMemoryAutomationAuditLog()
        controller = BrowserAutomationController(
            approval_gateway=AllowGateway(), runner=FailingBrowserRunner(), audit_log=audit,
        )
        results = await controller.execute_workflow(controller.plan_navigation("https://example.com"))
        entries = await audit.read()

        self.assertEqual(results[0].status, BrowserActionStatus.FAILED)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].status, "failed")
        self.assertIn("simulated failure", entries[0].error)
        datetime.fromisoformat(entries[0].recorded_at)

    async def test_controller_close_delegates_to_runner(self) -> None:
        runner = ClosableBrowserRunner()
        controller = BrowserAutomationController(runner=runner)
        await controller.close()
        self.assertTrue(runner.closed)

    async def test_playwright_runner_availability(self) -> None:
        self.assertIsInstance(PlaywrightBrowserRunner.is_available(), bool)

    async def test_module_emits_events(self) -> None:
        bus = EventBus()
        module = BrowserAutomationModule(BrowserAutomationController())
        planned, denied = [], []

        async def cap_p(e): planned.append(e)
        async def cap_d(e): denied.append(e)

        await bus.subscribe("browser.workflow_planned", cap_p)
        await bus.subscribe("browser.workflow_denied", cap_d)
        await module.start(bus)
        await bus.publish(Event(
            type="browser.workflow_requested",
            payload={"workflow_type": "navigate", "url": "https://example.com", "execute": True},
        ))
        self.assertEqual(planned[0].payload["name"], "Navigate to https://example.com")
        self.assertEqual(denied[0].payload["results"][0]["status"], "denied")


if __name__ == "__main__":
    unittest.main()
