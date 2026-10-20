"""Bounded telemetry for a NovaForge session (NF-02).

The rules this module follows are the rules the rest of the build learned the
hard way:

* **A figure that was not measured is ``None``, never a zero.** Action counts
  and accelerator telemetry are *unsupported* here and say so; they are not
  reported as ``0``.
* **Zero is a measurement.** A session with no frames reports ``frames: 0``; a
  session with no capture failures reports ``capture_failures: 0``. The two
  facts are distinguishable from ``unavailable``.
* **Memory is bounded.** Capture latencies keep an exact count/min/max and a
  bounded window for percentiles; the window is named in the payload, so a
  percentile over 120 samples is never presented as the whole session.
* **Elapsed time uses a monotonic clock.** Durations are accumulated from
  ``time.monotonic()`` at the moments the session actually ran or paused; the
  wall-clock timestamps are labels, not the source of a duration.
* **A stale or failed capture is not a frame.** It is counted in its own field
  and never enters the latency window.

Measurement happens once, at the place the fact occurs (the manager's tick,
pause, resume and finalize), so a second observer cannot double-count it.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: Percentiles are computed over the newest capture latencies only. 120 frames
#: is a couple of minutes at the NF-01 cadence and a small, bounded payload.
LATENCY_WINDOW = 120

#: A p95 over fewer samples is not a percentile worth showing. The median is
#: still shown for any non-empty window; the p95 explains why it is absent.
P95_MIN_SAMPLES = 20

#: A frame gap is a gap between two SUCCESSFUL frames while the session was
#: running. Pauses reset the previous frame, so an intentional pause never looks
#: like a capture stall.
GAP_FLOOR_MS = 1_500.0
GAP_FACTOR = 2.5

#: Below this much active time a frame rate is arithmetic, not a measurement.
RATE_MIN_ACTIVE_SECONDS = 1.0

_UNMEASURED_RESOURCE_REASONS = {
    "cpu": (
        "No per-session CPU probe is wired. Machine-wide readings are not "
        "attributed to a session, so none is reported here."
    ),
    "memory": (
        "No per-session memory probe is wired. Machine-wide readings are not "
        "attributed to a session, so none is reported here."
    ),
    "gpu": "No GPU provider exposes telemetry for this session on this host.",
    "npu": "No NPU provider exposes telemetry for this session on this host.",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _percentile(sorted_values: list[float], percent: float) -> float | None:
    """Nearest-rank percentile over an already sorted, non-empty list."""
    if not sorted_values:
        return None
    rank = math.ceil((percent / 100.0) * len(sorted_values)) - 1
    return sorted_values[min(max(rank, 0), len(sorted_values) - 1)]


def _age_ms(at: str) -> float | None:
    if not at:
        return None
    try:
        stamp = datetime.fromisoformat(at)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return round((datetime.now(UTC) - stamp).total_seconds() * 1000.0, 1)


@dataclass(slots=True)
class TelemetrySnapshot:
    """The bounded, persisted measurements for one session."""

    version: int = 1
    frame_interval_ms: float = 1_000.0
    gap_threshold_ms: float = GAP_FLOOR_MS

    frames: int = 0
    stale_frames: int = 0
    capture_failures: int = 0
    invalid_latency: int = 0

    latency_samples_total: int = 0
    latency_sum_ms: float = 0.0
    latency_min_ms: float | None = None
    latency_max_ms: float | None = None
    latency_window_ms: list[float] = field(default_factory=list)

    frame_gaps: int = 0
    max_gap_ms: float | None = None
    last_gap_ms: float | None = None

    active_seconds: float = 0.0
    paused_seconds: float = 0.0
    pause_count: int = 0
    interrupted: bool = False
    closed: bool = False

    target_checks: int = 0
    target_losses: int = 0
    target_changes: int = 0
    target_unreadable: int = 0
    last_target_check_at: str = ""
    last_frame_at: str = ""

    # Monotonic bookkeeping is deliberately NOT persisted: a new process has a
    # new monotonic clock, and inventing an origin for it would fabricate time.
    _active_since: float | None = field(default=None, repr=False)
    _paused_since: float | None = field(default=None, repr=False)
    _last_frame_monotonic: float | None = field(default=None, repr=False)

    # -- lifecycle ---------------------------------------------------------

    def begin(self, now: float) -> None:
        """Start the first active segment (called once, when the loop starts)."""
        self.closed = False
        self._active_since = now
        self._paused_since = None
        self._last_frame_monotonic = None

    def note_pause(self, now: float) -> None:
        """Close the active segment and open a paused one."""
        self._accumulate_active(now)
        if self._paused_since is None:
            self._paused_since = now
            self.pause_count += 1
        # An intentional pause is not a capture stall: the next frame after a
        # resume must not be compared with the last frame before the pause.
        self._last_frame_monotonic = None

    def note_resume(self, now: float) -> None:
        """Close the paused segment and reopen the active one."""
        if self._paused_since is not None:
            self.paused_seconds += max(0.0, now - self._paused_since)
            self._paused_since = None
        self._active_since = now
        self._last_frame_monotonic = None

    def close(self, now: float) -> None:
        """Accumulate whatever segment is open and mark the measurements closed."""
        self._accumulate_active(now)
        if self._paused_since is not None:
            self.paused_seconds += max(0.0, now - self._paused_since)
            self._paused_since = None
        self.closed = True

    def mark_interrupted(self) -> None:
        """The process stopped while this session was active; time is partial."""
        self.interrupted = True
        self.closed = True
        self._active_since = None
        self._paused_since = None

    def _accumulate_active(self, now: float) -> None:
        if self._active_since is not None:
            self.active_seconds += max(0.0, now - self._active_since)
            self._active_since = None

    # -- measurements ------------------------------------------------------

    def record_frame(self, capture_ms: Any, captured_at: str, now: float) -> None:
        """Record one FRESH frame and its measured capture duration."""
        self.frames += 1
        self.last_frame_at = captured_at or utc_now()
        if self._last_frame_monotonic is not None:
            gap = max(0.0, (now - self._last_frame_monotonic) * 1000.0)
            self.last_gap_ms = round(gap, 1)
            if self.max_gap_ms is None or gap > self.max_gap_ms:
                self.max_gap_ms = round(gap, 1)
            if gap > self.gap_threshold_ms:
                self.frame_gaps += 1
        self._last_frame_monotonic = now

        latency = _finite(capture_ms)
        if latency is None or latency < 0:
            # A capture reported no usable duration. The frame is real (it was
            # written and verified), but its latency is not invented.
            self.invalid_latency += 1
            return
        self.latency_samples_total += 1
        self.latency_sum_ms += latency
        self.latency_min_ms = latency if self.latency_min_ms is None else min(self.latency_min_ms, latency)
        self.latency_max_ms = latency if self.latency_max_ms is None else max(self.latency_max_ms, latency)
        self.latency_window_ms.append(round(latency, 3))
        del self.latency_window_ms[:-LATENCY_WINDOW]

    def record_stale(self) -> None:
        """A capture returned but was not fresh: a stale observation, not a frame."""
        self.stale_frames += 1

    def record_capture_failure(self) -> None:
        """The capture seam raised: distinct from a stale capture."""
        self.capture_failures += 1

    def note_target_check(self, at: str) -> None:
        self.target_checks += 1
        self.last_target_check_at = at

    def note_target_event(self, reason: str) -> None:
        if reason == "target_lost":
            self.target_losses += 1
        elif reason == "target_changed":
            self.target_changes += 1
        elif reason == "target_unreadable":
            self.target_unreadable += 1

    # -- reporting ---------------------------------------------------------

    def latency_summary(self) -> dict[str, Any]:
        window = list(self.latency_window_ms)
        ordered = sorted(window)
        median = _percentile(ordered, 50.0)
        p95 = _percentile(ordered, 95.0) if len(ordered) >= P95_MIN_SAMPLES else None
        return {
            "samples": self.latency_samples_total,
            "window": len(window),
            "window_limit": LATENCY_WINDOW,
            "min_ms": self.latency_min_ms,
            "median_ms": median,
            "p95_ms": p95,
            "max_ms": self.latency_max_ms,
            "mean_ms": round(self.latency_sum_ms / self.latency_samples_total, 3)
            if self.latency_samples_total
            else None,
            "p95_available": p95 is not None,
            "p95_min_samples": P95_MIN_SAMPLES,
            "p95_reason": ""
            if p95 is not None
            else (
                f"Only {len(ordered)} latency sample(s) are available; a p95 needs "
                f"at least {P95_MIN_SAMPLES}."
            ),
            "invalid_samples": self.invalid_latency,
            "basis": (
                f"all {self.latency_samples_total} sample(s)"
                if self.latency_samples_total <= LATENCY_WINDOW
                else f"the newest {LATENCY_WINDOW} of {self.latency_samples_total} sample(s)"
            ),
        }

    def describe(
        self,
        *,
        state: str,
        duration_seconds: float | None,
        state_changes: int,
        end_reason: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """The telemetry payload the API and the panel consume."""
        clock = time.monotonic() if now is None else now
        active = self.active_seconds + (
            max(0.0, clock - self._active_since) if self._active_since is not None else 0.0
        )
        paused = self.paused_seconds + (
            max(0.0, clock - self._paused_since) if self._paused_since is not None else 0.0
        )
        rate: float | None = None
        rate_reason = ""
        if self.frames > 0 and active >= RATE_MIN_ACTIVE_SECONDS:
            rate = round(self.frames / active, 3)
        elif self.frames == 0:
            rate_reason = "No frames have been captured."
        else:
            rate_reason = (
                f"Less than {RATE_MIN_ACTIVE_SECONDS:.0f}s of active time has elapsed; "
                "a rate would be arithmetic, not a measurement."
            )

        notes: list[str] = [
            "Elapsed durations use time.monotonic(); the ISO timestamps are labels.",
            "Percentiles cover the newest latency window only; the window size is reported.",
        ]
        if self.latency_samples_total > LATENCY_WINDOW:
            notes.append(
                f"Latency percentiles use the newest {LATENCY_WINDOW} of "
                f"{self.latency_samples_total} frame(s)."
            )
        if self.interrupted:
            notes.append(
                "The application stopped while this session was active. Active and paused "
                "durations are the last values persisted, not a reconstruction."
            )
        if self.invalid_latency:
            notes.append(
                f"{self.invalid_latency} frame(s) reported no usable capture duration; they "
                "are counted as frames but not in the latency statistics."
            )

        return {
            "version": self.version,
            "generated_at": utc_now(),
            "session": {
                "state": state,
                "duration_seconds": duration_seconds,
                "active_seconds": round(active, 3),
                "paused_seconds": round(paused, 3),
                "pause_count": self.pause_count,
                "state_changes": state_changes,
                "end_reason": end_reason,
                "interrupted": self.interrupted,
            },
            "capture": {
                "frames": self.frames,
                "stale_frames": self.stale_frames,
                "capture_failures": self.capture_failures,
                "latency_ms": self.latency_summary(),
                "last_frame_at": self.last_frame_at,
                "last_frame_age_ms": _age_ms(self.last_frame_at),
                "frame_gaps": self.frame_gaps,
                "max_gap_ms": self.max_gap_ms,
                "last_gap_ms": self.last_gap_ms,
                "gap_threshold_ms": round(self.gap_threshold_ms, 1),
            },
            "loop": {
                "configured_interval_ms": round(self.frame_interval_ms, 1),
                "observed_frame_rate_hz": rate,
                "rate_available": rate is not None,
                "rate_reason": rate_reason,
            },
            "target": {
                "checks": self.target_checks,
                "last_checked_at": self.last_target_check_at,
                "losses": self.target_losses,
                "changes": self.target_changes,
                "unreadable": self.target_unreadable,
            },
            "actions": {
                "available": False,
                "reason": (
                    "NF-02 attaches no action-execution path to a session. Proposed, "
                    "attempted, executed, failed and verified counts are NOT measured, "
                    "and are not zero."
                ),
                "proposed": None,
                "attempted": None,
                "executed": None,
                "failed": None,
                "verified": None,
            },
            "resources": {
                key: {"available": False, "value": None, "reason": reason}
                for key, reason in _UNMEASURED_RESOURCE_REASONS.items()
            },
            "measurement": {
                "clock": "time.monotonic for elapsed durations; UTC ISO-8601 for labels",
                "notes": notes,
            },
        }

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "frame_interval_ms": self.frame_interval_ms,
            "gap_threshold_ms": self.gap_threshold_ms,
            "frames": self.frames,
            "stale_frames": self.stale_frames,
            "capture_failures": self.capture_failures,
            "invalid_latency": self.invalid_latency,
            "latency_samples_total": self.latency_samples_total,
            "latency_sum_ms": self.latency_sum_ms,
            "latency_min_ms": self.latency_min_ms,
            "latency_max_ms": self.latency_max_ms,
            "latency_window_ms": list(self.latency_window_ms),
            "frame_gaps": self.frame_gaps,
            "max_gap_ms": self.max_gap_ms,
            "last_gap_ms": self.last_gap_ms,
            "active_seconds": self.active_seconds,
            "paused_seconds": self.paused_seconds,
            "pause_count": self.pause_count,
            "interrupted": self.interrupted,
            "closed": self.closed,
            "target_checks": self.target_checks,
            "target_losses": self.target_losses,
            "target_changes": self.target_changes,
            "target_unreadable": self.target_unreadable,
            "last_target_check_at": self.last_target_check_at,
            "last_frame_at": self.last_frame_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "TelemetrySnapshot":
        """Tolerant read of a persisted snapshot; missing fields are defaults."""
        data = dict(payload or {})
        snapshot = cls()

        def number(key: str, default: float | None = None) -> float | None:
            value = _finite(data.get(key))
            return default if value is None else value

        def integer(key: str, default: int = 0) -> int:
            value = data.get(key)
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, (int, float, str)):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return default
            return default

        snapshot.version = integer("version", 1)
        snapshot.frame_interval_ms = number("frame_interval_ms", 1_000.0) or 1_000.0
        snapshot.gap_threshold_ms = number("gap_threshold_ms", GAP_FLOOR_MS) or GAP_FLOOR_MS
        snapshot.frames = max(0, integer("frames"))
        snapshot.stale_frames = max(0, integer("stale_frames"))
        snapshot.capture_failures = max(0, integer("capture_failures"))
        snapshot.invalid_latency = max(0, integer("invalid_latency"))
        snapshot.latency_samples_total = max(0, integer("latency_samples_total"))
        snapshot.latency_sum_ms = max(0.0, number("latency_sum_ms", 0.0) or 0.0)
        snapshot.latency_min_ms = number("latency_min_ms")
        snapshot.latency_max_ms = number("latency_max_ms")
        window: list[float] = []
        for item in data.get("latency_window_ms") or []:
            value = _finite(item)
            if value is not None and value >= 0:
                window.append(round(value, 3))
        snapshot.latency_window_ms = window[-LATENCY_WINDOW:]
        snapshot.frame_gaps = max(0, integer("frame_gaps"))
        snapshot.max_gap_ms = number("max_gap_ms")
        snapshot.last_gap_ms = number("last_gap_ms")
        snapshot.active_seconds = max(0.0, number("active_seconds", 0.0) or 0.0)
        snapshot.paused_seconds = max(0.0, number("paused_seconds", 0.0) or 0.0)
        snapshot.pause_count = max(0, integer("pause_count"))
        snapshot.interrupted = bool(data.get("interrupted", False))
        snapshot.closed = bool(data.get("closed", True))
        snapshot.target_checks = max(0, integer("target_checks"))
        snapshot.target_losses = max(0, integer("target_losses"))
        snapshot.target_changes = max(0, integer("target_changes"))
        snapshot.target_unreadable = max(0, integer("target_unreadable"))
        snapshot.last_target_check_at = str(data.get("last_target_check_at", "") or "")
        snapshot.last_frame_at = str(data.get("last_frame_at", "") or "")
        return snapshot


def gap_threshold_for(frame_interval_seconds: float) -> float:
    """A gap worth reporting, derived from the configured loop cadence."""
    interval_ms = max(0.0, float(frame_interval_seconds)) * 1000.0
    return max(GAP_FLOOR_MS, GAP_FACTOR * interval_ms)
