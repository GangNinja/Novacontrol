"""The bridge to a future agent state — an observation, and nothing more.

Phase 20 already has the shape a policy reads: ``AgentState.observations`` is a
tuple of mappings. What Phase 21 owes it is a well-formed observation — what was
perceived, in a bounded, image-free form — so a later phase can build an agent
state on perception without this phase knowing anything about policies, rewards
or episodes.

Three rules keep the bridge a bridge:

**It is a mapping, not a state.** ``perception_observation`` returns plain data.
Nothing here imports a policy, chooses an action or advances an episode; a
perception layer that could advance a state machine would be running the loop
this phase explicitly does not own.

**It carries no image and no text dump.** Identifiers, labels, counts, boxes,
confidences and relationship kinds — the things a reasoning layer needs and a log
can safely hold. The text block is NOT included; a caller that wants the reading
asks the scene for it.

**The Phase 20 import is deferred.** The application imports this package, and
``novacontrol.application`` must not drag the agentic stack in at boot, so the
engine import happens inside the one function that needs it.

What this deliberately does not provide: world state, predicted next state,
counterfactuals, or a reward. Those are Phases 22–24, and inventing their shape
here would pre-empt them with something worse designed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from novacontrol.perception.models import PerceptionResult, SceneRepresentation

__all__ = [
    "OBSERVATION_KIND",
    "attach_observation",
    "perception_observation",
    "scene_observation",
]

#: The `kind` a perception observation carries, so a consumer can tell what it is.
OBSERVATION_KIND = "perception.scene"

#: A hard ceiling on every list in an observation. An observation that grows with
#: the scene is an observation that eventually breaks a context window.
MAX_OBSERVED_OBJECTS = 32
MAX_OBSERVED_RELATIONSHIPS = 24
MAX_OBSERVED_EVENTS = 16


def scene_observation(scene: SceneRepresentation | None) -> dict[str, Any]:
    """One scene as an observation mapping — bounded, and free of images."""
    if scene is None:
        return {"kind": OBSERVATION_KIND, "present": False}
    return {
        "kind": OBSERVATION_KIND,
        "present": True,
        "scene_id": scene.scene_id,
        "frame_id": scene.frame_id,
        "source_id": scene.source_id,
        "timestamp": scene.timestamp,
        "width": scene.width,
        "height": scene.height,
        "objects": [
            {
                "object_id": item.object_id,
                "label": item.label,
                "bbox": item.bbox.to_dict(),
                "confidence": item.confidence,
                "source": item.source,
            }
            for item in scene.objects[:MAX_OBSERVED_OBJECTS]
        ],
        "object_count": len(scene.objects),
        "text_lines": [item.text for item in scene.text[:MAX_OBSERVED_OBJECTS]],
        "text_line_count": len(scene.text),
        "relationships": [
            {
                "kind": item.kind.value,
                "subject_id": item.subject_id,
                "object_id": item.object_id,
                "confidence": item.confidence,
            }
            for item in scene.relationships[:MAX_OBSERVED_RELATIONSHIPS]
        ],
        "track_ids": [item.track_id for item in scene.tracks[:MAX_OBSERVED_OBJECTS]],
        "events": [
            {"kind": item.kind.value, "object_id": item.object_id, "label": item.label}
            for item in scene.temporal_events[:MAX_OBSERVED_EVENTS]
        ],
        "confidence": scene.confidence,
        "uncertainty": scene.uncertainty,
        "providers": dict(scene.providers),
    }


def perception_observation(result: PerceptionResult) -> dict[str, Any]:
    """A whole result as an observation, including how the request ended.

    The status and the reason travel with the scene because a reasoning layer has
    to be able to tell "nothing is there" from "nothing could look", and a scene
    alone cannot say which it is.
    """
    observation = scene_observation(result.scene)
    observation["status"] = result.status.value
    observation["status_reason"] = result.status_reason
    observation["escalated"] = result.escalated
    observation["confidence"] = result.confidence
    observation["uncertainty"] = result.uncertainty
    return observation


def attach_observation(
    state: Any,
    result: PerceptionResult,
    *,
    environment_state: Mapping[str, Any] | None = None,
) -> Any:
    """Advance any state that speaks Phase 20's ``advanced`` contract.

    Duck-typed on purpose: this function needs ONE method, and requiring the
    concrete class would mean importing the agentic package here for a type
    annotation nobody reads. A caller without that method gets the observation
    back instead of a broken state.
    """
    observation = perception_observation(result)
    if environment_state is None and result.scene is not None:
        environment_state = {
            "perception": {
                "scene_id": result.scene.scene_id,
                "objects": len(result.scene.objects),
                "status": result.status.value,
            }
        }
    advanced = getattr(state, "advanced", None)
    if callable(advanced):
        return advanced(observation=observation, environment_state=environment_state)
    return observation


def observation_for_state(result: PerceptionResult) -> dict[str, Any]:
    """The observation alone, for a caller building its own state mapping.

    Named separately from :func:`perception_observation` so a caller that wants
    the mapping has an obvious door and never reaches into the result's fields.
    """
    return perception_observation(result)


def states_from_results(results: Sequence[PerceptionResult]) -> tuple[dict[str, Any], ...]:
    """One observation per result, in order — a bounded history for a consumer."""
    return tuple(perception_observation(item) for item in results)
