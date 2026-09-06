"""Tests for phone control: runner, controller, approval, audit."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import unittest
from typing import Any

from novacontrol.core.audit import InMemoryAutomationAuditLog
from novacontrol.phone import (
    NoopPhoneRunner,
    PhoneActionStatus,
    PhoneBridgeState,
    PhoneBridgeStatus,
    PhoneControlController,
    PhoneDevice,
)
from conftest import AllowGateway


class FakePhoneRunner:
    def status(self) -> PhoneBridgeStatus:
        return PhoneBridgeStatus(
            available=True,
            state=PhoneBridgeState.DEVICE_CONNECTED,
            adapter="fake",
            devices=(PhoneDevice("device-1", "device", "Pixel"),),
        )

    async def run(self, action: PhoneAction) -> Mapping[str, Any]:
        return {"adapter": "fake", "target": action.target, "exit_code": 0}


class PhoneControlTests(unittest.IsolatedAsyncioTestCase):

    async def test_noop_reports_pairing_steps(self) -> None:
        status = NoopPhoneRunner().status()
        self.assertFalse(status.available)
        self.assertEqual(status.state, PhoneBridgeState.NOT_CONFIGURED)
        self.assertTrue(status.next_steps)

    async def test_controller_plans_known_app_package(self) -> None:
        workflow = PhoneControlController(runner=FakePhoneRunner()).plan_open_application("whatsapp")
        self.assertEqual(workflow.actions[0].target, "com.whatsapp")

    async def test_controller_requires_approval_by_default(self) -> None:
        controller = PhoneControlController(runner=FakePhoneRunner())
        results = await controller.execute_workflow(controller.plan_open_application("whatsapp"))
        self.assertEqual(results[0].status, PhoneActionStatus.DENIED)

    async def test_controller_executes_after_approval(self) -> None:
        controller = PhoneControlController(runner=FakePhoneRunner(), approval_gateway=AllowGateway())
        results = await controller.execute_workflow(controller.plan_open_application("whatsapp"))
        self.assertEqual(results[0].status, PhoneActionStatus.COMPLETED)
        self.assertEqual(results[0].output["target"], "com.whatsapp")

    async def test_plan_send_text_splits_recipient_and_body(self) -> None:
        from novacontrol.phone.models import PhoneActionType

        controller = PhoneControlController(runner=FakePhoneRunner())
        workflow = controller.plan_send_text("to mom say running late")
        action = workflow.actions[0]
        self.assertEqual(action.type, PhoneActionType.SEND_TEXT)
        self.assertEqual(action.parameters["recipient"], "mom")
        self.assertEqual(action.parameters["message"], "running late")
        # No 'to X say' shape: whole text becomes the body, generic target.
        bare = controller.plan_send_text("running late, leaving now").actions[0]
        self.assertEqual(bare.parameters["recipient"], "")
        self.assertEqual(bare.parameters["message"], "running late, leaving now")

    async def test_plan_call_and_screenshot(self) -> None:
        from novacontrol.phone.models import PhoneActionType

        controller = PhoneControlController(runner=FakePhoneRunner())
        call = controller.plan_call("john").actions[0]
        self.assertEqual(call.type, PhoneActionType.CALL)
        self.assertEqual(call.target, "john")
        shot = controller.plan_screenshot().actions[0]
        self.assertEqual(shot.type, PhoneActionType.SCREENSHOT)

    async def test_runner_runs_each_new_action_type(self) -> None:
        """AdbPhoneRunner speaks adb for text/call/screenshot, not just open."""
        from novacontrol.phone.controller import AdbPhoneRunner
        from novacontrol.phone.models import PhoneAction, PhoneActionType

        runner = AdbPhoneRunner.__new__(AdbPhoneRunner)  # skip adb_path probing
        runner.adb_path = "adb"
        runner.timeout_seconds = 5
        runner.status = lambda: FakePhoneRunner().status()  # paired device
        calls: list[tuple] = []

        def _fake_adb(args: tuple) -> dict:
            calls.append(args)
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        runner._run_adb = _fake_adb

        # Screenshots bypass the text pipe (binary-safe file capture), so the
        # test stubs the capture itself and asserts the dispatch reached it.
        shot_calls: list[bool] = []

        def _fake_capture() -> dict:
            shot_calls.append(True)
            return {"adapter": "adb", "command": "adb exec-out screencap -p",
                    "exit_code": 0, "verified": True, "file": "x.png", "bytes": 4,
                    "stdout": "", "stderr": ""}

        runner._screenshot_to_file = _fake_capture  # type: ignore[method-assign]

        text_action = PhoneAction(type=PhoneActionType.SEND_TEXT, target="mom",
                                  description="d", parameters={"recipient": "mom", "message": "hi"})
        await runner.run(text_action)
        self.assertIn("smsto:mom", calls[-1])
        self.assertIn("hi", calls[-1])

        call_action = PhoneAction(type=PhoneActionType.CALL, target="john",
                                  description="d", parameters={"contact": "john"})
        await runner.run(call_action)
        self.assertIn("tel:john", calls[-1])
        self.assertIn("android.intent.action.DIAL", calls[-1])

        shot_action = PhoneAction(type=PhoneActionType.SCREENSHOT, target="screen", description="d")
        shot_result = await runner.run(shot_action)
        self.assertTrue(shot_calls, "screenshot must dispatch to the binary-safe capture")
        self.assertTrue(shot_result["verified"])

        with self.assertRaises(ValueError):
            await runner.run(PhoneAction(type="unknown_action", target="x", description="d"))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Audit (same shared log the desktop and browser controllers append to)
# ---------------------------------------------------------------------------

class PhoneAuditTests(unittest.IsolatedAsyncioTestCase):

    async def test_approved_action_leaves_a_timestamped_audit_trace(self) -> None:
        """Desktop/browser parity: every approved run is appended with a timestamp."""
        audit = InMemoryAutomationAuditLog()
        controller = PhoneControlController(
            approval_gateway=AllowGateway(), runner=FakePhoneRunner(), audit_log=audit,
        )
        results = await controller.execute_workflow(controller.plan_open_application("whatsapp"))
        entries = await audit.read()

        self.assertEqual(results[0].status, PhoneActionStatus.COMPLETED)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].action_id, results[0].action_id)
        self.assertEqual(entries[0].status, "completed")
        datetime.fromisoformat(entries[0].recorded_at)  # timestamped trace
        self.assertEqual(entries[0].approval_id, results[0].approval_id)

    async def test_denied_action_is_audited_with_timestamp(self) -> None:
        """Denials are traced too — the default gateway refuses before any run."""
        audit = InMemoryAutomationAuditLog()
        controller = PhoneControlController(runner=FakePhoneRunner(), audit_log=audit)
        results = await controller.execute_workflow(controller.plan_open_application("whatsapp"))
        entries = await audit.read()

        self.assertEqual(results[0].status, PhoneActionStatus.DENIED)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].status, "denied")
        datetime.fromisoformat(entries[0].recorded_at)

    async def test_failed_action_is_audited_with_error(self) -> None:
        """Runner failures append a failed trace carrying the error text."""

        class _FailingRunner:
            def status(self) -> PhoneBridgeStatus:
                return FakePhoneRunner().status()

            async def run(self, action: Any) -> Mapping[str, Any]:
                raise RuntimeError("No phone bridge is configured.")

        audit = InMemoryAutomationAuditLog()
        controller = PhoneControlController(
            approval_gateway=AllowGateway(), runner=_FailingRunner(), audit_log=audit,
        )
        results = await controller.execute_workflow(controller.plan_open_application("whatsapp"))
        entries = await audit.read()

        self.assertEqual(results[0].status, PhoneActionStatus.FAILED)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].status, "failed")
        self.assertIn("RuntimeError", entries[0].error or "")
        datetime.fromisoformat(entries[0].recorded_at)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Connect Phone: the pairing flow behind the UI button
# ---------------------------------------------------------------------------

class PhoneConnectTests(unittest.TestCase):
    """The Connect Phone option must run the bridge's pairing flow — starting
    ADB and probing for devices so the phone shows its authorization prompt —
    and surface the resulting bridge status."""

    def test_controller_connect_delegates_to_runner(self) -> None:
        from novacontrol.phone.controller import PhoneControlController

        class ConnectableRunner(FakePhoneRunner):
            connect_calls = 0

            def connect(self) -> PhoneBridgeStatus:
                type(self).connect_calls += 1
                return PhoneBridgeStatus(
                    available=True,
                    state=PhoneBridgeState.DEVICE_CONNECTED,
                    adapter="fake",
                )

        controller = PhoneControlController(runner=ConnectableRunner())
        status = controller.connect()
        self.assertEqual(ConnectableRunner.connect_calls, 1)
        self.assertTrue(status.available)

    def test_controller_connect_without_runner_support_falls_back_to_status(self) -> None:
        from novacontrol.phone.controller import PhoneControlController

        controller = PhoneControlController(runner=FakePhoneRunner())  # no connect()
        status = controller.connect()
        self.assertEqual(status, controller.status())

    def test_adb_connect_without_adb_reports_install_steps(self) -> None:
        from novacontrol.phone.controller import AdbPhoneRunner

        runner = AdbPhoneRunner.__new__(AdbPhoneRunner)
        runner.adb_path = None
        runner.timeout_seconds = 5
        status = runner.connect()
        self.assertFalse(status.available)
        self.assertEqual(status.state, PhoneBridgeState.NOT_CONFIGURED)
        joined = " ".join(status.next_steps)
        self.assertIn("adb", joined.lower())

    def test_adb_connect_starts_server_and_probes_devices(self) -> None:
        from novacontrol.phone.controller import AdbPhoneRunner

        runner = AdbPhoneRunner.__new__(AdbPhoneRunner)
        runner.adb_path = "adb"
        runner.timeout_seconds = 5
        calls: list[tuple] = []

        def _fake_adb(args: tuple) -> dict:
            calls.append(args)
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        runner._run_adb = _fake_adb
        runner._devices = lambda: ()  # no authorized device yet
        status = runner.connect()
        self.assertIn(("start-server",), calls)
        self.assertIn(("devices", "-l"), calls)
        self.assertFalse(status.available)
        self.assertEqual(status.state, PhoneBridgeState.NO_DEVICE)
