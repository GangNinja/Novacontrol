"""Tests for browser automation: controller, approval, events."""

from __future__ import annotations

import unittest

from novacontrol.browser import (
    BrowserActionStatus,
    BrowserAutomationController,
    BrowserAutomationModule,
    PlaywrightBrowserRunner,
)
from novacontrol.core.events import Event, EventBus
from conftest import AllowGateway


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
