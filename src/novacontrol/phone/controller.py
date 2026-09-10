"""Approval-gated phone control controller."""

from __future__ import annotations

import re
import shutil
import subprocess
import urllib.parse
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
            # A saved contact NAME resolves to its number first; raw numbers
            # (and unknown names, which the messaging app can still match
            # against the on-device contact book) pass through untouched.
            recipient = str(action.parameters.get("recipient") or "").replace(" ", "")
            if recipient and not recipient.lstrip("+").isdigit():
                resolved = self._resolve_contact(recipient)
                if resolved:
                    recipient = resolved.replace(" ", "")
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
            if contact and not contact.lstrip("+").isdigit():
                resolved = self._resolve_contact(contact)
                if resolved:
                    contact = resolved.replace(" ", "")
            return self._run_adb(
                (
                    "shell", "am", "start", "-a", "android.intent.action.DIAL",
                    "-d", f"tel:{contact}",
                )
            )
        if action.type is PhoneActionType.SEARCH:
            query = str(action.parameters.get("query") or "")
            provider = str(action.parameters.get("provider") or "google")
            return self._run_adb(
                (
                    "shell", "am", "start", "-a", "android.intent.action.VIEW",
                    "-d", _search_uri(query, provider),
                )
            )
        if action.type is PhoneActionType.OPEN_FILE:
            name = str(action.parameters.get("name") or action.target)
            # FIND_CONTENT resolves the file through the system chooser; the
            # device owner picks the app, so nothing opens without consent.
            return self._run_adb(
                (
                    "shell", "am", "start", "-a", "android.intent.action.VIEW",
                    "-d", f"content://media/external/file?q={urllib.parse.quote(name)}",
                )
            )
        if action.type is PhoneActionType.CONTACT_LOOKUP:
            name = str(action.parameters.get("name") or action.target)
            number = self._resolve_contact(name)
            return {
                "adapter": "adb",
                "command": "adb shell content query --uri content://com.android.contacts/data/phones",
                "exit_code": 0 if number else 1,
                "resolved_number": number or "",
                "stdout": number or "contact not found",
                "stderr": "",
            }
        if action.type is PhoneActionType.SCREENSHOT:
            return self._screenshot_to_file()
        raise ValueError(f"Unsupported phone action type: {action.type}")

    def _resolve_contact(self, name: str) -> str:
        """Resolve a saved contact name to its phone number via the contacts
        provider (read-only; needs no extra permission over an adb session)."""
        if not self.adb_path:
            raise RuntimeError("adb is not installed or not available on PATH.")
        wanted = re.sub(r"\s+", " ", name.strip().lower())
        result = self._run_adb(
            ("shell", "content", "query", "--uri", "content://com.android.contacts/data/phones")
        )
        if result.get("exit_code") != 0:
            return ""
        for line in str(result.get("stdout", "")).splitlines():
            if not line.strip().startswith("Row:"):
                continue
            # Row fields are key=value pairs in arbitrary order, e.g.
            # Row: 5 ... display_name=Mom, ... data1=+91 70131 73263 ...
            dm = re.search(r"\bdisplay_name=([^,]+)", line)
            num = re.search(r"\bdata1=([^,]+)", line)
            if not dm or not num:
                continue
            display = dm.group(1).strip()
            number = num.group(1).strip()
            if re.sub(r"\s+", " ", display.lower()) == wanted:
                return number
        return ""

    def resolve_contact_prefix(self, text: str) -> str:
        """Return the longest leading word-sequence of ``text`` that matches a
        saved contact name ("mom good night" -> "mom" when mom is saved)."""
        words = text.split()
        for size in range(min(len(words), 3), 0, -1):
            candidate = " ".join(words[:size])
            if self._resolve_contact(candidate):
                return candidate
        return ""

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

    def plan_search(self, query_spec: str) -> PhoneWorkflow:
        """Plan an in-app/provider search: "search cats on youtube".

        The provider comes after on/in when present, defaulting to google.
        The runner opens a VIEW intent on the provider's search URL, which
        Android routes into the installed app (or browser) — nothing is
        typed or submitted beyond the URL itself.
        """
        query, provider = _phone_search_parts(query_spec)
        return PhoneWorkflow(
            name=f"Search {query!r} in {provider} on phone",
            actions=(
                PhoneAction(
                    type=PhoneActionType.SEARCH,
                    target=provider,
                    description=f"Search for {query!r} in {provider} on the paired phone",
                    parameters={"query": query, "provider": provider},
                ),
            ),
        )

    def plan_open_file(self, spec: str) -> PhoneWorkflow:
        """Plan opening a named file or folder on the phone.

        Files open through the system chooser (the device owner picks the
        app); folders land in the Files app.
        """
        name, is_folder = _phone_file_parts(spec)
        if is_folder:
            action = PhoneAction(
                type=PhoneActionType.OPEN_APPLICATION,
                target="com.google.android.apps.nbu.files",
                description=f"Open the Files app on the paired phone",
                parameters={"label": "Files", "package": "com.google.android.apps.nbu.files"},
            )
        else:
            action = PhoneAction(
                type=PhoneActionType.OPEN_FILE,
                target=name,
                description=f"Open the file {name!r} on the paired phone",
                parameters={"name": name},
            )
        return PhoneWorkflow(name=f"Open {name} on phone", actions=(action,))

    def plan_contact_lookup(self, name: str) -> PhoneWorkflow:
        """Plan resolving a saved contact name to a phone number (read-only)."""
        return PhoneWorkflow(
            name=f"Look up {name} in contacts on phone",
            actions=(
                PhoneAction(
                    type=PhoneActionType.CONTACT_LOOKUP,
                    target=name,
                    description=f"Look up the phone number for {name!r} in the paired phone's contacts",
                    parameters={"name": name},
                ),
            ),
        )

    def plan_send_text(self, message: str) -> PhoneWorkflow:
        """Plan a send-text action; target and recipient live in parameters.

        Deliberately approval-irreversible content: the full message text is in
        the plan so the user reviews exactly what would be sent.
        """
        recipient, body = _phone_message_parts(message)
        if not recipient:
            # "text mom good night" — no say/saying/that/: separator. Only split
            # when the leading words are a SAVED contact on the device, so free
            # text is never silently split at the wrong word.
            resolver = getattr(self.runner, "resolve_contact_prefix", None)
            if resolver is not None:
                prefix = resolver(message)
                if prefix:
                    recipient, body = prefix, message[len(prefix):].strip()
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
    # Substring aliases cover common spoken names; unknown names fall back to
    # a package-shaped literal or a package-manager search so EVERY installed
    # app is reachable, not just the alias list.
    aliases = {
        "whatsapp": ("WhatsApp", "com.whatsapp"),
        "chrome": ("Chrome", "com.android.chrome"),
        "youtube": ("YouTube", "com.google.android.youtube"),
        "youtube music": ("YouTube Music", "com.google.android.apps.youtube.music"),
        "yt music": ("YouTube Music", "com.google.android.apps.youtube.music"),
        "gmail": ("Gmail", "com.google.android.gm"),
        "settings": ("Settings", "com.android.settings"),
        "spotify": ("Spotify", "com.spotify.music"),
        "instagram": ("Instagram", "com.instagram.android"),
        "threads": ("Threads", "com.instagram.barcelona"),
        "telegram": ("Telegram", "org.telegram.messenger"),
        "maps": ("Maps", "com.google.android.apps.maps"),
        "camera": ("Camera", "com.android.camera"),
        "photos": ("Photos", "com.google.android.apps.photos"),
        "gallery": ("Gallery", "com.miui.gallery"),
        "files": ("Files", "com.google.android.apps.nbu.files"),
        "file manager": ("Files", "com.google.android.apps.nbu.files"),
        "play store": ("Play Store", "com.android.vending"),
        "play games": ("Play Games", "com.google.android.play.games"),
        "flipkart": ("Flipkart", "com.flipkart.android"),
        "chatgpt": ("ChatGPT", "com.openai.chatgpt"),
        "perplexity": ("Perplexity", "ai.perplexity.app.android"),
        "google tv": ("Google TV", "com.google.android.videos"),
        "podcasts": ("Podcasts", "com.google.android.apps.podcasts"),
        "google news": ("Google News", "com.google.android.apps.magazines"),
        "meet": ("Meet", "com.google.android.apps.tachyon"),
        "duo": ("Meet", "com.google.android.apps.tachyon"),
    }
    for name, mapped in aliases.items():
        if name in lower:
            return mapped
    if "." in value and " " not in value:
        return value, value
    return value, value


def _phone_search_parts(query_spec: str) -> tuple[str, str]:
    """Split "cats on youtube" / "cats in google" into (query, provider)."""
    match = re.match(
        r"^\s*(?P<query>.+?)\s+(?:on|in)\s+(?P<provider>[a-z0-9 ]+?)\s*$",
        " ".join(query_spec.strip().split()),
        re.IGNORECASE,
    )
    if match:
        return match.group("query").strip(), match.group("provider").strip().lower()
    return " ".join(query_spec.strip().split()), "google"


def _phone_file_parts(spec: str) -> tuple[str, bool]:
    """Return (name, is_folder) for an open-files phrasing."""
    value = " ".join(spec.strip().split())
    # Strip articles and the parser's "file " marker so the name stays clean:
    # "the file report.pdf" -> "report.pdf", "the report.pdf file" -> "report.pdf".
    value = re.sub(r"^(?:the|a|an)\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^file\s+", "", value, flags=re.IGNORECASE)
    match = re.match(r"^(?P<name>.+?)\s+(?:folder|directory|file)$", value, re.IGNORECASE)
    if match:
        name = match.group("name").strip()
        is_folder = match.group(0).lower().endswith(("folder", "directory"))
        return name or "Files", is_folder
    if value.lower().startswith(("folder ", "directory ")):
        return value.split(" ", 1)[1].strip(), True
    bare = value.lower() in ("", "file", "folder", "directory", "files")
    if bare:
        # No name given ("open the file"): open the Files app.
        return "Files", True
    return value, False


def _search_uri(query: str, provider: str) -> str:
    """The provider's search URL; Android routes it into the installed app."""
    encoded = urllib.parse.quote(query)
    if "youtube" in provider:
        return f"https://www.youtube.com/results?search_query={encoded}"
    if "maps" in provider or "map" == provider:
        return f"https://www.google.com/maps/search/{encoded}"
    if "play" in provider and "store" in provider:
        return f"https://play.google.com/store/search?q={encoded}"
    if "spotify" in provider:
        return f"https://open.spotify.com/search/{encoded}"
    if "amazon" in provider:
        return f"https://www.amazon.in/s?k={encoded}"
    if "flipkart" in provider:
        return f"https://www.flipkart.com/search?q={encoded}"
    return f"https://www.google.com/search?q={encoded}"


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
