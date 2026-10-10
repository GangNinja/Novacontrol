"""Real-time perception and abstraction — Phase 21.

One sentence for what this package is: **it turns frames into structured,
temporally consistent scenes, cheaply when it can and honestly when it cannot.**

It is built ON Phase 6's vision layer rather than beside it: the same
``VisionManager`` answers the deep questions, the same ``OcrEngine`` reads text,
the same ``VisionProvider`` boundary decides whether a model can see at all, and
the same ``VisionResult`` shape is what a model's answer arrives as. What is new
here is everything the phase is about — frames as objects with identity, a
provider-neutral detection and segmentation contract, spatial relationships with
their arithmetic, tracking that calls itself spatial, observed temporal change, a
serializable scene record, a deterministic abstraction, and a router that refuses
to ask a model what a cheap reader can answer.

The honest positions are stated at the same volume as the features:

  * detection is text geometry and classical regions — no semantic detector
  * segmentation returns real classical masks, with no class labels
  * tracking is spatial continuity, not recognition
  * abstraction is counting and quoting, not reasoning
  * semantic labels and scene descriptions are PROVIDER_DEPENDENT on a VLM
  * nothing here executes an action, predicts a future state or trains anything

``overview()`` is the machine-readable form of those statements, and the
capability table in :mod:`novacontrol.perception.capabilities` is the live one.
"""

from novacontrol.perception.agentic import (
    OBSERVATION_KIND,
    attach_observation,
    perception_observation,
    scene_observation,
)
from novacontrol.perception.capabilities import (
    CAPABILITY_SCHEMA_VERSION,
    CAPABILITY_TABLE,
    CapabilityDefinition,
    capability_rows,
    register_perception_capabilities,
)
from novacontrol.perception.engine import (
    EVENT_TYPES,
    FRAME_RECEIVED,
    MAX_STREAM_FRAMES,
    OBJECT_APPEARED,
    OBJECT_DISAPPEARED,
    OBJECT_MOVED,
    PERCEPTION_COMPLETED,
    PERCEPTION_FAILED,
    PERCEPTION_STARTED,
    SCENE_CHANGED,
    PerceptionEngine,
    PerceptionTelemetry,
)
from novacontrol.perception.frames import (
    CameraFrameSource,
    Frame,
    FrameSource,
    FrameSourceError,
    ImageFrameSource,
    ScreenFrameSource,
    SequenceFrameSource,
    TestFrameSource,
    describe_frame,
    probe_image_file,
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
    Relationship,
    RelationshipKind,
    SceneAbstraction,
    SceneRepresentation,
    SegmentationResult,
    TemporalEvent,
    TemporalEventKind,
    TrackedObject,
    TrackState,
)
from novacontrol.perception.preprocessing import (
    FrameUnreadable,
    GrayPreview,
    PreparedFrame,
    PreprocessSpec,
    PreviewDiff,
    prepare_frame,
    validate_frame,
)
from novacontrol.perception.providers import (
    ChainDetectionProvider,
    DetectionProvider,
    NullDetectionProvider,
    NullSegmentationProvider,
    OcrTextDetectionProvider,
    PerceptionProviderError,
    RegionDetectionProvider,
    RegionSegmentationProvider,
    SegmentationProvider,
    default_detection_provider,
    default_segmentation_provider,
)
from novacontrol.perception.routing import PerceptionRouter, describe_question
from novacontrol.perception.sampling import FrameSampler, SamplingDecision, SamplingPolicy
from novacontrol.perception.scene import abstraction_for, build_scene, focus_for, scene_confidence
from novacontrol.perception.spatial import RelationshipReport, derive_relationships
from novacontrol.perception.temporal import TemporalPerception, events_by_kind
from novacontrol.perception.tracking import SpatialTracker, TrackUpdate

#: The phase this package implements.
PHASE = "phase21"

#: The schema version of the records this package serializes.
PERCEPTION_SCHEMA_VERSION = "phase21.1"

#: What this phase deliberately does NOT implement, named so a reader never has
#: to infer the boundary from absence.
DEFERRED_PHASES: tuple[str, ...] = (
    "Phase 22: world model, memory and long-term state reasoning",
    "Phase 23: interactive learning and exploration environments",
    "Phase 24: planning, reasoning and action policy",
    "Phase 25: embodied and game agents",
    "Phase 26: generalization, ARC and intelligence evaluation",
)


def overview() -> dict[str, object]:
    """What this phase ships and what it refuses to claim — cheap and read-only.

    The classification flags are the point of this function: they are the
    machine-readable form of "no fake intelligence", so a report, a test or a
    dashboard can assert the phase's posture instead of trusting prose.
    """
    return {
        "phase": PHASE,
        "schema_version": PERCEPTION_SCHEMA_VERSION,
        "capability_schema_version": CAPABILITY_SCHEMA_VERSION,
        "capabilities": {row.name.value: row.state.value for row in CAPABILITY_TABLE},
        "capability_details": [row.to_dict() for row in CAPABILITY_TABLE],
        "modes": [member.value for member in PerceptionMode],
        "statuses": [member.value for member in PerceptionStatus],
        "track_states": [member.value for member in TrackState],
        "relationship_kinds": [member.value for member in RelationshipKind],
        "temporal_event_kinds": [member.value for member in TemporalEventKind],
        "profiles": list(PerceptionProfile.ALL),
        "event_types": list(EVENT_TYPES),
        "max_stream_frames": MAX_STREAM_FRAMES,
        "frame_sources": ["image", "sequence", "screen", "camera", "test"],
        "providers": {
            "detection": ["ocr-text-geometry", "classical-region"],
            "segmentation": ["classical-region-masks"],
            "vlm": "provider_dependent (Phase 6 VisionProvider)",
            "camera": "unavailable (no camera backend ships)",
        },
        "cuda_required": False,
        "automatic_model_loading": False,
        "automatic_model_downloads": False,
        "stores_raw_frames": False,
        "stores_masks": False,
        "stores_hidden_reasoning": False,
        "action_execution": False,
        "predicts_future_state": False,
        "deferred": list(DEFERRED_PHASES),
    }


__all__ = [
    "CAPABILITY_SCHEMA_VERSION",
    "CAPABILITY_TABLE",
    "DEFERRED_PHASES",
    "EVENT_TYPES",
    "FRAME_RECEIVED",
    "MAX_STREAM_FRAMES",
    "OBJECT_APPEARED",
    "OBJECT_DISAPPEARED",
    "OBJECT_MOVED",
    "OBSERVATION_KIND",
    "PERCEPTION_COMPLETED",
    "PERCEPTION_FAILED",
    "PERCEPTION_SCHEMA_VERSION",
    "PERCEPTION_STARTED",
    "PHASE",
    "SCENE_CHANGED",
    "AdmissionDecision",
    "BBox",
    "CameraFrameSource",
    "CapabilityDefinition",
    "CapabilityState",
    "CapabilityStatus",
    "ChainDetectionProvider",
    "DetectedObject",
    "DetectionProvider",
    "Frame",
    "FrameSampler",
    "FrameSource",
    "FrameSourceError",
    "FrameUnreadable",
    "GrayPreview",
    "ImageFrameSource",
    "NullDetectionProvider",
    "NullSegmentationProvider",
    "OcrText",
    "OcrTextDetectionProvider",
    "PerceptionCapability",
    "PerceptionEngine",
    "PerceptionMode",
    "PerceptionPlan",
    "PerceptionProfile",
    "PerceptionProviderError",
    "PerceptionRequest",
    "PerceptionResourceGate",
    "PerceptionResult",
    "PerceptionRouter",
    "PerceptionStatus",
    "PerceptionTelemetry",
    "PreparedFrame",
    "PreprocessSpec",
    "PreviewDiff",
    "RegionDetectionProvider",
    "RegionSegmentationProvider",
    "Relationship",
    "RelationshipKind",
    "RelationshipReport",
    "SamplingDecision",
    "SamplingPolicy",
    "SceneAbstraction",
    "SceneRepresentation",
    "ScreenFrameSource",
    "SegmentationProvider",
    "SegmentationResult",
    "SequenceFrameSource",
    "SpatialTracker",
    "TemporalEvent",
    "TemporalEventKind",
    "TemporalPerception",
    "TestFrameSource",
    "TrackState",
    "TrackUpdate",
    "TrackedObject",
    "abstraction_for",
    "attach_observation",
    "build_scene",
    "capability_rows",
    "default_detection_provider",
    "default_segmentation_provider",
    "derive_relationships",
    "describe_frame",
    "describe_question",
    "events_by_kind",
    "focus_for",
    "max_objects_for",
    "overview",
    "perception_observation",
    "prepare_frame",
    "preprocess_for",
    "probe_image_file",
    "register_perception_capabilities",
    "sampling_for",
    "scene_confidence",
    "scene_observation",
    "validate_frame",
]
