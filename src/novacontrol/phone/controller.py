"""Approval-gated phone control controller."""

from __future__ import annotations

from collections.abc import Mapping
import shutil
import subprocess
from typing import Any, Protocol, runtime_checkable

from novacontrol.core.security import (
    ApprovalGateway,
    ApprovalRequest,
    DenyByDefaultApprovalGateway,
    PermissionScope,
    RiskLevel,
)
from novacontrol.phone.models import (
    PhoneAction,
    PhoneActionResult,
    PhoneActionStatus,
    PhoneActionType,
    PhoneBridgeState,
    PhoneBridgeStatus,
    PhoneDevice,
    PhoneWorkflow,
)


@runtime_checkable
class PhoneCommandRunner(Protocol):
    def status(self) -> PhoneBridgeStatus:
        """Return bridge and device status."""

    async def run(self, action: PhoneAction) -> Mapping[str, Any]:
        """Run an approved phone action."""


class NoopPhoneRunner:
    """Safe phone runner used when no bridge is configured."""

    def status(self) -> PhoneBridgeStatus:
        return PhoneBridgeStatus(
            available=False,
            state=PhoneBridgeState.NOT_CONFIGURED,
            adapter="noop",
            next_steps=_phone_next_steps("Install Android platform tools or a companion phone app."),
        )

    async def run(self, action: PhoneAction) -> Mapping[str, Any]:
        raise RuntimeError("No phone bridge is configured.")


class AdbPhoneRunner:
    """Runs approved Android phone actions through Android Debug Bridge."""

    def __init__(self, *, adb_path: str | None = None, timeout_seconds: float = 10) -> None:
        self.adb_path = adb_path or shutil.which("adb")
        self.timeout_seconds = timeout_seconds

    def status(self) -> PhoneBridgeStatus:
        if not self.adb_path:
            return PhoneBridgeStatus(
                available=False,
                state=PhoneBridgeState.NOT_CONFIGURED,
                adapter="adb",
                next_steps=_phone_next_steps("Install Android platform tools and make adb available on PATH."),
            )
        devices = self._devices()
        state = PhoneBridgeState.DEVICE_CONNECTED if devices else PhoneBridgeState.NO_DEVICE
        return PhoneBridgeStatus(
            available=bool(devices),
            state=state,
            adapter="adb",
            devices=devices,
            next_steps=() if devices else _phone_next_steps("Enable USB debugging and authorize this computer."),
        )

    async def run(self, action: PhoneAction) -> Mapping[str, Any]:
        status = self.status()
        if not status.available:
            raise RuntimeError("No authorized ADB device is connected.")
        if action.type is PhoneActionType.OPEN_APPLICATION:
            package = action.parameters.get("package") or action.target
            return self._run_adb(
                (
                    "shell",
                    "monkey",
                    "-p",
                    str(package),
                    "-c",
                    "android.intent.category.LAUNCHER",
                    "1",
                )
            )
        raise ValueError(f"Unsupported phone action type: {action.type}")

    def _devices(self) -> tuple[PhoneDevice, ...]:
        result = self._run_adb(("devices", "-l"))
        devices: list[PhoneDevice] = []
        for line in str(result["stdout"]).splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            model = next((part.split(":", 1)[1] for part in parts if part.startswith("model:")), None)
            if parts[1] == "device":
                devices.append(PhoneDevice(id=parts[0], state=parts[1], model=model))
        return tuple(devices)

    def _run_adb(self, args: tuple[str, ...]) -> Mapping[str, Any]:
        if not self.adb_path:
            raise RuntimeError("adb is not installed or not available on PATH.")
        completed = subprocess.run(
            (self.adb_path, *args),
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        return {
            "adapter": "adb",
            "command": "adb " + " ".join(args),
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }


class PhoneControlController:
    """Plans and executes approved phone workflows."""

    def __init__(
        self,
        *,
        approval_gateway: ApprovalGateway | None = None,
        runner: PhoneCommandRunner | None = None,
    ) -> None:
        self.approval_gateway = approval_gateway or DenyByDefaultApprovalGateway()
        self.runner = runner or AdbPhoneRunner()

    def status(self) -> PhoneBridgeStatus:
        return self.runner.status()

    def plan_open_application(self, application: str) -> PhoneWorkflow:
        label, package = _phone_package(application)
        return PhoneWorkflow(
            name=f"Open {label} on phone",
            actions=(
                PhoneAction(
                    type=PhoneActionType.OPEN_APPLICATION,
                    target=package,
                    description=f"Open {label} on the paired phone",
                    parameters={"label": label, "package": package},
                ),
            ),
        )

    async def execute_workflow(self, workflow: PhoneWorkflow) -> tuple[PhoneActionResult, ...]:
        results = []
        for action in workflow.actions:
            result = await self.execute_action(action)
            results.append(result)
            if result.status is not PhoneActionStatus.COMPLETED:
                break
        return tuple(results)

    async def execute_action(self, action: PhoneAction) -> PhoneActionResult:
        approval = await self.approval_gateway.request_approval(
            ApprovalRequest(
                action=action.description,
                reason="Phone control requires explicit user approval.",
                permissions=(PermissionScope.PHONE_CONTROL,),
                risk=RiskLevel.CRITICAL,
                metadata=action.to_dict(),
            )
        )
        if not approval.approved:
            return PhoneActionResult(
                action_id=action.id,
                status=PhoneActionStatus.DENIED,
                output={},
                error=approval.reason or "Phone action was not approved.",
                approval_id=approval.request_id,
            )
        try:
            output = await self.runner.run(action)
            exit_code = output.get("exit_code")
            if isinstance(exit_code, int) and exit_code != 0:
                return PhoneActionResult(
                    action_id=action.id,
                    status=PhoneActionStatus.FAILED,
                    output=dict(output),
                    error=str(output.get("stderr") or "ADB command failed."),
                    approval_id=approval.request_id,
                )
            return PhoneActionResult(
                action_id=action.id,
                status=PhoneActionStatus.COMPLETED,
                output=dict(output),
                approval_id=approval.request_id,
            )
        except Exception as exc:
            return PhoneActionResult(
                action_id=action.id,
                status=PhoneActionStatus.FAILED,
                output={},
                error=f"{type(exc).__name__}: {exc}",
                approval_id=approval.request_id,
            )


def _phone_package(application: str) -> tuple[str, str]:
    value = " ".join(application.strip().split()) or "requested app"
    lower = value.lower()
    aliases = {
        "whatsapp": ("WhatsApp", "com.whatsapp"),
        "chrome": ("Chrome", "com.android.chrome"),
        "youtube": ("YouTube", "com.google.android.youtube"),
        "gmail": ("Gmail", "com.google.android.gm"),
        "settings": ("Settings", "com.android.settings"),
    }
    for name, mapped in aliases.items():
        if name in lower:
            return mapped
    if "." in value and " " not in value:
        return value, value
    return value, value


def _phone_next_steps(reason: str) -> tuple[str, ...]:
    return (
        reason,
        "Pair the phone explicitly before executing actions.",
        "Keep approval required for app launch, calls, messages, files, settings, and payments.",
    )
