"""Desktop automation controller."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Protocol, runtime_checkable

from novacontrol.core.security import (
    ApprovalGateway,
    ApprovalRequest,
    DenyByDefaultApprovalGateway,
    PermissionScope,
    RiskLevel,
)
from novacontrol.desktop.audit import AutomationAuditLog, InMemoryAutomationAuditLog
from novacontrol.desktop.models import (
    DesktopAction,
    DesktopActionResult,
    DesktopActionStatus,
    DesktopActionType,
    DesktopWorkflow,
)

@runtime_checkable
class DesktopCommandRunner(Protocol):
    async def run(self, action: DesktopAction) -> Mapping[str, Any]:
        """Run an approved desktop action."""


class VerificationError(RuntimeError):
    """An action ran, but its effect on the desktop could not be confirmed.

    JARVIS must not report success for actions that never took effect (an app
    that failed to show a window, text that had no focused window to land in).
    """


def _window_stem(target: str) -> str:
    """Reduce an app target to a matchable name: 'notepad' or 'C:\\...\\notepad.exe' -> 'notepad'."""
    value = target.strip().lower()
    if "\\" in value or "/" in value or value.endswith((".exe", ".lnk", ".app", ".bat", ".cmd")):
        value = Path(value).stem.lower()
    return value


def _window_matches(process_name: str, title: str, stem: str) -> bool:
    """A window matches when its process name or title contains the target name."""
    name = (process_name or "").strip().lower()
    window = (title or "").strip().lower()
    return bool(stem) and (name == stem or stem in name or stem in window)


class NoopDesktopRunner:
    """Safe runner that records intent without changing the desktop."""

    async def run(self, action: DesktopAction) -> Mapping[str, Any]:
        return {"would_run": action.to_dict()}


class LocalDesktopRunner:
    """Runs approved desktop actions on the local machine."""

    def __init__(self, *, command_timeout_seconds: float = 60) -> None:
        self.command_timeout_seconds = command_timeout_seconds

    async def run(self, action: DesktopAction) -> Mapping[str, Any]:
        if action.type is DesktopActionType.OPEN_APPLICATION:
            return await self._open_application(action.target)
        if action.type is DesktopActionType.CLOSE_APPLICATION:
            return await self._close_application(action.target)
        if action.type is DesktopActionType.EXECUTE_SCRIPT:
            return await self._execute_script(
                action.target,
                working_directory=_optional_str(action.parameters.get("working_directory")),
            )
        if action.type is DesktopActionType.ORGANIZE_FILES:
            return self._organize_files(
                action.target,
                extension=str(action.parameters["extension"]),
                destination=str(action.parameters["destination"]),
            )
        if action.type is DesktopActionType.TAKE_SCREENSHOT:
            return await self._take_screenshot(
                save_path=_optional_str(action.parameters.get("save_path")) or "screenshot.png",
            )
        if action.type is DesktopActionType.LIST_PROCESSES:
            return await self._list_processes(
                filter_name=_optional_str(action.parameters.get("filter_name")),
            )
        if action.type is DesktopActionType.LIST_FILES:
            return self._list_files(
                action.target,
                recursive=bool(action.parameters.get("recursive", False)),
                pattern=_optional_str(action.parameters.get("pattern")),
            )
        if action.type is DesktopActionType.READ_FILE:
            return self._read_file(action.target)
        if action.type is DesktopActionType.WRITE_FILE:
            return self._write_file(
                action.target,
                content=str(action.parameters.get("content", "")),
            )
        if action.type is DesktopActionType.SYSTEM_INFO:
            return await self._system_info()
        if action.type is DesktopActionType.KEYBOARD_SHORTCUT:
            return await self._keyboard_shortcut(action.target)
        if action.type is DesktopActionType.TYPE_TEXT:
            return await self._type_text(
                action.target,
                delay_ms=int(action.parameters.get("delay_ms", 30)),
            )
        if action.type is DesktopActionType.OPEN_FOLDER:
            return await self._open_folder(action.target)
        if action.type is DesktopActionType.SEARCH_START_MENU:
            return await self._search_start_menu(action.target)
        raise ValueError(f"Unsupported desktop action type: {action.type}")

    async def _open_application(self, target: str) -> Mapping[str, Any]:
        pid = await self._launch_open(target)
        verified = await self._wait_for_window(target)
        output: dict[str, Any] = {"adapter": "local-desktop", "pid": pid, "target": target}
        if verified is None:
            # Probe unavailable (non-Windows or probe failed): don't fail a launch we
            # cannot check, but never claim it was verified.
            output["verified"] = None
            output["verification"] = "window check unavailable on this platform"
        elif verified:
            output["verified"] = True
            output["window"] = verified["window"]
            output["verification"] = f"Window appeared: {verified['window']}"
        else:
            raise VerificationError(
                f"Launched '{target}' but no window appeared within 8 seconds — "
                "the app may not have launched."
            )
        return output

    async def _launch_open(self, target: str) -> int:
        if sys.platform.startswith("win"):
            process = subprocess.Popen(
                ["cmd", "/c", "start", "", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif sys.platform == "darwin":
            process = subprocess.Popen(
                ["open", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            process = subprocess.Popen(
                ["xdg-open", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return process.pid

    async def _powershell(self, script: str, *, timeout: float = 10.0) -> str:
        process = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-Command", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        return stdout.decode("utf-8", errors="replace")

    async def _window_lines(self) -> list[str] | None:
        """Visible top-level windows as 'processname|title' lines; None when the
        platform probe is unavailable so callers skip rather than fail."""
        if not sys.platform.startswith("win"):
            return None
        try:
            out = await self._powershell(
                "Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } "
                "| ForEach-Object { $_.ProcessName + '|' + $_.MainWindowTitle }",
                timeout=5,
            )
            return [line for line in out.splitlines() if "|" in line]
        except Exception:
            return None

    async def _wait_for_window(
        self, target: str, *, timeout_seconds: float = 8.0
    ) -> dict[str, Any] | None:
        """Poll for a visible window matching the target.

        Returns a dict with the matched title when found, an empty dict when the probe ran
        but nothing appeared, and None when the probe is unavailable.
        """
        stem = _window_stem(target)
        deadline = time.monotonic() + timeout_seconds
        while True:
            lines = await self._window_lines()
            if lines is None:
                return None
            for line in lines:
                name, sep, title = line.partition("|")
                if _window_matches(name, title if sep else "", stem):
                    return {"window": (title if sep else name).strip()}
            if time.monotonic() >= deadline:
                return {}
            await asyncio.sleep(0.5)

    async def _execute_script(
        self,
        command: str,
        *,
        working_directory: str | None,
    ) -> Mapping[str, Any]:
        # NO-SHELL INVARIANT: commands run exec-form only — shlex.split turns the command into
        # literal argv tokens and create_subprocess_exec never interprets shell metacharacters.
        # Do not switch to shell=True / create_subprocess_shell / os.system here; the shell would
        # re-interpret those tokens. DesktopNoShellInvariantTests guards this contract.
        import shlex
        parts = shlex.split(command)
        process = await asyncio.create_subprocess_exec(
            *parts,
            cwd=working_directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.command_timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise TimeoutError(f"Command timed out after {self.command_timeout_seconds:g}s") from exc

        return {
            "adapter": "local-desktop",
            "command": command,
            "working_directory": working_directory,
            "exit_code": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }

    def _organize_files(
        self,
        source_directory: str,
        *,
        extension: str,
        destination: str,
    ) -> Mapping[str, Any]:
        source = Path(source_directory).expanduser().resolve()
        destination_path = Path(destination).expanduser()
        target_directory = (
            destination_path.resolve()
            if destination_path.is_absolute()
            else (source / destination_path).resolve()
        )
        if not source.is_dir():
            raise FileNotFoundError(f"Source directory does not exist: {source}")
        if not str(target_directory).startswith(str(source)):
            raise ValueError(f"Destination escapes source directory: {destination}")

        target_directory.mkdir(parents=True, exist_ok=True)
        normalized_extension = extension if extension.startswith(".") else f".{extension}"
        moved: list[str] = []
        for child in source.iterdir():
            if not child.is_file() or child.suffix.lower() != normalized_extension.lower():
                continue
            destination_file = target_directory / child.name
            shutil.move(str(child), str(destination_file))
            moved.append(str(destination_file))

        return {
            "adapter": "local-desktop",
            "source_directory": str(source),
            "destination": str(target_directory),
            "extension": normalized_extension,
            "moved": moved,
            "moved_count": len(moved),
        }

    async def _type_text(self, text: str, *, delay_ms: int = 30) -> Mapping[str, Any]:
        """Type text into the currently focused window, then verify it landed.

        On Windows the paste is staged through the clipboard and Ctrl+V; verification
        confirms the exact payload reached the clipboard AND a window was focused to
        receive it before reporting success. Other platforms keep the raw keystroke
        behavior (unverified).
        """
        if sys.platform.startswith("win"):
            await self._stage_and_paste_windows(text)
            verification = await self._verify_type_windows(text)
            output: dict[str, Any] = {
                "adapter": "local-desktop",
                "action": "type_text",
                "text": text,
                "char_count": len(text),
            }
            if verification is None:
                output["verified"] = None
                output["verification"] = "paste verification unavailable on this platform"
            elif verification.get("clipboard_match") and verification.get("focused_window"):
                output["verified"] = True
                output["verification"] = {
                    "clipboard_staged": True,
                    "focused_window": verification.get("window_title") or "(unlabeled window)",
                }
            else:
                reason = (
                    "the text never reached the clipboard (staging failed)"
                    if not verification.get("clipboard_match")
                    else "no window is focused — the paste had nowhere to land"
                )
                raise VerificationError(f"Typed text was not verified: {reason}.")
            return output
        if sys.platform == "darwin":
            process = await asyncio.create_subprocess_exec(
                "osascript", "-e", f'tell application "System Events" to keystroke "{text}"',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=10)
        else:
            process = await asyncio.create_subprocess_exec(
                "xdotool", "type", "--delay", str(delay_ms), text,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=10)
        return {
            "adapter": "local-desktop",
            "action": "type_text",
            "text": text,
            "char_count": len(text),
        }

    async def _stage_and_paste_windows(self, text: str) -> None:
        """Stage the exact text on the clipboard and paste it into the focused window."""
        quoted = "'" + text.replace("'", "''") + "'"
        script = (
            f"Set-Clipboard -Value {quoted}; "
            "Add-Type -AssemblyName System.Windows.Forms; "
            "[System.Windows.Forms.SendKeys]::SendWait('^v')"
        )
        await self._powershell(script, timeout=10)

    async def _verify_type_windows(self, text: str) -> dict[str, Any] | None:
        """Confirm the staged text reached the clipboard and a window was focused.

        Returns None when the probe itself fails, so an unverifiable paste is not
        wrongly reported as success or failure.
        """
        quoted = "'" + text.replace("'", "''") + "'"
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; "
            "using System.Text; public class FG { "
            "[DllImport(\"user32.dll\")] public static extern IntPtr GetForegroundWindow(); "
            "[DllImport(\"user32.dll\")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n); }'; "
            "$h = [FG]::GetForegroundWindow(); "
            "$sb = New-Object System.Text.StringBuilder 512; "
            "[void][FG]::GetWindowText($h, $sb, 512); "
            "$cb = [System.Windows.Forms.Clipboard]::GetText(); "
            f"Write-Output ('CBOK|' + ($cb -eq {quoted})); "
            "Write-Output ('FGW|' + ($h -ne 0)); "
            "Write-Output ('FGTTL|' + $sb.ToString())"
        )
        try:
            out = await self._powershell(script, timeout=10)
        except Exception:
            return None
        parsed: dict[str, Any] = {"clipboard_match": False, "focused_window": False, "window_title": ""}
        for line in out.splitlines():
            key, sep, value = line.partition("|")
            if key == "CBOK":
                parsed["clipboard_match"] = value.strip() == "True"
            elif key == "FGW":
                parsed["focused_window"] = value.strip() == "True"
            elif key == "FGTTL":
                parsed["window_title"] = value.strip()
        return parsed

    async def _open_folder(self, folder_path: str) -> Mapping[str, Any]:
        """Open a folder in the system file manager."""
        path = Path(folder_path).expanduser().resolve()
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        target = str(path)
        if sys.platform.startswith("win"):
            process = subprocess.Popen(
                ["explorer", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif sys.platform == "darwin":
            process = subprocess.Popen(
                ["open", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            process = subprocess.Popen(
                ["xdg-open", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return {"adapter": "local-desktop", "action": "open_folder", "pid": process.pid, "path": target}

    async def _search_start_menu(self, query: str) -> Mapping[str, Any]:
        """Open Start Menu and search for an application on Windows."""
        if sys.platform.startswith("win"):
            # Press Win key, wait, then type the query
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "[System.Windows.Forms.SendKeys]::SendWait('^{ESC}'); "
                "Start-Sleep -Milliseconds 500; "
                f"[System.Windows.Forms.SendKeys]::SendWait('{query}')"
            )
            await self._powershell(script, timeout=10)
        elif sys.platform == "darwin":
            process = await asyncio.create_subprocess_exec(
                "open", "-a", "Spotlight",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=5)
            process = await asyncio.create_subprocess_exec(
                "osascript", "-e",
                f'tell application "System Events" to keystroke "{query}"',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=5)
        else:
            # Linux: use dmenu or rofi
            process = await asyncio.create_subprocess_exec(
                "bash", "-c", f"echo '{query}' | rofi -dmenu",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=5)
        return {
            "adapter": "local-desktop",
            "action": "search_start_menu",
            "query": query,
        }

    async def _close_application(self, target: str) -> Mapping[str, Any]:
        if sys.platform.startswith("win"):
            process = subprocess.Popen(
                ["taskkill", "/IM", target, "/F"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = process.communicate()
            return {
                "adapter": "local-desktop",
                "action": "close_application",
                "target": target,
                "exit_code": process.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            }
        elif sys.platform == "darwin":
            import shlex
            safe_target = shlex.quote(target)
            process = subprocess.Popen(
                ["osascript", "-e", f"quit app {safe_target}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = process.communicate()
            return {
                "adapter": "local-desktop",
                "action": "close_application",
                "target": target,
                "exit_code": process.returncode,
            }
        else:
            process = subprocess.Popen(
                ["pkill", "-f", target],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = process.communicate()
            return {
                "adapter": "local-desktop",
                "action": "close_application",
                "target": target,
                "exit_code": process.returncode,
            }

    async def _take_screenshot(self, save_path: str = "screenshot.png") -> Mapping[str, Any]:
        """Capture a screenshot using platform-native tools."""
        if sys.platform.startswith("win"):
            # Use PowerShell to take screenshot
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName System.Drawing; "
                "$bmp = [System.Drawing.Bitmap]::new([System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width, "
                "[System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height); "
                "$g = [System.Drawing.Graphics]::FromImage($bmp); "
                "$g.CopyFromScreen(0, 0, 0, 0, $bmp.Size); "
                f"$bmp.Save('{save_path}'); "
                "Write-Output 'screenshot_saved'"
            )
            await self._powershell(script, timeout=15)
        elif sys.platform == "darwin":
            process = await asyncio.create_subprocess_exec(
                "screencapture", "-x", save_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=15)
        else:
            process = await asyncio.create_subprocess_exec(
                "scrot", save_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=15)

        saved_path = Path(save_path).resolve()
        return {
            "adapter": "local-desktop",
            "action": "take_screenshot",
            "path": str(saved_path),
            "exists": saved_path.exists(),
            "size_bytes": saved_path.stat().st_size if saved_path.exists() else 0,
        }

    async def _list_processes(self, filter_name: str | None = None) -> Mapping[str, Any]:
        """List running processes."""
        if sys.platform.startswith("win"):
            cmd = ["tasklist", "/FO", "CSV"]
        else:
            cmd = ["ps", "aux"]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        output = stdout.decode("utf-8", errors="replace")

        processes: list[dict[str, str]] = []
        for line in output.strip().splitlines():
            if filter_name and filter_name.lower() not in line.lower():
                continue
            processes.append({"line": line})

        return {
            "adapter": "local-desktop",
            "action": "list_processes",
            "count": len(processes),
            "processes": processes[:50],
        }

    def _list_files(
        self,
        directory: str,
        *,
        recursive: bool = False,
        pattern: str | None = None,
    ) -> Mapping[str, Any]:
        """List files in a directory."""
        path = Path(directory).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"Directory not found: {path}")

        if recursive:
            files = list(path.rglob(pattern or "*"))
        else:
            files = list(path.glob(pattern or "*"))

        entries = []
        for f in sorted(files)[:200]:
            entries.append({
                "path": str(f),
                "name": f.name,
                "type": "directory" if f.is_dir() else "file",
                "size": f.stat().st_size if f.is_file() else 0,
            })

        return {
            "adapter": "local-desktop",
            "action": "list_files",
            "directory": str(path),
            "count": len(entries),
            "entries": entries,
        }

    def _read_file(self, file_path: str) -> Mapping[str, Any]:
        """Read a file's content."""
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")

        content = path.read_text(encoding="utf-8", errors="replace")
        return {
            "adapter": "local-desktop",
            "action": "read_file",
            "path": str(path),
            "size_bytes": len(content.encode()),
            "content": content[:10000],  # Limit for safety
            "truncated": len(content) > 10000,
        }

    def _write_file(self, file_path: str, content: str) -> Mapping[str, Any]:
        """Write content to a file."""
        path = Path(file_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {
            "adapter": "local-desktop",
            "action": "write_file",
            "path": str(path),
            "size_bytes": len(content.encode()),
        }

    async def _system_info(self) -> Mapping[str, Any]:
        """Get system information."""
        import platform
        info: dict[str, Any] = {
            "adapter": "local-desktop",
            "action": "system_info",
            "platform": platform.platform(),
            "python": platform.python_version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        }
        try:
            import psutil
            info["cpu_count"] = psutil.cpu_count()
            mem = psutil.virtual_memory()
            info["ram_total_gb"] = round(mem.total / (1024**3), 1)
            info["ram_used_percent"] = mem.percent
            disk = psutil.disk_usage("/")
            info["disk_total_gb"] = round(disk.total / (1024**3), 1)
            info["disk_used_percent"] = round(disk.percent, 1)
        except ImportError:
            info["note"] = "psutil not installed; limited system info available"
        return info

    async def _keyboard_shortcut(self, shortcut: str) -> Mapping[str, Any]:
        """Simulate a keyboard shortcut (e.g., 'ctrl+c')."""
        # Parse shortcut like 'ctrl+shift+s'
        keys = [k.strip().lower() for k in shortcut.split("+")]

        if sys.platform.startswith("win"):
            # Use PowerShell with SendKeys
            send_keys_map = {
                "ctrl": "^",
                "alt": "%",
                "shift": "+",
                "enter": "{ENTER}",
                "tab": "{TAB}",
                "esc": "{ESC}",
                "delete": "{DELETE}",
                "backspace": "{BACKSPACE}",
            }
            keys_str = "".join(send_keys_map.get(k, k) for k in keys)
            script = f"Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait('{keys_str}')"
            await self._powershell(script, timeout=5)
        else:
            # Use xdotool on Linux
            keys_str = "+".join(keys)
            process = await asyncio.create_subprocess_exec(
                "xdotool", "key", keys_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=5)

        return {
            "adapter": "local-desktop",
            "action": "keyboard_shortcut",
            "shortcut": shortcut,
            "keys": keys,
        }


def _single_action_workflow(
    name: str,
    action_type: DesktopActionType,
    target: str,
    description: str,
    parameters: Mapping[str, Any] | None = None,
) -> DesktopWorkflow:
    """Wrap one action in a workflow — the shared shape of every single-action plan."""
    return DesktopWorkflow(
        name=name,
        actions=(
            DesktopAction(
                type=action_type,
                target=target,
                description=description,
                parameters=parameters or {},
            ),
        ),
    )


class DesktopAutomationController:
    """Plans and executes approved desktop automation workflows."""

    def __init__(
        self,
        *,
        approval_gateway: ApprovalGateway | None = None,
        runner: DesktopCommandRunner | None = None,
        audit_log: AutomationAuditLog | None = None,
    ) -> None:
        self.approval_gateway = approval_gateway or DenyByDefaultApprovalGateway()
        self.runner = runner or NoopDesktopRunner()
        self.audit_log = audit_log or InMemoryAutomationAuditLog()

    def plan_open_application(self, application: str) -> DesktopWorkflow:
        return _single_action_workflow(
            f"Open {application}", DesktopActionType.OPEN_APPLICATION, application, f"Open application {application}",
        )

    def plan_execute_script(self, command: str, *, working_directory: str | None = None) -> DesktopWorkflow:
        return _single_action_workflow(
            "Execute script", DesktopActionType.EXECUTE_SCRIPT, command,
            "Execute a user-approved script or command.",
            {"working_directory": working_directory},
        )

    def plan_file_organization(
        self,
        source_directory: str,
        extension_to_directory: Mapping[str, str],
    ) -> DesktopWorkflow:
        actions = tuple(
            DesktopAction(
                type=DesktopActionType.ORGANIZE_FILES,
                target=source_directory,
                description=f"Move *{extension} files into {destination}",
                parameters={"extension": extension, "destination": destination},
            )
            for extension, destination in extension_to_directory.items()
        )
        return DesktopWorkflow(name="Organize files", actions=actions)

    def plan_screenshot(self, save_path: str = "screenshot.png") -> DesktopWorkflow:
        return _single_action_workflow(
            "Take screenshot", DesktopActionType.TAKE_SCREENSHOT, "screenshot",
            "Capture the current screen.", {"save_path": save_path},
        )

    def plan_system_info(self) -> DesktopWorkflow:
        return _single_action_workflow(
            "System information", DesktopActionType.SYSTEM_INFO, "system", "Gather system information.",
        )

    def plan_list_files(self, directory: str, *, recursive: bool = False) -> DesktopWorkflow:
        return _single_action_workflow(
            "List files", DesktopActionType.LIST_FILES, directory, f"List files in {directory}.",
            {"recursive": recursive},
        )

    def plan_keyboard_shortcut(self, shortcut: str) -> DesktopWorkflow:
        return _single_action_workflow(
            "Keyboard shortcut", DesktopActionType.KEYBOARD_SHORTCUT, shortcut, f"Press keyboard shortcut {shortcut}.",
        )

    def plan_type_text(self, text: str, *, delay_ms: int = 30) -> DesktopWorkflow:
        return _single_action_workflow(
            "Type text", DesktopActionType.TYPE_TEXT, text, f"Type '{text}' into the focused window.",
            {"delay_ms": delay_ms},
        )

    def plan_open_folder(self, folder_path: str) -> DesktopWorkflow:
        return _single_action_workflow(
            "Open folder", DesktopActionType.OPEN_FOLDER, folder_path, f"Open folder '{folder_path}'.",
        )

    def plan_search_start_menu(self, query: str) -> DesktopWorkflow:
        return _single_action_workflow(
            f"Search for {query}", DesktopActionType.SEARCH_START_MENU, query, f"Search Start Menu for '{query}'.",
        )

    async def execute_workflow(self, workflow: DesktopWorkflow) -> tuple[DesktopActionResult, ...]:
        results = []
        for action in workflow.actions:
            result = await self.execute_action(action)
            results.append(result)
            if result.status is not DesktopActionStatus.COMPLETED:
                break
        return tuple(results)

    async def execute_action(self, action: DesktopAction) -> DesktopActionResult:
        approval = await self.approval_gateway.request_approval(
            ApprovalRequest(
                action=action.description,
                reason="Desktop automation requires explicit user approval.",
                permissions=_permissions_for(action.type),
                risk=RiskLevel.HIGH,
                metadata=action.to_dict(),
            )
        )
        if not approval.approved:
            result = DesktopActionResult(
                action_id=action.id,
                status=DesktopActionStatus.DENIED,
                output={},
                error=approval.reason or "Desktop automation was not approved.",
                approval_id=approval.request_id,
            )
            await self.audit_log.append(result)
            return result

        try:
            output = await self.runner.run(action)
            result = DesktopActionResult(
                action_id=action.id,
                status=DesktopActionStatus.COMPLETED,
                output=dict(output),
                approval_id=approval.request_id,
            )
        except Exception as exc:
            result = DesktopActionResult(
                action_id=action.id,
                status=DesktopActionStatus.FAILED,
                output={},
                error=f"{type(exc).__name__}: {exc}",
                approval_id=approval.request_id,
            )
        await self.audit_log.append(result)
        return result


def _permissions_for(action_type: DesktopActionType) -> tuple[PermissionScope, ...]:
    if action_type is DesktopActionType.EXECUTE_SCRIPT:
        return (PermissionScope.DESKTOP_CONTROL, PermissionScope.SHELL_EXECUTE)
    if action_type is DesktopActionType.ORGANIZE_FILES:
        return (PermissionScope.DESKTOP_CONTROL, PermissionScope.FILESYSTEM_WRITE)
    if action_type is DesktopActionType.WRITE_FILE:
        return (PermissionScope.DESKTOP_CONTROL, PermissionScope.FILESYSTEM_WRITE)
    if action_type is DesktopActionType.READ_FILE:
        return (PermissionScope.DESKTOP_CONTROL,)
    if action_type is DesktopActionType.LIST_FILES:
        return (PermissionScope.DESKTOP_CONTROL,)
    if action_type is DesktopActionType.KEYBOARD_SHORTCUT:
        return (PermissionScope.DESKTOP_CONTROL, PermissionScope.SHELL_EXECUTE)
    if action_type is DesktopActionType.TYPE_TEXT:
        return (PermissionScope.DESKTOP_CONTROL,)
    if action_type is DesktopActionType.OPEN_FOLDER:
        return (PermissionScope.DESKTOP_CONTROL,)
    if action_type is DesktopActionType.SEARCH_START_MENU:
        return (PermissionScope.DESKTOP_CONTROL,)
    return (PermissionScope.DESKTOP_CONTROL,)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
