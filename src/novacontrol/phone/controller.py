"""Approval-gated phone control controller."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from novacontrol.core.audit import AutomationAuditLog, InMemoryAutomationAuditLog
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

    def connect(self) -> PhoneBridgeStatus:
        """Report how to create the connection, step by step."""
        return PhoneBridgeStatus(
            available=False,
            state=PhoneBridgeState.NOT_CONFIGURED,
            adapter="noop",
            next_steps=_phone_next_steps(
                "No phone bridge is installed yet. To create the connection: "
                "(1) Download Android platform-tools from "
                "https://developer.android.com/tools/releases/platform-tools "
                "and unzip it (e.g. to C:\\platform-tools); "
                "(2) add that folder to your PATH, or tell NovaControl where adb.exe lives; "
                "(3) on your phone: Settings → About phone → tap 'Build number' 7× to unlock "
                "Developer options → Settings → Developer options → enable 'USB debugging'; "
                "(4) plug the phone into this PC over USB and accept the 'Allow USB debugging' "
                "prompt on the phone (tick 'Always allow' for this computer); "
                "(5) press Connect Phone again — Bridge Status should then report the device."
            ),
        )

    async def run(self, action: PhoneAction) -> Mapping[str, Any]:
        raise RuntimeError("No phone bridge is configured.")


class AdbPhoneRunner:
    """Runs approved Android phone actions through Android Debug Bridge."""

    def __init__(self, *, adb_path: str | None = None, timeout_seconds: float = 10) -> None:
        self.adb_path = adb_path or _discover_adb()
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

    def connect(self) -> PhoneBridgeStatus:
        """Actively reach for a device and return the resulting bridge status.

        This is the pairing flow behind the UI's Connect Phone button: with adb
        installed it starts the ADB server and probes for devices (the phone
        shows the RSA authorization prompt the user must accept); without adb
        it reports exactly what to install. Nothing here is destructive — the
        worst case is an ADB server start and a device probe.
        """
        if not self.adb_path:
            return PhoneBridgeStatus(
                available=False,
                state=PhoneBridgeState.NOT_CONFIGURED,
                adapter="adb",
                next_steps=_phone_next_steps(
                    "adb was not found. To create the connection: download platform-tools from "
                    "https://developer.android.com/tools/releases/platform-tools, unzip it, "
                    "add the folder to PATH, enable USB debugging on the phone, plug it in over "
                    "USB, and press Connect Phone again."
                ),
            )
        try:
            # Starting the ADB server makes the connected phone show the RSA
            # authorization prompt; the device only appears once the user
            # accepts it ON THE PHONE. `adb devices` starts the server
            # implicitly, so the explicit `start-server` is best-effort only.
            self._run_adb(("start-server",))
            self._run_adb(("devices", "-l"))
        except Exception:
            pass  # status() below reports the true state either way
        return self.status()

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
        if action.type is PhoneActionType.SEND_TEXT:
            # ACTION_SENDTO with the sms: URI opens the messaging app pre-filled;
            # nothing transmits until the user presses send ON THE PHONE, so the
            # device owner keeps the final say over every outgoing message.
            recipient = str(action.parameters.get("recipient") or "").replace(" ", "")
            body = str(action.parameters.get("message") or "")
            return self._run_adb(
                (
                    "shell", "am", "start", "-a", "android.intent.action.SENDTO",
                    "-d", f"smsto:{recipient}", "--es", "sms_body", body,
                )
            )
        if action.type is PhoneActionType.CALL:
            # ACTION_DIAL only opens the dialer pre-filled — it never places the
            # call without the user pressing call ON THE PHONE.
            contact = str(action.parameters.get("contact") or action.target).replace(" ", "")
            return self._run_adb(
                (
                    "shell", "am", "start", "-a", "android.intent.action.DIAL",
                    "-d", f"tel:{contact}",
                )
            )
        if action.type is PhoneActionType.SCREENSHOT:
            return self._screenshot_to_file()
        raise ValueError(f"Unsupported phone action type: {action.type}")

    def _screenshot_to_file(self) -> Mapping[str, Any]:
        """Capture the device screen to a real PNG on this computer.

        screencap emits BINARY data, so the transfer must run in binary mode
        (a text-mode pipe corrupts every 0x0d/0x1a byte on Windows). The file
        is saved under data/screenshots and reported by absolute path so the
        user can actually open it.
        """
        if not self.adb_path:
            raise RuntimeError("adb is not installed or not available on PATH.")
        out_dir = Path("data") / "screenshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"phone-{stamp}.png"
        completed = subprocess.run(
            (self.adb_path, "exec-out", "screencap", "-p"),
            check=False,
            capture_output=True,
            timeout=self.timeout_seconds,
        )
        png = completed.stdout
        ok = completed.returncode == 0 and png[:8] == b"\x89PNG\r\n\x1a\n"
        if ok:
            path.write_bytes(png)
        return {
            "adapter": "adb",
            "command": "adb exec-out screencap -p",
            "exit_code": completed.returncode,
            "verified": ok,
            "file": str(path.resolve()) if ok else "",
            "bytes": len(png),
            "stdout": "" if ok else "screencap failed",
            "stderr": completed.stderr.decode("utf-8", errors="replace"),
        }

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
            encoding="utf-8",
            errors="replace",
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
        audit_log: AutomationAuditLog | None = None,
    ) -> None:
        self.approval_gateway = approval_gateway or DenyByDefaultApprovalGateway()
        self.runner = runner or AdbPhoneRunner()
        self.audit_log = audit_log or InMemoryAutomationAuditLog()

    def status(self) -> PhoneBridgeStatus:
        return self.runner.status()

    def connect(self) -> PhoneBridgeStatus:
        """Run the bridge's pairing flow and return the resulting status.

        Never executes a phone action and changes nothing on the device — it
        only reaches for the bridge (starting the ADB server and probing for
        devices) so the UI can guide the user through authorization.
        """
        connect = getattr(self.runner, "connect", None)
        if connect is None:
            return self.status()
        result: PhoneBridgeStatus = connect()
        return result

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

    def plan_send_text(self, message: str) -> PhoneWorkflow:
        """Plan a send-text action; target and recipient live in parameters.

        Deliberately approval-irreversible content: the full message text is in
        the plan so the user reviews exactly what would be sent.
        """
        recipient, body = _phone_message_parts(message)
        return PhoneWorkflow(
            name="Send text on phone",
            actions=(
                PhoneAction(
                    type=PhoneActionType.SEND_TEXT,
                    target=recipient or "messaging",  # the recipient, when known
                    description="Send a text message on the paired phone",
                    parameters={"recipient": recipient, "message": body},
                ),
            ),
        )

    def plan_call(self, contact: str) -> PhoneWorkflow:
        return PhoneWorkflow(
            name="Call on phone",
            actions=(
                PhoneAction(
                    type=PhoneActionType.CALL,
                    target=contact,
                    description=f"Call {contact} on the paired phone",
                    parameters={"contact": contact},
                ),
            ),
        )

    def plan_screenshot(self) -> PhoneWorkflow:
        return PhoneWorkflow(
            name="Screenshot on phone",
            actions=(
                PhoneAction(
                    type=PhoneActionType.SCREENSHOT,
                    target="screen",
                    description="Capture a screenshot on the paired phone",
                    parameters={},
                ),
            ),
        )

    async def execute_workflow(
        self,
        workflow: PhoneWorkflow,
        *,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[PhoneActionResult, ...]:
        results = []
        total = len(workflow.actions)
        for index, action in enumerate(workflow.actions, start=1):
            if progress is not None:
                await progress(f"Running phone action {index}/{total}: {action.description}")
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
            result = PhoneActionResult(
                action_id=action.id,
                status=PhoneActionStatus.DENIED,
                output={},
                error=approval.reason or "Phone action was not approved.",
                approval_id=approval.request_id,
            )
            await self.audit_log.append(result)
            return result
        try:
            output = await self.runner.run(action)
            exit_code = output.get("exit_code")
            if isinstance(exit_code, int) and exit_code != 0:
                result = PhoneActionResult(
                    action_id=action.id,
                    status=PhoneActionStatus.FAILED,
                    output=dict(output),
                    error=str(output.get("stderr") or "ADB command failed."),
                    approval_id=approval.request_id,
                )
            else:
                result = PhoneActionResult(
                    action_id=action.id,
                    status=PhoneActionStatus.COMPLETED,
                    output=dict(output),
                    approval_id=approval.request_id,
                )
        except Exception as exc:
            result = PhoneActionResult(
                action_id=action.id,
                status=PhoneActionStatus.FAILED,
                output={},
                error=f"{type(exc).__name__}: {exc}",
                approval_id=approval.request_id,
            )
        await self.audit_log.append(result)
        return result


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


def _phone_message_parts(message: str) -> tuple[str, str]:
    """Split a message spec into (recipient, body).

    Accepted shapes after the verb is stripped:
      "to mom say running late" / "mom saying running late" / "mom: running late"
      -> ("mom", "running late")
      "running late, leaving now" -> ("", whole text as body)
    A body without a say/saying/that/: separator stays whole, so free text is
    never silently split at the wrong word.
    """
    match = re.match(
        r"^\s*(?:(?:to\s+)?(?P<to>[^:]+?)\s*)?(?:says?\b|saying\b|that\b|:)\s*(?P<body>.+)$",
        message,
        re.IGNORECASE,
    )
    if match:
        return (match.group("to") or "").strip(), match.group("body").strip()
    return "", " ".join(message.strip().split())


def _discover_adb() -> str | None:
    """Find adb: PATH first, then the common Windows install locations."""
    found = shutil.which("adb")
    if found:
        return found
    home = Path.home()
    candidates = (
        home / "platform-tools" / "adb.exe",
        home / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe",
        home / "Downloads" / "platform-tools" / "adb.exe",
        home / "Desktop" / "platform-tools" / "adb.exe",
        Path("C:/platform-tools/adb.exe"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _phone_next_steps(reason: str) -> tuple[str, ...]:
    return (
        reason,
        "Pair the phone explicitly before executing actions.",
        "Keep approval required for app launch, calls, messages, files, settings, and payments.",
    )
