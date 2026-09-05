"""Tests for phone control: runner, controller, approval."""

from __future__ import annotations

from collections.abc import Mapping
import unittest
from typing import Any

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


if __name__ == "__main__":
    unittest.main()
