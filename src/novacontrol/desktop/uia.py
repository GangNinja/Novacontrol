"""UI Automation input, the only keyboard-free way into a desktop control.

The DF-6 investigation established that no synthetic-keyboard path inside the
user's interactive session can guarantee containment: an event that has been
dispatched cannot be recalled, and ``SendInput`` targets whatever holds the
foreground *at that event*. UI Automation is different in kind. A pattern such
as ``ValuePattern.SetValue`` is a message to one automation element; no
keyboard event is generated at all, so there is nothing that can land in
another window. Containment here is structural, not verified after the fact.

The honest limit is coverage: custom-drawn surfaces, games, canvas applications
and some Electron views expose no writable pattern. Those must be reported as
``unsupported_capability`` — never silently handed to keyboard injection, which
is how DF-6-L1 happened.

The adapter is deliberately transport-agnostic:

* :class:`UiaBackend` is the seam. The COM backend below talks to the UI
  Automation typelib Windows ships; tests inject a simulated backend and drive
  the same decision logic without touching a desktop.
* The adapter refuses ambiguous, stale, foreign-owned, disabled or
  pattern-less elements *before* interacting, and re-resolves the element
  immediately before the operation so a rebuild of the control is caught.
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast

__all__ = [
    "ComtypesUiaBackend",
    "UiaAdapter",
    "UiaBackend",
    "UiaElement",
    "UiaOperation",
    "UiaRefusal",
    "UiaResult",
    "UiaTarget",
]


class UiaOperation(StrEnum):
    """The element-scoped operations this build supports."""

    SET_VALUE = "set_value"
    INVOKE = "invoke"
    SELECT = "select"
    TOGGLE = "toggle"


class UiaRefusal(StrEnum):
    """Why a UIA interaction was refused (never why it was silently replaced)."""

    BACKEND_UNAVAILABLE = "backend_unavailable"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    STALE_ELEMENT = "stale_element"
    AMBIGUOUS_ELEMENT = "ambiguous_element"
    OWNERSHIP_MISMATCH = "element_ownership_mismatch"
    ELEMENT_UNAVAILABLE = "element_unavailable"
    VERIFICATION_FAILED = "verification_failed"


#: Which control pattern each operation needs. The name is the documented
#: pattern, so an unsupported control is reported by name rather than guessed at.
_PATTERN_FOR: dict[UiaOperation, str] = {
    UiaOperation.SET_VALUE: "ValuePattern",
    UiaOperation.INVOKE: "InvokePattern",
    UiaOperation.SELECT: "SelectionItemPattern",
    UiaOperation.TOGGLE: "TogglePattern",
}


@dataclass(frozen=True, slots=True)
class UiaTarget:
    """The objective-bound element a UIA operation is addressed to.

    ``window_handle`` and ``window_pid`` are the D12 identity of the owning
    window; ``name``/``automation_id``/``control_type`` locate the control
    inside it. A target with no control selector is not a target — the adapter
    refuses rather than picking the first element that looks plausible.
    """

    window_handle: int
    window_pid: int
    name: str = ""
    automation_id: str = ""
    control_type: str = ""

    @property
    def has_selector(self) -> bool:
        return bool(self.name or self.automation_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_handle": self.window_handle,
            "window_pid": self.window_pid,
            "name": self.name,
            "automation_id": self.automation_id,
            "control_type": self.control_type,
        }


@dataclass(frozen=True, slots=True)
class UiaElement:
    """One resolved automation element, with the evidence needed to re-check it."""

    runtime_id: tuple[int, ...]
    name: str
    automation_id: str
    control_type: str
    pid: int
    window_handle: int
    patterns: frozenset[str]
    enabled: bool = True
    offscreen: bool = False
    #: The top-level window this element lives in. ``window_handle`` is the
    #: ELEMENT's own handle, which for a child control is its own child HWND
    #: (measured live: a WinForms TextBox reported 393522 while the window was
    #: 917746), so only this field can prove which window an element belongs to.
    #: A backend that models whole windows leaves it at 0, and then
    #: ``window_handle`` is the window identity.
    top_level_handle: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_id": list(self.runtime_id),
            "name": self.name,
            "automation_id": self.automation_id,
            "control_type": self.control_type,
            "pid": self.pid,
            "window_handle": self.window_handle,
            "top_level_handle": self.top_level_handle,
            "patterns": sorted(self.patterns),
            "enabled": self.enabled,
            "offscreen": self.offscreen,
        }


@dataclass(frozen=True, slots=True)
class UiaResult:
    """Outcome of one UIA interaction, with its containment evidence."""

    ok: bool
    operation: UiaOperation
    refusal: UiaRefusal | None = None
    detail: str = ""
    target: dict[str, Any] | None = None
    element: dict[str, Any] | None = None
    value: str | None = None
    containment: str = "uia"
    keyboard_events: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "operation": self.operation.value,
            "refusal": self.refusal.value if self.refusal is not None else "",
            "detail": self.detail,
            "target": self.target,
            "element": self.element,
            "value": self.value,
            "containment": self.containment,
            "keyboard_events": self.keyboard_events,
        }


class UiaBackend(Protocol):
    """The transport the adapter reasons over.

    Implementations are synchronous — the adapter runs them in a worker thread —
    because COM calls must not block the event loop.
    """

    def availability(self) -> tuple[bool, str]: ...

    def resolve(self, target: UiaTarget) -> tuple[UiaElement, ...]: ...

    def read_value(self, element: UiaElement) -> str | None: ...

    def set_value(self, element: UiaElement, text: str) -> None: ...

    def invoke(self, element: UiaElement) -> None: ...

    def select(self, element: UiaElement) -> None: ...

    def toggle(self, element: UiaElement) -> None: ...


class UiaAdapter:
    """Resolve, verify and drive one automation element. Never falls back.

    Every refusal is explicit: a control without the needed pattern is
    ``unsupported_capability`` and the caller must either report it or move the
    work to an isolated session. There is no code path here that reaches for
    the keyboard.
    """

    def __init__(self, backend: UiaBackend | None = None) -> None:
        self._backend = backend

    def availability(self) -> dict[str, Any]:
        """Whether a UIA transport exists on this host, and which one."""
        if self._backend is None:
            return {
                "available": False,
                "backend": "",
                "detail": (
                    "No UI Automation transport is configured, so UIA-pattern "
                    "input cannot be attempted on this host."
                ),
                "verified_live": False,
            }
        ok, detail = self._backend.availability()
        return {
            "available": ok,
            "backend": type(self._backend).__name__,
            "detail": detail,
            "verified_live": bool(getattr(self._backend, "verified_live", False)),
        }

    async def set_text(self, target: UiaTarget, text: str) -> UiaResult:
        """Set a field's value through ``ValuePattern`` and read it back."""
        return await self._operate(target, UiaOperation.SET_VALUE, text=text)

    async def invoke(self, target: UiaTarget) -> UiaResult:
        return await self._operate(target, UiaOperation.INVOKE)

    async def select(self, target: UiaTarget) -> UiaResult:
        return await self._operate(target, UiaOperation.SELECT)

    async def toggle(self, target: UiaTarget) -> UiaResult:
        return await self._operate(target, UiaOperation.TOGGLE)

    async def _operate(
        self, target: UiaTarget, operation: UiaOperation, *, text: str | None = None
    ) -> UiaResult:
        backend = self._backend
        if backend is None:
            return UiaResult(
                ok=False,
                operation=operation,
                refusal=UiaRefusal.BACKEND_UNAVAILABLE,
                detail=(
                    "No UI Automation transport is configured; no keyboard "
                    "fallback is available in this profile."
                ),
                target=target.to_dict(),
            )
        available, detail = backend.availability()
        if not available:
            return UiaResult(
                ok=False,
                operation=operation,
                refusal=UiaRefusal.BACKEND_UNAVAILABLE,
                detail=detail or "The UI Automation transport reported unavailable.",
                target=target.to_dict(),
            )
        return await asyncio.to_thread(self._perform, backend, target, operation, text)

    def _perform(
        self,
        backend: UiaBackend,
        target: UiaTarget,
        operation: UiaOperation,
        text: str | None,
    ) -> UiaResult:
        """The decision logic, run off the event loop (COM calls are blocking)."""
        if not target.has_selector:
            return UiaResult(
                ok=False,
                operation=operation,
                refusal=UiaRefusal.UNSUPPORTED_CAPABILITY,
                detail=(
                    "The target names a window but no control inside it, so "
                    "there is no element to address. Addressing the window "
                    "itself would require keyboard input, which this profile "
                    "does not do."
                ),
                target=target.to_dict(),
            )
        try:
            matches = backend.resolve(target)
        except Exception as exc:
            return UiaResult(
                ok=False,
                operation=operation,
                refusal=UiaRefusal.BACKEND_UNAVAILABLE,
                detail=f"The UI Automation transport failed: {type(exc).__name__}: {exc}",
                target=target.to_dict(),
            )
        if not matches:
            return UiaResult(
                ok=False,
                operation=operation,
                refusal=UiaRefusal.STALE_ELEMENT,
                detail=(
                    f"No element matching {target.name or target.automation_id!r} "
                    "exists in the bound window now."
                ),
                target=target.to_dict(),
            )
        if len(matches) > 1:
            return UiaResult(
                ok=False,
                operation=operation,
                refusal=UiaRefusal.AMBIGUOUS_ELEMENT,
                detail=(
                    f"{len(matches)} elements match "
                    f"{target.name or target.automation_id!r}; refusing to "
                    "choose one. The target must identify a single control."
                ),
                target=target.to_dict(),
            )
        element = matches[0]
        ownership = self._ownership_problem(target, element)
        if ownership:
            return UiaResult(
                ok=False,
                operation=operation,
                refusal=UiaRefusal.OWNERSHIP_MISMATCH,
                detail=ownership,
                target=target.to_dict(),
                element=element.to_dict(),
            )
        if not element.enabled or element.offscreen:
            return UiaResult(
                ok=False,
                operation=operation,
                refusal=UiaRefusal.ELEMENT_UNAVAILABLE,
                detail=(
                    "The element is "
                    + ("disabled" if not element.enabled else "offscreen")
                    + "; interacting with it would not be a real interaction."
                ),
                target=target.to_dict(),
                element=element.to_dict(),
            )
        pattern = _PATTERN_FOR[operation]
        if pattern not in element.patterns:
            return UiaResult(
                ok=False,
                operation=operation,
                refusal=UiaRefusal.UNSUPPORTED_CAPABILITY,
                detail=(
                    f"The control exposes {sorted(element.patterns) or 'no patterns'}, "
                    f"not {pattern}. This application type is unsupported by the "
                    "UIA profile and needs an isolated session instead."
                ),
                target=target.to_dict(),
                element=element.to_dict(),
            )
        # Re-resolve immediately before the interaction: a rebuilt control has a
        # new runtime id, and acting on the old one would address a stale element.
        try:
            fresh = backend.resolve(target)
        except Exception as exc:
            return UiaResult(
                ok=False,
                operation=operation,
                refusal=UiaRefusal.BACKEND_UNAVAILABLE,
                detail=f"The UI Automation re-check failed: {type(exc).__name__}: {exc}",
                target=target.to_dict(),
                element=element.to_dict(),
            )
        if len(fresh) != 1 or fresh[0].runtime_id != element.runtime_id:
            return UiaResult(
                ok=False,
                operation=operation,
                refusal=UiaRefusal.STALE_ELEMENT,
                detail=(
                    "The element changed between resolution and interaction; "
                    "refusing to act on a stale control."
                ),
                target=target.to_dict(),
                element=element.to_dict(),
            )
        try:
            if operation is UiaOperation.SET_VALUE:
                backend.set_value(element, text or "")
            elif operation is UiaOperation.INVOKE:
                backend.invoke(element)
            elif operation is UiaOperation.SELECT:
                backend.select(element)
            else:
                backend.toggle(element)
        except Exception as exc:
            return UiaResult(
                ok=False,
                operation=operation,
                refusal=UiaRefusal.VERIFICATION_FAILED,
                detail=f"The pattern call failed: {type(exc).__name__}: {exc}",
                target=target.to_dict(),
                element=element.to_dict(),
            )
        value: str | None = None
        if operation is UiaOperation.SET_VALUE:
            try:
                value = backend.read_value(element)
            except Exception as exc:
                return UiaResult(
                    ok=False,
                    operation=operation,
                    refusal=UiaRefusal.VERIFICATION_FAILED,
                    detail=(
                        "The value was written but could not be read back: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    target=target.to_dict(),
                    element=element.to_dict(),
                )
            if value != (text or ""):
                return UiaResult(
                    ok=False,
                    operation=operation,
                    refusal=UiaRefusal.VERIFICATION_FAILED,
                    detail="The control's value did not match the requested text after SetValue.",
                    target=target.to_dict(),
                    element=element.to_dict(),
                    value=value,
                )
        return UiaResult(
            ok=True,
            operation=operation,
            detail=f"{pattern} succeeded on {element.name or element.automation_id!r}.",
            target=target.to_dict(),
            element=element.to_dict(),
            value=value,
        )

    @staticmethod
    def _ownership_problem(target: UiaTarget, element: UiaElement) -> str:
        """Why this element does not belong to the bound window, or ``""``."""
        if element.pid != target.window_pid:
            return (
                f"The element belongs to pid {element.pid}, not the bound pid "
                f"{target.window_pid} — refusing to interact with another process."
            )
        # Compare the element's TOP-LEVEL window, never its own handle: a child
        # control's handle is a child HWND, so the previous comparison refused
        # every control inside the bound window (measured live against the
        # disposable probe: `element_hwnd 393522, expected 917746`).
        owning = element.top_level_handle or element.window_handle
        if owning != target.window_handle:
            return (
                f"The element lives in window {owning}, not the "
                f"bound window {target.window_handle}."
            )
        return ""


class ComtypesUiaBackend:
    """UI Automation through the COM typelib Windows ships (``UIAutomationCore``).

    ``verified_live`` is True: this transport resolved an element by
    AutomationId, wrote through ``ValuePattern.SetValue``, invoked through
    ``InvokePattern.Invoke`` and refused all six unsafe controls against a
    disposable WinForms probe, with zero keyboard and zero mouse events (see
    ``docs/DF6_LIVE_UIA_VERIFICATION_REPORT.md``). That is a statement about
    the transport, not about coverage of arbitrary applications: the probe's
    Panel exposed no pattern at all, which is exactly what makes custom-drawn
    surfaces and games unsupported.

    A host where the typelib or COM registration is unavailable reports
    ``available: False`` with the reason, so an unsupported host is never
    mistaken for a working one.
    """

    verified_live = True

    #: CLSID_CUIAutomation — the UI Automation client object.
    _CLSID = "{ff48dba4-60ef-4201-aa87-54103eef594e}"

    def __init__(self) -> None:
        self._uia: Any = None
        self._module: Any = None
        self._error = ""

    # -- connection -------------------------------------------------------

    def _connect(self) -> Any:
        if self._uia is not None:
            return self._uia
        if self._error:
            raise RuntimeError(self._error)
        try:
            comtypes_client = importlib.import_module("comtypes.client")
            module = comtypes_client.GetModule("UIAutomationCore.dll")
            uia = comtypes_client.CreateObject(
                comtypes_client.GUID(self._CLSID),
                interface=module.IUIAutomation,
            )
        except Exception as exc:
            self._error = (
                "UI Automation is unavailable on this host: "
                f"{type(exc).__name__}: {exc}"
            )
            raise RuntimeError(self._error) from exc
        self._module = module
        self._uia = uia
        return uia

    def availability(self) -> tuple[bool, str]:
        try:
            self._connect()
        except Exception as exc:
            return False, str(exc)
        return True, "UI Automation client created and verified live (disposable probe)."

    # -- element resolution ----------------------------------------------

    def _property_ids(self) -> tuple[int, int]:
        module = self._module
        return (
            cast(int, module.UIA_NamePropertyId),
            cast(int, module.UIA_AutomationIdPropertyId),
        )

    def _patterns(self, element: Any) -> frozenset[str]:
        module = self._module
        found: set[str] = set()
        for name, attribute in (
            ("ValuePattern", "UIA_ValuePatternId"),
            ("InvokePattern", "UIA_InvokePatternId"),
            ("SelectionItemPattern", "UIA_SelectionItemPatternId"),
            ("TogglePattern", "UIA_TogglePatternId"),
        ):
            pattern_id = getattr(module, attribute, None)
            if pattern_id is None:
                continue
            try:
                pattern = element.GetCurrentPattern(pattern_id)
            except Exception:
                continue
            # ``GetCurrentPattern`` returns S_OK with a NULL pointer when the
            # element does not support the pattern, and comtypes hands that back
            # as an empty POINTER instance. ``is not None`` therefore reported
            # EVERY pattern on EVERY element (measured live: a WinForms Panel
            # "exposed" all four, and so did a TextBox), which silently disabled
            # the unsupported-capability refusal below.
            if pattern:
                found.add(name)
        return frozenset(found)

    def _top_level_handle(self, element: Any) -> int:
        """The window handle of the top-level window this element lives in.

        Walks the raw view upward and stops at the desktop root. The desktop is
        NOT handle 0 — it is the ``#32769`` window and it reports its own handle
        (measured live: 65548), so a walk that only stopped at 0 climbed into it
        and reported the desktop as the element's window. A failure part-way
        leaves the last element that had a handle, which is no worse than before.
        """
        try:
            walker = self._uia.RawViewWalker
        except Exception:
            walker = None
        desktop = 0
        try:
            desktop = int(ctypes.windll.user32.GetDesktopWindow())
        except Exception:  # pragma: no cover - not Windows
            desktop = 0
        current = element
        for _ in range(64):
            if walker is None:
                break
            try:
                parent = walker.GetParentElement(current)
            except Exception:
                break
            if parent is None:
                break
            try:
                parent_handle = int(parent.CurrentNativeWindowHandle)
            except Exception:
                parent_handle = 0
            if not parent_handle or (desktop and parent_handle == desktop):
                break
            current = parent
        try:
            return int(current.CurrentNativeWindowHandle) or 0
        except Exception:
            return 0

    def _describe(self, element: Any) -> UiaElement:
        runtime = element.GetRuntimeId()
        runtime_id = tuple(int(part) for part in (runtime or ()))
        return UiaElement(
            runtime_id=runtime_id,
            name=str(element.CurrentName or ""),
            automation_id=str(element.CurrentAutomationId or ""),
            control_type=str(element.CurrentControlType),
            pid=int(element.CurrentProcessId),
            window_handle=int(element.CurrentNativeWindowHandle),
            top_level_handle=self._top_level_handle(element),
            patterns=self._patterns(element),
            enabled=bool(element.CurrentIsEnabled),
            offscreen=bool(element.CurrentIsOffscreen),
        )

    def resolve(self, target: UiaTarget) -> tuple[UiaElement, ...]:
        uia = self._connect()
        module = self._module
        root = uia.ElementFromHandle(target.window_handle)
        if root is None:
            return ()
        name_id, automation_id_id = self._property_ids()
        if target.automation_id:
            condition = uia.CreatePropertyCondition(automation_id_id, target.automation_id)
        else:
            condition = uia.CreatePropertyCondition(name_id, target.name)
        collection = root.FindAll(cast(int, module.TreeScope_Descendants), condition)
        if collection is None:
            return ()
        found: list[UiaElement] = []
        for index in range(int(collection.Length)):
            element = collection.GetElement(index)
            if element is None:
                continue
            described = self._describe(element)
            if target.control_type and described.control_type != target.control_type:
                continue
            found.append(described)
        return tuple(found)

    # -- patterns ---------------------------------------------------------

    def _element(self, element: UiaElement) -> Any:
        """Re-find an element by runtime id inside its own window."""
        uia = self._connect()
        # Search from the TOP-LEVEL window: ``element.window_handle`` is the
        # element's own child handle, and asking UIA for that handle returns the
        # control itself, whose descendants contain nothing (measured live:
        # every SetValue failed with "no longer present in its window").
        window = element.top_level_handle or element.window_handle
        root = uia.ElementFromHandle(window)
        if root is None:
            raise RuntimeError("The element's window no longer exists.")
        collection = root.FindAll(
            cast(int, self._module.TreeScope_Descendants), uia.CreateTrueCondition()
        )
        if collection is None:
            raise RuntimeError("The element's window lists no elements.")
        for index in range(int(collection.Length)):
            candidate = collection.GetElement(index)
            if candidate is None:
                continue
            runtime = tuple(int(part) for part in (candidate.GetRuntimeId() or ()))
            if runtime == element.runtime_id:
                return candidate
        raise RuntimeError("The element is no longer present in its window.")

    def _pattern(self, element: UiaElement, interface: str) -> Any:
        module = self._module
        pattern_id = getattr(module, f"UIA_{interface}Id")
        raw = self._element(element).GetCurrentPattern(pattern_id)
        # Truthiness, not ``is None``: an unsupported pattern comes back as a
        # NULL pointer object, which is not None (see ``_patterns``).
        if not raw:
            raise RuntimeError(f"The element no longer exposes {interface}.")
        interface_type = getattr(module, f"IUIAutomation{interface}")
        return cast(Any, raw).QueryInterface(interface_type)

    def read_value(self, element: UiaElement) -> str | None:
        return str(self._pattern(element, "ValuePattern").CurrentValue)

    def set_value(self, element: UiaElement, text: str) -> None:
        self._pattern(element, "ValuePattern").SetValue(text)

    def invoke(self, element: UiaElement) -> None:
        self._pattern(element, "InvokePattern").Invoke()

    def select(self, element: UiaElement) -> None:
        self._pattern(element, "SelectionItemPattern").Select()

    def toggle(self, element: UiaElement) -> None:
        self._pattern(element, "TogglePattern").Toggle()
