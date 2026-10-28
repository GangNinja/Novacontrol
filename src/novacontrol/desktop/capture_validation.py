"""Frame validation: is this image a current frame OF the target?

Capture and validation are separate on purpose. A backend can succeed — it wrote
a PNG with a fresh timestamp at the right size — and still have produced a black
surface, the desktop, or the previous frame again. H02 is the recorded case: a
minimized fullscreen game kept its handle and pid, the full-screen fallback was
fresh by timestamp, and the panel labelled the NovaControl UI as ``LIVE`` game
footage.

So the facts live in separate fields and none is inferred from another:

* ``capture_ok`` — the backend produced a file it says is a frame;
* ``target_visible`` — the window is not minimized, hidden or off-screen;
* ``fresh`` — the frame is new enough to be current;
* ``content_ok`` — the PIXELS are plausibly the target (not blank, not a
  full-screen fallback, not a crop of a window that is behind another window).

``livable`` is the single conjunction a UI may show as ``LIVE``. When content
cannot be judged (unreadable image, a backend that gives no guarantee) this
module reports the limitation instead of guessing.

One case needs a SECOND signal rather than a single threshold: a screen that is
legitimately black (a fade, a loading screen) and a renderer that refuses to
paint are the same pixels. ``validate_frame(dark_confirmed=True)`` is how the
caller reports that an independent capture agreed the region is dark; the frame
is then a ``dark`` read of the target rather than a blank refusal. The default
is False, so a single black frame is still refused.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "THUMB_BYTES",
    "THUMB_HEIGHT",
    "THUMB_SUFFIX",
    "THUMB_WIDTH",
    "FrameInspection",
    "FrameVerdict",
    "inspect_frame",
    "reason_code",
    "validate_frame",
]

#: The worker's 48x27 paint read, written beside a frame when one is asked for.
#: Three bytes per pixel in BGR order (GDI+ writes 24bppRgb that way) with no
#: row padding: 48 * 3 = 144 is already 4-byte aligned. This is the SECOND thing
#: that exists so the paint can be judged without decoding the full frame; the
#: statistics below are computed from it in exactly one place.
THUMB_WIDTH = 48
THUMB_HEIGHT = 27
THUMB_BYTES = THUMB_WIDTH * THUMB_HEIGHT * 3

#: Suffix of that side car. It is a SIDE CAR, never a frame: the frame prunes
#: glob ``*.png`` and this name is not one, so every prune site sweeps it too,
#: and a side car whose frame is gone is garbage to remove.
THUMB_SUFFIX = ".thumb"

#: A pixel darker than this (0-255 luma) counts as "black" for the blank test.
#: A DirectX window that refuses PrintWindow is reported as a solid black bitmap,
#: and a black frame is indistinguishable from a real frame of a black screen —
#: which is exactly why it may never be called live evidence.
_BLACK_LUMA = 8
#: Below this non-black fraction the image is treated as unpainted.
_MIN_PAINTED = 0.01
#: Above this single-colour fraction the image is treated as a flat fill.
_MAX_UNIFORM = 0.995


@dataclass(frozen=True, slots=True)
class FrameInspection:
    """What was measured about one image file, and what could not be."""

    readable: bool
    width: int = 0
    height: int = 0
    mean_luma: float | None = None
    non_black_ratio: float | None = None
    uniform_ratio: float | None = None
    content_hash: str = ""
    blank: bool = False
    repeated: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "readable": self.readable,
            "width": self.width,
            "height": self.height,
            "mean_luma": self.mean_luma,
            "non_black_ratio": self.non_black_ratio,
            "uniform_ratio": self.uniform_ratio,
            "content_hash": self.content_hash,
            "blank": self.blank,
            "repeated": self.repeated,
            "reason": self.reason,
        }


#: Stable codes for the conditions a capture can fail in. The ORDER matters:
#: the first matching rule wins, and ``unreadable`` deliberately follows
#: ``blank`` because a blank frame is the more specific diagnosis of a renderer
#: that refused to paint.
_REASON_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("minimized", "is iconic"), "target_minimized"),
    (("not visible", "hidden"), "target_hidden"),
    (
        ("outside the captured display", "off-screen", "offscreen", "no usable on-screen"),
        "target_offscreen",
    ),
    (("behind another window", "not foreground", "covered"), "target_not_foreground"),
    (("blank", "unpainted", "solid fill"), "target_unreadable"),
    (("freshness window", "stale"), "stale_frame"),
)


#: The codes themselves. A caller that already HAS a code — a worker protocol
#: token, a backend's structured error — must not have it re-derived from prose:
#: `"target_unreadable"` matches none of `_REASON_RULES`' prose needles, so it
#: used to collapse to the generic `"capture_rejected"`, losing the diagnosis the
#: Guardian pauses on and the panel displays.
_STABLE_CODES = frozenset(
    {
        "target_minimized",
        "target_hidden",
        "target_offscreen",
        "target_not_foreground",
        "target_unreadable",
        "stale_frame",
        "capture_unavailable",
        "capture_timeout",
        "capture_failed",
        "capture_rejected",
        "observation_error",
        "frame_directory_unusable",
        "target_probe_failed",
    }
)


def reason_code(text: str) -> str:
    """A stable code for a human capture failure, or ``"capture_rejected"``.

    The UI and the Guardian pause reason read CODES, not prose: a note is
    documentation and may be reworded, while a code is a contract value the
    panel, the timeline and the tests depend on. A token that is ALREADY one of
    those codes is returned unchanged — re-reading a code as prose is how a
    backend's "target_unreadable" became "capture_rejected".
    """
    lowered = str(text or "").strip().lower()
    if not lowered:
        return "capture_rejected"
    if lowered in _STABLE_CODES:
        return lowered
    for needles, code in _REASON_RULES:
        if any(needle in lowered for needle in needles):
            return code
    return "capture_rejected"


def _thumbnail_pixels(path: str | Path) -> list[tuple[int, int, int]] | None:
    """The worker's raw 48x27 read as RGB tuples, or None when it is unusable.

    A missing, short or oversized file is ``None``: the caller then decodes the
    frame itself. Degrading to the slower truthful path is the only acceptable
    fallback — guessing the paint from a truncated read is not.
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if len(data) != THUMB_BYTES:
        return None
    # BGR in memory (GDI+ 24bppRgb), RGB to the statistics.
    return [(data[index + 2], data[index + 1], data[index]) for index in range(0, THUMB_BYTES, 3)]


def inspect_frame(
    path: str | Path, *, previous_hash: str = "", thumbnail: str | Path = ""
) -> FrameInspection:
    """Measure one frame's paint, dimensions and identity.

    Pillow is a hard dependency of the vision layer; if it is unavailable the
    image is ``readable=False`` rather than optimistically valid. The measurement
    is taken from a 48x27 reduction, so it stays cheaper than the capture it
    judges.

    ``thumbnail`` is the worker's raw 48x27 read of the SAME bitmap, written
    beside the frame. With it, the pixels are read from 3888 bytes instead of a
    full PNG decode (measured: ~35 ms warm and 300+ ms cold at 5.2 MP, more than
    the capture itself). The PNG is still opened for its dimensions, which reads
    the header only. Both paths run the same statistics code below, so a verdict
    never depends on which one produced the pixels.
    """
    try:
        from PIL import Image
    except Exception as exc:  # noqa: BLE001 - a missing reader is a reported gap
        return FrameInspection(readable=False, reason=f"The image reader is unavailable: {exc}.")

    pixels: list[tuple[int, int, int]] = []
    side_car = _thumbnail_pixels(thumbnail) if thumbnail else None
    try:
        with Image.open(path) as image:
            width, height = image.size
            if width < 8 or height < 8:
                return FrameInspection(
                    readable=True, width=width, height=height, blank=True,
                    reason=f"The image is too small to be a frame ({width}x{height}).",
                )
            if side_car is not None:
                pixels = side_car
            else:
                # Box-reduce BEFORE the resize: a 2880x1800 frame is 5.2 MP and a
                # direct bicubic resize of it cost ~74 ms, more than half the capture
                # it was judging. ``reduce`` averages blocks in C, so the same
                # measurement takes a few milliseconds and sees identical paint.
                thumb = image.convert("RGB")
                factor = max(1, min(thumb.width // 96, thumb.height // 54))
                if factor > 1:
                    thumb = thumb.reduce(factor)
                # The reduced image is tiny, so the default resample is already cheap.
                thumb = thumb.resize((THUMB_WIDTH, THUMB_HEIGHT))
                pixels = list(thumb.getdata())
    except Exception as exc:  # noqa: BLE001 - an unreadable frame is not evidence
        return FrameInspection(readable=False, reason=f"The frame could not be read: {exc}.")

    total = len(pixels)
    if not total:
        return FrameInspection(readable=False, reason="The frame has no pixels.")
    luma = [0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in pixels]
    mean_luma = round(sum(luma) / total, 2)
    painted = sum(1 for value in luma if value > _BLACK_LUMA) / total
    # Quantise to 6 bits per channel so imperceptible PNG differences between two
    # otherwise identical frames do not defeat the repeat check.
    quantised = Counter((r >> 2, g >> 2, b >> 2) for r, g, b in pixels)
    uniform = quantised.most_common(1)[0][1] / total
    digest = hashlib.sha256(bytes(c for pixel in pixels for c in pixel)).hexdigest()
    blank = painted < _MIN_PAINTED or uniform > _MAX_UNIFORM
    repeated = bool(previous_hash) and digest == previous_hash
    reason = ""
    if blank:
        reason = (
            "The frame is essentially blank (a solid fill or an unpainted surface), "
            "which is how a renderer that refuses window capture answers."
        )
    return FrameInspection(
        readable=True,
        width=width,
        height=height,
        mean_luma=mean_luma,
        non_black_ratio=round(painted, 4),
        uniform_ratio=round(uniform, 4),
        content_hash=digest,
        blank=blank,
        repeated=repeated,
        reason=reason,
    )


@dataclass(frozen=True, slots=True)
class FrameVerdict:
    """The four separate facts a live view needs, and the one derived answer."""

    capture_ok: bool
    target_visible: bool
    fresh: bool
    content_ok: bool
    livable: bool
    reason: str
    content: str  # valid | repeated | dark | blank | not_target | unverified
    backend: str = ""
    source: str = ""
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_ok": self.capture_ok,
            "target_visible": self.target_visible,
            "fresh": self.fresh,
            "content_ok": self.content_ok,
            "livable": self.livable,
            "reason": self.reason,
            "content": self.content,
            "backend": self.backend,
            "source": self.source,
            "limitations": list(self.limitations),
        }


def validate_frame(
    *,
    capture_ok: bool,
    error: str = "",
    backend: str = "",
    source: str = "",
    target_visible: bool = True,
    visibility_reason: str = "",
    foreground: bool = False,
    inspection: FrameInspection | None = None,
    age_seconds: float | None = None,
    max_age_seconds: float = 3.0,
    dark_confirmed: bool = False,
) -> FrameVerdict:
    """Judge one capture result without letting one fact stand in for another.

    ``window_crop`` is the composited desktop cropped to the window's rectangle.
    It is only trustworthy while the target is FOREGROUND: a covered window's
    rectangle still contains whatever is drawn on top of it. ``screen`` is the
    full-desktop fallback and is never a frame of the target. ``window_surface``
    is the target's own surface and may be captured while it is behind another
    window, provided the pixels are actually painted.

    ``dark_confirmed`` says an INDEPENDENT capture of the same region was also
    essentially black. Only then is a blank frame read as the target painting a
    dark scene (``content="dark"``); without it, blank stays a refusal, because
    an unpainted surface and a dark screen are indistinguishable from one image.
    """
    limitations: list[str] = []
    if not capture_ok:
        return FrameVerdict(
            capture_ok=False, target_visible=target_visible, fresh=False,
            content_ok=False, livable=False,
            reason=error or "The capture backend produced no frame.",
            content="unverified", backend=backend, source=source,
        )
    if not target_visible:
        return FrameVerdict(
            capture_ok=True, target_visible=False, fresh=False, content_ok=False,
            livable=False,
            reason=visibility_reason or "The target window is not visible.",
            content="not_target", backend=backend, source=source,
        )
    if source == "screen":
        return FrameVerdict(
            capture_ok=True, target_visible=True, fresh=False, content_ok=False,
            livable=False,
            reason="The capture fell back to the full screen; that is not a frame of the target.",
            content="not_target", backend=backend, source=source,
        )
    if source == "window_crop" and not foreground:
        return FrameVerdict(
            capture_ok=True, target_visible=True, fresh=False, content_ok=False,
            livable=False,
            reason=(
                "The target is not foreground, so a desktop crop of its rectangle may "
                "contain another window. Use a window-surface backend instead."
            ),
            content="not_target", backend=backend, source=source,
        )
    if inspection is None:
        return FrameVerdict(
            capture_ok=True, target_visible=True, fresh=False, content_ok=False,
            livable=False,
            reason="The frame was not inspected, so its pixels cannot be treated as the target.",
            content="unverified", backend=backend, source=source,
        )
    if not inspection.readable:
        return FrameVerdict(
            capture_ok=True, target_visible=True, fresh=False, content_ok=False,
            livable=False,
            reason=inspection.reason or "The frame could not be read.",
            content="unverified", backend=backend, source=source,
        )
    if inspection.blank and not dark_confirmed:
        return FrameVerdict(
            capture_ok=True, target_visible=True, fresh=False, content_ok=False,
            livable=False,
            reason=inspection.reason or "The frame is blank; nothing was painted.",
            content="blank", backend=backend, source=source,
            limitations=("A blank frame is how some DirectX windows refuse window capture.",),
        )
    fresh = age_seconds is None or age_seconds <= max_age_seconds
    if inspection.blank:
        # Two independent captures agree the region is black. The frame is not
        # presented as painted content, and the limitation travels with it so
        # the panel, the record and the timeline all say how it was decided.
        extra: tuple[str, ...] = ()
        if not fresh:
            extra = (f"The frame is older than the {max_age_seconds:.1f}s freshness window.",)
        return FrameVerdict(
            capture_ok=True, target_visible=True, fresh=fresh, content_ok=True,
            livable=bool(fresh),
            reason="" if fresh else "The frame is stale.",
            content="dark", backend=backend, source=source,
            limitations=(
                "The target and an independent capture of the same region are both "
                "essentially black, so this is read as a dark frame of the target "
                "rather than an unpainted surface; no paint is being claimed.",
                *extra,
            ),
        )
    if not fresh:
        limitations.append(
            f"The frame is older than the {max_age_seconds:.1f}s freshness window."
        )
    if inspection.repeated:
        limitations.append(
            "The content is identical to the previous accepted frame; the target may be "
            "static or the capture may be frozen."
        )
    return FrameVerdict(
        capture_ok=True,
        target_visible=True,
        fresh=fresh,
        content_ok=True,
        livable=bool(fresh),
        reason="" if fresh else "The frame is stale.",
        content="repeated" if inspection.repeated else "valid",
        backend=backend,
        source=source,
        limitations=tuple(limitations),
    )
