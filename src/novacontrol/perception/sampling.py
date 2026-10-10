"""Frame sampling: decide which frames are worth looking at, and why.

A real-time perception layer that processes every frame is a layer that spends
all its time on frames where nothing happened. This module makes the decision
explicit and measured:

    static scene          -> look less often (interval stretches out)
    meaningful change     -> look immediately (interval snaps back to the floor)
    same instant asked twice -> drop the duplicate (max FPS)

Three properties keep it from becoming a way to quietly miss things.

**The baseline is the last PROCESSED preview, not the last seen one.** If the
comparison were against whatever arrived most recently, a slow drift would be
invisible — each new frame would only be compared with the frame before it, each
of which was also skipped. Comparing against the last frame that was actually
analysed is what makes "nothing has happened since we looked" a true statement.

**Skips are bounded.** ``max_consecutive_skips`` is a hard floor on neglect: a
slow, continuous change that never trips ``change_threshold`` still gets
analysed within that many frames, so the sampler cannot starve perception.

**The decision is explained.** Every answer carries the reason it was reached,
which is what a status surface, a test and a person debugging "why is my screen
not updating" all need.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from novacontrol.perception.frames import monotonic_ms
from novacontrol.perception.preprocessing import GrayPreview

__all__ = ["FrameSampler", "SamplingDecision", "SamplingPolicy"]


@dataclass(frozen=True, slots=True)
class SamplingPolicy:
    """How much attention a stream deserves — the whole configuration of it.

    ``target_fps`` is the steady-state rate while things are happening;
    ``max_fps`` is the ceiling (a stream faster than this gets frames dropped);
    ``min_interval_ms`` is the shortest gap between two analyses, which is the
    actual guard against processing every frame of a 60 Hz screen.
    """

    target_fps: float = 2.0
    max_fps: float = 5.0
    min_interval_ms: int = 120
    change_threshold: float = 0.0025
    pixel_delta: int = 16
    adaptive: bool = True
    static_backoff: float = 2.0
    max_backoff_steps: int = 4
    max_consecutive_skips: int = 10

    @property
    def target_interval_ms(self) -> float:
        return 1000.0 / self.target_fps if self.target_fps > 0 else 0.0

    @property
    def minimum_interval_ms(self) -> float:
        """The effective floor: the stricter of the configured gap and max FPS."""
        by_max_fps = 1000.0 / self.max_fps if self.max_fps > 0 else 0.0
        return max(float(self.min_interval_ms), by_max_fps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_fps": self.target_fps,
            "max_fps": self.max_fps,
            "min_interval_ms": self.min_interval_ms,
            "change_threshold": self.change_threshold,
            "pixel_delta": self.pixel_delta,
            "adaptive": self.adaptive,
            "static_backoff": self.static_backoff,
            "max_backoff_steps": self.max_backoff_steps,
            "max_consecutive_skips": self.max_consecutive_skips,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> SamplingPolicy:
        """A policy from configuration, keeping defaults for unreadable values."""
        defaults = cls()

        def number(key: str, fallback: float) -> float:
            try:
                value = float(data.get(key, fallback))
            except (TypeError, ValueError):
                return fallback
            return value if value > 0 else fallback

        def whole(key: str, fallback: int) -> int:
            try:
                value = int(data.get(key, fallback))
            except (TypeError, ValueError):
                return fallback
            return value if value > 0 else fallback

        return cls(
            target_fps=number("target_fps", defaults.target_fps),
            max_fps=number("max_fps", defaults.max_fps),
            min_interval_ms=whole("min_interval_ms", defaults.min_interval_ms),
            change_threshold=(
                float(data["change_threshold"])
                if _is_fraction(data.get("change_threshold"))
                else defaults.change_threshold
            ),
            pixel_delta=whole("pixel_delta", defaults.pixel_delta),
            adaptive=bool(data.get("adaptive", defaults.adaptive)),
            static_backoff=number("static_backoff", defaults.static_backoff),
            max_backoff_steps=whole("max_backoff_steps", defaults.max_backoff_steps),
            max_consecutive_skips=whole(
                "max_consecutive_skips", defaults.max_consecutive_skips
            ),
        )


def _is_fraction(value: Any) -> bool:
    try:
        return 0.0 <= float(value) <= 1.0
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class SamplingDecision:
    """Whether to process this frame, in the sampler's own words."""

    process: bool = True
    reason: str = ""
    mode: str = "initial"
    changed_fraction: float | None = None
    interval_ms: float | None = None
    consecutive_skips: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "process": self.process,
            "reason": self.reason,
            "mode": self.mode,
            "changed_fraction": (
                round(self.changed_fraction, 6) if self.changed_fraction is not None else None
            ),
            "interval_ms": round(self.interval_ms, 3) if self.interval_ms is not None else None,
            "consecutive_skips": self.consecutive_skips,
        }


class FrameSampler:
    """Decides which frames of a stream get analysed, and accounts for the rest."""

    def __init__(self, policy: SamplingPolicy | None = None) -> None:
        self.policy = policy or SamplingPolicy()
        self._baseline: GrayPreview | None = None
        self._last_processed_ms: float | None = None
        self._consecutive_skips = 0
        self._static_streak = 0
        self._frames_seen = 0
        self._frames_processed = 0
        self._frames_skipped = 0
        self._frames_dropped = 0
        self._changes = 0

    # ── accounting ───────────────────────────────────────────────────────
    @property
    def frames_seen(self) -> int:
        return self._frames_seen

    @property
    def frames_processed(self) -> int:
        return self._frames_processed

    @property
    def frames_skipped(self) -> int:
        return self._frames_skipped

    @property
    def frames_dropped(self) -> int:
        """Frames refused purely because the stream was faster than ``max_fps``."""
        return self._frames_dropped

    @property
    def changes(self) -> int:
        return self._changes

    @property
    def mode(self) -> str:
        """The sampler's current posture: ``static`` or ``active``."""
        return "static" if self._static_streak else "active"

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.to_dict(),
            "frames_seen": self._frames_seen,
            "frames_processed": self._frames_processed,
            "frames_skipped": self._frames_skipped,
            "frames_dropped": self._frames_dropped,
            "changes": self._changes,
            "static_streak": self._static_streak,
            "consecutive_skips": self._consecutive_skips,
            "mode": self.mode,
            "has_baseline": self._baseline is not None,
        }

    def reset(self) -> None:
        """Forget the baseline and the accounting — a new stream, fresh state."""
        self._baseline = None
        self._last_processed_ms = None
        self._consecutive_skips = 0
        self._static_streak = 0

    # ── the decision ─────────────────────────────────────────────────────
    def decide(self, preview: GrayPreview, *, now_ms: float | None = None) -> SamplingDecision:
        """Whether this preview should be analysed, measuring change since baseline."""
        now = monotonic_ms() if now_ms is None else float(now_ms)
        self._frames_seen += 1
        if not preview.ok:
            # An empty preview is not a scene: there is nothing to compare and
            # nothing to skip — the caller will fail the frame honestly.
            return SamplingDecision(process=True, reason="the frame has no preview", mode="initial")

        if self._baseline is None:
            return self._accept(preview,
                now, reason="the first frame of the stream", mode="initial")

        diff = self._baseline.diff(preview, pixel_delta=self.policy.pixel_delta)
        changed = diff.comparable and diff.changed_fraction >= self.policy.change_threshold
        if changed:
            self._changes += 1
            self._static_streak = 0
            return self._accept(
                preview,
                now,
                reason=(
                    f"the scene changed ({diff.changed_fraction:.4f} of pixels, "
                    f"threshold {self.policy.change_threshold:g})"
                ),
                mode="changed",
                changed_fraction=diff.changed_fraction,
            )

        gap = None if self._last_processed_ms is None else now - self._last_processed_ms
        if gap is not None and gap < self.policy.minimum_interval_ms:
            self._frames_dropped += 1
            self._frames_skipped += 1
            self._consecutive_skips += 1
            self._static_streak += 1
            return SamplingDecision(
                process=False,
                reason=(
                    f"within the minimum frame interval "
                    f"({gap:.0f}ms < {self.policy.minimum_interval_ms:.0f}ms)"
                ),
                mode="dropped",
                changed_fraction=diff.changed_fraction,
                interval_ms=self.policy.minimum_interval_ms,
                consecutive_skips=self._consecutive_skips,
            )

        interval = self.policy.target_interval_ms
        if self.policy.adaptive and self._static_streak:
            steps = min(self._static_streak, self.policy.max_backoff_steps)
            interval = self.policy.target_interval_ms * (self.policy.static_backoff**steps)
        due = gap is None or gap >= interval
        if not due and self._consecutive_skips < self.policy.max_consecutive_skips:
            self._frames_skipped += 1
            self._consecutive_skips += 1
            self._static_streak += 1
            return SamplingDecision(
                process=False,
                reason=(
                    f"the scene is static and the next look is not due yet "
                    f"({gap:.0f}ms < {interval:.0f}ms, {self._consecutive_skips} skipped)"
                ),
                mode="static",
                changed_fraction=diff.changed_fraction,
                interval_ms=interval,
                consecutive_skips=self._consecutive_skips,
            )
        why = (
            "static scene reached the sampling floor for consecutive skips"
            if not due
            else "the sampling interval elapsed"
        )
        return self._accept(
            preview,
            now,
            reason=why,
            mode="static" if not due else "interval",
            changed_fraction=diff.changed_fraction,
            interval_ms=interval,
        )

    def _accept(
        self,
        preview: GrayPreview,
        now: float,
        *,
        reason: str,
        mode: str,
        changed_fraction: float | None = None,
        interval_ms: float | None = None,
    ) -> SamplingDecision:
        self._baseline = preview
        self._last_processed_ms = now
        self._frames_processed += 1
        self._consecutive_skips = 0
        if mode == "changed":
            self._static_streak = 0
        elif mode == "static":
            self._static_streak += 1
        return SamplingDecision(
            process=True,
            reason=reason,
            mode=mode,
            changed_fraction=changed_fraction,
            interval_ms=interval_ms,
            consecutive_skips=0,
        )
