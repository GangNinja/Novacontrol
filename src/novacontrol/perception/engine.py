"""The perception engine: frames in, structured scenes out, nothing else.

One request travels this path, and each stage is the layer that owns it::

    frame source -> validate -> preprocess -> route
                                              |
                    +-------------------------+--------------------------+
                    |                                                    |
             FAST (OCR / regions / masks)                    DEEP (vision model)
                    |                                                    |
                    +-------------------------+--------------------------+
                                              |
                          spatial relationships -> tracking -> temporal events
                                              |
                              scene representation -> abstraction
                                              |
                                  confidence / uncertainty -> result

Three behaviours are the point of the design, and each is a rule rather than a
tendency.

**The fast path is always tried first.** OCR and the classical region pass are
deterministic and cheap; they run before any model is considered, and for a text
or object question they usually ARE the answer. Escalation happens after them and
only when the router's plan asked for it and the fast evidence did not suffice.

**The model is asked through Phase 6, not beside it.** Escalation builds a
``VisionRequest`` and calls the ``VisionManager`` the application already owns, so
there is one place a model is asked about an image, one OCR/read-first policy,
and one structured result shape to map. Nothing here talks to a provider.

**A refusal is a result, and a partial answer says so.** No vision model, a
provider that raised, an unreadable frame, a capacity that was switched off —
each produces a status that names the capability responsible. Perception never
returns SUCCESS because *something* happened: SUCCESS means the capability the
request needed produced evidence.

Nothing here executes a desktop action or holds a permission. This layer
OBSERVES; acting on what it sees is another layer's decision, and the approval
gate stands where it always has — in front of the action, not in front of the
looking.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from novacontrol.perception.capabilities import capability_rows
from novacontrol.perception.frames import (
    CameraFrameSource,
    Frame,
    FrameSource,
    FrameSourceError,
    ImageFrameSource,
    ScreenFrameSource,
    SequenceFrameSource,
    monotonic_ms,
    source_is_available,
    source_kind_of,
)
from novacontrol.perception.governance import (
    AdmissionDecision,
    PerceptionProfile,
    PerceptionResourceGate,
    max_objects_for,
    preprocess_for,
    sampling_for,
)
from novacontrol.perception.models import (
    BBox,
    CapabilityState,
    CapabilityStatus,
    DetectedObject,
    OcrText,
    PerceptionCapability,
    PerceptionMode,
    PerceptionPlan,
    PerceptionRequest,
    PerceptionResult,
    PerceptionStatus,
    SceneRepresentation,
    SegmentationResult,
    TemporalEventKind,
)
from novacontrol.perception.preprocessing import FrameUnreadable, PreparedFrame, prepare_frame
from novacontrol.perception.providers import (
    default_detection_provider,
    default_segmentation_provider,
)
from novacontrol.perception.routing import PerceptionRouter, describe_question
from novacontrol.perception.sampling import FrameSampler
from novacontrol.perception.scene import abstraction_for, build_scene, focus_for
from novacontrol.perception.spatial import derive_relationships
from novacontrol.perception.temporal import TemporalPerception
from novacontrol.perception.tracking import SpatialTracker
from novacontrol.vision.manager import VisionManager, task_for_question
from novacontrol.vision.models import VisionRequest, VisionResult
from novacontrol.vision.ocr import OcrEngine, OcrWord, default_ocr_engine
from novacontrol.vision.providers import VisionProviderError

#: The event vocabulary this engine publishes through its observer seam.
PERCEPTION_STARTED = "perception.started"
PERCEPTION_COMPLETED = "perception.completed"
PERCEPTION_FAILED = "perception.failed"
FRAME_RECEIVED = "perception.frame"
SCENE_CHANGED = "perception.scene_changed"
OBJECT_APPEARED = "perception.object_appeared"
OBJECT_DISAPPEARED = "perception.object_disappeared"
OBJECT_MOVED = "perception.object_moved"

#: A stream is bounded, always. A caller asking for temporal context over a
#: hundred frames gets the last ``MAX_STREAM_FRAMES`` of them plus a note, because
#: an unbounded loop over a live screen is a runaway.
MAX_STREAM_FRAMES = 12

#: The source kinds that keep producing frames until they are stopped. Everything
#: else has a definite extent — and a still image has exactly one frame in it.
_LIVE_SOURCE_KINDS = frozenset({"screen", "camera"})


def _walk_length(source: FrameSource) -> int:
    """How many frames to try to read from a source, honestly.

    A declared extent wins (a two-frame list is a two-frame walk); a live source
    is bounded by the ceiling; and anything else is a STILL — one frame — because
    reading the same file twelve times is not perception, it is a loop.
    """
    remaining = getattr(source, "remaining", None)
    if isinstance(remaining, int):
        return max(0, min(MAX_STREAM_FRAMES, remaining))
    if str(getattr(source, "kind", "")) in _LIVE_SOURCE_KINDS:
        return MAX_STREAM_FRAMES
    return 1

#: The observer seam: a callable that is handed (event_type, payload). It may be
#: async; the engine awaits it when it is, and a failing observer never fails a
#: request.
PerceptionObserver = Callable[[str, Mapping[str, Any]], Any]

__all__ = [
    "FRAME_RECEIVED",
    "MAX_STREAM_FRAMES",
    "OBJECT_APPEARED",
    "OBJECT_DISAPPEARED",
    "OBJECT_MOVED",
    "PERCEPTION_COMPLETED",
    "PERCEPTION_FAILED",
    "PERCEPTION_STARTED",
    "SCENE_CHANGED",
    "PerceptionEngine",
    "PerceptionObserver",
    "PerceptionTelemetry",
]

@dataclass(slots=True)
class PerceptionTelemetry:
    """The counts a status surface needs, and none of the content.

    Deliberately counters only: no frame paths, no text, no scene payloads. A
    telemetry record here can be logged or shipped without carrying anything a
    person saw.
    """

    requests: int = 0
    succeeded: int = 0
    partial: int = 0
    unavailable: int = 0
    failed: int = 0
    escalations: int = 0
    escalation_denied: int = 0
    provider_failures: int = 0
    observer_failures: int = 0
    frames_acquired: int = 0
    frames_analysed: int = 0
    frames_skipped: int = 0
    objects_seen: int = 0
    text_lines_seen: int = 0

    def record_status(self, status: PerceptionStatus) -> None:
        if status is PerceptionStatus.SUCCESS:
            self.succeeded += 1
        elif status is PerceptionStatus.PARTIAL:
            self.partial += 1
        elif status is PerceptionStatus.UNAVAILABLE:
            self.unavailable += 1
        else:
            self.failed += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "succeeded": self.succeeded,
            "partial": self.partial,
            "unavailable": self.unavailable,
            "failed": self.failed,
            "escalations": self.escalations,
            "escalation_denied": self.escalation_denied,
            "provider_failures": self.provider_failures,
            "observer_failures": self.observer_failures,
            "frames_acquired": self.frames_acquired,
            "frames_analysed": self.frames_analysed,
            "frames_skipped": self.frames_skipped,
            "objects_seen": self.objects_seen,
            "text_lines_seen": self.text_lines_seen,
        }


@dataclass(frozen=True, slots=True)
class _CapabilityOutcome:
    """What one capability did for one request, before it becomes a public row."""

    name: PerceptionCapability
    state: CapabilityState
    available: bool | None = None
    provider: str = ""
    reason: str = ""
    latency_ms: float | None = None

    def to_status(self) -> CapabilityStatus:
        return CapabilityStatus(
            name=self.name.value,
            state=self.state,
            available=self.available,
            provider=self.provider,
            reason=self.reason,
            latency_ms=self.latency_ms,
        )


@dataclass(frozen=True, slots=True)
class _VlmProbe:
    """The deep path as a probeable provider: a name, a model, and a verdict."""

    name: str = "none"
    model: str = ""
    available: bool = False

    @property
    def unavailable_reason(self) -> str:
        return "" if self.available else "no vision model is wired"


@dataclass(frozen=True, slots=True)
class _Collected:
    """What a source gave up: frames, or the reason it gave none."""

    frames: tuple[Frame, ...] = ()
    error: str = ""


class _FrameListSource:
    """An explicitly supplied stream, read like any other source.

    Presenting a caller's frames through the same ``FrameSource`` protocol is
    what lets the temporal case be the ordinary case: the engine reads one frame
    at a time from whatever it was given, and there is no second code path that
    knows about lists of frames.
    """

    kind = "supplied"

    def __init__(self, frames: Sequence[Frame], *, source_id: str = "supplied") -> None:
        self._frames = tuple(frames)
        self._source_id = source_id
        self._index = 0

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def available(self) -> bool:
        return True

    @property
    def unavailable_reason(self) -> str:
        return ""

    @property
    def remaining(self) -> int:
        """How many frames are still to come — so the walk stops when THEY do."""
        return max(0, len(self._frames) - self._index)

    async def read(self) -> Frame | None:
        if self._index >= len(self._frames):
            return None
        frame = self._frames[self._index]
        self._index += 1
        return frame

    async def close(self) -> None:
        self._index = len(self._frames)


def function_of(source_kind: str) -> str:
    """The capability a source KIND implies, for naming the responsible one.

    An unusable camera and an unreadable image are different failures with
    different owners, and the result has to be able to name which one it was.
    """
    if source_kind == "camera":
        return "camera-frame"
    if source_kind == "screen":
        return "screen-frame"
    return "image-frame"


@dataclass(frozen=True, slots=True)
class _FrameOutcome:
    """Everything one frame's pass produced, before the walk merges it."""

    plan: PerceptionPlan | None = None
    scene: SceneRepresentation | None = None
    error: str = ""
    escalated: bool = False
    fast_sufficient: bool | None = None
    fast_reason: str = ""
    latency: Mapping[str, float] = field(default_factory=dict)
    outcomes: tuple[_CapabilityOutcome, ...] = ()
    providers: Mapping[str, str] = field(default_factory=dict)
    events: tuple[Any, ...] = ()
    #: Capabilities that were ASKED for and could not answer, while the frame
    #: itself was perceived. These degrade a verdict to PARTIAL: a request that
    #: wanted a description and got a failed model has not succeeded in full.
    failures: tuple[str, ...] = ()
    #: The sampler said this frame adds nothing, so no analysis was spent on it.
    #: The frame was still acquired, and the status surface still counts it.
    skipped: bool = False


class PerceptionEngine:
    """The pipeline. Every collaborator is injected, and every default is honest.

    The defaults matter as much as the arguments: with nothing passed, the engine
    still has a real OCR chain, a real classical detector and segmenter, a real
    tracker, and a ``NullVisionProvider`` behind the Phase 6 manager — a working
    fast path and an honest "no model" on the deep one. A test therefore drives
    the REAL pipeline with real providers and synthetic images, rather than a
    mock of it.
    """

    def __init__(
        self,
        *,
        vision: VisionManager | None = None,
        ocr: OcrEngine | None = None,
        detector: Any | None = None,
        segmenter: Any | None = None,
        tracker: SpatialTracker | None = None,
        sampler: FrameSampler | None = None,
        temporal: TemporalPerception | None = None,
        gate: PerceptionResourceGate | None = None,
        router: PerceptionRouter | None = None,
        observer: PerceptionObserver | None = None,
        profile: str = PerceptionProfile.BALANCED,
        max_objects: int | None = None,
        screen_source: FrameSource | None = None,
        camera_source: FrameSource | None = None,
    ) -> None:
        self.profile = PerceptionProfile.resolve(profile)
        self.vision = vision or VisionManager()
        self.ocr = ocr or getattr(self.vision, "ocr", None) or default_ocr_engine()
        self.detector = detector if detector is not None else default_detection_provider(self.ocr)
        self.segmenter = segmenter if segmenter is not None else default_segmentation_provider()
        self.tracker = tracker or SpatialTracker()
        self.sampler = sampler or FrameSampler(sampling_for(self.profile))
        self.temporal = temporal or TemporalPerception()
        self.gate = gate or PerceptionResourceGate(profile=self.profile)
        self.router = router or PerceptionRouter()
        self.observer = observer
        self.max_objects = max(1, int(max_objects or max_objects_for(self.profile)))
        self.screen_source = screen_source or ScreenFrameSource()
        self.camera_source = camera_source or CameraFrameSource()
        self.telemetry = PerceptionTelemetry()
        self._last_scene: SceneRepresentation | None = None
        self._last_frame: Frame | None = None
        self._preprocess = preprocess_for(self.profile)

    # ── wiring ───────────────────────────────────────────────────────────
    def set_observer(self, observer: PerceptionObserver | None) -> None:
        """Install the event seam — the one place this engine announces anything."""
        self.observer = observer

    def set_profile(self, profile: str) -> None:
        """Change the budget: sampling, preview size and object ceiling follow.

        The gate is rebuilt rather than mutated so the profile a decision was
        taken under travels with the decision.
        """
        self.profile = PerceptionProfile.resolve(profile)
        self.sampler.policy = sampling_for(self.profile)
        self._preprocess = preprocess_for(self.profile)
        self.max_objects = max_objects_for(self.profile)
        self.gate = self.gate.with_profile(self.profile)

    @property
    def last_scene(self) -> SceneRepresentation | None:
        """The most recent scene, for a follow-up against what was seen."""
        return self._last_scene

    @property
    def vlm_available(self) -> bool:
        """Whether a real vision model is wired — asked, never assumed."""
        provider = getattr(self.vision, "provider", None)
        return bool(getattr(provider, "available", False))

    @property
    def vlm_name(self) -> str:
        provider = getattr(self.vision, "provider", None)
        return str(getattr(provider, "name", "") or "none")

    # The provider slots the capability table probes by name. They are properties
    # rather than public attributes so the table and the engine cannot drift: a
    # renamed field here would make a capability report UNKNOWN, which is exactly
    # the honest outcome of a probe that cannot find its provider.
    @property
    def detection(self) -> Any:
        return self.detector

    @property
    def segmentation(self) -> Any:
        return self.segmenter

    @property
    def vlm(self) -> Any:
        """The deep path as something a probe can ask about."""
        return _VlmProbe(
            name=self.vlm_name,
            model=self._vlm_model(),
            available=self.vlm_available,
        )

    # ── the walk ─────────────────────────────────────────────────────────
    async def _run(
        self,
        request: PerceptionRequest,
        frames: tuple[Frame, ...],
        *,
        kind: str,
    ) -> PerceptionResult:
        """Walk the frames, merging what each pass produced.

        The tracker, the temporal layer and the sampler live on the ENGINE, so
        state carries across the walk by construction. The last frame's scene is
        the result's scene, and the capability outcomes are the union of the
        walk: "OCR worked on frame 2 and failed on frame 3" is a partial answer
        worth keeping rather than a detail the merge may drop.
        """
        outcomes: dict[str, _CapabilityOutcome] = {}
        latency: dict[str, float] = {}
        errors: list[str] = []
        providers: dict[str, str] = {}
        plan: PerceptionPlan | None = None
        scene: SceneRepresentation | None = None
        escalated = False
        sufficient: bool | None = None
        sufficient_reason = ""
        # Cross-frame state is opt-in and scoped to ONE observation. A request
        # that does not ask for temporal context is a fresh look: inheriting the
        # previous request's live tracks would report disappearances for objects
        # that were never in this frame's world, and inheriting its sampling
        # baseline would skip the first frame of a new picture as "unchanged".
        if not request.temporal_context:
            self.tracker.reset()
            self.temporal.reset()
            self.sampler.reset()
        for index, frame in enumerate(frames):
            await self._emit(FRAME_RECEIVED, frame.event_payload())
            self.telemetry.frames_acquired += 1
            outcome = await self._process_frame(request, frame, kind=kind)
            if outcome.skipped:
                self.telemetry.frames_skipped += 1
                continue
            self.telemetry.frames_analysed += 1
            for row in outcome.outcomes:
                outcomes[row.name.value] = row
                # Per-stage timing, taken from the capability that spent it: a
                # stage that did not run has no key rather than a zero, and the
                # cost of the reader is never folded into "preprocess".
                if row.latency_ms is not None:
                    latency[row.name.value] = row.latency_ms
            latency.update(outcome.latency)
            providers.update(outcome.providers)
            if outcome.plan is not None:
                plan = outcome.plan
            if outcome.scene is not None:
                scene = outcome.scene
            if outcome.fast_sufficient is not None:
                sufficient = outcome.fast_sufficient
                sufficient_reason = outcome.fast_reason
            for failure in outcome.failures:
                errors.append(failure)
            if outcome.error:
                errors.append(outcome.error)
                if scene is None and index == len(frames) - 1:
                    return self._result(
                        request,
                        plan=plan,
                        status=PerceptionStatus.FAILED,
                        status_reason=outcome.error,
                        errors=tuple(errors),
                        outcomes=tuple(outcomes.values()),
                        latency=latency,
                        providers=providers,
                    )
        return self._result(
            request,
            plan=plan,
            scene=scene,
            status=self._decide_status(scene, outcomes, errors=tuple(errors)),
            status_reason=self._status_reason(scene, outcomes, errors=tuple(errors)),
            escalated=escalated or bool(scene is not None and scene.metadata.get("escalated")),
            fast_sufficient=sufficient,
            fast_reason=sufficient_reason,
            errors=tuple(errors),
            outcomes=tuple(outcomes.values()),
            latency=latency,
            providers=providers,
        )

    # ── one frame ────────────────────────────────────────────────────────
    async def _process_frame(
        self, request: PerceptionRequest, frame: Frame, *, kind: str
    ) -> _FrameOutcome:
        """The whole pipeline for one frame, in the order the layers depend on."""
        latency: dict[str, float] = {}
        outcomes: list[_CapabilityOutcome] = []
        providers: dict[str, str] = {"preprocess": "luminance-preview"}
        started = monotonic_ms()
        try:
            prepared = prepare_frame(frame, self._preprocess)
        except FrameUnreadable as exc:
            latency["preprocess"] = monotonic_ms() - started
            return _FrameOutcome(error=exc.reason, latency=latency)
        latency["preprocess"] = monotonic_ms() - started
        decision = self.sampler.decide(prepared.preview)
        if not decision.process:
            # The sampler said this frame adds nothing (the scene has not changed
            # and the next look is not due yet). The frame was still ACQUIRED —
            # the event says so, and the decode was paid for — while the analysis
            # is what sampling exists to save. Analysing it anyway would make the
            # policy decorative.
            return _FrameOutcome(latency=latency, skipped=True)
        plan = self._plan(request)
        objects, text, segmentation, fast_outcomes, failures = await self._fast(
            request, frame, prepared, plan
        )
        outcomes.extend(fast_outcomes)
        providers.update({row.name.value: row.provider for row in fast_outcomes if row.provider})
        sufficient, sufficient_reason = _fast_sufficiency(request, objects, text)
        escalated = False
        if plan.deep and (request.mode is PerceptionMode.DEEP or not sufficient):
            escalated, escalation_outcome, extra_objects, extra_text = await self._escalate(
                request, frame, plan, fast_ms=sum(latency.values())
            )
            outcomes.append(escalation_outcome)
            if escalated:
                objects.extend(extra_objects)
                text = _merge_text(text, extra_text)
                providers["vlm"] = escalation_outcome.provider or self.vlm_name
            elif escalation_outcome.reason:
                # The deep path was ASKED for and did not answer. That is a
                # limitation of this observation, not a silent success: it is
                # recorded so the verdict degrades to PARTIAL and the reason
                # travels with the result.
                self._record_deep_failure(escalation_outcome, failures)
        return await self._assemble(
            request,
            frame,
            plan,
            prepared=prepared,
            objects=objects,
            text=text,
            segmentation=segmentation,
            outcomes=outcomes,
            providers=providers,
            latency=latency,
            decision=decision,
            sufficient=sufficient,
            sufficient_reason=sufficient_reason,
            escalated=escalated,
            failures=failures,
            source_kind=kind,
        )

    def _record_deep_failure(self, outcome: _CapabilityOutcome, failures: list[str]) -> None:
        """Note a deep path that was asked for and could not answer."""
        if outcome.reason:
            failures.append(f"deep path ({outcome.provider or 'vlm'}): {outcome.reason}")

    def _plan(self, request: PerceptionRequest) -> PerceptionPlan:
        """The router's plan, with what this machine can actually run."""
        return self.router.plan(
            request,
            vlm_available=self.vlm_available,
            ocr_available=bool(getattr(self.ocr, "available", False)),
            detection_available=bool(getattr(self.detector, "available", False)),
            segmentation_available=bool(getattr(self.segmenter, "available", False)),
        )

    async def _fast(
        self,
        request: PerceptionRequest,
        frame: Frame,
        prepared: PreparedFrame,
        plan: PerceptionPlan,
    ) -> tuple[
        list[DetectedObject],
        list[OcrText],
        list[SegmentationResult],
        list[_CapabilityOutcome],
        list[str],
    ]:
        """The cheap capabilities, each one's outcome recorded whatever happens.

        A provider that raises is turned into a FAILED outcome and a recorded
        failure, never into an exception out of ``perceive``: one broken reader
        must not take the whole observation with it.
        """
        objects: list[DetectedObject] = []
        text: list[OcrText] = []
        segmentation: list[SegmentationResult] = []
        outcomes: list[_CapabilityOutcome] = []
        failures: list[str] = []
        if request.allow_ocr and PerceptionCapability.OCR in plan.fast:
            started = monotonic_ms()
            provider = f"ocr:{getattr(self.ocr, 'name', 'none')}"
            try:
                words = await self.ocr.read(frame.path)
                text = _text_lines(words, source=provider, frame=frame)
                outcomes.append(
                    _CapabilityOutcome(
                        name=PerceptionCapability.OCR,
                        state=CapabilityState.IMPLEMENTED,
                        available=True,
                        provider=provider,
                        reason="" if text else "the OCR engine read no text",
                        latency_ms=monotonic_ms() - started,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - classified, then reported
                self.telemetry.provider_failures += 1
                failures.append(f"ocr failed: {type(exc).__name__}")
                outcomes.append(
                    _CapabilityOutcome(
                        name=PerceptionCapability.OCR,
                        state=CapabilityState.IMPLEMENTED,
                        available=False,
                        provider=provider,
                        reason=f"the OCR engine failed: {type(exc).__name__}",
                        latency_ms=monotonic_ms() - started,
                    )
                )
        if request.allow_detection and PerceptionCapability.DETECTION in plan.fast:
            started = monotonic_ms()
            provider = str(getattr(self.detector, "name", "none"))
            available = bool(getattr(self.detector, "available", False))
            if available:
                try:
                    objects = list(await self.detector.detect(prepared))
                    outcomes.append(
                        _CapabilityOutcome(
                            name=PerceptionCapability.DETECTION,
                            state=CapabilityState.IMPLEMENTED,
                            available=True,
                            provider=provider,
                            reason="" if objects else "the detector located nothing",
                            latency_ms=monotonic_ms() - started,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - classified, then reported
                    self.telemetry.provider_failures += 1
                    failures.append(f"detection failed: {_reason_of(exc)}")
                    outcomes.append(
                        _CapabilityOutcome(
                            name=PerceptionCapability.DETECTION,
                            state=CapabilityState.IMPLEMENTED,
                            available=False,
                            provider=provider,
                            reason=_reason_of(exc),
                            latency_ms=monotonic_ms() - started,
                        )
                    )
            else:
                outcomes.append(
                    _CapabilityOutcome(
                        name=PerceptionCapability.DETECTION,
                        state=CapabilityState.UNAVAILABLE,
                        available=False,
                        provider=provider,
                        reason=str(getattr(self.detector, "unavailable_reason", ""))
                        or "no detector is available",
                        latency_ms=monotonic_ms() - started,
                    )
                )
        if request.allow_segmentation and PerceptionCapability.SEGMENTATION in plan.fast:
            started = monotonic_ms()
            provider = str(getattr(self.segmenter, "name", "none"))
            available = bool(getattr(self.segmenter, "available", False))
            if available:
                try:
                    segmentation = list(await self.segmenter.segment(prepared))
                    outcomes.append(
                        _CapabilityOutcome(
                            name=PerceptionCapability.SEGMENTATION,
                            state=CapabilityState.IMPLEMENTED,
                            available=True,
                            provider=provider,
                            reason="" if segmentation else "no regions were segmented",
                            latency_ms=monotonic_ms() - started,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - classified, then reported
                    self.telemetry.provider_failures += 1
                    failures.append(f"segmentation failed: {_reason_of(exc)}")
                    outcomes.append(
                        _CapabilityOutcome(
                            name=PerceptionCapability.SEGMENTATION,
                            state=CapabilityState.IMPLEMENTED,
                            available=False,
                            provider=provider,
                            reason=_reason_of(exc),
                            latency_ms=monotonic_ms() - started,
                        )
                    )
            else:
                outcomes.append(
                    _CapabilityOutcome(
                        name=PerceptionCapability.SEGMENTATION,
                        state=CapabilityState.UNAVAILABLE,
                        available=False,
                        provider=provider,
                        reason=str(getattr(self.segmenter, "unavailable_reason", ""))
                        or "no segmentation provider is available",
                        latency_ms=monotonic_ms() - started,
                    )
                )
        return objects, text, segmentation, outcomes, failures

    # ── the result ───────────────────────────────────────────────────────
    def _result(
        self,
        request: PerceptionRequest,
        *,
        plan: PerceptionPlan | None = None,
        scene: SceneRepresentation | None = None,
        status: PerceptionStatus = PerceptionStatus.FAILED,
        status_reason: str = "",
        escalated: bool = False,
        fast_sufficient: bool | None = None,
        fast_reason: str = "",
        errors: tuple[str, ...] = (),
        outcomes: tuple[_CapabilityOutcome, ...] = (),
        latency: Mapping[str, float] | None = None,
        providers: Mapping[str, str] | None = None,
    ) -> PerceptionResult:
        """Assemble the outward result, with the summary a renderer reads first."""
        rows = _ordered_outcomes(outcomes)
        return PerceptionResult(
            status=status,
            status_reason=status_reason,
            request=request,
            plan=plan,
            scene=scene,
            confidence=scene.confidence if scene is not None else None,
            uncertainty=scene.uncertainty if scene is not None else None,
            escalated=escalated,
            fast_sufficient=fast_sufficient,
            fast_reason=fast_reason,
            providers=dict(providers or {}),
            capabilities=tuple(row.to_status() for row in rows),
            latency={key: round(value, 3) for key, value in dict(latency or {}).items()},
            errors=tuple(errors),
            summary=_summary_for(request, scene, status=status, status_reason=status_reason),
            telemetry=self.telemetry.to_dict(),
        )

    def _blocked_result(
        self,
        request: PerceptionRequest,
        reason: str,
        *,
        kind: str,
        capability: str,
    ) -> PerceptionResult:
        """The result when the request never reached a frame: UNAVAILABLE, named.

        A source that cannot run is not a failure of perception — it is a
        capability this machine does not have, and the status says which one.
        """
        return self._result(
            request,
            status=PerceptionStatus.UNAVAILABLE,
            status_reason=reason,
            errors=(reason,),
            outcomes=(
                _CapabilityOutcome(
                    name=PerceptionCapability.OCR
                    if capability in {"image-frame", "screen-frame"}
                    else PerceptionCapability.DETECTION,
                    state=CapabilityState.UNAVAILABLE,
                    available=False,
                    provider=kind,
                    reason=reason,
                ),
            ),
        )

    async def _finish(self, result: PerceptionResult) -> PerceptionResult:
        """Record the outcome and announce it, once, at the end of a request."""
        self.telemetry.record_status(result.status)
        if result.scene is not None:
            self._last_scene = result.scene
        if result.status is PerceptionStatus.FAILED:
            await self._emit(
                PERCEPTION_FAILED,
                {"status": result.status.value, "error": result.status_reason},
            )
        else:
            await self._emit(
                PERCEPTION_COMPLETED,
                {
                    "status": result.status.value,
                    "objects": len(result.objects),
                    "escalated": result.escalated,
                },
            )
        return result

    async def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        """Announce through the injected seam — a failing observer is never fatal.

        The same rule the audit trail follows: an announcement that can fail the
        work it announces is a new way for the work to fail.
        """
        observer = self.observer
        if observer is None:
            return
        try:
            produced = observer(event_type, dict(payload))
            if inspect.isawaitable(produced):
                await produced
        except Exception:  # noqa: BLE001 - a broken observer must not break perception
            self.telemetry.observer_failures += 1

    # ── status ───────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        """What this engine is and what it can do right now, with no content.

        Nothing here carries a frame path, a text block or an image: the status
        surface answers "what is wired" and "what has it done", which is
        everything an operator needs and nothing a person's screen should leak.
        """
        return {
            "profile": self.profile,
            "providers": {
                "ocr": {
                    "name": str(getattr(self.ocr, "name", "none")),
                    "available": bool(getattr(self.ocr, "available", False)),
                },
                "detection": _provider_row(self.detector),
                "segmentation": _provider_row(self.segmenter),
                "vlm": {
                    "name": self.vlm_name,
                    "model": self._vlm_model(),
                    "available": self.vlm_available,
                },
            },
            "capabilities": [row.to_dict() for row in capability_rows(self)],
            "sampling": self.sampler.to_dict(),
            "tracking": self.tracker.to_dict(),
            "temporal": self.temporal.to_dict(),
            "resources": self.gate.status(),
            "telemetry": self.telemetry.to_dict(),
            "last_scene": _scene_summary(self._last_scene),
            "raw_frames_stored": False,
            "action_execution": False,
            "cuda_required": False,
            "automatic_model_loading": False,
        }

    def capabilities(self) -> list[dict[str, Any]]:
        """The capability table: classification, availability, and the why."""
        return [row.to_dict() for row in capability_rows(self)]

    # ── verdict ──────────────────────────────────────────────────────────
    @staticmethod
    def _decide_status(
        scene: SceneRepresentation | None,
        outcomes: Mapping[str, _CapabilityOutcome],
        *,
        errors: tuple[str, ...],
    ) -> PerceptionStatus:
        """SUCCESS/PARTIAL/UNAVAILABLE/FAILED, decided from evidence and refusals.

        The rule is evidence-first. A scene exists -> the frame was perceived, so
        the only question left is whether something the plan needed could not run,
        which makes the answer PARTIAL and names it. No evidence and a refusal ->
        nothing could answer, so UNAVAILABLE. No evidence and no refusal -> a
        working reader looked at an empty frame, which is SUCCESS and is what a
        blank image must report.
        """
        if scene is None:
            return PerceptionStatus.FAILED
        rows = tuple(outcomes.values())
        evidence = bool(scene.objects or scene.text)
        blocked = [row for row in rows if row.available is False]
        if evidence:
            if blocked or errors:
                return PerceptionStatus.PARTIAL
            return PerceptionStatus.SUCCESS
        if blocked:
            return PerceptionStatus.UNAVAILABLE
        return PerceptionStatus.SUCCESS

    @staticmethod
    def _status_reason(
        scene: SceneRepresentation | None,
        outcomes: Mapping[str, _CapabilityOutcome],
        *,
        errors: tuple[str, ...],
    ) -> str:
        """Which capability decided the verdict, and in its own words."""
        if scene is None:
            return errors[0] if errors else "the frame could not be perceived"
        blocked = [row for row in outcomes.values() if row.available is False]
        if blocked:
            names = ", ".join(sorted({row.name.value for row in blocked}))
            reasons = "; ".join(row.reason for row in blocked if row.reason)
            return f"unavailable: {names}" + (f" ({reasons})" if reasons else "")
        if errors:
            return errors[0]
        return ""



    async def _assemble(
        self,
        request: PerceptionRequest,
        frame: Frame,
        plan: PerceptionPlan,
        *,
        prepared: PreparedFrame,
        objects: list[DetectedObject],
        text: list[OcrText],
        segmentation: list[SegmentationResult],
        outcomes: list[_CapabilityOutcome],
        providers: dict[str, str],
        latency: dict[str, float],
        decision: Any,
        sufficient: bool | None,
        sufficient_reason: str,
        escalated: bool,
        failures: list[str],
        source_kind: str,
    ) -> _FrameOutcome:
        """Relationships, tracking, the scene, then the change events for it.

        The order is a dependency order rather than a preference: relationships
        need the final object list, tracking needs the same list plus its own
        history, the scene needs all of them, and the temporal layer needs the
        scene it is comparing against.
        """
        started = monotonic_ms()
        relationships = derive_relationships(objects, max_pairs=self.max_objects * 3)
        latency["relationships"] = monotonic_ms() - started
        started = monotonic_ms()
        update = self.tracker.update(
            objects,
            frame_id=frame.frame_id,
            timestamp=frame.timestamp,
            width=frame.width,
            height=frame.height,
        )
        latency["tracking"] = monotonic_ms() - started
        metadata: dict[str, Any] = {
            "sampling": decision.to_dict(),
            "relationship_pairs": relationships.pairs_considered,
            "relationships_capped": relationships.pairs_skipped,
            "escalated": escalated,
            "fast_sufficient": sufficient,
            "fast_reason": sufficient_reason,
            "source_kind": source_kind,
            "preprocessing": prepared.to_dict(),
            "plan": plan.to_dict(),
        }
        if failures:
            metadata["provider_failures"] = list(failures)
        scene = build_scene(
            frame,
            objects=objects[: self.max_objects],
            text=text,
            relationships=relationships.relationships,
            track_update=update,
            segmentation=segmentation,
            providers=providers,
            capability_states={row.name.value: row.state.value for row in outcomes},
            metadata=metadata,
            abstraction=False,
        )
        started = monotonic_ms()
        events = self.temporal.observe(scene, object_events=update.events)
        latency["temporal"] = monotonic_ms() - started
        scene = replace(scene, temporal_events=events, focus=focus_for(request.asked, scene))
        scene = replace(scene, abstraction=abstraction_for(scene))
        latency["total"] = sum(value for key, value in latency.items() if key != "total") + sum(
            row.latency_ms for row in outcomes if row.latency_ms is not None
        )
        self.telemetry.objects_seen += len(scene.objects)
        self.telemetry.text_lines_seen += len(scene.text)
        await self._announce(scene)
        return _FrameOutcome(
            plan=plan,
            scene=scene,
            escalated=escalated,
            fast_sufficient=sufficient,
            fast_reason=sufficient_reason,
            latency=latency,
            outcomes=tuple(outcomes),
            providers=providers,
            events=scene.temporal_events,
            failures=tuple(failures),
        )

    async def _announce(self, scene: SceneRepresentation) -> None:
        """Publish the object and scene events one frame's observation produced.

        Payloads carry identifiers, labels and measurements — never an image, a
        path or a text block: the bus is a notification channel, and a watcher
        that wants the perception can ask the engine for the scene.
        """
        for event in scene.temporal_events:
            if event.kind is TemporalEventKind.OBJECT_APPEARED:
                await self._emit(
                    OBJECT_APPEARED, {"object_id": event.object_id, "label": event.label}
                )
            elif event.kind is TemporalEventKind.OBJECT_DISAPPEARED:
                await self._emit(
                    OBJECT_DISAPPEARED, {"object_id": event.object_id, "label": event.label}
                )
            elif event.kind is TemporalEventKind.OBJECT_MOVED:
                await self._emit(
                    OBJECT_MOVED,
                    {
                        "object_id": event.object_id,
                        "label": event.label,
                        "distance": event.detail,
                    },
                )
            elif event.kind is TemporalEventKind.SCENE_CHANGED:
                await self._emit(
                    SCENE_CHANGED, {"scene_id": scene.scene_id, "changes": event.detail}
                )

    # ── the deep path ────────────────────────────────────────────────────
    async def _escalate(
        self,
        request: PerceptionRequest,
        frame: Frame,
        plan: PerceptionPlan,
        *,
        fast_ms: float,
    ) -> tuple[bool, _CapabilityOutcome, list[DetectedObject], list[OcrText]]:
        """Ask the Phase 6 manager the question the fast path could not answer.

        Three doors stand in front of the model and each is checked in order: the
        request's own latency budget (already spent is already spent), the
        resource gate (which may refuse), and whether a model exists at all. Only
        after all three does a ``VisionRequest`` travel — and it travels through
        the SAME manager the rest of the build uses, so the read-first policy, the
        provider gate and the structured result are not re-implemented here.
        """
        provider = self.vlm_name
        model = self._vlm_model()
        state = CapabilityState.PROVIDER_DEPENDENT
        budget = request.max_latency_ms
        if budget is not None and fast_ms >= budget:
            self.telemetry.escalation_denied += 1
            return (
                False,
                _CapabilityOutcome(
                    name=PerceptionCapability.VLM,
                    state=state,
                    available=None,
                    provider=provider,
                    reason=(
                        f"the request's latency budget ({budget}ms) was already spent by "
                        f"the fast path ({fast_ms:.0f}ms)"
                    ),
                ),
                [],
                [],
            )
        # Is there a model at all, before spending a governance question on it?
        # A machine with no vision model is not resource-limited, it is unequipped,
        # and the reason must say which of the two stands in the way.
        if not self.vlm_available:
            return (
                False,
                _CapabilityOutcome(
                    name=PerceptionCapability.VLM,
                    state=state,
                    available=False,
                    provider=provider,
                    reason="no vision model is wired, so the deep path cannot run",
                ),
                [],
                [],
            )
        decision: AdmissionDecision = self.gate.admit(
            needs_model=True,
            required_bytes=self._required_bytes(model),
            model=model,
            latency_budget_ms=budget,
        )
        if decision.denied:
            self.telemetry.escalation_denied += 1
            return (
                False,
                _CapabilityOutcome(
                    name=PerceptionCapability.VLM,
                    state=state,
                    available=False,
                    provider=provider,
                    reason=decision.reason,
                ),
                [],
                [],
            )
        started = monotonic_ms()
        vision_request = VisionRequest(
            source=frame.path,
            question=plan.deep_question if request.asked or request.target else "",
            task=task_for_question(plan.deep_question, target=request.target),
            target=request.target,
            prefer_ocr=False,
            allow_vlm=True,
        )
        try:
            result = await self.vision.analyze(vision_request)
        except VisionProviderError as exc:
            self.telemetry.provider_failures += 1
            return (
                False,
                _CapabilityOutcome(
                    name=PerceptionCapability.VLM,
                    state=state,
                    available=True,
                    provider=provider,
                    reason=str(exc),
                    latency_ms=monotonic_ms() - started,
                ),
                [],
                [],
            )
        except Exception as exc:  # noqa: BLE001 - classified, then reported
            self.telemetry.provider_failures += 1
            return (
                False,
                _CapabilityOutcome(
                    name=PerceptionCapability.VLM,
                    state=state,
                    available=True,
                    provider=provider,
                    reason=f"the vision model failed: {type(exc).__name__}",
                    latency_ms=monotonic_ms() - started,
                ),
                [],
                [],
            )
        self.telemetry.escalations += 1
        objects = _objects_from_vision(result, frame=frame, provider=provider)
        text = _text_from_vision(result, frame=frame, provider=provider)
        answered = bool(result.answered) or bool(objects) or bool(text)
        reason = ""
        if not answered:
            reason = str(result.metadata.get("reason", "")) or (
                "the vision model returned nothing usable"
            )
        return (
            answered,
            _CapabilityOutcome(
                name=PerceptionCapability.VLM,
                state=state,
                available=True,
                provider=f"llm:{provider}",
                reason=reason,
                latency_ms=monotonic_ms() - started,
            ),
            objects,
            text,
        )

    def _vlm_model(self) -> str:
        provider = getattr(self.vision, "provider", None)
        return str(getattr(provider, "model", "") or "")

    def _required_bytes(self, model: str) -> int | None:
        """What the model needs, from the model manager's own catalogue.

        ``None`` when nobody can say — passed through rather than guessed, because
        a guessed size either refuses a load that would have fit or permits one
        that would not.
        """
        manager = self.gate.model_manager
        if manager is None or not model.strip():
            return None
        try:
            size = manager.model_size_bytes(model)
        except Exception:  # noqa: BLE001 - an unreadable catalogue is an unknown
            return None
        return int(size) if isinstance(size, (int, float)) and size > 0 else None

    # ── the request ──────────────────────────────────────────────────────
    async def perceive(
        self,
        request: PerceptionRequest | Mapping[str, Any] | None = None,
        *,
        source: FrameSource | None = None,
        frame: Frame | None = None,
        frames: Sequence[Frame] | None = None,
        **overrides: Any,
    ) -> PerceptionResult:
        """Perceive one frame, a supplied stream, or whatever ``source`` yields.

        ``frames`` is the explicit multi-frame case (the temporal workflow): the
        pipeline walks them in order, keeps the tracker and the temporal layer
        across the walk, and reports the LAST frame's scene with the events the
        whole walk produced. A single frame — the common case — takes exactly the
        same path with a one-frame walk, so there is no second pipeline.
        """
        resolved = self._request(request, overrides)
        self.telemetry.requests += 1
        await self._emit(
            PERCEPTION_STARTED,
            {"mode": resolved.mode.value, "source_kind": resolved.source_kind},
        )
        planned_source = source
        # An explicit ``frame`` is a one-frame walk, not a second pipeline: the
        # same ``_FrameListSource`` carries it so the tracker and the temporal
        # layer see exactly what they would see for a supplied stream.
        walk: tuple[Frame, ...] = (
            tuple(frames) if frames else ((frame,) if frame is not None else ())
        )
        if walk:
            planned_source = _FrameListSource(walk)
        elif planned_source is None:
            planned_source = self._source_for(resolved)
        kind = source_kind_of(planned_source)
        if not source_is_available(planned_source):
            reason = str(getattr(planned_source, "unavailable_reason", "")) or (
                f"the {kind} source is not available on this machine"
            )
            return await self._finish(
                self._blocked_result(resolved, reason, kind=kind, capability=function_of(kind))
            )
        collected = await self._collect(planned_source)
        if collected.error:
            return await self._finish(
                self._blocked_result(resolved,
                    collected.error, kind=kind, capability=function_of(kind))
            )
        if not collected.frames:
            return await self._finish(
                self._blocked_result(
                    resolved,
                    "the frame source produced no frames",
                    kind=kind,
                    capability=function_of(kind),
                )
            )
        result = await self._run(resolved, collected.frames, kind=kind)
        return await self._finish(result)

    @staticmethod
    def _request(
        request: PerceptionRequest | Mapping[str, Any] | None,
        overrides: Mapping[str, Any],
    ) -> PerceptionRequest:
        """A request from a record, a mapping, or nothing — plus any overrides."""
        base = request if isinstance(request, PerceptionRequest) else None
        if base is None and isinstance(request, Mapping):
            base = PerceptionRequest.from_mapping(request)
        resolved = base or PerceptionRequest()
        if overrides:
            rows = dict(overrides)
            rows.setdefault("source", resolved.source)
            rows.setdefault("question", resolved.question)
            rows.setdefault("source_kind", resolved.source_kind)
            resolved = PerceptionRequest.from_mapping(rows, defaults=resolved)
        return resolved

    def _source_for(self, request: PerceptionRequest) -> FrameSource:
        """The source this request names, or an honest unavailable one.

        A screen or camera request goes to the source the APPLICATION wired (the
        already approval-gated capture), never to a capture this module performs
        itself.
        """
        kind = request.source_kind
        if kind == "screen":
            return self.screen_source
        if kind == "camera":
            return self.camera_source
        if kind == "sequence":
            paths = [row.strip() for row in request.source.replace(
                ";", "\n").splitlines() if row.strip()]
            return SequenceFrameSource(paths)
        return ImageFrameSource(request.source)

    async def _collect(self, source: FrameSource) -> _Collected:
        """Read the frames this source actually HAS, up to the stream ceiling.

        A still image is ONE frame, not twelve: re-reading the same file would
        multiply the work by the ceiling and make the telemetry claim a stream
        that does not exist. A source that declares its extent (a sequence, a
        supplied list, the test source) is read for exactly that many, and a LIVE
        source (a screen, a camera) is read up to ``MAX_STREAM_FRAMES`` or until
        it ends. A source that raises is reported as its own error rather than
        being flattened into "no frames": "the camera is unplugged" and "the list
        was empty" must not read the same in a result.
        """
        frames: list[Frame] = []
        for _ in range(_walk_length(source)):
            try:
                frame = await source.read()
            except FrameSourceError as exc:
                return _Collected(error=str(exc.reason or exc))
            except Exception as exc:  # noqa: BLE001 - a broken source is a result
                return _Collected(error=f"the frame source failed: {type(exc).__name__}")
            if frame is None:
                break
            frames.append(frame)
        return _Collected(frames=tuple(frames))



# ── helpers ──────────────────────────────────────────────────────────────
def _reason_of(exc: BaseException) -> str:
    """A failure as one short sentence, using the provider's own wording."""
    reason = getattr(exc, "reason", "")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    text = str(exc).strip()
    return text or type(exc).__name__


def _ordered_outcomes(rows: Sequence[_CapabilityOutcome]) -> tuple[_CapabilityOutcome, ...]:
    """Outcomes in the capability vocabulary's own order, so runs match."""
    order = list(PerceptionCapability)
    return tuple(sorted(rows, key=lambda row: order.index(row.name)))


def _provider_row(provider: Any) -> dict[str, Any]:
    """A provider as status output: name, availability, and why not."""
    return {
        "name": str(getattr(provider, "name", "none")),
        "available": bool(getattr(provider, "available", False)),
        "reason": str(getattr(provider, "unavailable_reason", "")),
    }


def _scene_summary(scene: SceneRepresentation | None) -> dict[str, Any]:
    """A scene's shape for a status surface — counts and identifiers, no content.

    Reading the last scene is how a follow-up question plans against what was
    seen; storing a text block or a path in the status output would put the
    user's screen into every status poll.
    """
    if scene is None:
        return {}
    return {
        "scene_id": scene.scene_id,
        "frame_id": scene.frame_id,
        "source_id": scene.source_id,
        "timestamp": scene.timestamp,
        "objects": len(scene.objects),
        "text_lines": len(scene.text),
        "relationships": len(scene.relationships),
        "tracks": len(scene.tracks),
        "temporal_events": len(scene.temporal_events),
        "confidence": scene.confidence,
        "uncertainty": scene.uncertainty,
        "providers": dict(scene.providers),
    }


def _text_lines(
    words: Sequence[OcrWord], *, source: str, frame: Frame
) -> list[OcrText]:
    """Read words as LINES: one record per line, with the union of their boxes.

    Line granularity rather than word granularity because a person asking to read
    an image wants the text, not a bag of tokens, and because a line's box is the
    region that line occupies. Words without geometry still contribute their text
    (a plain text file is read with no coordinates) but produce no box, which is
    recorded as ``bbox=None`` rather than as a box at the origin.
    """
    grouped: dict[int, list[OcrWord]] = {}
    for index, word in enumerate(words):
        text = word.text.strip()
        if not text:
            continue
        grouped.setdefault(word.line if word.line else index, []).append(word)
    lines: list[OcrText] = []
    for order, key in enumerate(sorted(grouped)):
        group = grouped[key]
        text = " ".join(word.text.strip() for word in group).strip()
        if not text:
            continue
        placed = [word for word in group if word.has_geometry]
        bbox = None
        if placed:
            x = min(word.x for word in placed)
            y = min(word.y for word in placed)
            right = max(word.x + word.width for word in placed)
            bottom = max(word.y + word.height for word in placed)
            bbox = BBox(x=x, y=y, width=max(0, right - x), height=max(0, bottom - y))
        lines.append(
            OcrText(
                text=text,
                bbox=bbox,
                confidence=None,
                language="",
                reading_order=order,
                source=source,
            )
        )
    return lines


def _merge_text(existing: list[OcrText], extra: list[OcrText]) -> list[OcrText]:
    """Text from two readers, deduplicated by content case-folded.

    The fast path's lines stay first: if both readers saw the same sentence, the
    one that read it from pixels without a model is the one to keep.
    """
    seen = {" ".join(item.text.split()).casefold() for item in existing}
    merged = list(existing)
    for item in extra:
        key = " ".join(item.text.split()).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _fast_sufficiency(
    request: PerceptionRequest, objects: Sequence[DetectedObject], text: Sequence[OcrText]
) -> tuple[bool, str]:
    """Whether the cheap path answered the question — measured, and explained.

    The reading is per question kind rather than per pipeline: a text question is
    answered by text, an object question by located objects, a locator question
    by the label's own words, and a description is never answered by the fast
    path because describing needs semantics it does not produce.
    """
    kind = describe_question(request.asked, target=request.target)
    if kind == "text":
        if text:
            return True, f"the OCR path read {len(text)} line(s) of text"
        return False, "no text could be read"
    if kind == "objects":
        if objects:
            return True, f"the fast path located {len(objects)} object(s)"
        return False, "the fast path located nothing"
    if kind == "locate":
        wanted = " ".join(str(request.target or request.asked).split()).casefold()
        for item in objects:
            folded = item.label.casefold()
            if folded and (folded in wanted or wanted in folded):
                return True, f"the label was located: {item.label!r}"
        return False, "the label was not located by the fast path"
    if kind == "scene":
        return False, "describing a scene needs semantics the fast path does not produce"
    if objects or text:
        return True, "the fast path produced evidence"
    return False, "the fast path produced nothing"


def _objects_from_vision(
    result: VisionResult, *, frame: Frame, provider: str
) -> list[DetectedObject]:
    """A Phase 6 vision result's elements as provider-neutral detections.

    Only things with geometry become objects: a model that affirmed an element
    without saying where it is has not produced a located object, and inventing a
    box for it would make the detection list lie. The label, the box and whatever
    confidence the model reported travel together; ``source`` names the deep path
    so nothing downstream can mistake a model's noun for a measured one.
    """
    produced: list[DetectedObject] = []
    for element in result.ui_elements:
        bounds = {str(key): value for key, value in dict(element.bounds).items()}
        box = BBox.from_mapping(bounds)
        if not box.has_extent:
            continue
        produced.append(
            DetectedObject(
                label=str(element.label).strip() or "element",
                bbox=box,
                confidence=element.confidence if element.confidence else None,
                class_id=None,
                source=f"vlm:{provider}",
                frame_id=frame.frame_id,
                timestamp=frame.timestamp,
                metadata={"kind": str(element.kind or "element"), "reader": "vlm"},
            )
        )
    for region in result.relevant_regions:
        box = BBox.from_mapping(dict(region))
        if not box.has_extent:
            continue
        produced.append(
            DetectedObject(
                label=str(region.get("label", "") or "region"),
                bbox=box,
                confidence=None,
                class_id=None,
                source=f"vlm:{provider}",
                frame_id=frame.frame_id,
                timestamp=frame.timestamp,
                metadata={"kind": "region", "reader": "vlm"},
            )
        )
    return produced


def _text_from_vision(
    result: VisionResult, *, frame: Frame, provider: str
) -> list[OcrText]:
    """Text a model reported, as text records with no invented positions.

    The understand contract returns text, not word boxes, so these lines carry
    ``bbox=None``: a reader that wants a position must use the OCR path, and
    placing a whole paragraph at the origin would be a fabricated measurement.
    """
    del frame
    return [
        OcrText(
            text=str(line).strip(),
            bbox=None,
            confidence=None,
            language="",
            reading_order=index,
            source=f"vlm:{provider}",
        )
        for index, line in enumerate(result.detected_text)
        if str(line).strip()
    ]


def _summary_for(
    request: PerceptionRequest,
    scene: SceneRepresentation | None,
    *,
    status: PerceptionStatus,
    status_reason: str,
) -> str:
    """The sentence a renderer reads first: the answer, or the honest refusal.

    A text question is answered BY the text, so the text is the summary. Anything
    else reports the abstraction's own deterministic sentence, plus the reason
    when the status is not a clean success — a reader should never have to infer
    from an empty scene that a provider was missing.
    """
    if scene is None:
        return status_reason or "Nothing could be perceived."
    if describe_question(request.asked, target=request.target) == "text":
        block = scene.text_block.strip()
        if block:
            return block
    abstraction = scene.abstraction
    summary = abstraction.summary if abstraction is not None else ""
    if status is PerceptionStatus.SUCCESS:
        return summary or "The frame was perceived, and it contained nothing to report."
    detail = status_reason or status.value
    if summary:
        return f"{summary} {detail}"
    return detail


#: Re-exported for callers that build an observer from the engine's own names.
EVENT_TYPES: tuple[str, ...] = (
    PERCEPTION_STARTED,
    PERCEPTION_COMPLETED,
    PERCEPTION_FAILED,
    FRAME_RECEIVED,
    SCENE_CHANGED,
    OBJECT_APPEARED,
    OBJECT_DISAPPEARED,
    OBJECT_MOVED,
)

    # ── assembly ─────────────────────────────────────────────────────────

