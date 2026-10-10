"""The perception contracts: what Phase 21 hands to the systems after it.

Everything this package observes becomes one of these records, and every record
follows three rules the rest of the build already holds itself to:

**A figure that was not measured is ``None``, never a zero.** A detector that
does not report a score leaves ``confidence`` unset, and the scene records the
absence rather than a 0.0 a reader would take for a failed detection.
``UNCERTAINTY`` is derived from a confidence that was actually measured.

**Provenance travels with the claim.** Every object, text line, relationship and
temporal event names the provider that produced it, because "there are 3
objects" is only useful next to "…according to the OCR geometry" or "…according
to a vision model".

**No hidden reasoning, no raw pixels.** Nothing here carries chain-of-thought,
and nothing serializes image bytes or masks: a mask is a reference plus a pixel
count, and a frame is a reference to a file plus its metadata. A reader of a
``to_dict()`` sees what was perceived, never the reasoning behind it.

The scene representation is the phase's main output; the abstraction beside it
is a compact, evidence-linked summary of the SAME observation, not a prediction
about what happens next. Prediction is Phase 22's business.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4

__all__ = [
    "BBox",
    "CapabilityState",
    "CapabilityStatus",
    "DetectedObject",
    "OcrText",
    "PerceptionCapability",
    "PerceptionMode",
    "PerceptionRequest",
    "PerceptionResult",
    "PerceptionStatus",
    "Relationship",
    "RelationshipKind",
    "SceneAbstraction",
    "SceneRepresentation",
    "SegmentationResult",
    "TemporalEvent",
    "TemporalEventKind",
    "TrackState",
    "TrackedObject",
]


class PerceptionStatus(StrEnum):
    """How the whole request ended, and each value means something exact.

    ``SUCCESS`` — the capability the request asked for produced evidence.
    ``PARTIAL`` — some capabilities produced evidence and at least one could
    not run; ``status_reason`` names the one that could not.
    ``UNAVAILABLE`` — nothing could answer because no provider for what was
    asked exists on this machine (the honest answer of a build with no VLM).
    ``FAILED`` — the request could not be carried out at all: an unreadable
    frame, a provider that raised, malformed provider output.
    """

    SUCCESS = "success"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class CapabilityState(StrEnum):
    """The classification every Phase 21 capability must carry (§4 of the spec).

    This is a statement about the CAPABILITY as this build ships it — not about
    one request's outcome, which is a ``CapabilityStatus`` below. The two are
    separate on purpose: a mock segmentation provider is ``MOCK`` even on the
    request where it returned masks.
    """

    IMPLEMENTED = "implemented"
    PARTIALLY_IMPLEMENTED = "partially_implemented"
    PROVIDER_DEPENDENT = "provider_dependent"
    MOCK = "mock"
    UNAVAILABLE = "unavailable"
    FUTURE = "future"


class PerceptionCapability(StrEnum):
    """The named capabilities a perception request may ask for."""

    OCR = "ocr"
    DETECTION = "detection"
    SEGMENTATION = "segmentation"
    TRACKING = "tracking"
    RELATIONSHIPS = "relationships"
    TEMPORAL = "temporal"
    ABSTRACTION = "abstraction"
    VLM = "vlm"


class PerceptionMode(StrEnum):
    """Which route a request takes through the pipeline.

    ``AUTO`` is the default and the interesting one: it classifies the question
    and decides whether the fast path can answer it. ``FAST`` and ``DEEP`` are
    explicit overrides for a caller that already knows, and ``HYBRID`` means
    "fast first, then escalate only if the fast evidence does not answer it".
    """

    AUTO = "auto"
    FAST = "fast"
    DEEP = "deep"
    HYBRID = "hybrid"


class RelationshipKind(StrEnum):
    """Spatial relations derived from bounding geometry, and nothing else.

    Each kind is decidable from two boxes: ``INSIDE``/``CONTAINS`` from
    enclosure, ``OVERLAPS`` from intersection area, ``LEFT_OF``/``RIGHT_OF``/
    ``ABOVE``/``BELOW`` from a clear directional gap, and ``NEAR`` from a gap
    under a stated fraction of the larger object. No kind exists that would
    need a model to assert.
    """

    LEFT_OF = "left_of"
    RIGHT_OF = "right_of"
    ABOVE = "above"
    BELOW = "below"
    INSIDE = "inside"
    CONTAINS = "contains"
    OVERLAPS = "overlaps"
    NEAR = "near"


class TrackState(StrEnum):
    """Where one tracked object is in its life cycle.

    ``OCCLUDED`` is a real state rather than a deletion: a detection that
    vanished for one frame is usually something in front of it, and the tracker
    keeps the identity long enough to find out. ``LOST`` is the second strike,
    ``REMOVED`` the third — after which the identity is gone.
    """

    NEW = "new"
    VISIBLE = "visible"
    OCCLUDED = "occluded"
    LOST = "lost"
    REMOVED = "removed"


class TemporalEventKind(StrEnum):
    """What changed between two observations — observed change only.

    Nothing in this vocabulary predicts, plans or infers intent; each member
    is a measurable difference between two scenes the pipeline actually saw.
    """

    OBJECT_APPEARED = "object_appeared"
    OBJECT_DISAPPEARED = "object_disappeared"
    OBJECT_MOVED = "object_moved"
    TEXT_APPEARED = "text_appeared"
    TEXT_CHANGED = "text_changed"
    TEXT_REMOVED = "text_removed"
    SCENE_CHANGED = "scene_changed"
    SCENE_STATIC = "scene_static"
    CONFIDENCE_INCREASED = "confidence_increased"
    CONFIDENCE_DECREASED = "confidence_decreased"


@dataclass(frozen=True, slots=True)
class BBox:
    """An axis-aligned box in the frame's own pixels.

    Integer coordinates because that is what every reader in this build reports
    (Windows OCR words, the vision prompts' bounding boxes) and rounding at the
    boundary would make two claims about the same pixel disagree. Geometry is
    exact and cheap: ``iou``, ``contains``, ``intersects`` and ``gap_to`` are
    the four measurements the relationship layer is built from.
    """

    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)

    @property
    def has_extent(self) -> bool:
        """Whether this box can support a geometric claim at all."""
        return self.width > 0 and self.height > 0

    def intersection(self, other: BBox) -> int:
        """Pixels the two boxes share; 0 when they are disjoint."""
        width = min(self.right, other.right) - max(self.x, other.x)
        height = min(self.bottom, other.bottom) - max(self.y, other.y)
        if width <= 0 or height <= 0:
            return 0
        return width * height

    def intersects(self, other: BBox) -> bool:
        return self.intersection(other) > 0

    def iou(self, other: BBox) -> float:
        """Intersection over union, 0.0 for disjoint boxes and for empty ones."""
        shared = self.intersection(other)
        if not shared:
            return 0.0
        union = self.area + other.area - shared
        return shared / union if union > 0 else 0.0

    def contains(self, other: BBox) -> bool:
        """Whether ``other`` lies wholly within this box (boxes included)."""
        return (
            other.has_extent
            and self.has_extent
            and other.x >= self.x
            and other.y >= self.y
            and other.right <= self.right
            and other.bottom <= self.bottom
        )

    def gap_to(self, other: BBox) -> float:
        """Edge-to-edge distance in pixels; 0.0 when the boxes touch or overlap.

        Measured on both axes independently and then combined, so two boxes in
        the same column but different rows report their vertical gap rather
        than a corner-to-corner diagonal that no relationship rule uses.
        """
        dx = max(self.x - other.right, other.x - self.right, 0)
        dy = max(self.y - other.bottom, other.y - self.bottom, 0)
        return float((dx * dx + dy * dy) ** 0.5) if dx or dy else 0.0

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> BBox:
        """A box from whatever a provider reported, zero-filled for misses."""

        def number(key: str) -> int:
            try:
                return int(float(data.get(key, 0) or 0))
            except (TypeError, ValueError):
                return 0

        return cls(
            x=number("x"),
            y=number("y"),
            width=number("width"),
            height=number("height"),
        )


@dataclass(frozen=True, slots=True)
class DetectedObject:
    """One thing a provider found in one frame — provider-neutral by design.

    ``class_id`` stays ``None`` unless a provider reports one, and ``label`` is
    whatever that provider names the thing (an OCR word, a classical region, a
    VLM's noun). Downstream code must read ``source`` before trusting a label
    with semantics in it: ``source="vlm"`` means "a model said 'laptop'",
    ``source="ocr:windows-ocr"`` means "the reader found this text".
    """

    label: str
    bbox: BBox = field(default_factory=BBox)
    confidence: float | None = None
    class_id: int | None = None
    source: str = ""
    frame_id: str = ""
    timestamp: str = ""
    object_id: str = field(default_factory=lambda: uuid4().hex[:12])
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def located(self) -> bool:
        """Whether this detection can support geometry at all."""
        return self.bbox.has_extent

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "label": self.label,
            "bbox": self.bbox.to_dict(),
            "confidence": self.confidence,
            "class_id": self.class_id,
            "source": self.source,
            "frame_id": self.frame_id,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class OcrText:
    """One recognized text run, with where it was read when the engine knows.

    ``confidence`` and ``language`` are ``None``/empty when the underlying
    engine does not report them — the shipped Windows engine reports neither,
    and inventing a figure here would be the exact fabrication the phase rules
    out. ``reading_order`` is the engine's own index, which is the only order
    that is reproducible.
    """

    text: str
    bbox: BBox | None = None
    confidence: float | None = None
    language: str = ""
    reading_order: int | None = None
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "bbox": self.bbox.to_dict() if self.bbox is not None else None,
            "confidence": self.confidence,
            "language": self.language,
            "reading_order": self.reading_order,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class SegmentationResult:
    """One segmented region, described by a mask REFERENCE rather than pixels.

    A mask that has not been measured is not claimed: ``pixel_count`` is the
    real number of pixels the mask covers, ``mask_id`` names a mask a caller can
    look up from the provider that made it, and the mask bytes themselves never
    enter a result, an event or a log. A bounding box is NOT a mask — this type
    carries both, and only a provider that produced actual pixels may set
    ``mask_id``.
    """

    segment_id: str
    label: str = "region"
    bbox: BBox | None = None
    confidence: float | None = None
    frame_id: str = ""
    provider: str = ""
    mask_id: str = ""
    pixel_count: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def has_mask(self) -> bool:
        return bool(self.mask_id) and self.pixel_count > 0

    def to_dict(self) -> dict[str, Any]:
        """The serialized shape — a mask reference, never mask pixels."""
        return {
            "segment_id": self.segment_id,
            "label": self.label,
            "bbox": self.bbox.to_dict() if self.bbox is not None else None,
            "confidence": self.confidence,
            "frame_id": self.frame_id,
            "provider": self.provider,
            "mask_id": self.mask_id,
            "pixel_count": self.pixel_count,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class Relationship:
    """A spatial relation between two objects, with its evidence.

    ``evidence`` is the arithmetic that decided it ("a.right=100 < b.left=140"),
    which is what makes a relationship reviewable instead of assertable. The
    confidence is the weaker of the two objects' own confidences when both were
    measured — the relation cannot be surer than the boxes it is built from.
    """

    kind: RelationshipKind
    subject_id: str
    object_id: str
    confidence: float | None = None
    provenance: str = "geometry"
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "subject_id": self.subject_id,
            "object_id": self.object_id,
            "confidence": self.confidence,
            "provenance": self.provenance,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class TrackedObject:
    """One object followed across frames by SPATIAL continuity.

    The identity here is continuity of position and size, not recognition: the
    field ``spatial_only`` is ``True`` on every track this build produces, and a
    caller must not read ``track_id`` as "the same laptop" — only as "the thing
    that was here last frame". ``velocity`` is pixels per second when both frame
    timestamps were readable, and ``None`` when they were not.
    """

    track_id: str
    label: str = ""
    bbox: BBox = field(default_factory=BBox)
    previous_bbox: BBox | None = None
    velocity: tuple[float, float] | None = None
    confidence: float | None = None
    first_seen: str = ""
    last_seen: str = ""
    frame_count: int = 1
    missed_frames: int = 0
    state: TrackState = TrackState.NEW
    source: str = ""
    previous_label: str = ""
    spatial_only: bool = True

    @property
    def moved(self) -> bool:
        return self.previous_bbox is not None and self.previous_bbox != self.bbox

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "label": self.label,
            "bbox": self.bbox.to_dict(),
            "previous_bbox": (
                self.previous_bbox.to_dict() if self.previous_bbox is not None else None
            ),
            "velocity": list(self.velocity) if self.velocity is not None else None,
            "confidence": self.confidence,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "frame_count": self.frame_count,
            "missed_frames": self.missed_frames,
            "state": self.state.value,
            "source": self.source,
            "previous_label": self.previous_label,
            "spatial_only": self.spatial_only,
        }


@dataclass(frozen=True, slots=True)
class TemporalEvent:
    """One observed change between two frames — never a prediction.

    The event names the track it is about (so a caller can follow that object
    back to its detections), carries the boxes that changed where a move is
    being reported, and describes the change in ``detail`` as a short measured
    statement ("moved 24px right, 0px down").
    """

    kind: TemporalEventKind
    object_id: str = ""
    label: str = ""
    previous_bbox: BBox | None = None
    current_bbox: BBox | None = None
    detail: str = ""
    timestamp: str = ""
    frame_id: str = ""
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "object_id": self.object_id,
            "label": self.label,
            "previous_bbox": (
                self.previous_bbox.to_dict() if self.previous_bbox is not None else None
            ),
            "current_bbox": (
                self.current_bbox.to_dict() if self.current_bbox is not None else None
            ),
            "detail": self.detail,
            "timestamp": self.timestamp,
            "frame_id": self.frame_id,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class CapabilityStatus:
    """How one capability behaved for ONE request, and why.

    ``available`` is three-valued for the same reason every other fit check in
    this build is: ``True``, ``False``, or ``None`` for "the probe could not
    tell". A capability that never ran because an earlier one satisfied the
    request reports ``available=None`` and its reason says so.
    """

    name: str
    state: CapabilityState
    available: bool | None = None
    provider: str = ""
    reason: str = ""
    latency_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "available": self.available,
            "provider": self.provider,
            "reason": self.reason,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True, slots=True)
class SceneAbstraction:
    """The compact, evidence-linked summary of one scene — Phase 21's top layer.

    Every field is derived from the scene it describes: ``counts`` counts the
    objects that were actually detected, ``highlights`` quotes arithmetic or
    text that was actually read, and ``provenance`` names the providers. There
    is no field for what the scene MEANS, because meaning is a model's
    inference and this layer must not manufacture one; ``basis`` says whether
    the summary came from the deterministic fast path or from fast + VLM.
    """

    summary: str = ""
    counts: Mapping[str, int] = field(default_factory=dict)
    highlights: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()
    basis: str = "deterministic"
    confidence: float | None = None
    uncertainty: float | None = None
    text_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "counts": dict(self.counts),
            "highlights": list(self.highlights),
            "provenance": list(self.provenance),
            "basis": self.basis,
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
            "text_excerpt": self.text_excerpt,
        }


@dataclass(frozen=True, slots=True)
class SceneRepresentation:
    """Everything perceived about one frame, in one serializable record.

    This is the phase's main output. It holds objects, text, relationships,
    tracks, segmentation references, the temporal events the frame produced and
    the abstraction built from them — plus the confidence that was actually
    measured and the providers that contributed. It carries no pixels, no
    masks and no reasoning, so it can be stored, published on the event bus and
    handed to a later phase without leaking an image or a thought.
    """

    scene_id: str = field(default_factory=lambda: uuid4().hex[:12])
    timestamp: str = ""
    frame_id: str = ""
    source_id: str = ""
    sequence: int = 0
    width: int | None = None
    height: int | None = None
    objects: tuple[DetectedObject, ...] = ()
    text: tuple[OcrText, ...] = ()
    relationships: tuple[Relationship, ...] = ()
    tracks: tuple[TrackedObject, ...] = ()
    segmentation: tuple[SegmentationResult, ...] = ()
    temporal_events: tuple[TemporalEvent, ...] = ()
    focus: Mapping[str, Any] = field(default_factory=dict)
    confidence: float | None = None
    uncertainty: float | None = None
    providers: Mapping[str, str] = field(default_factory=dict)
    capability_states: Mapping[str, str] = field(default_factory=dict)
    abstraction: SceneAbstraction | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def text_block(self) -> str:
        """The recognized text as one block, in reading order."""
        return "\n".join(item.text for item in self.text if item.text.strip())

    @property
    def object_count(self) -> int:
        return len(self.objects)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "timestamp": self.timestamp,
            "frame_id": self.frame_id,
            "source_id": self.source_id,
            "sequence": self.sequence,
            "width": self.width,
            "height": self.height,
            "objects": [item.to_dict() for item in self.objects],
            "text": [item.to_dict() for item in self.text],
            "relationships": [item.to_dict() for item in self.relationships],
            "tracks": [item.to_dict() for item in self.tracks],
            "segmentation": [item.to_dict() for item in self.segmentation],
            "temporal_events": [item.to_dict() for item in self.temporal_events],
            "focus": dict(self.focus),
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
            "providers": dict(self.providers),
            "capability_states": dict(self.capability_states),
            "abstraction": self.abstraction.to_dict() if self.abstraction else None,
            "metadata": dict(self.metadata),
        }


def as_bool(value: Any, default: bool) -> bool:
    """A boolean from whatever a caller sent — the shared coercion, not a copy.

    API bodies arrive as JSON (real booleans) and CLI/config values arrive as
    strings, so both are read here rather than at each call site.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def as_optional_int(value: Any) -> int | None:
    """An integer or nothing — never a fabricated zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_optional_float(value: Any) -> float | None:
    """A real or nothing, clamped to a probability when one is asked for."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


@dataclass(frozen=True, slots=True)
class PerceptionRequest:
    """One perception request, before anything has been looked at.

    Deliberately not a second vision request: Phase 6's ``VisionRequest`` is one
    question about one image and stays the door for that. This is the wider
    contract a stream, a frame and a multi-frame observation need — which
    capabilities may run, which route to take, whether temporal context is
    wanted — and it is reduced to a ``VisionRequest`` when the deep path is
    used, so there is still exactly one way a model is asked about an image.
    """

    source: str = ""
    question: str = ""
    target: str = ""
    source_kind: str = "image"
    mode: PerceptionMode = PerceptionMode.AUTO
    max_latency_ms: int | None = None
    min_confidence: float | None = None
    temporal_context: bool = False
    resource_budget: Mapping[str, Any] = field(default_factory=dict)
    allow_ocr: bool = True
    allow_detection: bool = True
    allow_segmentation: bool = False
    allow_vlm: bool = True
    max_objects: int | None = None

    @property
    def asked(self) -> str:
        """The question, normalized the way the vision layer normalizes one."""
        return " ".join(str(self.question or "").split())

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "question": self.question,
            "target": self.target,
            "source_kind": self.source_kind,
            "mode": self.mode.value,
            "max_latency_ms": self.max_latency_ms,
            "min_confidence": self.min_confidence,
            "temporal_context": self.temporal_context,
            "resource_budget": dict(self.resource_budget),
            "allow_ocr": self.allow_ocr,
            "allow_detection": self.allow_detection,
            "allow_segmentation": self.allow_segmentation,
            "allow_vlm": self.allow_vlm,
            "max_objects": self.max_objects,
        }

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any], *, defaults: PerceptionRequest | None = None
    ) -> PerceptionRequest:
        """A request from an API body or a configuration mapping.

        An unknown mode is NOT silently accepted as something else: it falls
        back to ``AUTO``, which is the mode that decides by reading the
        question, because that is the behaviour a caller who sent no valid mode
        is least surprised by.
        """
        base = defaults or cls()
        mode = str(data.get("mode", base.mode.value) or base.mode.value).strip().lower()
        try:
            resolved_mode = PerceptionMode(mode)
        except ValueError:
            resolved_mode = base.mode
        budget = data.get("resource_budget", base.resource_budget)
        return cls(
            source=str(data.get("source", base.source) or "").strip(),
            question=str(data.get("question", base.question) or "").strip(),
            target=str(data.get("target", base.target) or "").strip(),
            source_kind=str(data.get("source_kind", base.source_kind) or base.source_kind)
            .strip()
            .lower(),
            mode=resolved_mode,
            max_latency_ms=as_optional_int(data.get("max_latency_ms", base.max_latency_ms)),
            min_confidence=as_optional_float(
                data.get("min_confidence", base.min_confidence)
            ),
            temporal_context=as_bool(
                data.get("temporal_context", base.temporal_context), base.temporal_context
            ),
            resource_budget=dict(budget) if isinstance(
                budget, Mapping) else dict(base.resource_budget),
            allow_ocr=as_bool(data.get("allow_ocr", base.allow_ocr), base.allow_ocr),
            allow_detection=as_bool(
                data.get("allow_detection", base.allow_detection), base.allow_detection
            ),
            allow_segmentation=as_bool(
                data.get("allow_segmentation", base.allow_segmentation),
                base.allow_segmentation,
            ),
            allow_vlm=as_bool(data.get("allow_vlm", base.allow_vlm), base.allow_vlm),
            max_objects=as_optional_int(data.get("max_objects", base.max_objects)),
        )


@dataclass(frozen=True, slots=True)
class PerceptionPlan:
    """What the router decided this request needs, and why it decided it.

    ``fast`` names the capabilities to try in order; ``deep`` says whether a
    vision model is part of the plan at all. ``reasons`` carries the reading
    that produced the plan ("the question asks for text"), so a status surface
    can explain a routing decision instead of asserting it.
    """

    mode: PerceptionMode
    fast: tuple[PerceptionCapability, ...] = ()
    deep: bool = False
    reasons: tuple[str, ...] = ()
    escalate_when_fast_insufficient: bool = True
    deep_question: str = ""
    unavailable_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "fast": [item.value for item in self.fast],
            "deep": self.deep,
            "reasons": list(self.reasons),
            "escalate_when_fast_insufficient": self.escalate_when_fast_insufficient,
            "deep_question": self.deep_question,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True, slots=True)
class PerceptionResult:
    """The whole answer of one perception request — the phase's outward contract.

    ``status`` distinguishes SUCCESS from PARTIAL from UNAVAILABLE from FAILED,
    and ``status_reason`` names the capability that decided it. The scene and
    the abstraction are the payload; ``latency`` holds the per-stage timings
    that were actually measured (a stage that did not run has no key rather
    than a zero), and ``capabilities`` records what each capability did.
    """

    status: PerceptionStatus = PerceptionStatus.FAILED
    status_reason: str = ""
    request: PerceptionRequest = field(default_factory=PerceptionRequest)
    plan: PerceptionPlan | None = None
    scene: SceneRepresentation | None = None
    confidence: float | None = None
    uncertainty: float | None = None
    escalated: bool = False
    fast_sufficient: bool | None = None
    fast_reason: str = ""
    providers: Mapping[str, str] = field(default_factory=dict)
    capabilities: tuple[CapabilityStatus, ...] = ()
    latency: Mapping[str, float] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    summary: str = ""
    telemetry: Mapping[str, Any] = field(default_factory=dict)

    @property
    def objects(self) -> tuple[DetectedObject, ...]:
        return self.scene.objects if self.scene is not None else ()

    @property
    def text(self) -> tuple[OcrText, ...]:
        return self.scene.text if self.scene is not None else ()

    @property
    def relationships(self) -> tuple[Relationship, ...]:
        return self.scene.relationships if self.scene is not None else ()

    @property
    def tracks(self) -> tuple[TrackedObject, ...]:
        return self.scene.tracks if self.scene is not None else ()

    @property
    def temporal_events(self) -> tuple[TemporalEvent, ...]:
        return self.scene.temporal_events if self.scene is not None else ()

    @property
    def abstraction(self) -> SceneAbstraction | None:
        return self.scene.abstraction if self.scene is not None else None

    @property
    def text_block(self) -> str:
        return self.scene.text_block if self.scene is not None else ""

    def capability(self, name: PerceptionCapability | str) -> CapabilityStatus | None:
        """One capability's row, or nothing when it was not part of the plan."""
        wanted = name.value if isinstance(name, PerceptionCapability) else str(name)
        for row in self.capabilities:
            if row.name == wanted:
                return row
        return None

    def to_dict(self) -> dict[str, Any]:
        """The wire shape. Pixels and masks are structurally absent."""
        return {
            "status": self.status.value,
            "status_reason": self.status_reason,
            "summary": self.summary,
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
            "escalated": self.escalated,
            "fast_sufficient": self.fast_sufficient,
            "fast_reason": self.fast_reason,
            "request": self.request.to_dict(),
            "plan": self.plan.to_dict() if self.plan is not None else None,
            "scene": self.scene.to_dict() if self.scene is not None else None,
            "providers": dict(self.providers),
            "capabilities": [row.to_dict() for row in self.capabilities],
            "latency": dict(self.latency),
            "errors": list(self.errors),
            "telemetry": dict(self.telemetry),
        }
