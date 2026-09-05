from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.core.events import Event
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.self_improvement import SelfImprovementEngine
from novacontrol.settings import ApprovalMode


class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_reports_real_adapter_runners(self) -> None:
        app = NovaControlApplication()

        status = app.status()
        await app.stop()

        self.assertEqual(status["desktop_runner"], "LocalDesktopRunner")
        self.assertEqual(status["browser_runner"], "PlaywrightBrowserRunner")
        self.assertIsInstance(status["browser_adapter_available"], bool)
        self.assertTrue(status["self_improvement_available"])

    async def test_explore_publishes_on_the_app_wide_event_bus(self) -> None:
        """Research progress must reach the single activity channel's bus.

        The web UI consumes /events/stream, which relays nova.event_bus — so
        ExploreService has to be wired onto that same bus (a private bus would
        silently drop every research step from the live UI).
        """
        with TemporaryDirectory() as temp_dir:
            app = NovaControlApplication(data_dir=temp_dir)
            await app.start()
            try:
                self.assertIs(app.explore._event_bus, app.event_bus)
            finally:
                await app.stop()

    async def test_executed_command_publishes_progress_on_the_app_bus(self) -> None:
        """Executing an approved command announces each action on the bus."""
        with TemporaryDirectory() as temp_dir:
            app = NovaControlApplication(data_dir=temp_dir)
            app.desktop.runner = NoopDesktopRunner()  # never touch the real desktop
            seen: list[Event] = []

            async def capture(event: Event) -> None:
                seen.append(event)

            await app.event_bus.subscribe("command.progress", capture)
            await app.start()
            try:
                plan = app.plan_desktop_command("open notepad")
                result = await app.execute_desktop_command(
                    "open notepad", approval_token=plan["approval"]["token"]
                )
            finally:
                await app.stop()

        self.assertEqual(result["status"], "executed")
        self.assertTrue(seen, "command.progress events must be published during execution")
        self.assertTrue(
            seen[-1].payload["detail"].startswith("Running action 1/1"),
            f"unexpected progress line: {seen[-1].payload['detail']}",
        )

    async def test_application_routes_self_improvement_requests(self) -> None:
        app = NovaControlApplication()
        await app.start()
        try:
            response = await app.handle_request("make it intelligent and code itself")
        finally:
            await app.stop()

        self.assertEqual(response.route, "self_improvement")
        self.assertEqual(response.intent, "self_improvement")
        self.assertIn("actions", response.payload)

    async def test_application_builds_friendly_improvement_workflow(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = NovaControlApplication()
            app.self_improvement = SelfImprovementEngine(Path(temp_dir))

            workflow = app.improvement_workflow("become the best AI agent available")
            preview = app.preview_improvement_workflow("become the best AI agent available")
            approved = await app.approve_improvement_workflow(
                "become the best AI agent available",
                preview_id=preview["preview"]["id"],
            )
            await app.stop()

        self.assertEqual(workflow["status"], "waiting_for_approval")
        self.assertFalse(workflow["approval"]["approved"])
        self.assertGreaterEqual(len(workflow["completed"]), 3)
        self.assertIn("current", workflow)
        self.assertIn("remaining", workflow)
        self.assertEqual(preview["status"], "temporary_preview_ready")
        self.assertEqual(approved["status"], "approved_and_applied")
        self.assertTrue(approved["approval"]["approved"])
        self.assertTrue(approved["apply_results"])

    async def test_application_persists_settings(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = NovaControlApplication(data_dir=Path(temp_dir))
            app.settings.update(approval_mode=ApprovalMode.DENY, include_videos_in_explore=False)
            await app.stop()

            restored = NovaControlApplication(data_dir=Path(temp_dir))
            try:
                self.assertEqual(restored.settings.settings.approval_mode, ApprovalMode.DENY)
                self.assertFalse(restored.settings.settings.include_videos_in_explore)
            finally:
                await restored.stop()

    async def test_application_learning_cycle_records_memory(self) -> None:
        app = NovaControlApplication()

        result = await app.learning_cycle("improve coding intelligence", feedback="prefer tests first")
        memory = await app.memory.recall("long_term", result["memory"]["key"])
        await app.stop()

        self.assertEqual(result["mode"], "local_feedback_learning")
        self.assertIsNotNone(memory)
        self.assertIn("plan", result)

    async def test_application_autonomous_learning_loop_is_bounded(self) -> None:
        app = NovaControlApplication()

        result = await app.autonomous_learning_loop("improve coding intelligence", iterations=10)
        await app.stop()

        self.assertEqual(result["mode"], "autonomous_local_learning")
        self.assertFalse(result["weight_training"])
        self.assertEqual(len(result["iterations"]), 5)

    async def test_application_routes_planning_requests(self) -> None:
        app = NovaControlApplication()
        await app.start()
        try:
            response = await app.handle_request("build a simple plan")
        finally:
            await app.stop()

        self.assertEqual(response.route, "planning")
        self.assertEqual(response.intent, "plan")
        self.assertIn("plan", response.payload)

    async def test_application_routes_chat_requests(self) -> None:
        app = NovaControlApplication()
        await app.start()
        try:
            response = await app.handle_request("hi")
        finally:
            await app.stop()

        self.assertEqual(response.route, "chat")
        self.assertEqual(response.intent, "chat")
        self.assertIn("message", response.payload)
        self.assertFalse(response.payload["model_configured"])

    async def test_application_routes_desktop_requests_to_approval_preview(self) -> None:
        app = NovaControlApplication()
        await app.start()
        try:
            response = await app.handle_request("open notepad")
        finally:
            await app.stop()

        self.assertEqual(response.route, "desktop_automation")
        self.assertEqual(response.intent, "desktop_automation")
        self.assertEqual(response.payload["target"], "notepad")
        self.assertFalse(response.payload["approval"]["approved"])

    async def test_application_routes_phone_requests_to_bridge_preview(self) -> None:
        app = NovaControlApplication()
        await app.start()
        try:
            response = await app.handle_request("open whatsapp on my phone")
        finally:
            await app.stop()

        self.assertEqual(response.route, "phone_control")
        self.assertEqual(response.intent, "phone_control")
        self.assertEqual(response.payload["target"], "whatsapp")
        self.assertIn("bridge", response.payload)
        self.assertFalse(response.payload["approval"]["approved"])

    async def test_application_unified_command_planner_routes_phone_and_desktop(self) -> None:
        app = NovaControlApplication()

        phone = app.plan_command("open whatsapp on my phone")
        desktop = app.plan_command("open calculator")
        await app.stop()

        self.assertEqual(phone["route"], "phone_control")
        self.assertEqual(desktop["route"], "desktop_automation")

    async def _desktop_app(self) -> NovaControlApplication:
        """App with a no-op runner so approvals execute without touching the real desktop."""
        app = NovaControlApplication()
        app.desktop.runner = NoopDesktopRunner()
        self.addAsyncCleanup(app.stop)
        return app

    async def test_desktop_plan_requires_token_to_execute(self) -> None:
        app = await self._desktop_app()
        plan = app.plan_desktop_command("open calculator")

        self.assertEqual(plan["route"], "desktop_automation")
        self.assertFalse(plan["approval"]["approved"])
        self.assertTrue(plan["approval"]["token"])

        # Without a token, execute refuses (HTTP 403 at the API layer) and never runs anything;
        # the preview path lives in plan_desktop_command.
        with self.assertRaises(ValueError):
            await app.execute_desktop_command("open calculator")
        preview = app.plan_desktop_command("open calculator")
        self.assertEqual(preview["status"], "waiting_for_approval")
        self.assertFalse(preview["approval"]["approved"])
        self.assertNotIn("execution_results", preview)

    async def test_desktop_token_executes_once_and_cannot_be_reused(self) -> None:
        app = await self._desktop_app()
        token = app.plan_desktop_command("open calculator")["approval"]["token"]

        executed = await app.execute_desktop_command("open calculator", approval_token=token)
        self.assertEqual(executed["status"], "executed")
        self.assertTrue(executed["approval"]["approved"])
        self.assertGreaterEqual(len(executed["execution_results"]), 1)

        # Single-use: replaying the same token must be rejected.
        with self.assertRaises(ValueError):
            await app.execute_desktop_command("open calculator", approval_token=token)

    async def test_desktop_execute_rejects_invalid_tokens(self) -> None:
        app = await self._desktop_app()
        token = app.plan_desktop_command("open calculator")["approval"]["token"]

        with self.subTest(kind="unknown"):
            with self.assertRaises(ValueError):
                await app.execute_desktop_command("open calculator", approval_token="forged-token")

        with self.subTest(kind="wrong-command"):
            with self.assertRaises(ValueError):
                await app.execute_desktop_command("open notepad", approval_token=token)

        # A rejected attempt does not consume the token: it still works for its own command.
        executed = await app.execute_desktop_command("open calculator", approval_token=token)
        self.assertEqual(executed["status"], "executed")

    async def test_desktop_execute_rejects_expired_token(self) -> None:
        app = await self._desktop_app()
        app.approval_ttl_seconds = -1  # token is born already expired
        token = app.plan_desktop_command("open calculator")["approval"]["token"]

        with self.assertRaises(ValueError):
            await app.execute_desktop_command("open calculator", approval_token=token)

    async def test_execute_command_routes_desktop_with_token(self) -> None:
        app = await self._desktop_app()
        token = app.plan_command("open calculator")["approval"]["token"]

        executed = await app.execute_command("open calculator", approval_token=token)

        self.assertEqual(executed["route"], "desktop_automation")
        self.assertEqual(executed["status"], "executed")
        self.assertTrue(executed["approval"]["approved"])

    async def test_application_routes_browser_requests_to_approval_preview(self) -> None:
        app = NovaControlApplication()
        await app.start()
        try:
            response = await app.handle_request("navigate to example.com")
        finally:
            await app.stop()

        self.assertEqual(response.route, "browser_automation")
        self.assertEqual(response.intent, "browser_automation")
        self.assertEqual(response.payload["target"], "https://example.com")
        self.assertEqual(response.payload["status"], "waiting_for_approval")
        self.assertFalse(response.payload["approval"]["approved"])
        self.assertNotIn("execution_results", response.payload)

    async def _browser_app(self) -> NovaControlApplication:
        """App with a no-op browser runner so approved workflows run without launching a browser."""
        app = NovaControlApplication()
        app.browser.runner = NoopBrowserRunner()
        self.addAsyncCleanup(app.stop)
        return app

    async def test_browser_plan_requires_token_to_execute(self) -> None:
        app = await self._browser_app()
        plan = app.plan_browser_command("navigate to example.com")

        self.assertEqual(plan["route"], "browser_automation")
        self.assertEqual(plan["status"], "waiting_for_approval")
        self.assertFalse(plan["approval"]["approved"])
        self.assertTrue(plan["approval"]["token"])
        self.assertEqual(plan["workflow"]["actions"][0]["type"], "navigate")
        self.assertEqual(plan["workflow"]["actions"][0]["target"], "https://example.com")

        # Without a token, execute refuses (HTTP 403 at the API layer) and never runs anything.
        with self.assertRaises(ValueError):
            await app.execute_browser_command("navigate to example.com")

    async def test_browser_fill_form_plans_navigate_and_fill(self) -> None:
        app = await self._browser_app()
        plan = app.plan_browser_command(
            "fill the form at https://example.com/login with username=admin, password=secret"
        )

        actions = plan["workflow"]["actions"]
        self.assertEqual([action["type"] for action in actions], ["navigate", "fill_form"])
        self.assertEqual(actions[1]["parameters"]["fields"], {"username": "admin", "password": "secret"})

        executed = await app.execute_browser_command(
            "fill the form at https://example.com/login with username=admin, password=secret",
            approval_token=plan["approval"]["token"],
        )
        self.assertEqual(executed["status"], "executed")
        self.assertEqual(len(executed["execution_results"]), 2)

    async def test_browser_token_executes_once_and_cannot_be_reused(self) -> None:
        app = await self._browser_app()
        token = app.plan_browser_command("navigate to example.com")["approval"]["token"]

        executed = await app.execute_browser_command("navigate to example.com", approval_token=token)
        self.assertEqual(executed["status"], "executed")
        self.assertTrue(executed["approval"]["approved"])
        self.assertGreaterEqual(len(executed["execution_results"]), 1)
        self.assertEqual(executed["execution_results"][0]["status"], "completed")

        # Single-use: replaying the same token must be rejected.
        with self.assertRaises(ValueError):
            await app.execute_browser_command("navigate to example.com", approval_token=token)

    async def test_browser_execute_restores_deny_by_default_gateway(self) -> None:
        app = await self._browser_app()
        token = app.plan_browser_command("navigate to example.com")["approval"]["token"]
        await app.execute_browser_command("navigate to example.com", approval_token=token)

        # The token window is over: a fresh workflow through the controller is denied again.
        results = await app.browser.execute_workflow(app.browser.plan_navigation("https://example.com"))
        self.assertEqual(results[0].status.value, "denied")

    async def test_browser_execute_rejects_invalid_tokens(self) -> None:
        app = await self._browser_app()
        token = app.plan_browser_command("navigate to example.com")["approval"]["token"]

        with self.subTest(kind="unknown"):
            with self.assertRaises(ValueError):
                await app.execute_browser_command("navigate to example.com", approval_token="forged-token")

        with self.subTest(kind="wrong-command"):
            with self.assertRaises(ValueError):
                await app.execute_browser_command("open example.com website", approval_token=token)

        # A rejected attempt does not consume the token: it still works for its own command.
        executed = await app.execute_browser_command("navigate to example.com", approval_token=token)
        self.assertEqual(executed["status"], "executed")

    async def test_browser_execute_rejects_expired_token(self) -> None:
        app = await self._browser_app()
        app.approval_ttl_seconds = -1  # token is born already expired
        token = app.plan_browser_command("navigate to example.com")["approval"]["token"]

        with self.assertRaises(ValueError):
            await app.execute_browser_command("navigate to example.com", approval_token=token)

    async def test_browser_unparseable_command_returns_non_action_plan(self) -> None:
        app = await self._browser_app()
        plan = app.plan_browser_command("download that file")

        self.assertEqual(plan["status"], "not_an_action")
        self.assertNotIn("token", plan["approval"])

    async def test_execute_command_routes_browser_with_token(self) -> None:
        app = await self._browser_app()
        token = app.plan_command("navigate to example.com")["approval"]["token"]

        executed = await app.execute_command("navigate to example.com", approval_token=token)

        self.assertEqual(executed["route"], "browser_automation")
        self.assertEqual(executed["status"], "executed")
        self.assertTrue(executed["approval"]["approved"])

    async def test_application_routes_agent_requests(self) -> None:
        app = NovaControlApplication()
        await app.start()
        try:
            response = await app.handle_request("create tests for this module")
        finally:
            await app.stop()

        self.assertEqual(response.route, "agent")
        self.assertEqual(response.intent, "agent")
        self.assertEqual(response.payload["role"], "testing")


if __name__ == "__main__":
    unittest.main()
