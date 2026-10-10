"""Perception providers: replaceable, and honest about what this build has.

Phase 6 drew one boundary — a thing that can SEE (``VisionProvider``) — and this
module draws the other two the perception layer needs: a thing that can FIND
objects (``DetectionProvider``) and a thing that can OUTLINE them
(``SegmentationProvider``). All three are protocols, so which one is installed is
configuration rather than architecture.

What ships, and exactly what each is allowed to claim:

``OcrTextDetectionProvider``
    Detection from OCR GEOMETRY: every recognized word that the engine placed
    becomes an object whose label is its text and whose box is where it was
    read. This is real detection (the boxes are measured, not guessed) of TEXT
    REGIONS — it detects words, and its objects say so in ``source`` and
    ``metadata.kind``. It finds no laptops, and it never says it found one.

``RegionDetectionProvider``
    Classical connected-component detection over the luminance preview: a
    region of pixels that differs from its background is located, and reported
    with the measurements that found it (area, contrast). It is a real detector
    at the region level; its label is literally ``region`` because a classical
    pass cannot name what a region IS, and naming it would be the fabrication
    this phase forbids.

``RegionSegmentationProvider``
    The same pass, returning pixel MASKS rather than boxes: ``mask_id``, a real
    ``pixel_count``, and a bounded store behind ``mask_pixels()`` for a consumer
    that genuinely needs the mask. A bounding box is never presented as a mask —
    the two are different fields on the same result, and only a provider that
    produced pixels may set ``mask_id``.

``NullDetectionProvider`` / ``NullSegmentationProvider``
    The honest answer of a machine with nothing wired: ``available = False`` and
    a reason, so a result records UNAVAILABLE and names the missing provider.

Semantic object labels (person, laptop, window) are PROVIDER_DEPENDENT: they
come from the vision-model escalation, which maps Phase 6's structured
``VisionResult`` elements into detections whose ``source`` names the model. No
model, no semantic labels — and the result says so instead of inventing them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from novacontrol.perception.models import BBox, DetectedObject, SegmentationResult
from novacontrol.perception.preprocessing import GrayPreview, PreparedFrame
from novacontrol.vision.ocr import OcrEngine, OcrWord, default_ocr_engine

__all__ = [
    "ChainDetectionProvider",
    "DetectionProvider",
    "NullDetectionProvider",
    "NullSegmentationProvider",
    "OcrTextDetectionProvider",
    "RegionDetectionProvider",
    "RegionSegmentationProvider",
    "SegmentationProvider",
    "default_detection_provider",
    "default_segmentation_provider",
]


@runtime_checkable
class DetectionProvider(Protocol):
    """Something that can locate things in a prepared frame."""

    @property
    def name(self) -> str:
        """A short, stable identifier for status output and provenance."""

    @property
    def available(self) -> bool:
        """Whether this provider can detect anything on this machine."""

    @property
    def unavailable_reason(self) -> str:
        """Why not — empty when it can."""

    async def detect(self, prepared: PreparedFrame) -> tuple[DetectedObject, ...]:
        """The objects this provider can locate, empty when it locates none.

        Raises ``PerceptionProviderError`` when the provider itself failed, which
        a caller must be able to tell apart from a frame with nothing in it.
        """


@runtime_checkable
class SegmentationProvider(Protocol):
    """Something that can outline regions of a prepared frame as pixels."""

    @property
    def name(self) -> str:
        """A short, stable identifier for status output and provenance."""

    @property
    def available(self) -> bool:
        """Whether this provider can segment anything on this machine."""

    @property
    def unavailable_reason(self) -> str:
        """Why not — empty when it can."""

    async def segment(self, prepared: PreparedFrame) -> tuple[SegmentationResult, ...]:
        """The regions this provider outlined, empty when it outlined none."""


class PerceptionProviderError(RuntimeError):
    """A provider failed — it never means "found nothing"."""

    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        self.reason = reason
        super().__init__(f"{provider}: {reason}")


class NullDetectionProvider:
    """No detector: the honest default on a build that wires none."""

    name = "none"

    def __init__(self, reason: str = "no object detector is wired") -> None:
        self._reason = reason

    @property
    def available(self) -> bool:
        return False

    @property
    def unavailable_reason(self) -> str:
        return self._reason

    async def detect(self, prepared: PreparedFrame) -> tuple[DetectedObject, ...]:
        del prepared
        return ()


class NullSegmentationProvider:
    """No segmenter: the same honesty for masks."""

    name = "none"

    def __init__(self, reason: str = "no segmentation provider is wired") -> None:
        self._reason = reason

    @property
    def available(self) -> bool:
        return False

    @property
    def unavailable_reason(self) -> str:
        return self._reason

    async def segment(self, prepared: PreparedFrame) -> tuple[SegmentationResult, ...]:
        del prepared
        return ()


class OcrTextDetectionProvider:
    """Text regions, located by the OCR engine that already reads this build.

    The objects this produces are the WORDS the reader placed: their label is
    the recognized text and their box is where it was read, which is a measured
    fact about the image. Their ``class_id`` is ``None`` and their ``confidence``
    is ``None`` because the shipped engine reports neither — a fabricated score
    beside an exact box would be worse than no score at all.
    """

    def __init__(self, ocr: OcrEngine | None = None, *, max_detections: int = 64) -> None:
        self._ocr = ocr or default_ocr_engine()
        self.max_detections = max(1, max_detections)

    @property
    def name(self) -> str:
        return f"ocr:{self._ocr.name}"

    @property
    def available(self) -> bool:
        return bool(self._ocr.available)

    @property
    def unavailable_reason(self) -> str:
        if self.available:
            return ""
        return f"the OCR engine {self._ocr.name!r} is not available on this machine"

    async def detect(self, prepared: PreparedFrame) -> tuple[DetectedObject, ...]:
        """Every placed word as one text detection, in reading order."""
        if not self.available:
            return ()
        try:
            words = await self._ocr.read(prepared.frame.path)
        except Exception as exc:  # noqa: BLE001 - classified, then reported
            raise PerceptionProviderError(
                self.name, f"the OCR engine failed: {type(exc).__name__}"
            ) from exc
        return _detections_from_words(words, prepared, source=self.name)[: self.max_detections]


def _detections_from_words(
    words: Sequence[OcrWord], prepared: PreparedFrame, *, source: str
) -> tuple[DetectedObject, ...]:
    """Placed words as detections — only words with geometry qualify.

    A reader that knows its words but not where they were drawn contributes no
    detections: a box at the origin would look like a measurement and be a lie.
    """
    produced: list[DetectedObject] = []
    for word in words:
        text = word.text.strip()
        if not text or not word.has_geometry:
            continue
        produced.append(
            DetectedObject(
                label=text,
                bbox=BBox(x=word.x, y=word.y, width=word.width, height=word.height),
                confidence=None,
                class_id=None,
                source=source,
                frame_id=prepared.frame.frame_id,
                timestamp=prepared.frame.timestamp,
                metadata={"kind": "text", "line": word.line, "reader": word.kind},
            )
        )
    return tuple(produced)


@dataclass(frozen=True, slots=True)
class _Region:
    """One connected region of the preview, with the measurements that found it.

    ``bbox`` is in PREVIEW coordinates; the providers that export this data map
    it through the prepared frame, so a caller only ever sees frame coordinates.
    """

    bbox: BBox
    area: int
    mean_luminance: float
    contrast: float

    @property
    def label(self) -> str:
        return "region"


def _foreground_mask(preview: GrayPreview, *, pixel_delta: int) -> tuple[bytearray, int]:
    """Preview pixels that differ from the background, and the background value.

    The background is the preview's MEDIAN luminance rather than its mean: a
    screen that is half one colour and half another has a mean that matches
    neither half, while the median is a value that actually occurs.
    """
    values = preview.values()
    if not values:
        return bytearray(), 0
    ordered = sorted(values)
    background = ordered[len(ordered) // 2]
    mask = bytearray(len(values))
    for index, value in enumerate(values):
        delta = value - background if value >= background else background - value
        if delta >= pixel_delta:
            mask[index] = 1
    return mask, int(background)


def _label_components(
    mask: bytearray,
    values: bytes,
    width: int,
    height: int,
    *,
    background: int,
    min_area: int,
    max_components: int,
) -> tuple[tuple[_Region, ...], int]:
    """Four-connected components over a bounded binary mask.

    ``values`` is the preview's own luminance, read for the statistics each
    region reports; ``mask`` doubles as the visited set, so every pixel is
    visited at most once and the cost is linear in the preview size — the
    preview is what bounds this pass, not luck. The second return value is the
    foreground left unlabelled when the component ceiling was reached, because a
    capped scan must be able to SAY it was capped rather than look complete.
    """
    regions: list[_Region] = []
    unlabelled = 0
    for start in range(len(mask)):
        if not mask[start]:
            continue
        if len(regions) >= max_components:
            unlabelled += 1
            mask[start] = 0
            continue
        stack: list[int] = [start]
        mask[start] = 0
        count = 0
        total = 0
        min_x = width
        min_y = height
        max_x = -1
        max_y = -1
        while stack:
            index = stack.pop()
            y, x = divmod(index, width)
            count += 1
            total += values[index]
            if x < min_x:
                min_x = x
            if y < min_y:
                min_y = y
            if x > max_x:
                max_x = x
            if y > max_y:
                max_y = y
            if x > 0 and mask[index - 1]:
                mask[index - 1] = 0
                stack.append(index - 1)
            if x + 1 < width and mask[index + 1]:
                mask[index + 1] = 0
                stack.append(index + 1)
            if y > 0 and mask[index - width]:
                mask[index - width] = 0
                stack.append(index - width)
            if y + 1 < height and mask[index + width]:
                mask[index + width] = 0
                stack.append(index + width)
        if count < min_area:
            continue
        mean = total / count if count else 0.0
        regions.append(
            _Region(
                bbox=BBox(x=min_x, y=min_y, width=max_x - min_x + 1, height=max_y - min_y + 1),
                area=count,
                mean_luminance=mean,
                contrast=abs(mean - background),
            )
        )
    return tuple(regions), unlabelled


class RegionDetectionProvider:
    """Classical connected-component detection over the luminance preview.

    This is a real detector that needs no model, no download and no GPU: it
    finds pixels that differ from the frame's own background and reports the
    regions they form. What it must NOT do is claim to know what those regions
    are — its label is ``region``, its ``class_id`` is ``None``, and its
    confidence is ``None`` because a connected component is a measurement, not a
    probability. The measurements that DID decide it (area, mean luminance,
    contrast) travel in ``metadata`` so a caller can judge for itself.
    """

    name = "region:classical"

    def __init__(
        self,
        *,
        pixel_delta: int = 24,
        min_area: int = 12,
        max_objects: int = 48,
    ) -> None:
        self.pixel_delta = max(1, pixel_delta)
        self.min_area = max(1, min_area)
        self.max_objects = max(1, max_objects)

    @property
    def available(self) -> bool:
        return True

    @property
    def unavailable_reason(self) -> str:
        return ""

    async def detect(self, prepared: PreparedFrame) -> tuple[DetectedObject, ...]:
        """Every qualifying region as one detection, mapped to frame pixels."""
        regions, unlabelled = scan_regions(
            prepared.preview,
            pixel_delta=self.pixel_delta,
            min_area=self.min_area,
            max_components=self.max_objects,
        )
        produced: list[DetectedObject] = []
        for index, region in enumerate(regions):
            produced.append(
                DetectedObject(
                    label=region.label,
                    bbox=prepared.to_frame_bbox(region.bbox),
                    confidence=None,
                    class_id=None,
                    source=self.name,
                    frame_id=prepared.frame.frame_id,
                    timestamp=prepared.frame.timestamp,
                    metadata={
                        "kind": "region",
                        "region_index": index,
                        "area_pixels": region.area,
                        "mean_luminance": round(region.mean_luminance, 3),
                        "contrast": round(region.contrast, 3),
                        "unlabelled_pixels": unlabelled,
                    },
                )
            )
        return tuple(produced)


def scan_regions(
    preview: GrayPreview,
    *,
    pixel_delta: int,
    min_area: int,
    max_components: int,
) -> tuple[tuple[_Region, ...], int]:
    """The classical pass both region providers share — masks and boxes alike.

    One implementation on purpose: detection and segmentation must agree about
    which regions exist, or a box would outline a different thing than the mask
    beside it.
    """
    if not preview.ok:
        return (), 0
    mask, background = _foreground_mask(preview, pixel_delta=pixel_delta)
    if not mask:
        return (), 0
    return _label_components(
        mask,
        preview.values(),
        preview.width,
        preview.height,
        background=background,
        min_area=min_area,
        max_components=max_components,
    )


class ChainDetectionProvider:
    """Ask each provider in order; the union of what they found, deduplicated.

    A chain rather than a fallback, because text detection and region detection
    answer different questions and both answers can be true at once (a button IS
    a region, and the word on it IS text). Near-identical boxes from a later
    provider are dropped, so the same thing does not arrive twice with two
    different provenances.
    """

    def __init__(self, providers: Sequence[DetectionProvider]) -> None:
        self._providers = tuple(providers)

    @property
    def providers(self) -> tuple[DetectionProvider, ...]:
        return self._providers

    @property
    def name(self) -> str:
        return "+".join(provider.name for provider in self._providers) or "none"

    @property
    def available(self) -> bool:
        return any(bool(getattr(provider, "available", False)) for provider in self._providers)

    @property
    def unavailable_reason(self) -> str:
        if self.available:
            return ""
        reasons = [
            str(getattr(provider, "unavailable_reason", ""))
            for provider in self._providers
            if str(getattr(provider, "unavailable_reason", ""))
        ]
        return "; ".join(reasons) or "no detection provider is available"

    async def detect(self, prepared: PreparedFrame) -> tuple[DetectedObject, ...]:
        produced: list[DetectedObject] = []
        failures: list[str] = []
        for provider in self._providers:
            if not bool(getattr(provider, "available", False)):
                continue
            try:
                found = await provider.detect(prepared)
            except PerceptionProviderError as exc:
                failures.append(str(exc))
                continue
            for item in found:
                if _duplicate_of(item, produced):
                    continue
                produced.append(item)
        if not produced and failures:
            raise PerceptionProviderError(self.name, "; ".join(failures))
        return tuple(produced)


def _duplicate_of(candidate: DetectedObject, seen: Sequence[DetectedObject]) -> bool:
    """Whether the same box for the same label already arrived from a provider."""
    for existing in seen:
        if existing.label != candidate.label:
            continue
        if existing.bbox == candidate.bbox:
            return True
        if candidate.bbox.has_extent and existing.bbox.iou(candidate.bbox) >= 0.9:
            return True
    return False


def default_detection_provider(
    ocr: OcrEngine | None = None, *, max_objects: int = 48
) -> DetectionProvider:
    """The detector this build ships: text geometry first, then classical regions.

    Ordered by cost and certainty — the OCR pass is needed for the text answer
    anyway, and the region pass only ever ADDS boxes (never replaces one), so a
    machine with no OCR still gets region detection and a machine with OCR gets
    both.
    """
    return ChainDetectionProvider(
        (
            OcrTextDetectionProvider(ocr, max_detections=max_objects),
            RegionDetectionProvider(max_objects=max_objects),
        )
    )


class RegionSegmentationProvider:
    """Pixel masks from the same classical pass, kept behind a bounded store.

    A mask is real here: ``mask_pixels(mask_id)`` returns the row bytes that
    outline the region at preview resolution, and ``pixel_count`` is how many of
    them the region covers. The store is bounded to the most recent frames, so a
    stream cannot grow memory without limit — and when a mask has been evicted,
    ``mask_pixels`` says ``None`` rather than returning something else that
    happens to fit.
    """

    name = "region:classical"

    def __init__(
        self,
        *,
        pixel_delta: int = 24,
        min_area: int = 12,
        max_segments: int = 32,
        keep_frames: int = 4,
    ) -> None:
        self.pixel_delta = max(1, pixel_delta)
        self.min_area = max(1, min_area)
        self.max_segments = max(1, max_segments)
        self._keep_frames = max(1, keep_frames)
        self._masks: dict[str, tuple[bytes, ...]] = {}
        self._order: list[str] = []

    @property
    def available(self) -> bool:
        return True

    @property
    def unavailable_reason(self) -> str:
        return ""

    async def segment(self, prepared: PreparedFrame) -> tuple[SegmentationResult, ...]:
        """One mask per qualifying region, with the box that bounds it."""
        preview = prepared.preview
        regions, unlabelled = scan_regions(
            preview,
            pixel_delta=self.pixel_delta,
            min_area=self.min_area,
            max_components=self.max_segments,
        )
        if not regions:
            return ()
        masks = _region_masks(preview, regions, pixel_delta=self.pixel_delta)
        results: list[SegmentationResult] = []
        for index, region in enumerate(regions):
            mask_id = f"{prepared.frame.frame_id}:{index}"
            mask = masks[index] if index < len(masks) else _MaskRows()
            self._store(mask_id, mask.rows)
            results.append(
                SegmentationResult(
                    segment_id=mask_id,
                    label=region.label,
                    bbox=prepared.to_frame_bbox(region.bbox),
                    confidence=None,
                    frame_id=prepared.frame.frame_id,
                    provider=self.name,
                    mask_id=mask_id,
                    pixel_count=region.area,
                    metadata={
                        "mean_luminance": round(region.mean_luminance, 3),
                        "contrast": round(region.contrast, 3),
                        # The mask is cropped to the region's own box; these say
                        # where it sits in the preview, so a consumer can place
                        # it without guessing a full-frame layout.
                        "mask_origin": [mask.origin_x, mask.origin_y],
                        "mask_width": mask.width,
                        "mask_height": mask.height,
                        "preview_width": preview.width,
                        "preview_height": preview.height,
                        "unlabelled_pixels": unlabelled,
                    },
                )
            )
        return tuple(results)

    def mask_pixels(self, mask_id: str) -> tuple[bytes, ...] | None:
        """The mask's own rows for a consumer that needs them, or nothing.

        The one door to mask bytes, deliberately separate from every serialized
        shape in this package: what a result, an event or a log carries is the
        reference, and the pixels stay here.
        """
        rows = self._masks.get(mask_id)
        return rows if rows else None

    def mask_count(self) -> int:
        """How many masks the store currently holds — observability, not content."""
        return len(self._masks)

    def _store(self, mask_id: str, rows: tuple[bytes, ...]) -> None:
        self._masks[mask_id] = rows
        self._order.append(mask_id)
        limit = self.max_segments * self._keep_frames
        while len(self._order) > limit:
            oldest = self._order.pop(0)
            self._masks.pop(oldest, None)


def _region_masks(
    preview: GrayPreview, regions: Sequence[_Region], *, pixel_delta: int
) -> tuple[_MaskRows, ...]:
    """Per-region mask rows, built by re-running the pass with one label at a time.

    Re-running is not wasteful here: the pass is linear in the PREVIEW size and a
    mask must correspond exactly to the region the box describes. Attaching a
    rectangle instead — which is what a cheaper implementation would do — is
    precisely the "bounding boxes are masks" claim this phase rules out.
    """
    masks: list[_MaskRows] = []
    for index in range(len(regions)):
        mask, _background = _foreground_mask(preview, pixel_delta=pixel_delta)
        masks.append(
            _mask_rows_for(mask, preview.width, preview.height, index=index)
        )
    return tuple(masks)


@dataclass(frozen=True, slots=True)
class _MaskRows:
    """One region's mask, cropped to the region's own box (255 = inside)."""

    rows: tuple[bytes, ...] = ()
    origin_x: int = 0
    origin_y: int = 0
    width: int = 0
    height: int = 0


def _mask_rows_for(
    mask: bytearray,
    width: int,
    height: int,
    *,
    index: int,
) -> _MaskRows:
    """The rows of the ``index``-th labelled component, and nothing else.

    Shares :func:`_label_components`' traversal order, which is what it takes to
    address "the region the box describes" and get that region's pixels. The
    mask is cropped to the region's bounding box so a stored mask costs its own
    area rather than a full preview per region.
    """
    seen = 0
    for start in range(len(mask)):
        if not mask[start]:
            continue
        stack: list[int] = [start]
        mask[start] = 0
        pixels: list[int] = []
        while stack:
            current = stack.pop()
            y, x = divmod(current, width)
            pixels.append(current)
            if x > 0 and mask[current - 1]:
                mask[current - 1] = 0
                stack.append(current - 1)
            if x + 1 < width and mask[current + 1]:
                mask[current + 1] = 0
                stack.append(current + 1)
            if y > 0 and mask[current - width]:
                mask[current - width] = 0
                stack.append(current - width)
            if y + 1 < height and mask[current + width]:
                mask[current + width] = 0
                stack.append(current + width)
        if seen < index:
            seen += 1
            continue
        return _crop_mask(pixels, width, height)
    return _MaskRows()


def _crop_mask(pixels: Sequence[int], width: int, height: int) -> _MaskRows:
    """A sparse pixel list as rows cropped to its own bounding box."""
    del height
    if not pixels:
        return _MaskRows()
    xs = [index % width for index in pixels]
    ys = [index // width for index in pixels]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    mask_width = max_x - min_x + 1
    mask_height = max_y - min_y + 1
    rows = [bytearray(mask_width) for _ in range(mask_height)]
    for index in pixels:
        y, x = divmod(index, width)
        rows[y - min_y][x - min_x] = 255
    return _MaskRows(
        rows=tuple(bytes(row) for row in rows),
        origin_x=min_x,
        origin_y=min_y,
        width=mask_width,
        height=mask_height,
    )


def default_segmentation_provider() -> SegmentationProvider:
    """The segmenter this build ships — the classical region masks."""
    return RegionSegmentationProvider()


def provider_status(provider: Any) -> dict[str, Any]:
    """A provider as status output: name, availability, and why not."""
    return {
        "name": str(getattr(provider, "name", "")),
        "available": bool(getattr(provider, "available", False)),
        "reason": str(getattr(provider, "unavailable_reason", "")),
    }

