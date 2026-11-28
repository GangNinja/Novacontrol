"""Tests for browser automation: controller, approval, audit, events."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from conftest import AllowGateway
from novacontrol.browser import (
    BrowserAction,
    BrowserActionStatus,
    BrowserActionType,
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

    async def test_download_dispatch_saves_a_real_file(self) -> None:
        """The declared DOWNLOAD action must actually reach the download path."""
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "downloads" / "phase29.txt"

            class _Download:
                suggested_filename = "phase29.txt"

                async def save_as(self, path: str) -> None:
                    Path(path).write_text("NovaControl download", encoding="utf-8")

            class _DownloadInfo:
                @property
                def value(self):
                    async def _value() -> _Download:
                        return _Download()

                    return _value()

                async def __aenter__(self) -> "_DownloadInfo":
                    return self

                async def __aexit__(self, *_args: object) -> None:
                    return None

            class _Page:
                url = "https://example.com/download"

                def __init__(self) -> None:
                    self.clicked: list[tuple[str, float | None]] = []

                def expect_download(self, *, timeout: float) -> _DownloadInfo:
                    del timeout
                    return _DownloadInfo()

                async def click(self, selector: str, *, timeout: float) -> None:
                    self.clicked.append((selector, timeout))

            page = _Page()
            runner = PlaywrightBrowserRunner()
            runner._page = page
            result = await runner.run(BrowserAction(
                type=BrowserActionType.DOWNLOAD,
                target="#download",
                description="Download the generated text file",
                parameters={"selector": "#download", "save_path": str(destination)},
            ))

            self.assertEqual(page.clicked, [("#download", runner.timeout_ms)])
            self.assertEqual(result["action"], "download")
            self.assertEqual(result["size_bytes"], len(b"NovaControl download"))
            self.assertEqual(destination.read_text(encoding="utf-8"), "NovaControl download")

    async def test_tab_actions_use_the_browser_context(self) -> None:
        """New/switch/list/close are real tab operations, not page reloads."""

        class _TabPage:
            def __init__(self, url: str, title: str) -> None:
                self.url = url
                self._title = title
                self.closed = False
                self.front = False
                self.goto_url = ""

            async def title(self) -> str:
                return self._title

            async def bring_to_front(self) -> None:
                self.front = True

            async def close(self) -> None:
                self.closed = True

            async def goto(self, url: str, **_kwargs: object) -> None:
                self.goto_url = url
                self.url = url

        class _TabContext:
            def __init__(self, first: _TabPage) -> None:
                self.pages: list[_TabPage] = [first]

            async def new_page(self) -> _TabPage:
                page = _TabPage("about:blank", "New Tab")
                self.pages.append(page)
                return page

        first = _TabPage("https://example.com/one", "One")
        context = _TabContext(first)
        runner = PlaywrightBrowserRunner()
        runner._context = context
        runner._page = first

        opened = await runner.run(BrowserAction(
            type=BrowserActionType.TAB_NEW,
            target="https://example.com/two",
            description="Open a second tab",
            parameters={"url": "https://example.com/two"},
        ))
        self.assertEqual(opened["action"], "tab_new")
        self.assertEqual(opened["tab_count"], 2)
        self.assertEqual(context.pages[1].goto_url, "https://example.com/two")

        listing = await runner.run(BrowserAction(
            type=BrowserActionType.TAB_LIST,
            target="tabs",
            description="List the tabs",
        ))
        self.assertEqual(listing["count"], 2)
        self.assertEqual([tab["url"] for tab in listing["tabs"]], ["https://example.com/one", "https://example.com/two"])
        self.assertFalse(listing["tabs"][0]["active"])
        self.assertTrue(listing["tabs"][1]["active"])

        switched = await runner.run(BrowserAction(
            type=BrowserActionType.TAB_SWITCH,
            target="0",
            description="Switch to the first tab",
            parameters={"index": 0},
        ))
        self.assertEqual(switched["tab_index"], 0)
        self.assertIs(runner._page, first)
        self.assertTrue(first.front)

        closed = await runner.run(BrowserAction(
            type=BrowserActionType.TAB_CLOSE,
            target="1",
            description="Close the second tab",
            parameters={"index": 1},
        ))
        self.assertEqual(closed["closed_index"], 1)
        self.assertEqual(closed["tab_count"], 1)
        self.assertTrue(context.pages[1].closed)
        self.assertIs(runner._page, first)

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
