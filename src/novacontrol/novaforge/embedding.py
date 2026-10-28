"""NovaForge NF-13A — host an existing game window inside a Qt surface.

The question this module answers is narrow and checkable: *can this game's own
top-level window become a child of a NovaControl window, and can that be proven
from the parent relationship and geometry?* It is not a capture path, not a
renderer, and not gameplay input. A successful ``SetParent`` is only the first
fact; :meth:`EmbeddedGameHost.embed` reports ``embedded`` only after the parent,
the ``WS_CHILD`` style, the window's visibility and its rectangle inside the
host client area have all been read back — and even then it says nothing about
whether the game renders, which stays a separate observation (and capture) fact.

Design boundaries
-----------------

* **Windows-only, behind a protocol.** :class:`WindowApi` is the whole native
  surface. :class:`Win32WindowApi` is the only implementation that touches
  ``ctypes``; tests drive a fake API, so no test reparents a real window.
* **Identity is ``(pid, start time, hwnd)``.** A PID alone can be reused. The
  window's owning process must match the requested PID *and* its creation time
  must match the launch record, or the host refuses to touch it.
* **Every mutation is reversible.** The original parent, style and screen
  rectangle are saved before ``SetParent``. Any failed verification, an
  unexpected parent change, or a minimized window restores them.
* **No process termination, no input, no display-mode change.** Closing a game
  remains :class:`~novacontrol.novaforge.launcher.GameLaunchService`'s job
  (WM_CLOSE, owned instances only); this module never kills, never sends keys
  and never calls ``ChangeDisplaySettings`` or ``ShowWindow(SW_*FULLSCREEN)``.
* **Fullscreen is a decision, not a default.** A borderless window that covers
  its monitor is reported as ``borderless_fullscreen`` preflight risk; embedding
  it requires an explicit ``allow_fullscreen=True`` from the operator, because
  an exclusive/borderless presentation surface may ignore or break under
  reparenting. Nothing changes the game's configuration to make it fit.
"""

from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol

from novacontrol.novaforge.compat import (
    CAPTURE_AVAILABLE,
    CAPTURE_NOT_TESTED,
    CAPTURE_UNAVAILABLE,
    NOT_TESTED,
    CompatibilityRecord,
    Evidence,
    apply_attempt,
    apply_restore,
    classify_preflight,
    note_window_ended_embedding,
)

__all__ = [
    "EMBEDDED",
    "EMBEDDING",
    "EMBEDDING_UNSUPPORTED",
    "LAUNCHING",
    "RUNNING",
    "DETACHED",
    "CLOSED",
    "EmbedPreflight",
    "EmbeddedGameHost",
    "GameIdentity",
    "Rect",
    "Win32WindowApi",
    "WindowApi",
    "WindowApiError",
    "WindowApiUnavailable",
    "WindowInfo",
    "WindowLookup",
    "fit_rect",
    "parse_start_time",
    "window_mode",
]

#: The seven states the phase names. They are deliberately NOT folded into one
#: another: ``running`` is "the process and window exist", ``embedded`` is "the
#: window is our child and that was read back", ``embedding_unsupported`` is "we
#: looked and it cannot be hosted", ``detached`` is "it was hosted and is now
#: restored", ``closed`` is "there is no window to host any more".
LAUNCHING = "launching"
RUNNING = "running"
EMBEDDING = "embedding"
EMBEDDED = "embedded"
EMBEDDING_UNSUPPORTED = "embedding_unsupported"
DETACHED = "detached"
CLOSED = "closed"

STATE_LABELS: dict[str, str] = {
    LAUNCHING: "Launching",
    RUNNING: "Running (not embedded)",
    EMBEDDING: "Embedding…",
    EMBEDDED: "Embedded",
    EMBEDDING_UNSUPPORTED: "Embedding unsupported",
    DETACHED: "Detached (original window restored)",
    CLOSED: "Closed",
}

# -- Win32 constants used by this module only -------------------------------

GWL_STYLE = -16
GWL_EXSTYLE = -20

WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_VISIBLE = 0x10000000

WS_EX_TOOLWINDOW = 0x00000080

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040

GA_ROOT = 2
GW_OWNER = 4
MONITOR_DEFAULTTONEAREST = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

#: ``MONITORINFO`` is not exposed by ``ctypes.wintypes`` on every supported
#: Python, so the structure is declared here (the layout is winuser.h's).
class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


#: A delta this size between PowerShell's ``StartTime`` and the Win32 creation
#: time is still the same process; a reused PID differs by far more.
START_TIME_TOLERANCE_SECONDS = 2.0

#: How far outside the host client area a window edge may sit before the
#: embedding is called unverified. Two pixels absorb non-client rounding.
GEOMETRY_TOLERANCE_PX = 2


@dataclass(frozen=True, slots=True)
class Rect:
    """A rectangle in the coordinate space its caller documents."""

    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


def fit_rect(host_width: int, host_height: int, aspect: float) -> Rect:
    """Letterbox ``aspect`` inside the host's client area, centred.

    The game keeps its own aspect ratio rather than being stretched: a 16:9 game
    in a square panel gets bars on the sides, and the window's own size is what
    decides the ratio, not the panel. A degenerate host or aspect returns the
    full host rectangle rather than a zero-sized window, which Windows would
    treat as "keep the current size".
    """
    width = int(host_width)
    height = int(host_height)
    if width <= 0 or height <= 0:
        return Rect(0, 0, max(0, width), max(0, height))
    if aspect <= 0 or aspect != aspect:  # <= 0 or NaN
        return Rect(0, 0, width, height)
    if width / height > aspect:
        fitted_height = height
        fitted_width = int(round(height * aspect))
    else:
        fitted_width = width
        fitted_height = int(round(width / aspect))
    fitted_width = max(1, min(width, fitted_width))
    fitted_height = max(1, min(height, fitted_height))
    return Rect((width - fitted_width) // 2, (height - fitted_height) // 2,
                fitted_width, fitted_height)


def parse_start_time(value: str) -> float | None:
    """ISO-8601 ``StartTime`` → epoch seconds, or ``None`` when unreadable.

    PowerShell emits seven fractional digits (``…:30.5955071Z``); Python accepts
    at most six, so the excess is trimmed instead of the whole identity being
    discarded. A naive timestamp is read as UTC because that is the space the
    probe reports in.
    """
    text = str(value or "").strip()
    if not text:
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


@dataclass(frozen=True, slots=True)
class WindowInfo:
    """Everything this module reads about ONE native window."""

    handle: int
    pid: int
    title: str
    class_name: str
    style: int
    ex_style: int
    rect: Rect
    client: Rect
    visible: bool
    minimized: bool
    owner: int
    parent: int
    dpi: int

    @property
    def is_top_level(self) -> bool:
        return self.parent == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "pid": self.pid,
            "title": self.title,
            "class_name": self.class_name,
            "style": f"0x{self.style:08X}",
            "ex_style": f"0x{self.ex_style:08X}",
            "rect": self.rect.to_dict(),
            "client": self.client.to_dict(),
            "visible": self.visible,
            "minimized": self.minimized,
            "owner": self.owner,
            "parent": self.parent,
            "dpi": self.dpi,
        }


@dataclass(frozen=True, slots=True)
class WindowLookup:
    """The window chosen for a PID, and how many candidates there were."""

    handle: int
    candidates: int
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"handle": self.handle, "candidates": self.candidates,
                "reason": self.reason}


class WindowApiError(RuntimeError):
    """A native call refused or failed; the message is the reason."""


class WindowApiUnavailable(WindowApiError):
    """The platform (or its call surface) cannot answer at all."""


class WindowApi(Protocol):
    """The native surface the host is allowed to use. Nothing else is called."""

    @property
    def platform_supported(self) -> bool: ...

    def find_window(self, pid: int, title_hint: str = "") -> WindowLookup: ...

    def window(self, handle: int) -> WindowInfo: ...

    def is_window(self, handle: int) -> bool: ...

    def parent(self, handle: int) -> int: ...

    def set_parent(self, child: int, parent: int) -> None: ...

    def set_style(self, handle: int, style: int) -> int: ...

    def set_window_pos(
        self, handle: int, x: int, y: int, width: int, height: int, flags: int
    ) -> bool: ...

    def client_rect(self, handle: int) -> Rect: ...

    def client_origin(self, handle: int) -> tuple[int, int]: ...

    def screen_rect(self, handle: int) -> Rect: ...

    def monitor_rect(self, handle: int) -> Rect | None: ...

    def process_creation_time(self, pid: int) -> float | None: ...

    def focus(self, handle: int) -> bool: ...


def window_mode(info: WindowInfo, monitor: Rect | None) -> str:
    """Classify a window's presentation mode from measurable style/geometry.

    ``borderless_fullscreen`` is the risky one: a captionless popup exactly the
    size of its monitor is how both exclusive and borderless fullscreen games
    present. It is *reported* and requires explicit confirmation; it is never
    changed.
    """
    if info.minimized:
        return "minimized"
    has_caption = bool(info.style & WS_CAPTION)
    is_popup = bool(info.style & WS_POPUP)
    if monitor is not None and monitor.area > 0:
        tolerance = 8
        covers = (
            abs(info.rect.x - monitor.x) <= tolerance
            and abs(info.rect.y - monitor.y) <= tolerance
            and abs(info.rect.width - monitor.width) <= tolerance
            and abs(info.rect.height - monitor.height) <= tolerance
        )
        if covers and is_popup and not has_caption:
            return "borderless_fullscreen"
    if is_popup and not has_caption:
        return "borderless"
    return "windowed"


@dataclass(frozen=True, slots=True)
class GameIdentity:
    """The identity a caller asks the host to embed, never guessed by it."""

    pid: int
    start_time: str = ""
    window_title: str = ""
    name: str = ""
    owned: bool = True
    launch_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "start_time": self.start_time,
            "window_title": self.window_title,
            "name": self.name,
            "owned": self.owned,
            "launch_id": self.launch_id,
        }


@dataclass(frozen=True, slots=True)
class EmbedPreflight:
    """What embedding this window would mean, WITHOUT changing anything."""

    supported: bool
    reason: str
    reason_code: str
    window: WindowInfo | None = None
    window_mode: str = ""
    requires_confirmation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "window": self.window.to_dict() if self.window is not None else None,
            "window_mode": self.window_mode,
            "requires_confirmation": self.requires_confirmation,
        }


@dataclass(slots=True)
class _SavedWindow:
    parent: int
    style: int
    rect: Rect


class EmbeddedGameHost:
    """Reparent ONE game window into a host handle, and prove or undo it.

    The host is deliberately synchronous: the calls it makes are immediate
    Win32 reads/writes, and a Qt timer drives :meth:`check`/:meth:`resize` on
    the GUI thread. It owns no thread and no process; the Qt layer owns the
    timer and the launch service owns the game.
    """

    def __init__(self, api: WindowApi, *, tolerance: float = START_TIME_TOLERANCE_SECONDS) -> None:
        self.api = api
        self.tolerance = float(tolerance)
        self._state = CLOSED
        self._identity: GameIdentity | None = None
        self._window: WindowInfo | None = None
        self._saved: _SavedWindow | None = None
        self._host_handle = 0
        self._aspect = 0.0
        self._target = Rect()
        self._reason = ""
        self._reason_code = ""
        self._attempted = False
        self._verified = False
        self._checks = 0
        self._last_check_reason = ""
        self._window_mode = ""
        self._name = ""
        # NF-14: the compatibility record for the window currently under
        # consideration, and the exact mutation steps applied by the last
        # attempt (empty before any native write, so a pre-mutation refusal is
        # never described as a partial embedding).
        self._compatibility: CompatibilityRecord | None = None
        self._applied: list[str] = []
        self._recovery: dict[str, Any] = {}

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def identity(self) -> GameIdentity | None:
        return self._identity

    @property
    def host_handle(self) -> int:
        return self._host_handle

    @property
    def embedded(self) -> bool:
        return self._state == EMBEDDED

    @property
    def close_allowed(self) -> bool:
        """Only a launch THIS build started may be closed through the service."""
        return bool(self._identity is not None and self._identity.owned)

    def set_launching(self, name: str = "") -> dict[str, Any]:
        """Record that a launch was requested; nothing is embedded yet.

        A launch while a window is hosted would drop the only handle that can
        restore it, leaving the game parented to a panel that no longer tracks
        it. Refused for the same reason ``set_running`` refuses it.
        """
        if self._state in {EMBEDDED, EMBEDDING}:
            raise RuntimeError(
                "A game is embedded in this host; detach it before launching another."
            )
        self._identity = None
        self._compatibility = None
        self._window = None
        self._saved = None
        self._host_handle = 0
        self._aspect = 0.0
        self._target = Rect()
        self._applied = []
        self._recovery = {}
        self._attempted = False
        self._verified = False
        self._reason = "The launch has been requested; a window has not appeared yet."
        self._reason_code = "launch_requested"
        self._window_mode = ""
        self._state = LAUNCHING
        self._name = str(name or "")
        return self.status_payload()

    def set_running(self, record: Mapping[str, Any], *, name: str = "") -> dict[str, Any]:
        """Record a verified running instance as embeddable, not embedded.

        ``record`` is a ``GameLaunchService`` launch record (or a catalog
        ``running`` row adapted by the caller); this method only reads identity
        fields — it does not launch, verify or capture anything.

        ``owned`` defaults to FALSE: a record that does not say this process
        started the game must never become closable through the service. The
        launch path says ``owned=True`` explicitly.
        """
        identity = GameIdentity(
            pid=int(record.get("pid") or 0),
            start_time=str(record.get("start_time") or ""),
            window_title=str(record.get("window") or ""),
            name=str(name or record.get("name") or ""),
            owned=bool(record.get("owned", False)),
            launch_id=str(record.get("launch_id") or ""),
        )
        if identity.pid <= 0:
            raise ValueError("A running instance needs a process id.")
        if self._state == EMBEDDED:
            # Never silently swap the embedded window: the caller must detach.
            raise RuntimeError("A game is already embedded; detach it first.")
        self._identity = identity
        # A different process identity is a different compatibility question;
        # keeping the previous window's record here would answer it with stale
        # evidence.
        self._compatibility = None
        self._window = None
        self._saved = None
        self._host_handle = 0
        self._applied = []
        self._recovery = {}
        self._attempted = False
        self._verified = False
        self._reason = "The game is running; embedding has not been attempted."
        self._reason_code = "running"
        self._window_mode = ""
        self._name = identity.name
        self._state = RUNNING
        return self.status_payload()

    def adopt_running(
        self, running: Mapping[str, Any], *, name: str = "", owned: bool = False
    ) -> dict[str, Any]:
        """An instance that was already running (the operator's, not ours)."""
        row = dict(running)
        row["owned"] = bool(owned)
        return self.set_running(row, name=name)

    # -- compatibility (NF-14) --------------------------------------------

    def _preflight_record(
        self, identity: GameIdentity, app_id: str = ""
    ) -> tuple[EmbedPreflight, CompatibilityRecord]:
        pre = self.preflight(identity)
        return pre, classify_preflight(identity, pre, app_id=app_id)

    def probe_compatibility(
        self, identity: GameIdentity, *, app_id: str = ""
    ) -> CompatibilityRecord:
        """Classify this window from measured preflight facts; changes nothing.

        The record carries its own identity, evidence and timestamp and is kept
        on the host so ``status_payload`` reports the same answer the caller
        just produced — the panel never re-derives a class from prose.
        """
        _pre, record = self._preflight_record(identity, app_id)
        self._compatibility = record
        return record

    def note_compatibility(self, record: CompatibilityRecord) -> None:
        """Adopt a record produced elsewhere (e.g. restored from a log)."""
        self._compatibility = record

    def note_capture(self, ok: bool, *, error: str = "") -> CompatibilityRecord | None:
        """Record a MEASURED capture result for the window under consideration.

        Capture is an independent axis: this never changes the classification or
        the embedding status, and the caller must have actually captured and
        validated THIS window's frame. The argument is a result, not a
        capability — a capability read is not evidence that a frame exists, and
        on this host the read says "available" while a hosted window refuses
        PrintWindow (NF-13A).
        """
        record = self._compatibility
        if record is None:
            return None
        record.capture = CAPTURE_AVAILABLE if ok else CAPTURE_UNAVAILABLE
        record.evidence = record.evidence + (
            Evidence(
                "capture",
                "the hosted window's frame was captured and validated"
                if ok
                else f"capturing the hosted window produced no valid frame: {error or 'no frame'}",
                True,
            ),
        )
        return record

    @property
    def compatibility(self) -> CompatibilityRecord | None:
        return self._compatibility

    def refresh_window(self) -> dict[str, Any]:
        """Look again for the game's window when the process appeared first.

        A launcher can take seconds to create its real window (or create it
        after the splash). This is the bounded answer to "window appears after a
        delay": the panel's existing timer calls it while the host is
        ``running`` with no measured window — no second process manager and no
        relaunch. A window that belongs to another PID is refused.
        """
        if self._identity is None:
            return {**self.status_payload(), "window_found": False,
                    "reason": "No game has been associated with this host."}
        if self._state not in {LAUNCHING, RUNNING}:
            return {**self.status_payload(), "window_found": self._window is not None,
                    "reason": f"The host is {STATE_LABELS.get(self._state, self._state)}; "
                              "a running instance is not being searched for."}
        if self._window is not None and self.api.is_window(self._window.handle):
            # A handle is reusable: a cached HWND that now belongs to another
            # process must not be reported as the game's window. Re-read it and
            # require the recorded pid before trusting the cache.
            try:
                cached = self.api.window(self._window.handle)
            except WindowApiError:
                cached = None
            if cached is not None and cached.pid == self._identity.pid:
                self._window = cached
                return {**self.status_payload(), "window_found": True, "reason": ""}
        lookup = self.api.find_window(self._identity.pid, self._identity.window_title)
        if lookup.handle <= 0:
            return {
                **self.status_payload(),
                "window_found": False,
                "reason": lookup.reason
                or f"No visible top-level window belongs to pid {self._identity.pid} yet.",
            }
        try:
            info = self.api.window(lookup.handle)
        except WindowApiError as exc:
            return {**self.status_payload(), "window_found": False,
                    "reason": f"The window disappeared while it was being read: {exc}"}
        if info.pid != self._identity.pid:
            return {
                **self.status_payload(),
                "window_found": False,
                "reason": f"Window {info.handle} belongs to pid {info.pid}, not the "
                          f"requested pid {self._identity.pid}; it was not adopted.",
            }
        creation = self.api.process_creation_time(self._identity.pid)
        expected = parse_start_time(self._identity.start_time)
        if creation is not None and expected is not None and abs(creation - expected) > self.tolerance:
            self._state = CLOSED
            self._verified = False
            self._window = None
            self._reason = "The process identity changed; the new window is not the same process."
            self._reason_code = "process_identity_changed"
            return {**self.status_payload(), "window_found": False}
        self._window = info
        self._window_mode = window_mode(info, self.api.monitor_rect(info.handle))
        self._state = RUNNING
        self._reason = (
            "The game's window appeared after the launch and can be embedded now."
        )
        self._reason_code = "window_detected"
        return {**self.status_payload(), "window_found": True}

    # -- preflight ---------------------------------------------------------

    def preflight(self, identity: GameIdentity) -> EmbedPreflight:
        """Can this identity be embedded? Reads only; changes nothing."""
        if not self.api.platform_supported:
            return EmbedPreflight(
                supported=False,
                reason=(
                    "Native window embedding is implemented for Windows only. This host "
                    "cannot host a game window inside NovaControl."
                ),
                reason_code="platform_unsupported",
            )
        if identity.pid <= 0:
            return EmbedPreflight(
                supported=False,
                reason="A running process id is required; none was supplied.",
                reason_code="no_process",
            )
        lookup = self.api.find_window(identity.pid, identity.window_title)
        if lookup.handle <= 0:
            return EmbedPreflight(
                supported=False,
                reason=lookup.reason
                or f"No visible top-level window belongs to pid {identity.pid}.",
                reason_code="window_not_found",
            )
        try:
            info = self.api.window(lookup.handle)
        except WindowApiError as exc:
            return EmbedPreflight(
                supported=False,
                reason=f"The window disappeared before it could be embedded: {exc}",
                reason_code="window_not_found",
            )
        if info.pid != identity.pid:
            return EmbedPreflight(
                supported=False,
                reason=(
                    f"Window {lookup.handle} belongs to pid {info.pid}, not the requested "
                    f"pid {identity.pid}; nothing was touched."
                ),
                reason_code="window_owner_mismatch",
                window=info,
            )
        identity_reason = self._identity_issue(identity.pid, identity.start_time)
        if identity_reason:
            return EmbedPreflight(
                supported=False,
                reason=identity_reason,
                reason_code="process_identity_mismatch",
                window=info,
            )
        if info.minimized:
            return EmbedPreflight(
                supported=False,
                reason=(
                    "The game window is minimized. Restore it to the foreground before "
                    "embedding; a minimized window has no usable client area."
                ),
                reason_code="window_minimized",
                window=info,
                window_mode="minimized",
            )
        if not info.visible:
            return EmbedPreflight(
                supported=False,
                reason="The game window is not visible, so there is nothing to embed.",
                reason_code="window_hidden",
                window=info,
            )
        if info.style & WS_CHILD or info.parent:
            return EmbedPreflight(
                supported=False,
                reason=(
                    "The window is already a child of another window; embedding a window "
                    "that is already hosted is refused rather than re-parented twice."
                ),
                reason_code="already_child",
                window=info,
            )
        if info.rect.area == 0:
            return EmbedPreflight(
                supported=False,
                reason="The game window reports no area, so it cannot be sized into the panel.",
                reason_code="zero_geometry",
                window=info,
            )
        mode = window_mode(info, self.api.monitor_rect(info.handle))
        if mode == "borderless_fullscreen":
            # Measured live on NF-13A: SetParent on a borderless incoming Unity
            # window is refused AND the game process exits. Detection therefore
            # ends the attempt; there is no "try anyway", because the observed
            # cost of trying is the game, not just a failed call.
            return EmbedPreflight(
                supported=False,
                reason=(
                    "The window is a borderless/fullscreen presentation surface that "
                    "covers its monitor. Reparenting these windows is refused: on this "
                    "host the attempt was rejected by Windows and the game exited. Set "
                    "the game to Windowed mode in ITS OWN settings if you want a "
                    "reliable host; NovaControl does not change the game's display mode "
                    "or configuration."
                ),
                reason_code="fullscreen_presentation_unsupported",
                window=info,
                window_mode=mode,
            )
        return EmbedPreflight(
            supported=True,
            reason="The window can be embedded.",
            reason_code="ok",
            window=info,
            window_mode=mode,
        )

    # -- embed -------------------------------------------------------------

    def embed(self, identity: GameIdentity, host_handle: int) -> dict[str, Any]:
        """Reparent the game window under ``host_handle`` and verify the result.

        Returns the host's status payload with an ``embedded`` boolean. A failure
        never raises: it restores the original window and reports
        ``embedding_unsupported`` with a machine-readable ``reason_code``. A
        borderless/fullscreen presentation window is refused by ``preflight``
        before any native call is made.
        """
        if self._state == EMBEDDED:
            if self._host_handle == host_handle and self._identity == identity:
                return {**self.status_payload(), "embedded": True, "already_embedded": True}
            # Refuse WITHOUT overwriting the state: the window is still hosted,
            # so turning the state into ``embedding_unsupported`` would take
            # away the only path that can detach it again.
            payload = self.status_payload()
            return {
                **payload,
                "embedded": False,
                "embedding": {
                    **payload["embedding"],
                    "reason": "A game is already embedded in this host; detach it "
                              "before embedding something else.",
                    "reason_code": "already_embedded",
                },
            }
        self._attempted = True
        self._verified = False
        self._reason = ""
        self._reason_code = ""
        self._window = None
        self._saved = None
        self._target = Rect()
        self._window_mode = ""
        self._applied = []
        self._recovery = {}
        self._identity = identity
        self._name = identity.name

        # Preflight FIRST: it is read-only on every platform and it is what
        # produces the NF-14 compatibility record, including "this host cannot
        # host anything". The host-handle check then runs only where a native
        # call could actually be made (off Windows it would blame a perfect
        # handle for a platform refusal).
        pre, compat_record = self._preflight_record(identity)
        self._compatibility = compat_record
        self._window_mode = pre.window_mode
        if pre.reason_code == "platform_unsupported":
            return self._with_attempt(
                compat_record, self._fail(pre.reason_code, pre.reason)
            )
        # The window's own refusal is the more specific answer and needs no
        # host: reporting "the panel has no handle" for a fullscreen game would
        # send the operator to fix the wrong thing.
        if not pre.supported:
            return self._with_attempt(
                compat_record, self._fail(pre.reason_code, pre.reason)
            )
        if host_handle <= 0 or not self.api.is_window(host_handle):
            return self._with_attempt(
                compat_record,
                self._fail(
                    "host_not_a_window",
                    "The game panel has no native window handle yet; nothing was embedded.",
                ),
            )
        assert pre.window is not None
        info = pre.window
        self._window = info
        # The aspect that must be preserved is the CONTENT's, which is the
        # client rect: after the frame is cleared the whole window is content,
        # so sizing by the framed window rect (caption + borders included)
        # stretches the game by the frame's share. Measured on the probe: a
        # 1800x1000 window holding 1768x922 of content is 1.80 vs 1.92 — a 6%
        # vertical stretch if the window rect is used. A frameless window has
        # client == rect, so this is a no-op for borderless presentation.
        self._aspect = (
            info.client.width / info.client.height
            if info.client.height
            else (info.rect.width / info.rect.height if info.rect.height else 0.0)
        )
        self._saved = _SavedWindow(parent=info.parent, style=info.style, rect=info.rect)
        self._host_handle = int(host_handle)

        # NF-14: re-read the window IMMEDIATELY before the first native write.
        # The preflight above and this line can be seconds apart (an approval, a
        # game update, a user action); touching a handle that changed owner in
        # between would modify an unrelated window. Nothing has been written, so
        # this refusal does not restore anything — running the restore would
        # itself move a window this host never touched.
        stale = self._revalidate_before_mutation(identity, info)
        if stale is not None:
            self._saved = None
            payload = self._fail("window_changed_before_embed", stale)
            self._compatibility = apply_attempt(compat_record, payload)
            return {**payload, "compatibility": self._compatibility.to_dict()}

        self._state = EMBEDDING
        self._reason = "Changing the window's parent and style…"
        self._reason_code = "embedding"

        try:
            # WS_CHILD is applied BEFORE SetParent. A window that is still
            # WS_POPUP after SetParent makes GetParent report 0, so the previous
            # order "proved" a successful reparent was a failure (and restoring
            # it in that state is what destroyed the window). Measured live.
            style = (
                info.style
                & ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_SYSMENU
                    | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)
            ) | WS_CHILD
            # Recorded BEFORE the call: an API that applies the style and then
            # fails to read it back left the window modified, and the recovery
            # below must know that.
            self._applied.append("style")
            applied = self.api.set_style(info.handle, style)
            if not applied & WS_CHILD or applied & WS_POPUP:
                raise WindowApiError(
                    f"The window style could not be changed to a child window "
                    f"(read back 0x{applied:08X})"
                )
            self._applied.append("parent")
            self.api.set_parent(info.handle, host_handle)
            if self.api.parent(info.handle) != host_handle:
                raise WindowApiError(
                    f"SetParent did not take effect (parent is "
                    f"{self.api.parent(info.handle)}, expected {host_handle})"
                )
            host_client = self.api.client_rect(host_handle)
            target = fit_rect(host_client.width, host_client.height, self._aspect)
            self._applied.append("placement")
            self._place(info.handle, target)
            self._window = self.api.window(info.handle)
            problem = self._verify_embedded(info.handle, host_handle, target)
            if problem is not None:
                raise WindowApiError(problem)
        except Exception as exc:  # noqa: BLE001 - a refused embed is a result
            applied_steps = list(self._applied)
            # A window (or its process) that died while the mutation ran is not
            # a compatibility verdict: there is nothing left to restore, and the
            # game never showed that it refuses to be hosted. Reporting
            # UNSUPPORTED here would turn a crash into a permanent "this game
            # cannot embed" fact.
            dead = not self.api.is_window(info.handle)
            restored = False if dead else self.restore()
            self._saved = None
            self._recovery = {
                "attempted": bool(applied_steps),
                "applied": applied_steps,
                "restored": None if dead else restored,
                "verified": None if dead else bool(restored),
            }
            payload = self._fail(
                "window_destroyed" if dead else "embedding_failed",
                (
                    "The game window disappeared while embedding; the mutation "
                    "stopped and there is no window left to restore. The game "
                    "may have exited or recreated its window."
                    if dead
                    else f"Embedding failed and the original window was "
                    f"{'restored' if restored else 'NOT restored'}: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
            self._compatibility = apply_attempt(compat_record, payload)
            return {**payload, "compatibility": self._compatibility.to_dict()}

        self._state = EMBEDDED
        self._verified = True
        self._target = target
        self._reason = (
            f"Parent verified as {host_handle}; WS_CHILD applied; window "
            f"{target.width}x{target.height} inside the {host_client.width}x"
            f"{host_client.height} client area."
        )
        self._reason_code = "embedded"
        self._compatibility = apply_attempt(
            compat_record,
            {
                "embedded": True,
                "embedding": {"reason_code": "embedded", "reason": self._reason},
            },
        )
        return {**self.status_payload(), "embedded": True, "partial": False}

    def _revalidate_before_mutation(
        self, identity: GameIdentity, info: WindowInfo
    ) -> str | None:
        """Re-read the window right before the first native write.

        Returns a refusal reason, or ``None`` when the same process still owns
        the same, unchanged, top-level window. This is the NF-14 requirement to
        validate the HWND and process identity *immediately* before native
        operations rather than trusting a preflight taken seconds earlier.
        """
        if not self.api.is_window(info.handle):
            return (
                "The game window disappeared between the preflight and the first "
                "native call; nothing was changed."
            )
        try:
            fresh = self.api.window(info.handle)
        except WindowApiError as exc:
            return (
                "The game window could not be read immediately before embedding: "
                f"{exc}; nothing was changed."
            )
        if fresh.pid != identity.pid:
            return (
                f"Window {fresh.handle} now belongs to pid {fresh.pid}, not the "
                f"requested pid {identity.pid}; it was not touched."
            )
        if fresh.parent != info.parent or (fresh.style & WS_CHILD):
            return (
                "The window's parent or style changed after the preflight; embedding "
                "was refused rather than re-parenting an unexpected window."
            )
        creation = self.api.process_creation_time(identity.pid)
        expected = parse_start_time(identity.start_time)
        if creation is not None and expected is not None and abs(creation - expected) > self.tolerance:
            return (
                "The process identity changed between the preflight and the first "
                "native call; embedding was refused rather than touching a reused PID."
            )
        return None

    def _place(self, handle: int, target: Rect) -> None:
        """Move/size the (now child) window inside its host client area."""
        # SWP_FRAMECHANGED: the style was changed moments ago, so the non-client
        # area is recalculated with the placement instead of keeping a frame the
        # window no longer has.
        if not self.api.set_window_pos(
            handle, target.x, target.y, target.width, target.height,
            SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        ):
            raise WindowApiError("SetWindowPos refused the client-area geometry")
        applied = self.api.window(handle)
        if applied.rect.width <= 0 or applied.rect.height <= 0:
            raise WindowApiError(
                f"the window reports no size after placement ({applied.rect.to_dict()})"
            )

    def _verify_embedded(self, handle: int, host_handle: int, target: Rect) -> str | None:
        """Return a refusal reason, or ``None`` when the host relationship holds."""
        parent = self.api.parent(handle)
        if parent != host_handle:
            return f"the window's parent is {parent}, not {host_handle}"
        info = self.api.window(handle)
        if info.pid <= 0:
            return "the window has no owning process"
        if not info.visible:
            return "the window is not visible after embedding"
        if not info.style & WS_CHILD:
            return "the window does not carry WS_CHILD after embedding"
        host_client = self.api.client_rect(host_handle)
        origin = self.api.client_origin(host_handle)
        screen = self.api.screen_rect(handle)
        tolerance = GEOMETRY_TOLERANCE_PX
        inside = (
            screen.x >= origin[0] - tolerance
            and screen.y >= origin[1] - tolerance
            and screen.right <= origin[0] + host_client.width + tolerance
            and screen.bottom <= origin[1] + host_client.height + tolerance
        )
        if not inside:
            return (
                f"the window {screen.to_dict()} is outside the host client area "
                f"{host_client.to_dict()} at {origin}"
            )
        if (screen.width, screen.height) != (target.width, target.height):
            return (
                f"the window is {screen.width}x{screen.height}, not the requested "
                f"{target.width}x{target.height}"
            )
        return None

    def _identity_issue(self, pid: int, recorded_start: str) -> str:
        """The reason this PID is not the process the caller recorded, if any."""
        if not recorded_start:
            return (
                "The launch record carries no process start time, so PID reuse cannot be "
                "ruled out; embedding is refused rather than guessing it is the same game."
            )
        expected = parse_start_time(recorded_start)
        if expected is None:
            return (
                f"The launch record's start time {recorded_start!r} could not be read, so "
                "the process identity cannot be verified."
            )
        actual = self.api.process_creation_time(pid)
        if actual is None:
            return (
                f"The creation time of pid {pid} could not be read, so PID reuse cannot "
                "be ruled out; embedding is refused rather than guessed."
            )
        if abs(actual - expected) > self.tolerance:
            return (
                f"pid {pid} was created at {datetime.fromtimestamp(actual, UTC).isoformat()}"
                f" but the launch record says {recorded_start}; this is a reused PID, not "
                "the game that was launched."
            )
        return ""

    # -- resize / check / detach ------------------------------------------

    def resize(self) -> dict[str, Any]:
        """Fit the embedded window into the host's current client area."""
        if self._state != EMBEDDED or self._identity is None or not self._host_handle:
            return {**self.status_payload(), "resized": False}
        handle = self._window_handle()
        if handle <= 0 or not self.api.is_window(handle):
            return {**self.status_payload(), "resized": False,
                    "reason": "The embedded window no longer exists."}
        if not self._host_on_screen():
            # A hidden tab (or a minimized dashboard) hides the game WITH its
            # parent. Sizing and verifying then would read the child as hidden
            # and "fail" a resize that never had a problem; defer it and let
            # the surface's own show/resize event apply it when it is visible.
            return {**self.status_payload(), "resized": False,
                    "deferred": True,
                    "reason": "The NovaControl surface is not on screen (hidden tab or "
                              "minimized dashboard); the resize was deferred."}
        try:
            host_client = self.api.client_rect(self._host_handle)
        except WindowApiError as exc:
            # A surface that cannot be measured must not be sized from a guessed
            # rectangle: report the refusal and leave the window as it is.
            return {**self.status_payload(), "resized": False,
                    "reason": f"The NovaControl surface could not be measured: {exc}"}
        target = fit_rect(host_client.width, host_client.height, self._aspect)
        try:
            self._place(handle, target)
            problem = self._verify_embedded(handle, self._host_handle, target)
        except Exception as exc:  # noqa: BLE001 - a refused resize is a result
            # Put the child back at the last VERIFIED geometry before reporting:
            # a half-applied size is a window state the operator did not ask for.
            rollback = self._target.area > 0
            if rollback:
                try:
                    self._place(handle, self._target)
                except Exception:  # noqa: BLE001 - the last geometry is gone too
                    rollback = False
            if not rollback:
                restored = self.restore()
                self._state = DETACHED if restored else EMBEDDING_UNSUPPORTED
                self._verified = False
                self._window = None
                self._saved = None
                self._reason = (
                    f"Resize failed ({type(exc).__name__}: {exc}) and the previous "
                    f"geometry could not be re-applied; the original window was "
                    f"{'restored' if restored else 'NOT restored'}."
                )
                self._reason_code = "resize_failed"
                if self._compatibility is not None:
                    self._compatibility = apply_restore(
                        self._compatibility, {"restored": restored}
                    )
                return {**self.status_payload(), "resized": False, "restored": restored}
            return {
                **self.status_payload(),
                "resized": False,
                "reason": f"Resize failed and the last verified geometry was kept: "
                          f"{type(exc).__name__}: {exc}",
            }
        if problem is not None:
            restored = self.restore()
            self._state = DETACHED if restored else EMBEDDING_UNSUPPORTED
            self._verified = False
            self._window = None
            self._saved = None
            self._reason = (
                f"Resize verification failed ({problem}); the original window was "
                f"{'restored' if restored else 'NOT restored'}."
            )
            self._reason_code = "resize_failed"
            if self._compatibility is not None:
                self._compatibility = apply_restore(
                    self._compatibility, {"restored": restored}
                )
            return {**self.status_payload(), "resized": False, "restored": restored}
        self._target = target
        self._window = self.api.window(handle)
        return {**self.status_payload(), "resized": True, "target": target.to_dict()}

    def _window_handle(self) -> int:
        return int(self._window.handle) if self._window is not None else 0

    def check(self) -> dict[str, Any]:
        """Poll the embedded window's identity/parent; never touches focus."""
        self._checks += 1
        if self._identity is None:
            return self.status_payload()
        handle = self._window_handle()
        if handle <= 0:
            return self.status_payload()
        if not self.api.is_window(handle):
            replacement = self.api.find_window(self._identity.pid, self._identity.window_title)
            if replacement.handle > 0:
                self._note_window_limitation(
                    "window_replaced",
                    "The game destroyed its window and created another while embedded.",
                )
                self._state = DETACHED
                self._verified = False
                self._window = None
                self._saved = None
                self._reason = (
                    "The hosted window was destroyed and the game created another one; "
                    "the host is detached and will not attach the replacement automatically."
                )
                self._reason_code = "window_replaced"
                self._last_check_reason = self._reason_code
            else:
                self._state = CLOSED
                self._verified = False
                self._window = None
                self._saved = None
                self._reason = "The game window no longer exists."
                self._reason_code = "process_exited"
                self._last_check_reason = self._reason_code
            return self.status_payload()
        try:
            info = self.api.window(handle)
        except WindowApiError:
            return self.status_payload()
        self._window = info
        if self._state == EMBEDDED:
            if info.minimized:
                self._note_window_limitation(
                    "window_minimized",
                    "The game minimized its window while embedded; the host detached "
                    "and the window was returned to the desktop.",
                )
                restored = self.restore()
                return self._detached_reason(
                    "window_minimized",
                    "The game minimized itself while embedded; the host detached and "
                    f"the original window was {'restored' if restored else 'NOT restored'}.",
                    restored=restored,
                )
            if self._host_handle and not self.api.is_window(self._host_handle):
                restored = self.restore()
                return self._detached_reason(
                    "host_destroyed",
                    "The NovaControl game surface was destroyed; the game window was "
                    f"{'returned to the desktop' if restored else 'not restorable from here'}.",
                    restored=restored,
                )
            if info.parent != self._host_handle:
                self._saved = None
                self._note_window_limitation(
                    "window_left_host",
                    "The game re-parented its window out of the host while embedded.",
                )
                return self._detached_reason(
                    "window_left_host",
                    "The window left the embed host (the game re-parented or recreated it); "
                    "restore is no longer possible from here.",
                )
            if not info.style & WS_CHILD:
                # A game that re-applies its own style while the parent still
                # points at the host is a partial reversal of the embedding: the
                # hierarchy looks right and the window is no longer a child.
                self._note_window_limitation(
                    "window_style_reverted",
                    "The game restored a non-child window style while embedded.",
                )
                restored = self.restore()
                return self._detached_reason(
                    "window_style_reverted",
                    "The game reversed the child window style; the host detached and "
                    f"the original window was {'restored' if restored else 'NOT restored'}.",
                    restored=restored,
                )
            if not info.visible:
                if not self._host_on_screen():
                    # The host itself is off screen (a dashboard tab switch, or
                    # the whole dashboard minimized): the game is hidden WITH
                    # it. Detaching here would undo a perfectly good embedding
                    # every time the operator looked at another tab.
                    self._last_check_reason = "host_hidden"
                    return self.status_payload()
                self._note_window_limitation(
                    "window_hidden", "The game hid its window while embedded."
                )
                return self._detached_reason(
                    "window_hidden", "The embedded window became hidden; embedding is lost."
                )
            creation = self.api.process_creation_time(self._identity.pid)
            if creation is not None and parse_start_time(self._identity.start_time):
                expected = parse_start_time(self._identity.start_time)
                assert expected is not None
                if abs(creation - expected) > self.tolerance:
                    self._state = CLOSED
                    self._verified = False
                    self._window = None
                    self._saved = None
                    self._reason = "The process identity changed; the embedded window is not ours."
                    self._reason_code = "process_identity_changed"
            return self.status_payload()
        return self.status_payload()

    def _host_on_screen(self) -> bool:
        """Whether the surface that parents the game is itself visible.

        A child window is only visible while every ancestor is, so this is what
        separates "the game hid itself" from "the operator switched tabs".
        """
        if not self._host_handle:
            return False
        try:
            return bool(self.api.window(self._host_handle).visible)
        except WindowApiError:
            return False

    def _note_window_limitation(self, code: str, detail: str) -> None:
        """Record that the WINDOW undid a verified embedding, not the host."""
        if self._compatibility is not None:
            self._compatibility = note_window_ended_embedding(
                self._compatibility, code, detail
            )

    def _detached_reason(
        self, code: str, reason: str, *, restored: bool | None = None
    ) -> dict[str, Any]:
        if restored is not None and self._compatibility is not None:
            self._compatibility = apply_restore(
                self._compatibility, {"restored": restored}
            )
        self._state = DETACHED
        self._verified = False
        self._host_handle = 0
        self._reason = reason
        self._reason_code = code
        self._last_check_reason = code
        return self.status_payload()

    def detach(self) -> dict[str, Any]:
        """Restore the window's original parent, style and screen rectangle."""
        if self._identity is None:
            return {**self.status_payload(), "detached": False,
                    "reason": "No game has been associated with this host."}
        # A DETACHED host can still hold the saved original state: that is a
        # restore that FAILED, and the operator (or shutdown cleanup) must be
        # able to retry it instead of losing the only copy of the geometry.
        retryable = self._state == DETACHED and self._saved is not None
        if self._state not in {EMBEDDED, EMBEDDING, EMBEDDING_UNSUPPORTED} and not retryable:
            return {**self.status_payload(), "detached": False,
                    "reason": f"The host is {STATE_LABELS.get(self._state, self._state)}; "
                              "there is no embedding to undo."}
        if self._saved is None:
            # A refused embed (fullscreen, no window, wrong PID) never changed
            # the window, so claiming a restore here would be a false success.
            return {**self.status_payload(), "detached": False,
                    "reason": "Nothing was changed on this window, so there is nothing "
                              "to restore."}
        handle = self._window_handle()
        exists = handle > 0 and self.api.is_window(handle)
        restored = self.restore() if exists else False
        if self._compatibility is not None:
            # There is nothing to restore when the window is gone: passing
            # ``False`` here would turn a dead window into "restore failed".
            self._compatibility = apply_restore(
                self._compatibility, {"restored": restored if exists else None}
            )
        if not exists:
            self._state = CLOSED
            self._verified = False
            self._window = None
            self._saved = None
            self._reason = "The game window no longer exists; there was nothing to restore."
            self._reason_code = "window_destroyed"
            return {**self.status_payload(), "detached": False, "restored": None}
        self._state = DETACHED
        self._verified = False
        if restored:
            self._window = None
            self._saved = None
        # A failed restore keeps the handle and the saved state: clearing them
        # here would throw away the only path that can still put the window back.
        self._host_handle = 0
        self._reason = (
            "The game window was returned to the desktop with its original style and position."
            if restored
            else "The original window could not be fully restored; see the reason."
        )
        self._reason_code = "detached" if restored else "restore_failed"
        return {**self.status_payload(), "detached": True, "restored": restored}

    def cleanup(self) -> dict[str, Any]:
        """Idempotent teardown, safe to call on every close and every failure.

        NF-14 requires cleanup to be repeatable: the dashboard may close while a
        detach is already in flight, and a refusal path may clean up after a
        failed attempt. Calling this twice changes nothing the second time and
        never reports a restore it did not perform.
        """
        if self._saved is not None and self._state in {
            EMBEDDED, EMBEDDING, EMBEDDING_UNSUPPORTED, DETACHED
        }:
            result = dict(self.detach())
            result["cleaned"] = True
            result["changed"] = bool(result.get("detached"))
            return result
        return {
            **self.status_payload(),
            "cleaned": True,
            "changed": False,
            "detached": self._state == DETACHED,
            # No restore happened in THIS call, even when an earlier detach did
            # one; reporting True here would claim work the call did not do.
            "restored": None,
            "reason": "Nothing needed cleanup; this host has not modified the window.",
        }

    def fallback(self) -> dict[str, Any]:
        """What to do instead when native embedding is unavailable.

        NF-14 Scope D: stop the failed attempt, keep the game accessible, and
        offer the EXISTING normal-launch workflow. This is a report, not an
        action — the host still never launches, moves or kills anything.
        """
        embedded = self._state == EMBEDDED
        # No game associated, no fallback: at rest the panel must not claim that
        # native embedding "is not available for this window" when there is no
        # window and nothing was attempted.
        offer = self._identity is not None and self._state in {
            RUNNING, DETACHED, EMBEDDING_UNSUPPORTED, CLOSED, LAUNCHING
        }
        if embedded:
            return {
                "active": False,
                "native_embedding": True,
                "workflow": "native_embedding",
                "reason": "The window is embedded natively; no fallback is in use.",
                "next_steps": [],
            }
        steps = [
            "Use the game's own window on the desktop; it stays a normal application.",
            "Launch or reopen it through the NovaForge Game Library (NF-12) if it is not running.",
            "Switch the game to Windowed mode in its own settings if you want to embed it.",
        ]
        if self._identity is not None and self._window is not None and self._reason_code:
            steps.insert(1, f"Embedding status: {self._reason}")
        return {
            "active": bool(offer),
            "native_embedding": False,
            "workflow": "desktop",
            "reason": self._reason
            or (
                "Native embedding is not available for this window."
                if offer
                else "No game has been associated with this host yet."
            ),
            "next_steps": steps if offer else [],
        }

    def restore(self) -> bool:
        """Best-effort restoration used by detach, failures and minimization.

        Never raises: a window that died mid-restore is reported by the caller's
        next check rather than crashing the GUI thread.
        """
        saved = self._saved
        handle = self._window_handle()
        if saved is None or handle <= 0:
            return False
        try:
            if not self.api.is_window(handle):
                return False
            if self.api.parent(handle) != saved.parent:
                self.api.set_parent(handle, saved.parent)
            self.api.set_style(handle, saved.style)
            self.api.set_window_pos(
                handle, saved.rect.x, saved.rect.y, saved.rect.width, saved.rect.height,
                SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
            )
            info = self.api.window(handle)
            return info.parent == saved.parent and info.style == saved.style
        except Exception:  # noqa: BLE001 - restoration must not raise into the UI
            return False

    def focus_game(self) -> dict[str, Any]:
        """Give the game keyboard focus. Called only from an explicit action."""
        if self._state != EMBEDDED:
            return {**self.status_payload(), "focused": False,
                    "reason": "No game is embedded."}
        handle = self._window_handle()
        if handle <= 0 or not self.api.is_window(handle):
            return {**self.status_payload(), "focused": False,
                    "reason": "The embedded window no longer exists."}
        try:
            focused = bool(self.api.focus(handle))
        except Exception as exc:  # noqa: BLE001 - a refused focus is a result
            return {**self.status_payload(), "focused": False,
                    "reason": f"Focus failed: {type(exc).__name__}: {exc}"}
        return {
            **self.status_payload(),
            "focused": focused,
            "reason": "The game received keyboard focus from an explicit request."
            if focused
            else "Windows refused the focus request; click the game surface instead.",
        }

    def mark_closed(self, reason: str = "") -> dict[str, Any]:
        """Forget the window after the launch service confirmed the game exited."""
        self._state = CLOSED
        self._verified = False
        self._window = None
        self._saved = None
        self._host_handle = 0
        self._reason = reason or "The game is closed."
        self._reason_code = "closed"
        return self.status_payload()

    def _fail(self, code: str, reason: str) -> dict[str, Any]:
        self._state = EMBEDDING_UNSUPPORTED
        self._verified = False
        self._reason_code = code
        self._reason = reason
        return {**self.status_payload(), "embedded": False}

    def _with_attempt(
        self, record: CompatibilityRecord, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Fold this result into the compatibility record and return the payload."""
        self._compatibility = apply_attempt(record, payload)
        return {**payload, "compatibility": self._compatibility.to_dict()}

    # -- reporting ---------------------------------------------------------

    def status_payload(
        self, observation: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Embedding facts, plus an observation block that is never inferred.

        ``observation`` is the caller's capture/observation capability read.
        It is copied under its own key with ``implied_by_embedding: False`` so no
        renderer can turn a successful embed into a capture claim.
        """
        payload: dict[str, Any] = {
            "state": self._state,
            "state_label": STATE_LABELS.get(self._state, self._state),
            "name": self._name,
            "identity": self._identity.to_dict() if self._identity is not None else None,
            "owned": bool(self._identity is not None and self._identity.owned),
            "close_allowed": self.close_allowed,
            "host_handle": self._host_handle,
            "window": self._window.to_dict() if self._window is not None else None,
            "window_mode": self._window_mode,
            "embedding": {
                "attempted": self._attempted,
                "verified": self._verified,
                "reason": self._reason,
                "reason_code": self._reason_code,
                "target": self._target.to_dict() if self._target.area else None,
                "aspect": round(self._aspect, 6) if self._aspect else None,
                "checks": self._checks,
                "last_check_reason": self._last_check_reason,
                # NF-14 recovery facts: which native steps were applied, and
                # whether the original window was confirmed back afterwards.
                "partial": bool(self._applied) and self._state != EMBEDDED,
                "applied": list(self._applied),
                # Always present, so a reader never has to guess whether a
                # missing key means "no recovery" or "old payload".
                "recovery": dict(self._recovery)
                or {"attempted": False, "applied": [], "restored": None,
                    "verified": None},
            },
            "compatibility": (
                self._compatibility.to_dict()
                if self._compatibility is not None
                else {
                    "classification": str(NOT_TESTED),
                    "capture": CAPTURE_NOT_TESTED,
                    "embedding": "not_attempted",
                    "evidence": [],
                    "limitations": [],
                }
            ),
            "fallback": self.fallback(),
        }
        if observation is not None:
            payload["observation"] = {
                **dict(observation),
                "implied_by_embedding": False,
            }
        return payload


# -- the one native implementation ------------------------------------------


def _win32_rect(rect: Any) -> Rect:
    return Rect(
        x=int(rect.left),
        y=int(rect.top),
        width=max(0, int(rect.right) - int(rect.left)),
        height=max(0, int(rect.bottom) - int(rect.top)),
    )


def _windll() -> Any:
    """``ctypes.windll``, which typeshed declares Windows-only.

    Only reached once ``platform_supported`` has confirmed Windows, so the
    attribute is there; reading it through ``getattr`` keeps the Linux mypy
    view (CI's native one) checking this module instead of suppressing the
    whole access for every platform.
    """
    import ctypes

    return getattr(ctypes, "windll")


class Win32WindowApi:
    """``WindowApi`` over User32/Kernel32. The only ctypes in this subsystem.

    Every handle is bound as ``HWND`` (a pointer-sized value). Without those
    prototypes ctypes truncates 64-bit handles to 32 bits, which silently reads
    a different window — the reason the bindings below are explicit.
    """

    def __init__(self, *, platform: str | None = None) -> None:
        self._platform = os.name if platform is None else platform
        self._user32: Any = None
        self._kernel32: Any = None

    @property
    def platform_supported(self) -> bool:
        return self._platform == "nt"

    # -- binding -----------------------------------------------------------

    def _require(self) -> Any:
        if not self.platform_supported:
            raise WindowApiUnavailable(
                f"Native window embedding needs Windows; this host is {self._platform!r}."
            )
        if self._user32 is None:
            import ctypes

            windll = _windll()
            self._user32 = windll.user32
            self._kernel32 = windll.kernel32
            self._bind(ctypes)
        return self._user32

    def _bind(self, ctypes: Any) -> None:
        from ctypes import wintypes

        u = self._user32
        k = self._kernel32
        long_ptr = ctypes.c_ssize_t
        u.GetParent.argtypes = [wintypes.HWND]
        u.GetParent.restype = wintypes.HWND
        u.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
        u.SetParent.restype = wintypes.HWND
        u.IsWindow.argtypes = [wintypes.HWND]
        u.IsWindow.restype = wintypes.BOOL
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsIconic.restype = wintypes.BOOL
        u.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
        u.GetWindow.restype = wintypes.HWND
        u.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
        u.GetAncestor.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetWindowRect.restype = wintypes.BOOL
        u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetClientRect.restype = wintypes.BOOL
        u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        u.ClientToScreen.restype = wintypes.BOOL
        u.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        u.SetWindowPos.restype = wintypes.BOOL
        if hasattr(u, "GetWindowLongPtrW"):
            u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            u.GetWindowLongPtrW.restype = long_ptr
            u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, long_ptr]
            u.SetWindowLongPtrW.restype = long_ptr
        else:  # 32-bit Windows exports only the non-Ptr names
            u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            u.GetWindowLongW.restype = long_ptr
            u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, long_ptr]
            u.SetWindowLongW.restype = long_ptr
        if hasattr(u, "GetDpiForWindow"):
            u.GetDpiForWindow.argtypes = [wintypes.HWND]
            u.GetDpiForWindow.restype = ctypes.c_uint
        u.MonitorFromWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
        u.MonitorFromWindow.restype = wintypes.HMONITOR
        u.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MonitorInfo)]
        u.GetMonitorInfoW.restype = wintypes.BOOL
        u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        u.AttachThreadInput.restype = wintypes.BOOL
        u.SetFocus.argtypes = [wintypes.HWND]
        u.SetFocus.restype = wintypes.HWND
        u.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL
        k.GetCurrentThreadId.argtypes = []
        k.GetCurrentThreadId.restype = wintypes.DWORD
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.GetProcessTimes.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        k.GetProcessTimes.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL

    # -- helpers -----------------------------------------------------------

    def _long(self, handle: int, index: int) -> int:
        u = self._require()
        fn = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
        return int(fn(handle, index)) & 0xFFFFFFFF

    # -- reads -------------------------------------------------------------

    def is_window(self, handle: int) -> bool:
        if not self.platform_supported or handle <= 0:
            return False
        return bool(self._require().IsWindow(handle))

    def parent(self, handle: int) -> int:
        return int(self._require().GetParent(handle) or 0)

    def find_window(self, pid: int, title_hint: str = "") -> WindowLookup:
        if not self.platform_supported:
            return WindowLookup(0, 0, "Native window embedding is unavailable off Windows.")
        import ctypes
        from ctypes import wintypes

        u = self._require()
        matches: list[tuple[int, int, int, str]] = []  # (title_match, area, handle, title)
        hint = str(title_hint or "").strip().lower()

        callback_type = getattr(ctypes, "WINFUNCTYPE")(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd: Any, _lparam: Any) -> bool:
            window_pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            if int(window_pid.value) != int(pid):
                return True
            if not u.IsWindowVisible(hwnd):
                return True
            if int(u.GetAncestor(hwnd, GA_ROOT)) != int(hwnd):
                return True
            if int(u.GetWindow(hwnd, GW_OWNER) or 0):
                return True
            ex_style = self._long(int(hwnd), GWL_EXSTYLE)
            if ex_style & WS_EX_TOOLWINDOW:
                return True
            rect = wintypes.RECT()
            if not u.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            measured = _win32_rect(rect)
            if measured.width <= 0 or measured.height <= 0:
                return True
            title = self._title(int(hwnd))
            matched = 1 if hint and hint in title.lower() else 0
            matches.append((matched, measured.area, int(hwnd), title))
            return True

        u.EnumWindows(callback_type(visit), 0)
        if not matches:
            return WindowLookup(
                0, 0,
                f"No visible top-level window belongs to pid {pid}. A window that is "
                "minimized to the tray, still booting, or owned by a launcher process is "
                "not embeddable yet.",
            )
        matches.sort(key=lambda row: (-row[0], -row[1]))
        return WindowLookup(matches[0][2], len(matches))

    def _title(self, handle: int) -> str:
        import ctypes

        u = self._require()
        buffer = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(handle, buffer, len(buffer))
        return str(buffer.value)

    def _class_name(self, handle: int) -> str:
        import ctypes

        u = self._require()
        buffer = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(handle, buffer, len(buffer))
        return str(buffer.value)

    def window(self, handle: int) -> WindowInfo:
        import ctypes
        from ctypes import wintypes

        u = self._require()
        if not u.IsWindow(handle):
            raise WindowApiError(f"Window {handle} does not exist.")
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        rect = wintypes.RECT(0, 0, 0, 0)
        client = wintypes.RECT(0, 0, 0, 0)
        # A window can disappear between IsWindow and these reads. Reporting the
        # zeroed struct then would make a dead window look like a 0x0 measurement.
        if not u.GetWindowRect(handle, ctypes.byref(rect)) or not u.GetClientRect(
            handle, ctypes.byref(client)
        ):
            raise WindowApiError(
                f"Window {handle} disappeared while it was being measured; its geometry "
                "could not be read."
            )
        dpi = int(u.GetDpiForWindow(handle)) if hasattr(u, "GetDpiForWindow") else 96
        return WindowInfo(
            handle=int(handle),
            pid=int(pid.value),
            title=self._title(handle),
            class_name=self._class_name(handle),
            style=self._long(handle, GWL_STYLE),
            ex_style=self._long(handle, GWL_EXSTYLE),
            rect=_win32_rect(rect),
            client=_win32_rect(client),
            visible=bool(u.IsWindowVisible(handle)),
            minimized=bool(u.IsIconic(handle)),
            owner=int(u.GetWindow(handle, GW_OWNER) or 0),
            parent=int(u.GetParent(handle) or 0),
            dpi=max(1, dpi),
        )

    def client_rect(self, handle: int) -> Rect:
        import ctypes
        from ctypes import wintypes

        u = self._require()
        rect = wintypes.RECT(0, 0, 0, 0)
        if not u.GetClientRect(handle, ctypes.byref(rect)):
            raise WindowApiError(
                f"Window {handle} has no readable client rectangle; it cannot be measured."
            )
        return _win32_rect(rect)

    def client_origin(self, handle: int) -> tuple[int, int]:
        import ctypes
        from ctypes import wintypes

        u = self._require()
        point = wintypes.POINT(0, 0)
        if not u.ClientToScreen(handle, ctypes.byref(point)):
            raise WindowApiError(
                f"Window {handle} has no screen origin; it cannot be measured."
            )
        return int(point.x), int(point.y)

    def screen_rect(self, handle: int) -> Rect:
        import ctypes
        from ctypes import wintypes

        u = self._require()
        my_rect = wintypes.RECT(0, 0, 0, 0)
        if not u.GetWindowRect(handle, ctypes.byref(my_rect)):
            raise WindowApiError(
                f"Window {handle} has no readable screen rectangle; it cannot be measured."
            )
        return _win32_rect(my_rect)

    def monitor_rect(self, handle: int) -> Rect | None:
        import ctypes
        from ctypes import wintypes

        u = self._require()
        monitor = u.MonitorFromWindow(handle, MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return None
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if not u.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        return _win32_rect(info.rcMonitor)

    def process_creation_time(self, pid: int) -> float | None:
        if not self.platform_supported or pid <= 0:
            return None
        import ctypes
        from ctypes import wintypes

        k = self._kernel32
        handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            if not k.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(exit_time),
                ctypes.byref(kernel_time), ctypes.byref(user_time),
            ):
                return None
            ticks = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
            return ticks / 10_000_000 - 11_644_473_600.0
        finally:
            k.CloseHandle(handle)

    # -- writes ------------------------------------------------------------

    def set_parent(self, child: int, parent: int) -> None:
        u = self._require()
        u.SetParent(child, parent)

    def set_style(self, handle: int, style: int) -> int:
        u = self._require()
        fn = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
        fn(handle, GWL_STYLE, int(style) & 0xFFFFFFFF)
        return self._long(handle, GWL_STYLE)

    def set_window_pos(
        self, handle: int, x: int, y: int, width: int, height: int, flags: int
    ) -> bool:
        u = self._require()
        return bool(u.SetWindowPos(handle, 0, int(x), int(y), int(width), int(height),
                                   int(flags)))

    def focus(self, handle: int) -> bool:
        import ctypes
        from ctypes import wintypes

        u = self._require()
        k = self._kernel32
        pid = wintypes.DWORD()
        target_thread = int(u.GetWindowThreadProcessId(handle, ctypes.byref(pid)))
        current = int(k.GetCurrentThreadId())
        attached = False
        try:
            if target_thread and target_thread != current:
                attached = bool(u.AttachThreadInput(current, target_thread, True))
            return bool(u.SetFocus(handle))
        finally:
            if attached:
                u.AttachThreadInput(current, target_thread, False)
