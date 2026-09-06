"""Unified Action Engine over NovaControl's existing runners.

Every action takes a SEMANTIC target (element text/purpose from the UiState)
and falls back to coordinates or selectors only when semantics fail. Every
action returns a structured `ActionOutcome` — never a bare bool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from novacontrol.agentcore.ui_state import UiState


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    success: bool
    action: str
    target: str
    detail: str = ""
    observed_change: bool | None = None  # None = not observed yet
    output: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "action": self.action,
            "target": self.target,
            "detail": self.detail,
            "observed_change": self.observed_change,
            "output": self.output,
        }


class _BrowserRunner(Protocol):
    async def run(self, action: Any) -> dict[str, Any]:  # pragma: no cover - protocol
        ...


class _DesktopRunner(Protocol):
    async def run(self, action: Any) -> dict[str, Any]:  # pragma: no cover - protocol
        ...


class ActionEngine:
    """Semantic-first actions backed by the browser and desktop controllers."""

    def __init__(self, *, browser_runner: Any | None = None, desktop_runner: Any | None = None) -> None:
        self._browser = browser_runner
        self._desktop = desktop_runner
        # Destructive actions require an approval token from the caller; the
        # engine refuses to run them without one.
        self._destructive = {"delete", "close_application", "execute_terminal_command", "download", "upload"}

    # -- high-level semantic actions -----------------------------------------

    async def click_element(self, ui_state: UiState, label: str) -> ActionOutcome:
        """Click by semantic label; falls back to the element's selector."""
        element = ui_state.find(label)
        if element is None:
            return ActionOutcome(False, "click", label, detail="No matching element in the current UI state")
        if self._browser is None:
            return ActionOutcome(False, "click", label, detail="No browser runner configured")
        selector = element.selector or f"text={element.text}"
        from novacontrol.browser.models import BrowserAction, BrowserActionType

        result = await self._browser.run(BrowserAction(
            type=BrowserActionType.CLICK,
            target=selector,
            description=f"Click {element.text or label}",
            parameters={},
        ))
        ok = not str(result.get("error", ""))
        return ActionOutcome(
            success=ok,
            action="click",
            target=element.text or label,
            detail=f"clicked via {selector}",
            output=dict(result),
        )

    async def type_text(self, ui_state: UiState, field_label: str, text: str) -> ActionOutcome:
        element = ui_state.find(field_label)
        if element is None or self._browser is None:
            return ActionOutcome(False, "type", field_label, detail="No matching input in the current UI state")
        from novacontrol.browser.models import BrowserAction, BrowserActionType

        result = await self._browser.run(BrowserAction(
            type=BrowserActionType.FILL_FORM,
            target=ui_state.url or "current_page",
            description=f"Type into {element.text or field_label}",
            parameters={"fields": {element.selector or field_label: text}},
        ))
        ok = not str(result.get("error", ""))
        return ActionOutcome(success=ok, action="type", target=element.text or field_label, output=dict(result))

    async def navigate(self, url: str) -> ActionOutcome:
        if self._browser is None:
            return ActionOutcome(False, "navigate", url, detail="No browser runner configured")
        from novacontrol.browser.models import BrowserAction, BrowserActionType

        result = await self._browser.run(BrowserAction(
            type=BrowserActionType.NAVIGATE,
            target=url,
            description=f"Navigate to {url}",
            parameters={},
        ))
        ok = not str(result.get("error", ""))
        return ActionOutcome(success=ok, action="navigate", target=url, output=dict(result))

    async def press_key(self, shortcut: str) -> ActionOutcome:
        """Keyboard shortcut through the desktop runner (e.g. 'ctrl+s')."""
        if self._desktop is None:
            return ActionOutcome(False, "press_key", shortcut, detail="No desktop runner configured")
        from novacontrol.desktop.models import DesktopAction, DesktopActionType

        result = await self._desktop.run(DesktopAction(
            type=DesktopActionType.KEYBOARD_SHORTCUT,
            target=shortcut,
            description=f"Press {shortcut}",
            parameters={},
        ))
        ok = not str(result.get("error", ""))
        return ActionOutcome(success=ok, action="press_key", target=shortcut, output=dict(result))

    async def open_application(self, name: str) -> ActionOutcome:
        if self._desktop is None:
            return ActionOutcome(False, "open_application", name, detail="No desktop runner configured")
        from novacontrol.desktop.models import DesktopAction, DesktopActionType

        result = await self._desktop.run(DesktopAction(
            type=DesktopActionType.OPEN_APPLICATION,
            target=name,
            description=f"Open {name}",
            parameters={},
        ))
        ok = not str(result.get("error", ""))
        return ActionOutcome(success=ok, action="open_application", target=name, output=dict(result))

    async def screenshot(self, save_path: str) -> ActionOutcome:
        if self._desktop is None:
            return ActionOutcome(False, "screenshot", save_path, detail="No desktop runner configured")
        from novacontrol.desktop.models import DesktopAction, DesktopActionType

        result = await self._desktop.run(DesktopAction(
            type=DesktopActionType.TAKE_SCREENSHOT,
            target="screen",
            description="Capture the current screen",
            parameters={"save_path": save_path},
        ))
        ok = bool(result.get("exists"))
        return ActionOutcome(success=ok, action="screenshot", target=save_path, output=dict(result))

    async def execute_terminal_command(self, command: str, *, approval_token: str | None = None) -> ActionOutcome:
        """Destructive-gated shell execution."""
        if not approval_token:
            return ActionOutcome(False, "execute_terminal_command", command, detail="Refused: shell execution requires explicit approval")
        if self._desktop is None:
            return ActionOutcome(False, "execute_terminal_command", command, detail="No desktop runner configured")
        from novacontrol.desktop.models import DesktopAction, DesktopActionType

        result = await self._desktop.run(DesktopAction(
            type=DesktopActionType.EXECUTE_SCRIPT,
            target=command,
            description=f"Execute: {command}",
            parameters={},
        ))
        ok = not str(result.get("error", "")) and result.get("exit_code") in (0, None)
        return ActionOutcome(success=ok, action="execute_terminal_command", target=command, output=dict(result))

    # -- registry view --------------------------------------------------------

    def catalog(self) -> tuple[dict[str, Any], ...]:
        """Tool-registry-style description of the engine's capabilities."""
        return (
            {"name": "click_element", "risk": "medium", "input": {"label": "string"}, "surface": "browser"},
            {"name": "type_text", "risk": "medium", "input": {"field_label": "string", "text": "string"}, "surface": "browser"},
            {"name": "navigate", "risk": "low", "input": {"url": "string"}, "surface": "browser"},
            {"name": "press_key", "risk": "medium", "input": {"shortcut": "string"}, "surface": "desktop"},
            {"name": "open_application", "risk": "low", "input": {"name": "string"}, "surface": "desktop"},
            {"name": "screenshot", "risk": "low", "input": {"save_path": "string"}, "surface": "desktop"},
            {"name": "execute_terminal_command", "risk": "high", "input": {"command": "string", "approval_token": "string"}, "surface": "desktop"},
        )
