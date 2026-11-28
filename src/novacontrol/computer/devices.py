"""Live device and capability discovery for Phase 29.

Nothing here executes an action.  The registry answers, per device, what this
machine can actually do, what is connected, and what is not supported — the
three answers a computer operator must not conflate.
"""

from __future__ import annotations

import ctypes
import importlib.util
import platform as platform_module
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from novacontrol.computer.models import (
    CapabilitySupport,
    DeviceInfo,
    DeviceKind,
    DeviceReport,
)


def _windows_displays() -> tuple[dict[str, Any], ...]:
    """Monitors as Windows reports them, including the virtual desktop bounds.

    The capture worker is per-monitor-DPI-aware, so these rectangles are the
    same physical-pixel space input uses where the OS grants it.  The reader
    deliberately reports `dpi_aware` as False for THIS process: the application
    process is not the DPI-aware capture worker, and pretending otherwise would
    make a scaled coordinate look safe when it is not.
    """
    if not platform_module.system().lower().startswith("windows"):
        return ()
    try:
        user32 = ctypes.windll.user32
    except Exception:
        return ()

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", ctypes.c_ulong),
        ]

    found: list[dict[str, Any]] = []

    def _callback(handle: Any, _hdc: Any, _rect: Any, _data: Any) -> int:
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            return 1
        monitor = info.rcMonitor
        work = info.rcWork
        found.append(
            {
                "index": len(found),
                "primary": bool(info.dwFlags & 1),
                "bounds": {
                    "left": int(monitor.left),
                    "top": int(monitor.top),
                    "right": int(monitor.right),
                    "bottom": int(monitor.bottom),
                    "width": int(monitor.right - monitor.left),
                    "height": int(monitor.bottom - monitor.top),
                },
                "work_area": {
                    "left": int(work.left),
                    "top": int(work.top),
                    "right": int(work.right),
                    "bottom": int(work.bottom),
                    "width": int(work.right - work.left),
                    "height": int(work.bottom - work.top),
                },
                "dpi_aware": False,
            }
        )
        return 1

    try:
        callback = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        )(_callback)
        user32.EnumDisplayMonitors(None, None, callback, 0)
    except Exception:
        try:
            width = int(user32.GetSystemMetrics(78))
            height = int(user32.GetSystemMetrics(79))
        except Exception:
            return ()
        if width <= 0 or height <= 0:
            return ()
        found.append(
            {
                "index": 0,
                "primary": True,
                "bounds": {"left": 0, "top": 0, "right": width, "bottom": height, "width": width, "height": height},
                "work_area": {"left": 0, "top": 0, "right": width, "bottom": height, "width": width, "height": height},
                "dpi_aware": False,
            }
        )
    return tuple(found)


def probe_displays() -> tuple[dict[str, Any], ...]:
    """Public, injectable display probe.  Empty means 'this host did not say'."""
    try:
        return _windows_displays()
    except Exception:
        return ()


def _browser_package_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def _cap(name: str, available: bool, detail: str, limitations: tuple[str, ...] = ()) -> CapabilitySupport:
    return CapabilitySupport(name=name, available=available, detail=detail, limitations=limitations)


class DeviceRegistry:
    """Probes the desktop, displays, browser bridge and paired phone.

    The registry never launches a browser or connects a phone.  It reads the
    existing runners' own availability and reports the limitation when that
    availability is only "the adapter is installed".
    """

    def __init__(
        self,
        *,
        desktop_runner: Any | None = None,
        browser_runner: Any | None = None,
        phone_status: Callable[[], Any] | None = None,
        display_probe: Callable[[], tuple[dict[str, Any], ...]] | None = None,
        platform_name: str = "",
    ) -> None:
        self._desktop = desktop_runner
        self._browser = browser_runner
        self._phone_status = phone_status
        self._display_probe = display_probe or probe_displays
        self._platform = platform_name or platform_module.platform()

    async def discover(self) -> DeviceReport:
        notes: list[str] = []
        devices: list[DeviceInfo] = []

        # Desktop: capabilities are read from the runner's public seams, not
        # assumed from the operating system's name.
        window_probe = getattr(self._desktop, "window_lines", None)
        geometry = getattr(self._desktop, "window_geometry", None)
        runner_available = self._desktop is not None
        desktop_caps = (
            _cap(
                "screen_capture",
                bool(getattr(self._desktop, "capture_screen", None)),
                "approval-gated desktop capture through the persistent worker",
                ("primary display only", "captured pixels are physical-pixel coordinates"),
            ),
            _cap(
                "window_enumeration",
                callable(window_probe),
                "visible top-level windows",
                ("a probe that cannot read the OS reports unavailable, not empty",),
            ),
            _cap(
                "window_geometry",
                callable(geometry),
                "window bounds, client area and DPI scale",
                ("window must still exist when input is sent",),
            ),
            _cap(
                "mouse",
                runner_available and callable(getattr(self._desktop, "run", None)),
                "move, click, double/right click, drag and scroll through the input guard",
                ("input is validated against the target window before it is sent",),
            ),
            _cap(
                "keyboard",
                runner_available and callable(getattr(self._desktop, "run", None)),
                "typing, key combinations and release cleanup",
                ("text paste verification is Windows-specific",),
            ),
            _cap(
                "vision_click",
                callable(getattr(self._desktop, "vision_click", None))
                or runner_available,
                "locate a labeled element through OCR/vision before clicking",
                ("vision-model judgments are not treated as success",),
            ),
            _cap(
                "file_operations",
                callable(getattr(self._desktop, "run", None)),
                "list/read/write files and organize a dedicated directory",
                ("writes and moves are permission-gated",),
            ),
        )
        devices.append(
            DeviceInfo(
                id="desktop",
                kind=DeviceKind.DESKTOP,
                name="Local desktop",
                available=runner_available,
                connected=runner_available,
                capabilities=desktop_caps,
                limitations=(
                    "Actions run against the signed-in Windows session only.",
                    "This process is not the DPI-aware capture worker; coordinates are normalised by window geometry.",
                ),
                metadata={"platform": self._platform},
            )
        )

        displays = tuple(self._display_probe() or ())
        if displays:
            for index, display in enumerate(displays):
                bounds = dict(display.get("bounds", {}))
                devices.append(
                    DeviceInfo(
                        id=f"display:{index}",
                        kind=DeviceKind.DISPLAY,
                        name="Primary display" if display.get("primary") else f"Display {index + 1}",
                        available=True,
                        connected=True,
                        capabilities=(
                            _cap("observe", True, "monitor bounds and work area were read from the OS"),
                            _cap("coordinate_input", True, "global physical-pixel coordinates"),
                        ),
                        limitations=(
                            "Screen capture is wired to the primary display; a secondary display is a coordinate surface, not yet a capture surface.",
                        ),
                        metadata={
                            "bounds": bounds,
                            "work_area": display.get("work_area", {}),
                            "primary": bool(display.get("primary")),
                            "dpi_aware": bool(display.get("dpi_aware", False)),
                        },
                    )
                )
        else:
            notes.append("Display geometry was not reported by this host; multi-monitor support is unverified.")
            devices.append(
                DeviceInfo(
                    id="display:unknown",
                    kind=DeviceKind.DISPLAY,
                    name="Display geometry",
                    available=False,
                    connected=False,
                    limitations=("The OS display probe returned no monitors.",),
                )
            )

        keyboard_mouse = DeviceInfo(
            id="input:primary",
            kind=DeviceKind.KEYBOARD_MOUSE,
            name="Keyboard and mouse",
            available=runner_available,
            connected=runner_available,
            capabilities=(
                _cap("move", runner_available, "absolute pointer movement"),
                _cap("click", runner_available, "left/right/middle and repeated clicks"),
                _cap("drag", runner_available, "press, move and release as one guarded sequence"),
                _cap("scroll", runner_available, "vertical wheel at a target point"),
                _cap("type", runner_available, "typing with clipboard verification on Windows"),
                _cap("shortcut", runner_available, "key combinations with paired release"),
                _cap("release_cleanup", runner_available, "InputGuard releases held keys/buttons on stop and close"),
            ),
            limitations=("Input is global to the signed-in desktop session.",),
        )
        devices.append(keyboard_mouse)

        browser_ready = _browser_package_available()
        if self._browser is not None:
            browser_ready = browser_ready and callable(getattr(self._browser, "run", None))
        devices.append(
            DeviceInfo(
                id="browser",
                kind=DeviceKind.BROWSER,
                name="Playwright browser",
                available=browser_ready,
                connected=False,
                capabilities=(
                    _cap("navigate", browser_ready, "semantic page navigation"),
                    _cap("click", browser_ready, "selector-addressed click"),
                    _cap("form_input", browser_ready, "fill and submit fields"),
                    _cap("extract", browser_ready, "page text and interactive elements"),
                    _cap("screenshot", browser_ready, "page screenshot"),
                    _cap("scroll", browser_ready, "page wheel input"),
                    _cap("upload", browser_ready, "file chooser upload when authorised"),
                    _cap("download", browser_ready, "approved download to a requested path"),
                    _cap("tabs", browser_ready, "new, switch, list and close browser tabs"),
                    _cap("session", browser_ready, "the browser context preserves its own cookies and session"),
                ),
                limitations=(
                    "Connected means the adapter is installed, not that a browser is currently running.",
                    "Authentication, CAPTCHAs and website access controls are never bypassed.",
                    "A real browser launch is validated on first use.",
                ),
                metadata={"adapter": type(self._browser).__name__ if self._browser is not None else ""},
            )
        )

        phone_info: dict[str, Any] = {}
        if self._phone_status is not None:
            try:
                status = self._phone_status()
                phone_info = status.to_dict() if hasattr(status, "to_dict") else dict(status)
            except Exception as exc:
                phone_info = {"available": False, "state": "probe_failed", "error": f"{type(exc).__name__}: {exc}"}
            devices.append(
                DeviceInfo(
                    id="phone",
                    kind=DeviceKind.PHONE,
                    name="Connected phone",
                    available=bool(phone_info.get("available", False)),
                    connected=bool(phone_info.get("available", False)),
                    capabilities=(
                        _cap("open_application", bool(phone_info.get("available")), "ADB intent"),
                        _cap("send_text", bool(phone_info.get("available")), "SMS compose/dial intent; device user confirms"),
                        _cap("call", bool(phone_info.get("available")), "ACTION_DIAL; device user places the call"),
                        _cap("screenshot", bool(phone_info.get("available")), "adb screencap"),
                    ),
                    limitations=tuple(str(item) for item in phone_info.get("next_steps", []) or ())
                    + ("Nothing is sent or called without on-device confirmation.",),
                    metadata=phone_info,
                )
            )
        else:
            notes.append("No phone bridge probe is wired; phone support is reported unavailable.")

        return DeviceReport(
            devices=tuple(devices),
            platform=self._platform,
            probed_at=datetime.now(UTC).isoformat(),
            notes=tuple(notes),
        )

    async def report(self) -> dict[str, Any]:
        return (await self.discover()).to_dict()

    def capability(self, report: DeviceReport | Mapping[str, Any], operation: str) -> dict[str, Any]:
        """Look up an operation across a report without inventing availability."""
        if isinstance(report, DeviceReport):
            payload = report.to_dict()
        else:
            payload = dict(report)
        matches = []
        for device in payload.get("devices", []) or []:
            for capability in device.get("capabilities", []) or []:
                if capability.get("name") == operation and capability.get("available"):
                    matches.append({"device": device.get("id"), "detail": capability.get("detail", "")})
        return {"operation": operation, "available": bool(matches), "devices": matches}
