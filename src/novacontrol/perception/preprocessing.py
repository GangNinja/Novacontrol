"""Preprocessing: the cheap, provider-independent work done once per frame.

Everything downstream — change detection, region detection, segmentation — reads
a LUMINANCE PREVIEW rather than the original pixels. That is a deliberate
budget decision, not a shortcut: a 4K screenshot is 8 million pixels, and asking
a pure-Python or even a classic-CV pass to visit all of them on every frame is
how a real-time layer becomes a slow one. The preview is bounded by
``max_side`` and scaled back up arithmetically when a box needs original
coordinates, which ``PreparedFrame.to_frame_bbox`` does.

Three properties this module keeps:

**Provider-independent.** The preview is grayscale rows of bytes and nothing
else — no analyzer, no model, no vendor type. A provider that wants colour can
read the file itself, and one that wants luminance gets it here.

**Cheap by construction.** Each step is recorded only when it actually ran
(a resize that would change nothing is not a step), and the preview size is a
configurable budget rather than a constant.

**Honest about failure.** An unreadable file, an empty file, a corrupt header or
a zero-sized image raises ``FrameUnreadable`` with the reason — the engine turns
that into a FAILED result that names the frame, never into an empty scene that
looks like a successful look at a blank picture.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from novacontrol.perception.frames import Frame
from novacontrol.perception.models import BBox

__all__ = [
    "FrameUnreadable",
    "GrayPreview",
    "PreparedFrame",
    "PreprocessSpec",
    "PreviewDiff",
    "prepare_frame",
    "validate_frame",
]

#: A pixel this much darker or brighter than its counterpart is a CHANGE, not
#: capture noise. 16/255 is the same floor the desktop's before/after diff uses,
#: so the two layers agree about what a visible difference is.
DEFAULT_PIXEL_DELTA = 16


class FrameUnreadable(ValueError):
    """The frame could not be read — the reason says why, in the caller's words."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class GrayPreview:
    """A bounded luminance view of a frame: one byte per pixel, one row per line.

    Rows of ``bytes`` rather than a nested tuple or a list of ints: a 192-wide
    row is one small immutable object, indexing it is O(1), and the whole preview
    hashes cheaply for the scene's change signature.
    """

    width: int
    height: int
    rows: tuple[bytes, ...] = ()
    scale: float = 1.0

    @property
    def pixels(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def ok(self) -> bool:
        return self.width > 0 and self.height > 0 and len(self.rows) == self.height

    def pixel(self, x: int, y: int) -> int:
        """One luminance value, or 0 outside the preview."""
        if not self.ok or not (0 <= x < self.width and 0 <= y < self.height):
            return 0
        return self.rows[y][x]

    def values(self) -> bytes:
        """The whole preview as one flat buffer, row-major."""
        return b"".join(self.rows)

    def mean_luminance(self) -> float | None:
        """The average pixel value, or ``None`` for an empty preview."""
        if not self.ok:
            return None
        total = sum(sum(row) for row in self.rows)
        return total / self.pixels

    def signature(self) -> str:
        """A short stable digest of the preview — cheap change bookkeeping.

        Not a checksum of the FILE: two frames of the same screen saved to
        different paths share a signature, which is exactly what the sampler
        needs to know that the scene did not move.
        """
        import hashlib

        digest = hashlib.sha256()
        digest.update(f"{self.width}x{self.height}".encode())
        digest.update(self.values())
        return digest.hexdigest()[:16]

    def diff(self, other: GrayPreview, *, pixel_delta: int = DEFAULT_PIXEL_DELTA) -> PreviewDiff:
        """How much this preview differs from another, in measured terms.

        Sizes that do not match make the comparison meaningless, so the diff
        reports ``comparable=False`` instead of comparing the region they share
        and producing a number that looks like a measurement but is not one.
        """
        if not self.ok or not other.ok or (self.width, self.height) != (
            other.width,
            other.height,
        ):
            return PreviewDiff(comparable=False, width=0, height=0)
        total = self.pixels
        changed = 0
        delta_sum = 0
        max_delta = 0
        for left_row, right_row in zip(self.rows, other.rows, strict=True):
            for left, right in zip(left_row, right_row, strict=True):
                delta = left - right if left >= right else right - left
                if delta:
                    delta_sum += delta
                    if delta > max_delta:
                        max_delta = delta
                if delta >= pixel_delta:
                    changed += 1
        return PreviewDiff(
            comparable=True,
            width=self.width,
            height=self.height,
            mean_delta=(delta_sum / total) if total else 0.0,
            changed_fraction=(changed / total) if total else 0.0,
            max_delta=max_delta,
        )

    def to_dict(self) -> dict[str, Any]:
        """Preview METADATA only — the luminance rows never serialize."""
        return {
            "width": self.width,
            "height": self.height,
            "scale": round(self.scale, 6),
            "signature": self.signature(),
            "mean_luminance": _rounded(self.mean_luminance(), 3),
        }


@dataclass(frozen=True, slots=True)
class PreviewDiff:
    """Two previews compared, with the measurements that comparison produced."""

    comparable: bool = False
    width: int = 0
    height: int = 0
    mean_delta: float = 0.0
    changed_fraction: float = 0.0
    max_delta: int = 0

    @property
    def changed(self) -> bool:
        return self.comparable and self.changed_fraction > 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "comparable": self.comparable,
            "width": self.width,
            "height": self.height,
            "mean_delta": round(self.mean_delta, 4),
            "changed_fraction": round(self.changed_fraction, 6),
            "max_delta": self.max_delta,
        }


@dataclass(frozen=True, slots=True)
class PreprocessSpec:
    """What preprocessing is allowed to cost, and what it should produce.

    ``max_side`` bounds the preview — the single knob that decides how much work
    every later stage does. ``crop`` restricts attention to a region of interest
    (an app window, a HUD), and its coordinates are in the ORIGINAL image's
    pixels, because that is the space a caller knows.
    """

    max_side: int = 192
    crop: BBox | None = None

    @property
    def bounded(self) -> bool:
        return self.max_side > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_side": self.max_side,
            "crop": self.crop.to_dict() if self.crop is not None else None,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> PreprocessSpec:
        """A spec from a mapping, defaulting rather than raising on nonsense."""
        raw_max = data.get("max_side", cls().max_side)
        try:
            max_side = int(raw_max)
        except (TypeError, ValueError):
            max_side = cls().max_side
        raw_crop = data.get("crop")
        crop = BBox.from_mapping(raw_crop) if isinstance(raw_crop, Mapping) else None
        if crop is not None and not crop.has_extent:
            crop = None
        return cls(max_side=max_side, crop=crop)


def _rounded(value: float | None, places: int) -> float | None:
    """A measured number rounded for a wire shape — absent stays absent."""
    return round(value, places) if value is not None else None


@dataclass(frozen=True, slots=True)
class PreparedFrame:
    """A frame decoded once, with the geometry needed to map boxes back.

    ``scale_x``/``scale_y`` are original-pixels per preview-pixel, so a preview
    box divided by them lands back in the frame's own coordinates. They are kept
    apart rather than as one factor because a rounded preview size makes the two
    axes differ by a fraction of a pixel, and a single factor would drift a box
    by a few pixels at the far edge.
    """

    frame: Frame
    spec: PreprocessSpec
    preview: GrayPreview
    original_width: int = 0
    original_height: int = 0
    offset_x: int = 0
    offset_y: int = 0
    scale_x: float = 1.0
    scale_y: float = 1.0
    steps: tuple[str, ...] = ()
    decode_ms: float | None = None

    @property
    def preview_size(self) -> tuple[int, int]:
        return self.preview.width, self.preview.height

    def to_frame_bbox(self, box: BBox) -> BBox:
        """A preview-space box in the ORIGINAL frame's coordinates.

        The rounding is deliberate and one-way: a caller maps a preview box onto
        the frame it came from, and every pixel it returns is a pixel of that
        image. Nothing maps the other way, because a full-resolution position
        does not need to be reduced to be useful.
        """
        x = int(round(box.x * self.scale_x)) + self.offset_x
        y = int(round(box.y * self.scale_y)) + self.offset_y
        width = int(round(box.width * self.scale_x))
        height = int(round(box.height * self.scale_y))
        return BBox(
            x=max(0, x),
            y=max(0, y),
            width=max(0, width),
            height=max(0, height),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame.frame_id,
            "source_id": self.frame.source_id,
            "sequence": self.frame.sequence,
            "original_width": self.original_width,
            "original_height": self.original_height,
            "preview": self.preview.to_dict(),
            "offset": [self.offset_x, self.offset_y],
            "scale": [
                round(self.scale_x, 6),
                round(self.scale_y, 6),
                ],
            "spec": self.spec.to_dict(),
            "steps": list(self.steps),
            "decode_ms": _rounded(self.decode_ms, 3),
        }


def validate_frame(frame: Frame) -> str:
    """Why this frame cannot be read, or an empty string when it can.

    The cheap checks only — a path, a file, a non-zero size, decodable headers.
    Whether the PIXELS can be decoded is answered by :func:`prepare_frame`, which
    has to decode them anyway; duplicating that here would decode twice.
    """
    if not frame.located:
        return "no image source was given"
    from novacontrol.perception.frames import probe_image_file
    from novacontrol.vision.providers import unreadable_image_message

    if probe_image_file(frame.path) is None:
        return unreadable_image_message(frame.path)
    return ""


def prepare_frame(frame: Frame, spec: PreprocessSpec | None = None) -> PreparedFrame:
    """Decode ``frame`` into a bounded luminance preview, once.

    Raises ``FrameUnreadable`` with a reason for every way this can fail, which
    is the whole point: the caller gets a specific statement instead of a scene
    that looks like it found nothing. The reason for an unreadable FILE reuses
    the vision layer's own wording (``unreadable_image_message``) so the same
    missing attachment reads the same in both pipelines.
    """
    resolved = spec or PreprocessSpec()
    if not frame.located:
        raise FrameUnreadable("no image source was given")
    from novacontrol.perception.frames import monotonic_ms, probe_image_file
    from novacontrol.vision.providers import unreadable_image_message

    info = probe_image_file(frame.path)
    if info is None:
        raise FrameUnreadable(unreadable_image_message(frame.path))
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        raise FrameUnreadable(
            "Pillow is not installed, so image frames cannot be decoded on this machine"
        ) from None
    started = monotonic_ms()
    steps: list[str] = ["decode"]
    try:
        with Image.open(frame.path) as image:
            luminance = image.convert("L")
            if resolved.crop is not None:
                box = resolved.crop
                luminance = luminance.crop(
                    (box.x, box.y, max(box.x + 1, box.right), max(box.y + 1, box.bottom))
                )
                steps.append("crop")
            width, height = luminance.size
            if width <= 0 or height <= 0:
                raise FrameUnreadable(f"{frame.path} has no pixels")
            target_width, target_height = width, height
            if resolved.bounded and max(width, height) > resolved.max_side:
                factor = resolved.max_side / max(width, height)
                target_width = max(1, round(width * factor))
                target_height = max(1, round(height * factor))
                luminance = luminance.resize(
                    (target_width, target_height), Image.Resampling.LANCZOS
                )
                steps.append("downscale")
            data = luminance.tobytes()
    except FrameUnreadable:
        raise
    except Exception as exc:  # noqa: BLE001 - any decode failure is the same fact
        raise FrameUnreadable(f"could not decode {frame.path}: {type(exc).__name__}") from exc
    rows = tuple(data[index * target_width : (
        index + 1) * target_width] for index in range(target_height))
    preview = GrayPreview(
        width=target_width,
        height=target_height,
        rows=rows,
        scale=target_width / width if width else 0.0,
    )
    crop_x = resolved.crop.x if resolved.crop is not None else 0
    crop_y = resolved.crop.y if resolved.crop is not None else 0
    return PreparedFrame(
        frame=frame,
        spec=resolved,
        preview=preview,
        original_width=info.width,
        original_height=info.height,
        offset_x=crop_x,
        offset_y=crop_y,
        scale_x=width / target_width if target_width else 1.0,
        scale_y=height / target_height if target_height else 1.0,
        steps=tuple(steps),
        decode_ms=monotonic_ms() - started,
    )

