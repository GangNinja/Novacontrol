"""NF-15 — one observation pipeline for the target game.

A NovaForge *session* (NF-01) records what happened during a run. This module
answers a narrower, older question that later components depend on: **is that
really a fresh frame of the game right now?** It binds a target window, captures
on a bounded schedule through the existing capture router, validates every frame
with the existing verdict, and publishes a record that says what was measured,
what was refused and why nothing could be read.

It does not interpret the game, plan an action or send input. NF-15 delivers
reliable observations; understanding and gameplay are later milestones.

Design commitments, each because the alternative lies:

* **A capture is not an observation until validation passes.** The record status
  is ``observed`` (a validated frame), ``rejected`` (a frame was produced and
  the verdict refused it) or ``unavailable`` (no frame exists: the target is
  missing/minimized, or the capture failed/timed out). ``last_observation`` is
  ``None`` until a read was attempted, so "not tested" is a real answer.
* **Identity is pinned and re-checked.** The window handle, its process id and
  (when supplied) the process start time are stored at start. A handle that
  changes with the same process is a recreated window and is re-bound; a
  different pid under the same title is never adopted, and an exited process is
  reported as the terminal reason instead of a missing window.
* **Pausing is not resuming.** A minimized/hidden/missing target pauses
  observation. The loop keeps checking the window cheaply, and capture resumes
  only after the target is valid again. A user pause is never auto-cleared.
* **Every tick is bounded.** One loop, one capture in flight (a concurrent
  caller is answered ``busy``), a timeout on the probe and on the capture, a
  bounded history, a bounded frame set, and a maximum interval. The measured
  rate is computed from real capture starts — if capture is slower than the
  requested interval, the reported rate is what happened, not what was asked.
* **No pixel content is kept in memory.** A record carries the frame's path,
  dimensions and measurements; frames live on disk and are pruned. Rejected
  frames are deleted immediately, because they are not evidence of anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The paint side car's name and its orphan sweep belong to the capture worker
# that writes it; this module only has to keep it from outliving its frame.
from novacontrol.desktop.capture import THUMB_SUFFIX, sweep_orphan_thumbnails

__all__ = [
    "CapturedFrame",
    "DEFAULT_IDENTITY_CACHE",
    "DEFAULT_INTERVAL",
    "FRAME_STATUSES",
    "GameFrameObservation",
    "GameObserver",
    "MAX_INTERVAL",
    "MIN_INTERVAL",
    "OBSERVATION_STATES",
    "WindowState",
    "target_visibility",
    "window_state",
]

#: Session lifecycle states. ``stopped`` and ``failed`` are terminal; a new
#: ``start`` creates a new session rather than reopening a dead one.
IDLE = "idle"
OBSERVING = "observing"
PAUSED = "paused"
STOPPED = "stopped"
FAILED = "failed"
OBSERVATION_STATES = (IDLE, OBSERVING, PAUSED, STOPPED, FAILED)
_TERMINAL = (STOPPED, FAILED)

#: Per-read statuses. The first is the only one that may be shown as live.
OBSERVED = "observed"
REJECTED = "rejected"
UNAVAILABLE = "unavailable"
FRAME_STATUSES = (OBSERVED, REJECTED, UNAVAILABLE)

#: Capture refusals that describe the TARGET'S STATE, not a bad frame. A covered
#: or minimized window is not a capture failure: counting it as one made five
#: consecutive focus losses terminate an observation that should have paused.
TARGET_STATE_CODES = frozenset(
    {
        "target_minimized",
        "target_hidden",
        "target_offscreen",
        "target_not_foreground",
    }
)

#: The scheduler's bounds. 0.25 s is four captures per second — plenty for a
#: control loop and cheap enough that validation does not become the bottleneck.
MIN_INTERVAL = 0.25
MAX_INTERVAL = 5.0
DEFAULT_INTERVAL = 1.0
DEFAULT_CAPTURE_TIMEOUT = 6.0
DEFAULT_PROBE_TIMEOUT = 3.0
DEFAULT_IDENTITY_TIMEOUT = 6.0
DEFAULT_STOP_TIMEOUT = 6.0
#: A paused observer probes its target every ``poll_interval``. The process
#: identity probe spawns a PowerShell run, so its answer is cached: a window that
#: stays missing must not cost a process launch every half second.
DEFAULT_IDENTITY_CACHE = 5.0
#: Pause polling is never faster than this: a paused observer must not spin.
DEFAULT_POLL_INTERVAL = 0.5
MAX_CONSECUTIVE_FAILURES = 5
MAX_HISTORY = 120
MAX_FRAMES = 12
MAX_SESSIONS_KEPT = 3
PERSIST_INTERVAL = 5.0
#: Bounded so a status response cannot become the replay subsystem this phase
#: explicitly does not build.
STATUS_HISTORY = 20

_STATE_NAMESPACE = "novaforge_observation"


def utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _number(value: Any) -> float | None:
    """A measured non-negative number, or ``None`` when nothing measured it."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result < 0:
        return None
    return result


@dataclass(frozen=True, slots=True)
class WindowState:
    """One read of the target window: identity plus its current state.

    Identity (``handle`` + ``pid``) and state (``minimized``, ``visible``,
    ``foreground``, ``bounds``) are separate fields on purpose. H02 is the
    recorded case of a minimized fullscreen game that kept answering its handle
    and pid while every screen capture contained the desktop.
    """

    title: str
    handle: int
    pid: int
    bounds: tuple[int, int, int, int]
    client: tuple[int, int, int, int] = (0, 0, 0, 0)
    scale: float = 1.0
    foreground: bool = False
    minimized: bool = False
    visible: bool = True

    @property
    def width(self) -> int:
        return max(0, self.bounds[2] - self.bounds[0])

    @property
    def height(self) -> int:
        return max(0, self.bounds[3] - self.bounds[1])

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "handle": self.handle,
            "pid": self.pid,
            "bounds": list(self.bounds),
            "width": self.width,
            "height": self.height,
            "scale": self.scale,
            "foreground": self.foreground,
            "minimized": self.minimized,
            "visible": self.visible,
        }


def _read_field(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def window_state(geometry: Any) -> WindowState | None:
    """Adapt a geometry object or mapping without importing the desktop layer.

    ``None`` means the probe returned nothing usable (no window, or no handle).
    A window with no pid is still a window: its pid stays 0 and identity is the
    handle alone, which the record and the limitations state honestly.
    """
    if geometry is None:
        return None
    try:
        handle = int(_read_field(geometry, "handle", 0) or 0)
        pid = int(_read_field(geometry, "pid", 0) or 0)
        bounds = tuple(int(value) for value in _read_field(geometry, "bounds", ()) or ())
        client = tuple(int(value) for value in _read_field(geometry, "client", ()) or ())
    except (TypeError, ValueError):
        return None
    if handle <= 0:
        return None
    if len(bounds) != 4:
        return None
    if len(client) != 4:
        client = (0, 0, 0, 0)
    try:
        scale = float(_read_field(geometry, "scale", 1.0) or 1.0)
    except (TypeError, ValueError):
        scale = 1.0
    return WindowState(
        title=str(_read_field(geometry, "title", "") or ""),
        handle=handle,
        pid=pid,
        bounds=(bounds[0], bounds[1], bounds[2], bounds[3]),
        client=(client[0], client[1], client[2], client[3]),
        scale=scale,
        foreground=bool(_read_field(geometry, "foreground", False)),
        minimized=bool(_read_field(geometry, "minimized", False)),
        visible=bool(_read_field(geometry, "visible", True)),
    )


def target_visibility(window: WindowState) -> tuple[str, str]:
    """Why this window is not capturable right now, or ``("", "")``.

    This is the ONE implementation: the session manager delegates here, so an
    observation and a session can never disagree about a minimized target.
    """
    if window.minimized:
        return "target_minimized", "The target window is minimized."
    if not window.visible:
        return "target_hidden", "The target window is not visible."
    if window.width < 8 or window.height < 8:
        return "target_offscreen", "The target window has no usable client area."
    # The Win32 minimized-window placeholder. A worker from an older build does
    # not report ``isIconic``, so the sentinel bounds are the fallback.
    if window.bounds[0] <= -16000 or window.bounds[1] <= -16000:
        return "target_minimized", "The target window is minimized (off-screen placeholder bounds)."
    return "", ""


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    """The capture abstraction's answer for one requested frame.

    ``elapsed_ms`` and ``age_seconds`` are ``None`` when nothing measured them.
    A backend that cannot report its own latency is not credited with mine —
    the observer measures the await instead and records that separately.
    """

    ok: bool
    path: str = ""
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    backend: str = ""
    source: str = ""
    elapsed_ms: float | None = None
    age_seconds: float | None = None
    reason: str = ""
    reason_code: str = "capture_rejected"
    verdict: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    clipped: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "backend": self.backend,
            "source": self.source,
            "elapsed_ms": self.elapsed_ms,
            "age_seconds": self.age_seconds,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "verdict": dict(self.verdict),
            "content_hash": self.content_hash,
            "clipped": self.clipped,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class GameFrameObservation:
    """One read of the target, with every fact kept separate.

    ``status`` is the whole contract: only ``observed`` produced a validated
    frame. ``fresh`` is ``None`` when no frame existed, never ``False`` for a
    frame that was never captured, because the two are different diagnoses.
    """

    observation_id: str
    session_id: str
    seq: int
    game: str
    captured_at: str
    status: str
    target: dict[str, Any]
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    backend: str = ""
    source: str = ""
    path: str = ""
    capture_ms: float | None = None
    schedule_delay_ms: float | None = None
    frame_age_seconds: float | None = None
    fresh: bool | None = None
    target_valid: bool = False
    visibility: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    reason_code: str = ""
    reason: str = ""
    consecutive_failures: int = 0
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "session_id": self.session_id,
            "seq": self.seq,
            "game": self.game,
            "captured_at": self.captured_at,
            "status": self.status,
            "target": dict(self.target),
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "backend": self.backend,
            "source": self.source,
            "path": self.path,
            "capture_ms": self.capture_ms,
            "schedule_delay_ms": self.schedule_delay_ms,
            "frame_age_seconds": self.frame_age_seconds,
            "fresh": self.fresh,
            "target_valid": self.target_valid,
            "visibility": dict(self.visibility),
            "validation": dict(self.validation),
            "reason_code": self.reason_code,
            "reason": self.reason,
            "consecutive_failures": self.consecutive_failures,
            "limitations": list(self.limitations),
        }


#: ``resolve(title)`` -> a geometry-like object (or None when no window matches).
ResolveProbe = Callable[[str], Awaitable[Any]]
#: ``capture(window, path, previous_hash=...)`` -> one validated capture.
CaptureProbe = Callable[..., Awaitable[CapturedFrame]]
#: ``identity(pid)`` -> a process row with ``pid``/``start_time``, or None when
#: the process is not running. Raising means "could not tell", not "dead".
IdentityProbe = Callable[[int], Awaitable[Mapping[str, Any] | None]]
Publisher = Callable[[str, dict[str, Any]], Awaitable[None]]


class GameObserver:
    """Bind one target, capture on a bounded schedule, and report honestly."""

    def __init__(
        self,
        *,
        frame_dir: Path,
        resolve: ResolveProbe,
        capture: CaptureProbe,
        identity: IdentityProbe | None = None,
        publish: Publisher | None = None,
        store: Any | None = None,
        interval: float = DEFAULT_INTERVAL,
        min_interval: float = MIN_INTERVAL,
        max_interval: float = MAX_INTERVAL,
        capture_timeout: float = DEFAULT_CAPTURE_TIMEOUT,
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
        identity_timeout: float = DEFAULT_IDENTITY_TIMEOUT,
        stop_timeout: float = DEFAULT_STOP_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        identity_ttl: float = DEFAULT_IDENTITY_CACHE,
        max_failures: int = MAX_CONSECUTIVE_FAILURES,
        max_history: int = MAX_HISTORY,
        max_frames: int = MAX_FRAMES,
        max_sessions_kept: int = MAX_SESSIONS_KEPT,
        persist_interval: float = PERSIST_INTERVAL,
        keep_rejected: bool = False,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.frame_dir = Path(frame_dir)
        self._resolve = resolve
        self._capture = capture
        self._identity = identity
        self._publish = publish
        self._store = store
        self._clock = clock
        self._wall = wall
        self._sleep = sleep or asyncio.sleep
        self.min_interval = max(0.01, float(min_interval))
        self.max_interval = max(self.min_interval, float(max_interval))
        self.interval = self.applied_interval(interval)
        self.capture_timeout = max(0.1, float(capture_timeout))
        self.probe_timeout = max(0.05, float(probe_timeout))
        self.identity_timeout = max(0.05, float(identity_timeout))
        self.stop_timeout = max(0.1, float(stop_timeout))
        self.poll_interval = max(0.05, float(poll_interval))
        self.identity_ttl = max(0.0, float(identity_ttl))
        self.max_failures = max(1, int(max_failures))
        self.max_frames = max(1, int(max_frames))
        self.max_sessions_kept = max(1, int(max_sessions_kept))
        self.persist_interval = max(0.0, float(persist_interval))
        self.keep_rejected = bool(keep_rejected)

        self._state = IDLE
        self._session_id = ""
        self._game = ""
        self._title = ""
        self._pid = 0
        self._start_time = ""
        self._process = ""
        self._handle = 0
        self._started_at = ""
        self._ended_at = ""
        self._started_monotonic: float | None = None
        #: The loop generation. A task is only the observer's current loop while
        #: it holds this token, so a loop that was finishing a terminal failure
        #: can never keep ticking (or clear the new task) after a restart.
        self._loop_token = 0
        self._end_reason = ""
        self._pause_reason = ""
        self._pause_detail = ""
        self._user_paused = False
        self._stop_requested = False
        self._task: asyncio.Task[None] | None = None
        self._busy = False
        self._seq = 0
        self._rows: deque[GameFrameObservation] = deque(
            maxlen=max(1, int(max_history))
        )
        self._latencies: deque[float] = deque(maxlen=30)
        self._schedule_delays: deque[float] = deque(maxlen=30)
        self._starts: deque[float] = deque(maxlen=60)
        self._consecutive_failures = 0
        self._total_failures = 0
        self._observed = 0
        self._rejected = 0
        self._unavailable = 0
        self._last_success: dict[str, Any] | None = None
        self._last_frame_path = ""
        self._last_frame_key = ""
        self._last_hash = ""
        self._last_requested_interval: float | None = None
        self._last_persist = 0.0
        self._probe_error = ""
        self._starting = False
        # ``(pid, read_at, verdict, row)`` — a paused observer polls, and each
        # uncached identity read spawns a PowerShell run.
        self._identity_cache: tuple[int, float, str, dict[str, Any]] | None = None
        self._previous = self._restore()

    # -- capability and status ---------------------------------------------

    def applied_interval(self, requested: float | None) -> float:
        """The interval this scheduler will actually use for ``requested``."""
        if requested is None:
            return self.interval if hasattr(self, "interval") else DEFAULT_INTERVAL
        try:
            value = float(requested)
        except (TypeError, ValueError):
            return self.interval if hasattr(self, "interval") else DEFAULT_INTERVAL
        if not math.isfinite(value) or value <= 0:
            return self.interval if hasattr(self, "interval") else DEFAULT_INTERVAL
        return max(self.min_interval, min(self.max_interval, value))

    def capability(self) -> dict[str, Any]:
        """What this observer can measure on THIS host, with the reason."""
        wired = self._resolve is not None and self._capture is not None
        reasons: list[str] = []
        if self._resolve is None:
            reasons.append("No window probe is wired, so no target can be bound.")
        if self._capture is None:
            reasons.append("No capture seam is wired, so no frame can be captured.")
        return {
            "available": wired,
            "window_probe": self._resolve is not None,
            "capture": self._capture is not None,
            "process_identity": self._identity is not None,
            "interval_seconds": self.interval,
            "min_interval_seconds": self.min_interval,
            "max_interval_seconds": self.max_interval,
            "max_rate_hz": round(1.0 / self.min_interval, 2),
            "reason": " ".join(reasons),
        }

    def _rate_hz(self) -> float | None:
        """Measured capture rate from real starts; None when it is unmeasured."""
        if len(self._starts) < 2:
            return None
        span = self._starts[-1] - self._starts[0]
        if span <= 0:
            return None
        return round((len(self._starts) - 1) / span, 3)

    def _latency_summary(self) -> dict[str, float | None]:
        if not self._latencies:
            return {"last": None, "average": None, "max": None}
        return {
            "last": round(self._latencies[-1], 1),
            "average": round(sum(self._latencies) / len(self._latencies), 1),
            "max": round(max(self._latencies), 1),
        }

    def _delay_summary(self) -> dict[str, float | None]:
        if not self._schedule_delays:
            return {"last": None, "average": None, "max": None}
        return {
            "last": round(self._schedule_delays[-1], 1),
            "average": round(sum(self._schedule_delays) / len(self._schedule_delays), 1),
            "max": round(max(self._schedule_delays), 1),
        }

    def _target_snapshot(self) -> dict[str, Any]:
        return {
            "title": self._title,
            "game": self._game,
            "handle": self._handle,
            "pid": self._pid,
            "process": self._process,
            "start_time": self._start_time,
            # A start time makes a pid an identity (a reused pid is a different
            # process); without one the handle is the identity that exists.
            "identity": (
                f"pid:{self._pid}:{self._start_time}"
                if self._start_time
                else f"handle:{self._handle}:pid:{self._pid}"
            ),
        }

    def status(self) -> dict[str, Any]:
        """The whole observation state, ready for a panel or a log line."""
        last = self._rows[-1].to_dict() if self._rows else None
        limitations = [
            "Observation reads the target window only: it sends no input, plans no "
            "action and makes no claim about game state.",
            (
                f"Capture is scheduled no faster than every {self.min_interval:.2f}s "
                f"({1.0 / self.min_interval:.2f} Hz); a slower capture reports the "
                "rate it achieved."
            ),
            (
                "Only a validated frame is reported as observed; a rejected frame is "
                "deleted, and a target that is minimized or hidden is not captured."
            ),
            (
                f"Frames are pruned to the newest {self.max_frames} per session and "
                f"the newest {self.max_sessions_kept} sessions are kept on disk."
            ),
        ]
        if self._identity is None:
            limitations.append(
                "No process-identity probe is wired, so an exited process and a "
                "destroyed window cannot be told apart; observation pauses instead "
                "of claiming either."
            )
        return {
            "available": self._resolve is not None and self._capture is not None,
            "state": self._state,
            "session_id": self._session_id,
            "game": self._game,
            "target": self._target_snapshot() if self._session_id else None,
            "started_at": self._started_at,
            "ended_at": self._ended_at,
            "interval": {
                "requested_seconds": self._last_requested_interval
                if self._last_requested_interval is not None
                else self.interval,
                "applied_seconds": self.interval,
                "min_seconds": self.min_interval,
                "max_seconds": self.max_interval,
            },
            "backend": str((last or {}).get("backend") or ""),
            "tested": bool(self._rows),
            "last_observation": last,
            "last_success": dict(self._last_success) if self._last_success else None,
            "consecutive_failures": self._consecutive_failures,
            "total_failures": self._total_failures,
            "counts": {
                "observed": self._observed,
                "rejected": self._rejected,
                "unavailable": self._unavailable,
                "attempts": self._observed + self._rejected + self._unavailable,
            },
            "measured": {
                "rate_hz": self._rate_hz(),
                "latency_ms": self._latency_summary(),
                "schedule_delay_ms": self._delay_summary(),
            },
            "pause": {
                "code": self._pause_reason,
                "detail": self._pause_detail,
                "user_requested": self._user_paused,
            },
            "reason": self._end_reason if self._state in _TERMINAL else self._pause_detail,
            "frames": {
                "kept": self._frame_count(),
                "newest_seq": self._frames_newest_seq(),
                "path_available": bool(self._last_frame_path),
            },
            "history": [row.to_dict() for row in list(self._rows)[-STATUS_HISTORY:]],
            "previous": dict(self._previous) if self._previous else None,
            "limitations": limitations,
        }

    # -- lifecycle ----------------------------------------------------------

    async def start(
        self,
        *,
        title: str,
        game: str = "",
        pid: int = 0,
        start_time: str = "",
        process: str = "",
        interval: float | None = None,
    ) -> dict[str, Any]:
        """Bind a target and begin observing it. Returns a refusal, not a lie."""
        title = str(title or "").strip()
        if not title:
            return self._refusal(
                "target_required",
                "A target window title is required to bind an observation.",
            )
        if self._state in (OBSERVING, PAUSED):
            return self._refusal(
                "already_observing",
                (
                    f"Observation session {self._session_id} is {self._state}; stop it "
                    "before starting another."
                ),
                key="started",
            )
        if self._starting:
            # Two concurrent starts would bind two sessions and run two loops for
            # one target. The check-to-bind path awaits (a probe and an identity
            # read), so the state alone cannot stop the second one.
            return self._refusal(
                "start_in_progress",
                "Another start is already binding a target; nothing was bound twice.",
                key="started",
            )
        self._starting = True
        try:
            self._last_requested_interval = interval
            self.interval = self.applied_interval(interval)
            window = await self._probe(title)
            if not isinstance(window, WindowState):
                if self._probe_error:
                    return self._refusal(
                        "target_probe_failed",
                        f"The window probe failed: {self._probe_error}.",
                    )
                return self._refusal(_NOT_FOUND_CODE, _NOT_FOUND_DETAIL.format(title=title))
            requested_pid = int(pid or 0)
            pinned_start = str(start_time or "").strip()
            if (requested_pid or pinned_start) and not window.pid:
                # A pinned process cannot be checked against a window that does
                # not report one. Binding anyway would claim an identity nobody
                # verified.
                return self._refusal(
                    "target_pid_unavailable",
                    (
                        f"The window matching {title!r} does not report a process id, "
                        "so the pinned process identity cannot be verified."
                    ),
                )
            if requested_pid and window.pid and requested_pid != window.pid:
                return self._refusal(
                    "target_mismatch",
                    (
                        f"The window matching {title!r} belongs to process {window.pid}, "
                        f"not the requested process {pid}. Nothing was bound."
                    ),
                )
            if window.pid:
                # Read the identity at BIND time. When the caller supplied no
                # start time, adopt the one the process reports: without it a
                # reused pid is indistinguishable from the process that was
                # bound, and every later check would only compare the pid.
                verdict, row = await self._read_identity(window.pid, force=True)
                if verdict == "gone":
                    return self._refusal(
                        "process_exited",
                        (
                            f"Process {window.pid} is not running, so the window that "
                            "matched the title is stale and was not bound."
                        ),
                    )
                if verdict == "alive":
                    reported = str(row.get("start_time") or "")
                    if pinned_start and reported and reported != pinned_start:
                        return self._refusal(
                            "process_replaced",
                            (
                                f"Process {window.pid} started at {reported}, not "
                                f"{pinned_start}: the pid was reused and was not adopted."
                            ),
                        )
                    if not pinned_start and reported:
                        pinned_start = reported
                elif pinned_start:
                    return self._refusal(
                        "process_identity_unavailable",
                        (
                            f"The pinned start time for process {window.pid} could not "
                            "be verified on this host, so nothing was bound."
                        ),
                    )
            self._reset_session(
                title=title,
                game=str(game or "").strip() or title,
                window=window,
                pid=int(pid or window.pid or 0),
                start_time=pinned_start,
                process=str(process or ""),
            )
            self._state = OBSERVING
            self._stop_requested = False
            self._user_paused = False
            await self._publish_state(
                "novaforge.observation.started", extra={"interval": self.interval}
            )
            self._persist(force=True)
            self._spawn_loop()
            return {
                **self.status(),
                "started": True,
                "reason_code": "",
                "reason": "",
            }
        finally:
            self._starting = False

    async def pause(self, reason: str = "user") -> dict[str, Any]:
        """Pause observation. A user pause is never auto-cleared."""
        if self._state not in (OBSERVING, PAUSED):
            return self._refusal(
                "not_observing",
                f"Observation is {self._state}; there is nothing to pause.",
                key="paused",
            )
        if self._state == PAUSED and self._user_paused:
            return {
                **self.status(),
                "paused": True,
                "reason_code": self._pause_reason,
                "reason": "Observation is already paused by the user.",
            }
        self._user_paused = True
        await self._pause("user_paused", str(reason or "user"))
        await self._cancel_loop()
        return {
            **self.status(),
            "paused": True,
            "reason_code": self._pause_reason,
            "reason": self._pause_detail,
        }

    async def resume(self) -> dict[str, Any]:
        """Resume a USER pause after re-binding the same target."""
        if self._state != PAUSED:
            return self._refusal(
                "not_paused",
                f"Observation is {self._state}; there is no pause to clear.",
                key="resumed",
            )
        if not self._user_paused:
            return self._refusal(
                self._pause_reason or "waiting_for_target",
                (
                    "Observation is paused because the target is unavailable; it "
                    "resumes automatically once the target is valid again."
                ),
                key="resumed",
            )
        window = await self._probe(self._title)
        if not isinstance(window, WindowState):
            return self._refusal(
                "target_probe_failed" if self._probe_error else _NOT_FOUND_CODE,
                (
                    f"The window probe failed: {self._probe_error}."
                    if self._probe_error
                    else _NOT_FOUND_DETAIL.format(title=self._title)
                ),
                key="resumed",
            )
        if self._pid and window.pid and window.pid != self._pid:
            return self._refusal(
                "target_replaced",
                (
                    f"The window matching {self._title!r} now belongs to process "
                    f"{window.pid}, not {self._pid}; refusing to observe it."
                ),
                key="resumed",
            )
        if not await self._rebind(window):
            return self._refusal(
                "process_replaced",
                "The process behind this pid is not the one that was bound.",
                key="resumed",
            )
        issue, detail = target_visibility(window)
        if issue:
            return self._refusal(
                issue, f"{detail} Restore the window and resume again.", key="resumed"
            )
        self._user_paused = False
        self._state = OBSERVING
        self._pause_reason = ""
        self._pause_detail = ""
        await self._publish_state("novaforge.observation.resumed", extra={"detail": "user"})
        self._persist(force=True)
        if self._task is None or self._task.done():
            self._stop_requested = False
            self._spawn_loop()
        return {
            **self.status(),
            "resumed": True,
            "reason_code": "",
            "reason": "",
        }

    async def stop(self, reason: str = "user") -> dict[str, Any]:
        """Stop observing and clean up. Safe to call repeatedly."""
        already = self._state in _TERMINAL and self._task is None
        if already or self._state == IDLE:
            return {
                **self.status(),
                "stopped": False,
                "already_stopped": True,
                "reason_code": self._end_reason or "not_observing",
                "reason": (
                    f"Observation is {self._state}; nothing was running."
                    if self._state == IDLE
                    else f"Observation already ended ({self._end_reason or self._state})."
                ),
            }
        self._stop_requested = True
        await self._cancel_loop()
        if self._state not in _TERMINAL:
            self._state = STOPPED
            self._end_reason = str(reason or "user")
            self._ended_at = _stamp(self._wall())
            await self._publish_state(
                "novaforge.observation.stopped",
                extra={"observations": self._observed, "failures": self._total_failures},
            )
        self._pause_reason = ""
        self._pause_detail = ""
        # A stopped session is not paused: leaving the user-pause flag set made
        # `status.pause` claim a pause that no longer exists.
        self._user_paused = False
        self._persist(force=True)
        return {
            **self.status(),
            "stopped": True,
            "already_stopped": False,
            "reason_code": self._end_reason,
            "reason": "",
        }

    def _refusal(self, code: str, reason: str, *, key: str = "started") -> dict[str, Any]:
        """A refusal that keeps the observer's own status, without out-ranking it.

        The status carries its own ``reason`` (a pause or end reason). Building
        the answer the other way round would let that field overwrite the
        refusal and report the wrong diagnosis.
        """
        return {**self.status(), key: False, "reason_code": code, "reason": reason}

    async def shutdown(self) -> None:
        """Application shutdown: stop without claiming anything was observed."""
        with contextlib.suppress(Exception):
            await self.stop("application_shutdown")

    async def observe_once(self, *, schedule_delay_ms: float | None = None) -> dict[str, Any]:
        """Capture and validate one frame immediately.

        Returns the record, or a ``busy``/``not_observing`` answer when nothing
        was attempted. A busy answer is not a failed observation: it means the
        previous capture is still in flight and no second one was started.
        """
        if self._state not in (OBSERVING, PAUSED) or self._user_paused:
            return {
                "status": "not_observing",
                "reason_code": "paused_by_user" if self._user_paused else "not_observing",
                "reason": (
                    "Observation was paused by the user; no read was attempted."
                    if self._user_paused
                    else f"Observation is {self._state}; no read was attempted."
                ),
            }
        if self._busy:
            return {
                "status": "busy",
                "reason_code": "capture_in_flight",
                "reason": "A capture is already in flight for this target; none was started.",
            }
        self._busy = True
        try:
            record = await self._sample(schedule_delay_ms)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad read never kills the loop
            record = await self._failure_record(
                status=UNAVAILABLE,
                reason_code="observation_error",
                reason=f"The observation raised {type(exc).__name__}: {exc}.",
                schedule_delay_ms=schedule_delay_ms,
                count_failure=True,
            )
        finally:
            self._busy = False
        if record is not None and record.status in (OBSERVED, REJECTED):
            await self._publish_state(
                "novaforge.observation.frame",
                extra={
                    "observation_id": record.observation_id,
                    "seq": record.seq,
                    "status": record.status,
                    "backend": record.backend,
                    "width": record.width,
                    "height": record.height,
                    "capture_ms": record.capture_ms,
                    "reason_code": record.reason_code,
                },
            )
        if record is None:
            if self._pause_reason:
                return {
                    "status": "unavailable",
                    "reason_code": self._pause_reason,
                    "reason": self._pause_detail
                    or "The target is unavailable; no frame was captured.",
                }
            if self._stop_requested:
                return {
                    "status": "unavailable",
                    "reason_code": "stopped",
                    "reason": (
                        "A stop landed while the read was in flight; the frame was "
                        "discarded and nothing was recorded."
                    ),
                }
            return {
                "status": "unavailable",
                "reason_code": "no_record",
                "reason": "The observation produced no record.",
            }
        return record.to_dict()

    # -- scheduling ---------------------------------------------------------

    def _spawn_loop(self) -> None:
        """Start the one loop for the current generation.

        The token is what makes a restart safe: a terminal failure finishes its
        loop with awaits still pending, and without a generation a new ``start``
        issued in that window would leave the old loop running under the new
        session's state — two capture loops for one target, and an old loop's
        exit clearing the new task.
        """
        self._loop_token += 1
        token = self._loop_token
        self._task = asyncio.create_task(
            self._loop(token), name=f"novaforge-observer-{self._session_id}"
        )

    def _current_loop(self, token: int) -> bool:
        return token == self._loop_token

    async def _loop(self, token: int) -> None:
        """One bounded loop: observe, then sleep until the next due tick."""
        expected = self._clock()
        try:
            while (
                not self._stop_requested
                and self._state in (OBSERVING, PAUSED)
                and self._current_loop(token)
            ):
                now = self._clock()
                delay = max(0.0, expected - now)
                if delay:
                    await self._sleep(delay)
                if (
                    self._stop_requested
                    or self._state not in (OBSERVING, PAUSED)
                    or not self._current_loop(token)
                ):
                    break
                late_ms = round(max(0.0, self._clock() - expected) * 1000.0, 1)
                started = self._clock()
                try:
                    await self.observe_once(schedule_delay_ms=late_ms)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - recorded, then the loop ends
                    await self._failure_record(
                        status=UNAVAILABLE,
                        reason_code="observation_error",
                        reason=f"The observation loop raised {type(exc).__name__}: {exc}.",
                        schedule_delay_ms=late_ms,
                        count_failure=True,
                    )
                if (
                    self._stop_requested
                    or self._state in _TERMINAL
                    or not self._current_loop(token)
                ):
                    break
                # A paused observer only probes the target; it must not spin.
                step = (
                    self.interval
                    if self._state == OBSERVING
                    else max(self.interval, self.poll_interval)
                )
                expected = started + step
        except asyncio.CancelledError:
            pass
        finally:
            # Only this generation may clear the task; a newer loop's reference
            # must survive an older loop's exit.
            if self._current_loop(token) and self._task is not None:
                self._task = None

    async def _cancel_loop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self.stop_timeout)
        except (TimeoutError, asyncio.CancelledError):
            # The loop is cancelled/abandoned; it reports its own terminal state
            # and clearing ``_task`` here already prevents a second stop waiting
            # on a task that will never finish.
            pass

    # -- one read -----------------------------------------------------------

    async def _sample(self, schedule_delay_ms: float | None) -> GameFrameObservation | None:
        """Read the target once: probe, verify, capture, validate, record."""
        self._seq += 1
        seq = self._seq
        at = _stamp(self._wall())
        target = self._target_snapshot()
        try:
            geometry = await asyncio.wait_for(
                self._resolve(self._title), timeout=self.probe_timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - an unreadable probe is a gap
            return await self._failure_record(
                status=UNAVAILABLE,
                reason_code="target_probe_failed",
                reason=(
                    "The target window could not be read: "
                    f"{type(exc).__name__}: {exc}."
                ),
                schedule_delay_ms=schedule_delay_ms,
                count_failure=True,
                seq=seq,
                at=at,
                target=target,
            )
        window = window_state(geometry)
        if window is None:
            return await self._target_missing(
                seq=seq, at=at, target=target, schedule_delay_ms=schedule_delay_ms
            )
        if self._pid and not window.pid:
            # The pinned process cannot be checked against a window that reports
            # no pid. Pause instead of capturing a window whose owner is unknown.
            await self._pause(
                "target_identity_unavailable",
                "The target window does not report a process id, so its identity "
                "cannot be checked; no frame was captured.",
            )
            return self._pause_record(
                seq=seq,
                at=at,
                target=target,
                window=window,
                code="target_identity_unavailable",
                reason=(
                    "The window's process id is unreadable; observation paused "
                    "rather than capture a window it cannot identify."
                ),
                schedule_delay_ms=schedule_delay_ms,
            )
        if self._pid and window.pid and window.pid != self._pid:
            await self._pause(
                "target_replaced",
                (
                    f"The title now resolves to process {window.pid}, not the pinned "
                    f"process {self._pid}. A different process is never adopted."
                ),
            )
            return self._pause_record(
                seq=seq,
                at=at,
                target=target,
                window=None,
                code="target_replaced",
                reason="A different process now owns the target title; no frame was captured.",
                schedule_delay_ms=schedule_delay_ms,
            )
        if window.handle != self._handle:
            if not await self._rebind(window):
                await self._pause(
                    "target_replaced",
                    "The window behind this title could not be tied to the pinned process.",
                )
                return self._pause_record(
                    seq=seq,
                    at=at,
                    target=target,
                    window=None,
                    code="target_replaced",
                    reason=(
                        "The window could not be tied to the pinned process; no frame "
                        "was captured."
                    ),
                    schedule_delay_ms=schedule_delay_ms,
                )
            target = self._target_snapshot()
        issue, detail = target_visibility(window)
        if issue:
            await self._pause(issue, detail)
            return self._pause_record(
                seq=seq,
                at=at,
                target=target,
                window=window,
                code=issue,
                reason=f"{detail} No target frame was captured.",
                schedule_delay_ms=schedule_delay_ms,
            )
        if self._state == PAUSED and not self._user_paused:
            await self._resume_internal("the target is valid again")
        started = self._clock()
        self._starts.append(started)
        path = self._frame_dir_for_session() / f"{seq:06d}.png"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return await self._failure_record(
                status=UNAVAILABLE,
                reason_code="frame_directory_unusable",
                reason=f"The observation frame directory is unusable: {exc}.",
                schedule_delay_ms=schedule_delay_ms,
                count_failure=True,
                seq=seq,
                at=at,
                target=target,
            )
        try:
            frame = await asyncio.wait_for(
                self._capture(window, str(path), previous_hash=self._last_hash),
                timeout=self.capture_timeout,
            )
        except TimeoutError:
            self._discard(path)
            return await self._failure_record(
                status=UNAVAILABLE,
                reason_code="capture_timeout",
                reason=(
                    f"The capture did not finish within {self.capture_timeout:.1f}s; "
                    "nothing was recorded as observed."
                ),
                schedule_delay_ms=schedule_delay_ms,
                count_failure=True,
                seq=seq,
                at=at,
                target=target,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed capture is a result
            self._discard(path)
            return await self._failure_record(
                status=UNAVAILABLE,
                reason_code="capture_failed",
                reason=f"Capture failed: {type(exc).__name__}: {exc}.",
                schedule_delay_ms=schedule_delay_ms,
                count_failure=True,
                seq=seq,
                at=at,
                target=target,
            )
        if self._stop_requested or self._state in _TERMINAL:
            # A stop landed while this capture was in flight. The frame is real
            # but it must not become an observation of a session that has ended:
            # "stopped" has to mean no later read is published.
            self._discard(path)
            if self._starts:
                self._starts.pop()
            return None
        measured_ms = _number(frame.elapsed_ms)
        if measured_ms is None:
            measured_ms = round(max(0.0, self._clock() - started) * 1000.0, 1)
        if not frame.ok and frame.reason_code in TARGET_STATE_CODES:
            # No frame exists because no backend can see the target right now.
            # That is exactly the H02.1 condition: pause and say why, do not
            # count it as a rejected capture or fail the session after five.
            self._discard(path)
            if self._starts:
                # No backend produced a frame, so this is not a capture start the
                # measured rate may count.
                self._starts.pop()
            await self._pause(frame.reason_code, frame.reason or "No backend can see the target.")
            return self._pause_record(
                seq=seq,
                at=at,
                target=target,
                window=window,
                code=frame.reason_code,
                reason=frame.reason or (
                    "No capture backend can see the target in its current state; "
                    "no frame was captured."
                ),
                schedule_delay_ms=schedule_delay_ms,
            )
        self._latencies.append(measured_ms)
        if schedule_delay_ms is not None:
            self._schedule_delays.append(float(schedule_delay_ms))
        if not frame.ok:
            self._rejected += 1
            if not self.keep_rejected:
                self._discard(path)
                # The file is gone, so the record must not point at it: a path
                # that 404s is worse than no path at all.
                frame = replace(frame, path="")
            record = self._record(
                seq=seq,
                at=at,
                status=REJECTED,
                target=target,
                window=window,
                target_valid=True,
                frame=frame,
                capture_ms=measured_ms,
                schedule_delay_ms=schedule_delay_ms,
                reason_code=frame.reason_code or "capture_rejected",
                reason=frame.reason or "The capture was refused by validation.",
                # The record reports the count INCLUDING this read: a rejected
                # row saying "0 consecutive failures" is the number before the
                # failure it is describing.
                consecutive_failures=self._consecutive_failures + 1,
            )
            self._store_row(record)
            await self._count_failure()
            return record
        self._observed += 1
        self._consecutive_failures = 0
        self._last_hash = str(frame.content_hash or "")
        self._last_frame_path = frame.path or str(path)
        self._last_frame_key = f"{self._session_id}:{seq}"
        self._last_success = {
            "observation_id": f"{self._session_id}:{seq:04d}",
            "seq": seq,
            "at": at,
            "width": frame.width,
            "height": frame.height,
            "backend": frame.backend,
            "capture_ms": measured_ms,
            "frame_age_seconds": _number(frame.age_seconds),
        }
        record = self._record(
            seq=seq,
            at=at,
            status=OBSERVED,
            target=target,
            window=window,
            target_valid=True,
            frame=frame,
            capture_ms=measured_ms,
            schedule_delay_ms=schedule_delay_ms,
            reason_code="ok",
            reason="",
        )
        self._store_row(record)
        self._prune_frames()
        self._persist()
        return record

    async def _target_missing(
        self,
        *,
        seq: int,
        at: str,
        target: dict[str, Any],
        schedule_delay_ms: float | None,
    ) -> GameFrameObservation | None:
        """Handle a title that resolves to no window."""
        verdict, row = await self._read_identity(self._pid)
        code = _NOT_FOUND_CODE
        detail = _NOT_FOUND_DETAIL.format(title=self._title)
        if verdict == "gone":
            # A single "gone" can be a probe that answered nothing about a live
            # process (permissions, a transient failure). Confirm it once before
            # ending the observation: a terminal stop is not a guess.
            confirmed, _ = await self._read_identity(self._pid, force=True)
            if confirmed == "gone":
                await self._finish(
                    STOPPED,
                    "process_exited",
                    f"{detail} Process {self._pid} is no longer running.",
                )
                code = "process_exited"
                detail = f"Process {self._pid} exited; observation stopped."
            else:
                await self._pause(
                    "window_missing",
                    f"{detail} The process identity could not be confirmed, so "
                    "observation pauses instead of claiming the game exited.",
                )
                detail = (
                    f"{detail} The process identity could not be confirmed; no "
                    "frame was captured."
                )
        elif verdict == "alive":
            reported = str(row.get("start_time") or "")
            if self._start_time and reported and reported != self._start_time:
                await self._finish(
                    STOPPED,
                    "process_replaced",
                    (
                        f"Process {self._pid} now reports start time {reported}, not "
                        f"the pinned {self._start_time}: the pid was reused."
                    ),
                )
                code = "process_replaced"
                detail = f"Process {self._pid} is a different process; observation stopped."
            else:
                await self._pause(
                    "window_missing",
                    f"{detail} The process is still running, so this is a missing or "
                    "recreated window, not an exited game.",
                )
        else:
            await self._pause(
                "window_missing",
                f"{detail} The process identity could not be checked on this host, "
                "so observation stays paused rather than claiming the game exited.",
            )
        return self._pause_record(
            seq=seq,
            at=at,
            target=target,
            window=None,
            code=code,
            reason=detail,
            schedule_delay_ms=schedule_delay_ms,
        )

    def _already_paused_for(self, code: str) -> bool:
        """Whether the last record is already this pause (not a new read)."""
        return bool(
            self._rows
            and self._rows[-1].status == UNAVAILABLE
            and self._rows[-1].reason_code == code
        )

    def _pause_record(
        self,
        *,
        seq: int,
        at: str,
        target: dict[str, Any],
        window: WindowState | None,
        code: str,
        reason: str,
        schedule_delay_ms: float | None,
    ) -> GameFrameObservation | None:
        """Record the FIRST read of a pause; later polls are not new observations.

        A paused observer keeps probing its target, but nothing is captured. The
        transition is diagnostic evidence; repeating the same ``unavailable``
        row every poll would inflate the history, the counts and the persisted
        snapshot while saying nothing new. ``None`` means "already recorded" —
        the pause itself stays visible in ``status.pause``.
        """
        if self._already_paused_for(code):
            return None
        record = self._record(
            seq=seq,
            at=at,
            status=UNAVAILABLE,
            target=target,
            window=window,
            target_valid=False,
            reason_code=code,
            reason=reason,
            schedule_delay_ms=schedule_delay_ms,
        )
        self._store_row(record)
        return record

    async def _failure_record(
        self,
        *,
        status: str,
        reason_code: str,
        reason: str,
        schedule_delay_ms: float | None,
        count_failure: bool,
        seq: int | None = None,
        at: str | None = None,
        target: dict[str, Any] | None = None,
    ) -> GameFrameObservation:
        """Record a read that produced no frame, and stop if it keeps failing."""
        if seq is None:
            self._seq += 1
            seq = self._seq
        record = self._record(
            seq=seq,
            at=at or _stamp(self._wall()),
            status=status,
            target=target or self._target_snapshot(),
            window=None,
            target_valid=False,
            reason_code=reason_code,
            reason=reason,
            schedule_delay_ms=schedule_delay_ms,
            consecutive_failures=(
                self._consecutive_failures + 1
                if count_failure
                else self._consecutive_failures
            ),
        )
        self._store_row(record)
        if count_failure:
            await self._count_failure()
        return record

    async def _count_failure(self) -> None:
        self._consecutive_failures += 1
        self._total_failures += 1
        if self._consecutive_failures >= self.max_failures and self._state not in _TERMINAL:
            await self._finish(
                FAILED,
                "consecutive_capture_failures",
                (
                    f"{self._consecutive_failures} consecutive captures produced no "
                    "validated frame; observation stopped. The last reason is above."
                ),
            )

    def _store_row(self, record: GameFrameObservation) -> None:
        self._rows.append(record)
        if record.status == UNAVAILABLE:
            self._unavailable += 1
        self._persist()

    def _record(
        self,
        *,
        seq: int,
        at: str,
        status: str,
        target: dict[str, Any],
        window: WindowState | None,
        target_valid: bool,
        frame: CapturedFrame | None = None,
        capture_ms: float | None = None,
        schedule_delay_ms: float | None = None,
        reason_code: str = "",
        reason: str = "",
        consecutive_failures: int | None = None,
    ) -> GameFrameObservation:
        verdict = dict(frame.verdict) if frame is not None else {}
        fresh: bool | None = None
        if frame is not None:
            fresh = bool(verdict["fresh"]) if "fresh" in verdict else bool(frame.ok)
        limitations = [str(item) for item in verdict.get("limitations") or () if item]
        if frame is not None and frame.clipped:
            limitations.append(
                "The target extends past the display; this frame is a clipped view."
            )
        if frame is not None and frame.note:
            limitations.append(frame.note)
        return GameFrameObservation(
            observation_id=f"{self._session_id}:{seq:04d}",
            session_id=self._session_id,
            seq=seq,
            game=self._game,
            captured_at=at,
            status=status,
            target=dict(target),
            width=int(frame.width) if frame is not None else 0,
            height=int(frame.height) if frame is not None else 0,
            size_bytes=int(frame.size_bytes) if frame is not None else 0,
            backend=str(frame.backend) if frame is not None else "",
            source=str(frame.source) if frame is not None else "",
            path=str(frame.path) if frame is not None and frame.path else "",
            capture_ms=capture_ms,
            schedule_delay_ms=schedule_delay_ms,
            frame_age_seconds=_number(frame.age_seconds) if frame is not None else None,
            fresh=fresh,
            target_valid=target_valid,
            visibility=window.to_dict() if window is not None else {},
            validation=verdict,
            reason_code=reason_code,
            reason=reason,
            consecutive_failures=(
                self._consecutive_failures
                if consecutive_failures is None
                else int(consecutive_failures)
            ),
            limitations=tuple(dict.fromkeys(limitations)),
        )

    # -- target identity ----------------------------------------------------

    async def _probe(self, title: str) -> WindowState | None:
        self._probe_error = ""
        try:
            geometry = await asyncio.wait_for(
                self._resolve(title), timeout=self.probe_timeout
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._probe_error = f"it did not answer within {self.probe_timeout:.1f}s"
            return None
        except Exception as exc:  # noqa: BLE001 - an unreadable probe is reported as such
            self._probe_error = f"{type(exc).__name__}: {exc}"
            return None
        return window_state(geometry)

    async def _read_identity(
        self, pid: int, *, force: bool = False
    ) -> tuple[str, dict[str, Any]]:
        """``("alive"|"gone"|"unknown", row)``, cached briefly.

        ``gone`` is only returned by a probe that actually answered. A probe
        that raises or times out is ``unknown``: a broken PowerShell run must
        never be reported as an exited game. The cache exists because a paused
        observer polls, and an uncached read is a process launch; a binding
        check passes ``force=True`` so a reused pid is never accepted from a
        stale answer.
        """
        if self._identity is None or not pid:
            return "unknown", {}
        now = self._clock()
        cached = self._identity_cache
        if (
            not force
            and cached is not None
            and cached[0] == pid
            and (now - cached[1]) < self.identity_ttl
        ):
            return cached[2], dict(cached[3])
        try:
            row = await asyncio.wait_for(self._identity(pid), timeout=self.identity_timeout)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - an unreadable probe is not a verdict
            # A failed probe is not cached: the next poll may answer.
            return "unknown", {}
        if row is None:
            verdict, payload = "gone", {}
        else:
            payload = dict(row)
            try:
                reported_pid = int(payload.get("pid") or pid)
            except (TypeError, ValueError):
                reported_pid = pid
            verdict = "gone" if reported_pid != pid else "alive"
        self._identity_cache = (pid, now, verdict, dict(payload))
        return verdict, payload

    async def _rebind(self, window: WindowState) -> bool:
        """Adopt a recreated window only when it is the SAME process.

        A pinned process whose window reports no pid cannot be verified, and a
        pinned start time that the probe cannot confirm is a refusal: rebinding
        is the one moment where adopting the wrong process would silently
        continue an observation of something else.
        """
        if self._pid and not window.pid:
            return False
        if self._pid and window.pid and window.pid != self._pid:
            return False
        if self._start_time:
            verdict, row = await self._read_identity(window.pid, force=True)
            if verdict != "alive":
                return False
            reported = str(row.get("start_time") or "")
            if reported and reported != self._start_time:
                return False
        if not self._pid and window.handle != self._handle:
            # With no process identity, a new handle under the same title could
            # be a different program. Refuse instead of guessing.
            return False
        if window.handle == self._handle:
            return True
        previous = self._handle
        self._handle = window.handle
        await self._publish_state(
            "novaforge.observation.rebound",
            extra={"previous_handle": previous, "handle": window.handle},
        )
        return True

    # -- state --------------------------------------------------------------

    def _reset_session(
        self,
        *,
        title: str,
        game: str,
        window: WindowState,
        pid: int,
        start_time: str,
        process: str,
    ) -> None:
        self._session_id = "obs-" + uuid.uuid4().hex[:12]
        self._game = game
        self._title = title
        self._handle = window.handle
        self._pid = int(pid or 0)
        self._start_time = start_time
        self._process = process
        self._started_at = _stamp(self._wall())
        self._ended_at = ""
        self._started_monotonic = self._clock()
        self._end_reason = ""
        self._pause_reason = ""
        self._pause_detail = ""
        self._seq = 0
        self._rows.clear()
        self._latencies.clear()
        self._schedule_delays.clear()
        self._starts.clear()
        self._consecutive_failures = 0
        self._total_failures = 0
        self._observed = 0
        self._rejected = 0
        self._unavailable = 0
        self._last_success = None
        self._last_frame_path = ""
        self._last_frame_key = ""
        self._last_hash = ""
        self._last_persist = 0.0
        self._identity_cache = None
        # Create this session's directory BEFORE pruning so the current session
        # occupies a slot: pruning only the directories that already exist left
        # `max_sessions_kept + 1` on disk until the next start.
        with contextlib.suppress(OSError):
            self._frame_dir_for_session().mkdir(parents=True, exist_ok=True)
        self._prune_sessions()

    async def _pause(self, code: str, detail: str) -> None:
        changed = self._state != PAUSED or self._pause_reason != code
        self._state = PAUSED
        self._pause_reason = code
        self._pause_detail = detail
        if changed:
            await self._publish_state(
                "novaforge.observation.paused",
                extra={"reason_code": code, "reason": detail},
            )
            self._persist(force=True)

    async def _resume_internal(self, why: str) -> None:
        if self._user_paused:
            return
        self._state = OBSERVING
        previous = self._pause_reason
        self._pause_reason = ""
        self._pause_detail = ""
        await self._publish_state(
            "novaforge.observation.resumed",
            extra={"detail": why, "previous_reason_code": previous},
        )
        self._persist(force=True)

    async def _finish(self, state: str, code: str, detail: str) -> None:
        if self._state in _TERMINAL:
            return
        self._state = state
        self._end_reason = code
        self._pause_reason = code if state == FAILED else ""
        self._pause_detail = detail
        self._user_paused = False
        self._ended_at = _stamp(self._wall())
        self._stop_requested = True
        event = (
            "novaforge.observation.failed" if state == FAILED else "novaforge.observation.stopped"
        )
        await self._publish_state(
            event,
            extra={
                "reason_code": code,
                "reason": detail,
                "observations": self._observed,
                "failures": self._total_failures,
            },
        )
        self._persist(force=True)

    async def _publish_state(self, event_type: str, *, extra: Mapping[str, Any] | None = None) -> None:
        if self._publish is None:
            return
        payload: dict[str, Any] = {
            "session_id": self._session_id,
            "game": self._game,
            "state": self._state,
            "title": self._title,
            "pid": self._pid,
            "handle": self._handle,
        }
        payload.update(dict(extra or {}))
        try:
            await self._publish(event_type, payload)
        except Exception:  # noqa: BLE001 - a lost event cannot stop observation
            return

    # -- frames and persistence --------------------------------------------

    def _frame_dir_for_session(self) -> Path:
        return self.frame_dir / (self._session_id or "unbound")

    def _frames(self) -> list[Path]:
        directory = self._frame_dir_for_session()
        try:
            return sorted(path for path in directory.glob("*.png") if path.is_file())
        except OSError:
            return []

    def _frame_count(self) -> int:
        return len(self._frames())

    def _frames_newest_seq(self) -> int:
        files = self._frames()
        if not files:
            return 0
        try:
            return int(files[-1].stem)
        except ValueError:
            return 0

    def _prune_frames(self) -> None:
        files = self._frames()
        for path in files[: max(0, len(files) - self.max_frames)]:
            self._discard(path)
            # A frame's paint side car goes WITH its frame: a side car whose
            # frame is gone is garbage, and this is where a frame prune that
            # left one behind is swept up.
            self._discard(path.with_name(path.name + THUMB_SUFFIX))
        sweep_orphan_thumbnails(self._frame_dir_for_session())

    def _discard(self, path: Path) -> None:
        with contextlib.suppress(OSError):
            path.unlink()

    def _prune_sessions(self) -> None:
        """Keep the current session plus the newest ``max_sessions_kept - 1``."""
        current = self._frame_dir_for_session()
        try:
            directories = sorted(
                (
                    path
                    for path in self.frame_dir.iterdir()
                    if path.is_dir() and path != current
                ),
                key=lambda path: path.stat().st_mtime,
            )
        except OSError:
            return
        keep = max(0, self.max_sessions_kept - 1)
        for directory in directories[: max(0, len(directories) - keep)]:
            with contextlib.suppress(OSError):
                for path in (
                    *directory.glob("*.png"),
                    *directory.glob("*" + THUMB_SUFFIX),
                ):
                    self._discard(path)
                directory.rmdir()

    def frame_path(self) -> Path | None:
        """The newest VALIDATED frame, confined to this observer's own directory."""
        if not self._last_frame_path:
            return None
        path = Path(self._last_frame_path)
        try:
            root = self._frame_dir_for_session().resolve()
            resolved = path.resolve()
        except OSError:
            return None
        if root not in resolved.parents or not resolved.is_file():
            return None
        return resolved

    def _snapshot(self) -> dict[str, Any]:
        return {
            "saved_at": _stamp(self._wall()),
            "session": {
                "session_id": self._session_id,
                "game": self._game,
                "state": self._state,
                "title": self._title,
                "pid": self._pid,
                "handle": self._handle,
                "started_at": self._started_at,
                "ended_at": self._ended_at,
                "end_reason": self._end_reason,
                "observations": self._observed,
                "counts": {
                    "observed": self._observed,
                    "rejected": self._rejected,
                    "unavailable": self._unavailable,
                },
                "last_success": dict(self._last_success) if self._last_success else None,
            },
            "rows": [row.to_dict() for row in list(self._rows)[-60:]],
        }

    def _persist(self, *, force: bool = False) -> None:
        if self._store is None:
            return
        now = self._clock()
        if not force and (now - self._last_persist) < self.persist_interval:
            return
        self._last_persist = now
        try:
            self._store.write(_STATE_NAMESPACE, self._snapshot())
        except Exception:  # noqa: BLE001 - losing a snapshot must not stop observation
            return

    def _restore(self) -> dict[str, Any] | None:
        if self._store is None:
            return None
        try:
            payload = self._store.read(_STATE_NAMESPACE)
        except Exception:  # noqa: BLE001 - an unreadable store is not an observation
            return None
        if not isinstance(payload, Mapping):
            return None
        session = payload.get("session")
        if not isinstance(session, Mapping) or not session.get("session_id"):
            return None
        rows = [
            row
            for row in (payload.get("rows") or [])
            if isinstance(row, Mapping) and row.get("observation_id")
        ]
        return {
            "session": dict(session),
            "rows": [dict(row) for row in rows[-5:]],
            "saved_at": str(payload.get("saved_at") or ""),
            "restored": True,
        }


#: Kept as module constants so the refusal wording is defined once.
_NOT_FOUND_CODE = "target_missing"
_NOT_FOUND_DETAIL = (
    "No open window matches {title!r}, so nothing was bound and no observation "
    "session exists."
)
