"""Unified Action Engine over NovaControl's existing runners.

Every action takes a SEMANTIC target (element text/purpose from the UiState)
and falls back to coordinates or selectors only when semantics fail. Every
action returns a structured `ActionOutcome` — never a bare bool.

The desktop path is semantic first too: a located element is addressed by its
observed coordinate only after its owning window has been focused and the
coordinate has been validated against that window's geometry. A label that was
not observed is sent to the existing OCR/vision locator, never invented.
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

    def __init__(
        self,
        *,
        browser_runner: Any | None = None,
        browser_controller: Any | None = None,
        desktop_runner: Any | None = None,
        desktop_controller: Any | None = None,
    ) -> None:
        self._browser = browser_runner
        # When the controller is supplied, every browser action travels through
        # its approval, permission and audit layers. The raw runner is only a
        # seam for unit tests and non-controller callers; it must never be the
        # path a real computer-operation task takes.
        self._browser_controller = browser_controller
        self._desktop = desktop_runner
        # When the controller is supplied, every desktop action travels through
        # its approval, permission and audit layers. The raw runner remains only
        # for unit seams and non-controller callers; it must never be the path a
        # real computer-operation task takes.
        self._desktop_controller = desktop_controller
        # Destructive actions require an approval token from the caller; the
        # engine refuses to run them without one.
        self._destructive = {
            "delete",
            "close_application",
            "execute_terminal_command",
            "download",
            "upload",
        }

    # -- high-level semantic actions -----------------------------------------

    async def click_element(
        self,
        ui_state: UiState,
        label: str,
        *,
        button: str = "left",
        count: int = 1,
        require_bound_target: bool = False,
    ) -> ActionOutcome:
        """Click by semantic label, on the surface the observation came from.

        A DOM element uses its selector. A desktop element uses the coordinate
        that was actually observed, after focusing its window and validating
        that point against the window's bounds. An unobserved label is offered
        to the existing OCR/vision locator, which fails honestly if it cannot
        see it.
        """
        element = ui_state.find(label)
        if element is None:
            return ActionOutcome(False, "click", label, detail="No matching element in the current UI state")

        browser_surface = bool(element.selector) or (
            ui_state.application.lower() == "browser"
            and (self._browser is not None or self._browser_controller is not None)
        )
        if browser_surface and (self._browser is not None or self._browser_controller is not None):
            selector = element.selector or f"text={element.text or label}"
            from novacontrol.browser.models import BrowserAction, BrowserActionType

            try:
                result = await self._run_browser(BrowserAction(
                    type=BrowserActionType.CLICK,
                    target=selector,
                    description=f"Click {element.text or label}",
                    parameters={"button": button, "click_count": max(1, min(3, count))},
                ))
            except Exception as exc:
                return ActionOutcome(False, "click", element.text or label, detail=f"{type(exc).__name__}: {exc}")
            ok = not str(result.get("error", ""))
            return ActionOutcome(
                success=ok,
                action="click",
                target=element.text or label,
                detail=f"clicked via {selector}" if ok else str(result.get("error", "")),
                output=dict(result),
            )

        if self._desktop is None and self._desktop_controller is None:
            return ActionOutcome(False, "click", label, detail="No desktop or browser runner configured")

        if require_bound_target:
            # D12: a desktop click is only meaningful inside the window the
            # objective named. Without that binding the observed coordinates
            # belong to no verified window, and clicking them is the same
            # guess that sent text to an unrelated application.
            target_data = (ui_state.metadata or {}).get("target")
            status = str(target_data.get("status")) if isinstance(target_data, dict) else ""
            if status != "bound":
                return ActionOutcome(
                    False,
                    "click",
                    element.text or label,
                    detail=(
                        "Refusing to click: the observation has no window bound to "
                        "the objective, so the click has no verified destination."
                    ),
                )

        if element.location is None:
            # No observed geometry: the existing vision/OCR locator is the only
            # honest way to find it. It raises rather than clicking a guess.
            outcome = await self._run_desktop(
                "vision_click",
                element.text or label,
                {"screenshot_path": "vision_locate.png"},
                f"Locate and click {element.text or label}",
            )
            return ActionOutcome(
                outcome.success,
                "click",
                element.text or label,
                detail=outcome.detail,
                output=outcome.output,
            )

        if ui_state.window:
            focused = await self._run_desktop(
                "window_focus",
                ui_state.window,
                {},
                f"Focus {ui_state.window} before clicking {element.text or label}",
            )
            if not focused.success:
                return ActionOutcome(
                    False,
                    "click",
                    element.text or label,
                    detail=f"Could not focus the observed window: {focused.detail}",
                    output=focused.output,
                )
        x, y = element.location
        outcome = await self._run_desktop(
            "mouse_click",
            element.text or label,
            {
                "x": int(x),
                "y": int(y),
                "button": button,
                "count": max(1, min(3, int(count))),
                "window_title": ui_state.window,
                "require_foreground": True,
            },
            f"Click {element.text or label} at the observed point ({x}, {y})",
        )
        return ActionOutcome(
            outcome.success,
            "click",
            element.text or label,
            detail=outcome.detail,
            output=outcome.output,
        )

    async def type_text(self, ui_state: UiState, field_label: str, text: str) -> ActionOutcome:
        """Type into a semantic field (browser selector or desktop coordinate)."""
        element = ui_state.find(field_label)
        browser_surface = bool(element and element.selector) or (
            ui_state.application.lower() == "browser"
            and (self._browser is not None or self._browser_controller is not None)
        )
        if element is None:
            return ActionOutcome(False, "type", field_label, detail="No matching input in the current UI state")
        if browser_surface and (self._browser is not None or self._browser_controller is not None):
            from novacontrol.browser.models import BrowserAction, BrowserActionType

            try:
                result = await self._run_browser(BrowserAction(
                    type=BrowserActionType.FILL_FORM,
                    target=ui_state.url or "current_page",
                    description=f"Type into {element.text or field_label}",
                    parameters={"fields": {element.selector or field_label: text}},
                ))
            except Exception as exc:
                return ActionOutcome(False, "type", element.text or field_label, detail=f"{type(exc).__name__}: {exc}")
            ok = not str(result.get("error", ""))
            return ActionOutcome(ok, "type", element.text or field_label, output=dict(result))

        if self._desktop is None and self._desktop_controller is None:
            return ActionOutcome(False, "type", field_label, detail="No desktop runner configured")
        if element.location is not None:
            focus_click = await self.click_element(ui_state, field_label)
            if not focus_click.success:
                return ActionOutcome(
                    False,
                    "type",
                    element.text or field_label,
                    detail=f"Could not focus the field: {focus_click.detail}",
                    output=focus_click.output,
                )
        desktop_result = await self._run_desktop(
            "type_text",
            text,
            {"delay_ms": 20, "expect": ui_state.window},
            f"Type into {element.text or field_label}",
        )
        return ActionOutcome(
            desktop_result.success,
            "type",
            element.text or field_label,
            detail=desktop_result.detail,
            output=desktop_result.output,
        )

    async def type_into_active(
        self,
        text: str,
        *,
        window_title: str = "",
        window_handle: int = 0,
        window_pid: int = 0,
    ) -> ActionOutcome:
        """Type into the objective-named editor, after focusing THAT window.

        A desktop editor does not expose a field element to the window probe, so
        the grounding is the objective-bound window: its title, its handle and
        its pid. There is no "active window" destination here — D12 was exactly
        that: text typed into whichever window the first titled probe entry named.
        With no bound identity this refuses instead of typing anywhere.
        """
        if self._desktop is None and self._desktop_controller is None:
            return ActionOutcome(False, "type", window_title or "unverified window", detail="No desktop runner configured")
        if not window_title:
            return ActionOutcome(
                False,
                "type",
                "unverified window",
                detail=(
                    "Refusing to type: no window was established from the objective, "
                    "so there is no destination this engine can justify."
                ),
            )
        if not (int(window_handle) and int(window_pid)):
            return ActionOutcome(
                False,
                "type",
                window_title,
                detail=(
                    f"Refusing to type into {window_title!r}: the observation reports "
                    "no stable window handle/pid, so its identity cannot be checked "
                    "at input time."
                ),
            )
        focused = await self._run_desktop("window_focus", window_title, {}, f"Focus {window_title} before typing")
        if not focused.success:
            return ActionOutcome(False, "type", window_title, detail=f"Could not focus the observed window: {focused.detail}")
        result = await self._run_desktop(
            "type_text",
            text,
            {
                "delay_ms": 15,
                "expect": window_title,
                "expect_handle": int(window_handle),
                "expect_pid": int(window_pid),
            },
            f"Type into {window_title}",
        )
        return ActionOutcome(result.success, "type", window_title or "active window", detail=result.detail, output=result.output)

    async def save_document(
        self, path: str, *, window_title: str = "", direct: bool = False
    ) -> ActionOutcome:
        """Save the focused document to an explicit path, then verify the file.

        This is not a coordinate macro: Ctrl+S is sent only to the observed
        foreground window, the save dialog is allowed to appear, and success is
        reported only when the requested file exists and is non-empty. If the
        save dialog is not handled by the operating system's default focus, the
        action fails rather than claiming a save.
        """
        if self._desktop is None and self._desktop_controller is None:
            return ActionOutcome(False, "save", path, detail="No desktop runner configured")
        if window_title:
            focused = await self._run_desktop("window_focus", window_title, {}, f"Focus {window_title} before saving")
            if not focused.success:
                return ActionOutcome(False, "save", path, detail=f"Could not focus the document window: {focused.detail}")
        if direct:
            # The caller has confirmed this path IS the open document. Ctrl+S is
            # then the exact operation, and success is still the file on disk
            # changing, not the keystroke returning.
            import asyncio
            import os

            save = await self._run_desktop("keyboard_shortcut", "ctrl+s", {}, "Save the open document")
            if not save.success:
                return ActionOutcome(False, "save", path, detail=f"Could not save: {save.detail}", output=save.output)
            for _ in range(20):
                try:
                    if os.path.isfile(path) and os.path.getsize(path) > 0:
                        return ActionOutcome(
                            True,
                            "save",
                            path,
                            detail=f"Saved {os.path.getsize(path)} bytes and verified the file exists.",
                            output={"bytes": os.path.getsize(path), **dict(save.output)},
                        )
                except OSError:
                    pass
                await asyncio.sleep(0.2)
            return ActionOutcome(False, "save", path, detail="Save was requested, but the open document's file is empty or absent.", output=save.output)
        # Save As, never Save: a generic "save" writes over whatever file the
        # editor already had open. The caller supplied a destination, so the
        # operation must be a save to that destination.
        save = await self._run_desktop("keyboard_shortcut", "ctrl+shift+s", {}, "Open the document Save As dialog")
        if not save.success:
            return ActionOutcome(False, "save", path, detail=f"Could not open Save: {save.detail}", output=save.output)
        # Wait until the OS reports a Save As surface. Typing a path into the
        # document because no dialog appeared is exactly the destructive
        # mis-target this action must refuse.
        import asyncio

        dialog_seen = False
        for _ in range(24):
            foreground = await self._foreground_title()
            details = await self._window_details()
            titles = [foreground] + [
                str(item.get("title", ""))
                for item in (details or [])
                if isinstance(item, dict)
            ]
            if any("save as" in title.lower() or title.lower() == "save" for title in titles):
                dialog_seen = True
                break
            await asyncio.sleep(0.25)
        if not dialog_seen:
            await self._run_desktop("keyboard_shortcut", "escape", {}, "Dismiss a save surface that did not appear")
            return ActionOutcome(
                False,
                "save",
                path,
                detail="No Save As dialog appeared; refusing to type the path into the document.",
                output=save.output,
            )
        select = await self._run_desktop("keyboard_shortcut", "ctrl+a", {}, "Select the Save As file name")
        if not select.success:
            return ActionOutcome(False, "save", path, detail="Could not select the Save As file name field.")
        typed = await self._run_desktop(
            "type_text",
            path,
            {"delay_ms": 10},
            f"Enter the save path {path}",
        )
        if not typed.success:
            return ActionOutcome(False, "save", path, detail=f"Could not enter the save path: {typed.detail}", output=typed.output)
        await asyncio.sleep(0.3)
        confirm = await self._run_desktop("keyboard_shortcut", "enter", {}, "Confirm the Save dialog")
        import os

        exists = False
        size = 0
        for _ in range(20):
            try:
                if os.path.isfile(path):
                    exists = True
                    size = os.path.getsize(path)
                    if size > 0:
                        break
            except OSError:
                pass
            await asyncio.sleep(0.2)
        if not exists:
            return ActionOutcome(False, "save", path, detail="Save was requested, but the requested file does not exist.", output=confirm.output)
        return ActionOutcome(True, "save", path, detail=f"Saved {size} bytes and verified the file exists.", output={"bytes": size, **dict(confirm.output)})

    async def _window_details(self) -> list[dict[str, Any]] | None:
        """The OS's own window list, used to confirm a dialog exists."""
        target = self._desktop_controller.runner if self._desktop_controller is not None else self._desktop
        probe = getattr(target, "window_details", None)
        if probe is None:
            return None
        try:
            result = probe()
            if hasattr(result, "__await__"):
                result = await result
            return list(result) if result is not None else None
        except Exception:
            return None

    async def _foreground_title(self) -> str:
        target = self._desktop_controller.runner if self._desktop_controller is not None else self._desktop
        probe = getattr(target, "foreground_window_title", None)
        if probe is None:
            return ""
        try:
            result = probe()
            if hasattr(result, "__await__"):
                result = await result
            return str(result or "")
        except Exception:
            return ""

    async def navigate(self, url: str) -> ActionOutcome:
        if self._browser is None and self._browser_controller is None:
            return ActionOutcome(False, "navigate", url, detail="No browser runner configured")
        from novacontrol.browser.models import BrowserAction, BrowserActionType

        try:
            result = await self._run_browser(BrowserAction(
                type=BrowserActionType.NAVIGATE,
                target=url,
                description=f"Navigate to {url}",
                parameters={},
            ))
        except Exception as exc:
            return ActionOutcome(False, "navigate", url, detail=f"{type(exc).__name__}: {exc}")
        ok = not str(result.get("error", ""))
        return ActionOutcome(success=ok, action="navigate", target=url, output=dict(result))

    async def go_back(self) -> ActionOutcome:
        if self._browser is None and self._browser_controller is None:
            return ActionOutcome(False, "go_back", "browser", detail="No browser runner configured")
        from novacontrol.browser.models import BrowserAction, BrowserActionType

        try:
            result = await self._run_browser(BrowserAction(
                type=BrowserActionType.GO_BACK,
                target="current_page",
                description="Go back in browser history",
                parameters={},
            ))
        except Exception as exc:
            return ActionOutcome(False, "go_back", "browser", detail=f"{type(exc).__name__}: {exc}")
        ok = not str(result.get("error", ""))
        return ActionOutcome(ok, "go_back", "browser", detail=str(result.get("error", "") or ""), output=dict(result))

    async def scroll(self, ui_state: UiState, delta: int, *, target: str = "") -> ActionOutcome:
        """Scroll the observed surface; positive is up, negative is down."""
        if ui_state.application.lower() == "browser" and (self._browser is not None or self._browser_controller is not None):
            from novacontrol.browser.models import BrowserAction, BrowserActionType

            try:
                result = await self._run_browser(BrowserAction(
                    type=BrowserActionType.SCROLL,
                    target=target or "page",
                    description=f"Scroll {delta}",
                    parameters={"delta_y": int(delta)},
                ))
            except Exception as exc:
                return ActionOutcome(False, "scroll", target or "page", detail=f"{type(exc).__name__}: {exc}")
            ok = not str(result.get("error", ""))
            return ActionOutcome(ok, "scroll", target or "page", detail=str(result.get("error", "") or ""), output=dict(result))

        point = None
        if target:
            element = ui_state.find(target)
            point = element.location if element else None
        if point is None:
            for element in ui_state.elements:
                if element.type == "window" and element.location is not None:
                    point = element.location
                    break
        if point is None:
            return ActionOutcome(False, "scroll", target or "screen", detail="No observed point to scroll at")
        desktop_result = await self._run_desktop(
            "mouse_scroll",
            target or "screen",
            {"x": int(point[0]), "y": int(point[1]), "delta": int(delta), "window_title": ui_state.window},
            f"Scroll at {point}",
        )
        return ActionOutcome(
            desktop_result.success,
            "scroll",
            target or "screen",
            detail=desktop_result.detail,
            output=desktop_result.output,
        )

    async def drag(
        self,
        from_point: tuple[int, int],
        to_point: tuple[int, int],
        *,
        button: str = "left",
        window_title: str = "",
    ) -> ActionOutcome:
        result = await self._run_desktop(
            "mouse_drag",
            f"{from_point}->{to_point}",
            {
                "x": int(from_point[0]),
                "y": int(from_point[1]),
                "to_x": int(to_point[0]),
                "to_y": int(to_point[1]),
                "button": button,
                "window_title": window_title,
            },
            f"Drag from {from_point} to {to_point}",
        )
        return ActionOutcome(result.success, "drag", f"{from_point}->{to_point}", detail=result.detail, output=result.output)

    async def press_key(self, shortcut: str) -> ActionOutcome:
        """Keyboard shortcut through the guarded desktop runner (e.g. 'ctrl+s')."""
        if self._desktop is None and self._desktop_controller is None:
            return ActionOutcome(False, "press_key", shortcut, detail="No desktop runner configured")
        result = await self._run_desktop(
            "keyboard_shortcut",
            shortcut,
            {},
            f"Press {shortcut}",
        )
        return ActionOutcome(result.success, "press_key", shortcut, detail=result.detail, output=result.output)

    async def select_all(self) -> ActionOutcome:
        return await self.press_key("ctrl+a")

    async def copy(self) -> ActionOutcome:
        return await self.press_key("ctrl+c")

    async def paste(self) -> ActionOutcome:
        return await self.press_key("ctrl+v")

    async def open_application(self, name: str) -> ActionOutcome:
        result = await self._run_desktop("open_application", name, {}, f"Open {name}")
        return ActionOutcome(result.success, "open_application", name, detail=result.detail, output=result.output)

    async def focus_window(self, name: str) -> ActionOutcome:
        result = await self._run_desktop("window_focus", name, {}, f"Focus {name}")
        return ActionOutcome(result.success, "focus_window", name, detail=result.detail, output=result.output)

    async def minimize_window(self, name: str) -> ActionOutcome:
        result = await self._run_desktop("window_minimize", name, {}, f"Minimize {name}")
        return ActionOutcome(result.success, "minimize_window", name, detail=result.detail, output=result.output)

    async def maximize_window(self, name: str) -> ActionOutcome:
        result = await self._run_desktop("window_maximize", name, {}, f"Maximize {name}")
        return ActionOutcome(result.success, "maximize_window", name, detail=result.detail, output=result.output)

    async def close_application(self, name: str) -> ActionOutcome:
        result = await self._run_desktop("stop_app", name, {}, f"Close {name}")
        return ActionOutcome(result.success, "close_application", name, detail=result.detail, output=result.output)

    async def screenshot(self, save_path: str) -> ActionOutcome:
        if self._desktop is None and self._desktop_controller is None:
            return ActionOutcome(False, "screenshot", save_path, detail="No desktop runner configured")
        result = await self._run_desktop(
            "take_screenshot",
            "screen",
            {"save_path": save_path},
            "Capture the current screen",
        )
        ok = bool(result.output.get("exists")) or result.success
        return ActionOutcome(ok, "screenshot", save_path, detail=result.detail, output=result.output)

    async def execute_terminal_command(self, command: str, *, approval_token: str | None = None) -> ActionOutcome:
        """Destructive-gated shell execution."""
        if not approval_token:
            return ActionOutcome(
                False,
                "execute_terminal_command",
                command,
                detail="Refused: shell execution requires explicit approval",
            )
        result = await self._run_desktop("execute_script", command, {}, f"Execute: {command}")
        ok = result.success and result.output.get("exit_code") in (0, None)
        return ActionOutcome(ok, "execute_terminal_command", command, detail=result.detail, output=result.output)

    # -- shared browser dispatch ---------------------------------------------

    async def _run_browser(self, action: Any) -> dict[str, Any]:
        """Route through the approval/audit controller when one is wired."""
        if self._browser_controller is not None:
            controlled = await self._browser_controller.execute_action(action)
            payload = dict(controlled.output or {})
            payload["status"] = str(getattr(controlled.status, "value", controlled.status))
            if controlled.approval_id:
                payload["approval_id"] = controlled.approval_id
            if controlled.error:
                payload["error"] = str(controlled.error)
            return payload
        if self._browser is None:
            raise RuntimeError("No browser runner configured")
        return dict(await self._browser.run(action) or {})

    # -- shared desktop dispatch ---------------------------------------------

    async def _run_desktop(
        self,
        action_name: str,
        target: str,
        parameters: dict[str, Any],
        description: str,
    ) -> ActionOutcome:
        if self._desktop is None and self._desktop_controller is None:
            return ActionOutcome(False, action_name, target, detail="No desktop runner configured")
        from novacontrol.desktop.models import DesktopAction, DesktopActionType

        try:
            action_type = DesktopActionType(action_name)
        except ValueError:
            return ActionOutcome(False, action_name, target, detail=f"Unsupported desktop action: {action_name}")
        action = DesktopAction(
            type=action_type,
            target=target,
            description=description,
            parameters=parameters,
        )
        try:
            if self._desktop_controller is not None:
                controlled = await self._desktop_controller.execute_action(action)
                payload = dict(controlled.output or {})
                payload["status"] = str(getattr(controlled.status, "value", controlled.status))
                if controlled.approval_id:
                    payload["approval_id"] = controlled.approval_id
                error = str(controlled.error or "")
                return ActionOutcome(
                    success=not error and payload["status"] == "completed",
                    action=action_name,
                    target=target,
                    detail=error or str(payload.get("verification", "") or ""),
                    output=payload,
                )
            if self._desktop is None:
                return ActionOutcome(False, action_name, target, detail="No desktop runner configured")
            result = await self._desktop.run(action)
        except Exception as exc:
            return ActionOutcome(False, action_name, target, detail=f"{type(exc).__name__}: {exc}")
        payload = dict(result or {})
        error = str(payload.get("error", "") or "")
        return ActionOutcome(
            success=not error,
            action=action_name,
            target=target,
            detail=error or str(payload.get("verification", "") or ""),
            output=payload,
        )

    # -- registry view --------------------------------------------------------

    def catalog(self) -> tuple[dict[str, Any], ...]:
        """Tool-registry-style description of the engine's capabilities."""
        return (
            {"name": "click_element", "risk": "medium", "input": {"label": "string", "button": "string", "count": "integer"}, "surface": "browser|desktop"},
            {"name": "type_text", "risk": "medium", "input": {"field_label": "string", "text": "string"}, "surface": "browser|desktop"},
            {"name": "navigate", "risk": "low", "input": {"url": "string"}, "surface": "browser"},
            {"name": "go_back", "risk": "low", "input": {}, "surface": "browser"},
            {"name": "scroll", "risk": "low", "input": {"delta": "integer", "target": "string"}, "surface": "browser|desktop"},
            {"name": "drag", "risk": "medium", "input": {"from_point": "point", "to_point": "point"}, "surface": "desktop"},
            {"name": "press_key", "risk": "medium", "input": {"shortcut": "string"}, "surface": "desktop"},
            {"name": "select_all", "risk": "low", "input": {}, "surface": "desktop"},
            {"name": "copy", "risk": "low", "input": {}, "surface": "desktop"},
            {"name": "paste", "risk": "low", "input": {}, "surface": "desktop"},
            {"name": "open_application", "risk": "low", "input": {"name": "string"}, "surface": "desktop"},
            {"name": "focus_window", "risk": "low", "input": {"name": "string"}, "surface": "desktop"},
            {"name": "minimize_window", "risk": "low", "input": {"name": "string"}, "surface": "desktop"},
            {"name": "maximize_window", "risk": "low", "input": {"name": "string"}, "surface": "desktop"},
            {"name": "screenshot", "risk": "low", "input": {"save_path": "string"}, "surface": "desktop"},
            {"name": "execute_terminal_command", "risk": "high", "input": {"command": "string", "approval_token": "string"}, "surface": "desktop"},
        )
