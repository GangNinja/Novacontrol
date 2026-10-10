"""Observation ingestion: turn whatever a source said into one normalized record (§8).

This is the door every fact enters the world model through, and it has one job —
produce an :class:`~novacontrol.world.models.Observation` the estimator can
trust — plus one refusal: an observation that claims nothing is rejected rather
than quietly advancing the state.

What is deliberately NOT a rejection:

* **A missing timestamp.** The observation is accepted and its facts are marked
  as carrying no readable time; the engine records an uncertainty row instead.
  Throwing away a fact because a field was blank would be the opposite trade from
  the one this phase makes.
* **An unrecognised source name.** The source label is metadata, and a caller
  that invented a name still brought facts.
* **One unusable entity row.** The readable rows are kept and the count of
  dropped ones travels in the metadata, so a malformed row narrows the
  observation instead of cancelling it (``PARTIAL``).

Phase 21 is converted here rather than re-perceived: a ``PerceptionResult`` or a
``SceneRepresentation`` becomes an observation whose entity ids are the scene's
own object and track ids, which the estimator uses as identity HINTS. The text of
a scene is not copied by default — a screen is somebody's screen, and a state
store that hoards it is a privacy surface with no user (``include_text=True`` is
the explicit opt-in).

Nothing here mutates the state, decides identity or writes to a store: ingestion
normalizes, and the estimator reconciles.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from novacontrol.perception.models import BBox, PerceptionResult, SceneRepresentation
from novacontrol.world.models import (
    EntityAttribute,
    EvidenceReference,
    FactBasis,
    Observation,
    ObservationSource,
    ObservedEntity,
    as_bool,
    as_optional_float,
    iso_now,
)

__all__ = [
    "MAX_INGESTED_ENTITIES",
    "MAX_INGESTED_FACTS",
    "ObservationRejected",
    "entities_from_scene",
    "facts_from_scene",
    "normalize",
    "observation_from_perception",
    "scene_relationships",
    "validate",
]

#: A hard ceiling on one observation. An observation that grows with the source
#: is an observation that eventually breaks the state's own bounds; the excess is
#: reported rather than silently dropped.
MAX_INGESTED_ENTITIES = 128
MAX_INGESTED_FACTS = 64

#: What a source says about WHOLE-scope coverage. A Phase 21 scene is one view of
#: one frame, so absence from it proves nothing unless the caller says otherwise.
DEFAULT_COMPLETE = False


class ObservationRejected(ValueError):
    """An observation that cannot be ingested, with the reason why."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _count(metadata: Mapping[str, Any], key: str) -> int:
    """One of the ingest counters as a positive integer, or 0 when it is not set."""
    try:
        value = int(metadata.get(key) or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def validate(observation: Observation) -> tuple[str, str]:
    """Whether an observation may be applied: ``(verdict, reason)``.

    ``accepted`` — there is something to record.
    ``partial`` — there is something to record, and something unreadable on the
    same record (an unknown source, no timestamp, dropped entity rows).
    ``rejected`` — nothing to record at all.
    """
    reasons: list[str] = []
    if not observation.timestamp:
        reasons.append("no timestamp, so the facts carry no readable time")
    if observation.source is ObservationSource.EXTERNAL and not observation.source_id:
        reasons.append("an external source with no id, so nothing can be traced to it")
    unusable = _count(observation.metadata, "dropped_entities")
    if unusable:
        reasons.append(f"{unusable} unusable entity row(s) dropped")
    # Rows past a ceiling were READABLE — they were not ingested. Reporting them as
    # "unusable" told a caller a fact about their data that was not true, and the
    # ceiling is what actually stopped them, so the ceiling is what gets named.
    truncated_entities = _count(observation.metadata, "truncated_entities")
    if truncated_entities:
        reasons.append(
            f"{truncated_entities} entity row(s) past the {MAX_INGESTED_ENTITIES}-row "
            "ingest ceiling were not ingested"
        )
    truncated_facts = _count(observation.metadata, "truncated_facts")
    if truncated_facts:
        reasons.append(
            f"{truncated_facts} fact(s) past the {MAX_INGESTED_FACTS}-fact ingest "
            "ceiling were not ingested"
        )
    if not observation.entities and not observation.facts and not observation.complete:
        return "rejected", (
            "the observation reports nothing: no entities, no facts, and it does not "
            "claim to cover a scope"
        )
    if reasons:
        return "partial", "; ".join(reasons)
    return "accepted", ""


def normalize(value: Any, *, timestamp: str = "") -> Observation:
    """One observation from whatever a caller handed over.

    Accepts an ``Observation``, a mapping shaped like one, a Phase 21
    ``PerceptionResult`` or a ``SceneRepresentation``. Anything else is rejected
    with the type it actually was, because "I do not understand this" is more
    useful than a silent empty observation.
    """
    if isinstance(value, Observation):
        return _bounded(value, timestamp=timestamp)
    if isinstance(value, SceneRepresentation):
        return _bounded(observation_from_perception(value), timestamp=timestamp)
    if isinstance(value, PerceptionResult):
        return _bounded(observation_from_perception(value), timestamp=timestamp)
    if isinstance(value, Mapping):
        payload = dict(value)
        nested = payload.get("perception") or payload.get("scene")
        if nested is not None and not payload.get("entities"):
            # ``{"source": "perception", "perception": {...}}`` — the shape an API
            # caller naturally sends when forwarding a perception result.
            outer = {key: item for key, item in payload.items() if key != "perception"}
            converted = normalize(nested, timestamp=timestamp)
            merged = {
                **outer,
                **converted.to_dict(),
                "source": str(payload.get("source", "") or "perception"),
            }
            observation = Observation.from_mapping(merged)
            return _bounded(observation, timestamp=timestamp)
        return _bounded(Observation.from_mapping(payload), timestamp=timestamp)
    raise ObservationRejected(
        "an observation must be an Observation, a mapping, a PerceptionResult or a "
        f"SceneRepresentation; this was {type(value).__name__}"
    )


def _bounded(observation: Observation, *, timestamp: str) -> Observation:
    """Apply the ceilings, recording what was cut instead of cutting silently.

    Three different things can leave an observation, and they are counted
    separately because they mean different things: a row that was UNREADABLE, an
    entity row past ``MAX_INGESTED_ENTITIES``, and a fact past
    ``MAX_INGESTED_FACTS``. Folding them into one counter and calling the total
    "unusable entity rows" told the caller something false about their own data —
    a row that was merely over a ceiling was perfectly readable (report §25.8).
    """
    entities = observation.entities
    facts = observation.facts
    truncated_entities = 0
    truncated_facts = 0
    if len(entities) > MAX_INGESTED_ENTITIES:
        truncated_entities = len(entities) - MAX_INGESTED_ENTITIES
        entities = entities[:MAX_INGESTED_ENTITIES]
    if len(facts) > MAX_INGESTED_FACTS:
        truncated_facts = len(facts) - MAX_INGESTED_FACTS
        facts = facts[:MAX_INGESTED_FACTS]
    unusable = 0
    usable: list[ObservedEntity] = []
    for item in entities:
        if item.label or item.bbox is not None or item.attributes or item.entity_id:
            usable.append(item)
        else:
            unusable += 1
    metadata = dict(observation.metadata)
    if unusable:
        metadata["dropped_entities"] = int(metadata.get("dropped_entities", 0)) + unusable
    if truncated_entities:
        metadata["truncated_entities"] = (
            int(metadata.get("truncated_entities", 0)) + truncated_entities
        )
    if truncated_facts:
        metadata["truncated_facts"] = int(metadata.get("truncated_facts", 0)) + truncated_facts
    # An attribute a source REPORTED without labelling its basis is an observed
    # fact: the caller is telling this layer what it saw. A caller that means
    # "this is derived" says so with an explicit basis, and that basis travels
    # through untouched — which is what stops an inference from being silently
    # filed as an observation.
    reported_entities = tuple(
        replace(item, attributes=tuple(_as_reported(row) for row in item.attributes))
        for item in usable
    )
    return Observation(
        observation_id=observation.observation_id,
        source=observation.source,
        source_id=observation.source_id,
        timestamp=observation.timestamp or timestamp or iso_now(),
        scope=observation.scope,
        complete=observation.complete,
        entities=reported_entities,
        facts=tuple(_as_reported(row) for row in facts),
        confidence=observation.confidence,
        evidence=observation.evidence
        or (
            EvidenceReference(
                evidence_id=observation.observation_id,
                kind="observation",
                source=observation.source_id or observation.source.value,
                timestamp=observation.timestamp or timestamp,
            ),
        ),
        correlation_id=observation.correlation_id,
        clock=observation.clock,
        text=observation.text,
        metadata=metadata,
    )


# ── Phase 21 conversion ──────────────────────────────────────────────────────


def _as_reported(attribute: EntityAttribute) -> EntityAttribute:
    """An incoming attribute with no stated basis, read as reported.

    Storage keeps ``UNKNOWN`` for a row that omitted its basis (the safe reading
    for something already written down); INGESTION reads the same omission as
    "the source reported this value", which is what an observation is. The
    distinction is why this helper exists here rather than in the model.
    """
    if attribute.basis is not FactBasis.UNKNOWN:
        return attribute
    return replace(attribute, basis=FactBasis.OBSERVED)


def observation_from_perception(
    value: PerceptionResult | SceneRepresentation,
    *,
    include_text: bool = False,
    complete: bool = False,
    scope: str = "",
) -> Observation:
    """A Phase 21 result as an observation — structured, bounded, image-free.

    The scene's object ids and track ids become the observation's identity hints,
    and the scene's own relationship list is carried as geometry BETWEEN THOSE
    HINTS rather than as world relationships: deciding which durable entity ids a
    per-frame relation belongs to is the estimator's job, not the frame's.
    """
    status = "success"
    reason = ""
    if isinstance(value, PerceptionResult):
        scene = value.scene
        confidence = value.confidence
        status = value.status.value
        reason = value.status_reason
        source_id = ""
        if scene is not None:
            source_id = scene.source_id
        if not source_id:
            request = getattr(value, "request", None)
            source_id = str(getattr(request, "source", "") or "") if request is not None else ""
        metadata: dict[str, Any] = {
            "perception_status": status,
            "perception_reason": reason,
            "escalated": value.escalated,
        }
    elif isinstance(value, SceneRepresentation):
        scene = value
        confidence = value.confidence
        source_id = scene.source_id
        metadata = {}
    else:  # pragma: no cover - defensive: normalize() already narrowed the type
        raise ObservationRejected(
            f"expected a PerceptionResult or a SceneRepresentation, got {type(value).__name__}"
        )
    if scene is None:
        # "Nothing could look" is itself a fact: the observation carries the
        # failure as a condition (no entities, no completeness claim), so the
        # state records that a look was attempted and reports why it produced
        # nothing — instead of reading a failed perception as an empty room.
        metadata["scene"] = None
        return Observation(
            source=ObservationSource.PERCEPTION,
            source_id=source_id,
            scope=scope,
            complete=False,
            facts=(
                EntityAttribute.observed(
                    "perception_status",
                    {"status": status, "reason": reason},
                    source="perception",
                ),
            ),
            confidence=confidence,
            metadata=metadata,
        )
    metadata["scene_id"] = scene.scene_id
    metadata["frame_id"] = scene.frame_id
    metadata["providers"] = dict(scene.providers)
    metadata["relationship_hints"] = list(scene_relationships(scene))
    metadata["abstraction"] = dict(scene.abstraction.counts) if scene.abstraction else {}
    if include_text and scene.text_block:
        metadata["text_lines"] = [item.text for item in scene.text[:MAX_INGESTED_FACTS]]
    return Observation(
        source=ObservationSource.PERCEPTION,
        source_id=source_id,
        timestamp=scene.timestamp,
        scope=scope,
        complete=complete,
        entities=entities_from_scene(scene),
        facts=(
            *facts_from_scene(scene, include_text=include_text),
            # Every look records its own outcome, and the estimator re-measures
            # this condition on each observation, so it can never go stale.
            EntityAttribute.observed(
                "perception_status",
                {"status": status, "reason": reason},
                source="perception",
            ),
        ),
        confidence=confidence,
        evidence=(
            EvidenceReference(
                evidence_id=scene.scene_id,
                kind="scene",
                source=scene.source_id or "perception",
                detail=f"frame {scene.frame_id}" if scene.frame_id else "",
                timestamp=scene.timestamp,
            ),
        ),
        metadata=metadata,
    )


def entities_from_scene(scene: SceneRepresentation) -> tuple[ObservedEntity, ...]:
    """The scene's objects as observed entities, with their identity hints.

    A tracked object is preferred over the raw detection when both describe the
    same thing: the track carries continuity the detection does not. Tracks whose
    box was never measured still become entities — a label seen without geometry
    is a fact, it just cannot support a spatial claim.
    """
    tracked: dict[str, ObservedEntity] = {}
    for track in scene.tracks:
        if track.state.value == "removed":
            continue
        tracked[track.track_id] = ObservedEntity(
            label=track.label or track.previous_label,
            entity_type="object",
            entity_id=track.track_id,
            track_id=track.track_id,
            bbox=track.bbox,
            confidence=track.confidence,
            source=track.source or "tracking",
            metadata={"track_state": track.state.value, "frame_count": track.frame_count},
        )
    entities: list[ObservedEntity] = []
    seen: set[tuple[str, int, int, int, int]] = set()
    for detection in scene.objects:
        key = _box_key(detection.label, detection.bbox)
        if key in seen:
            # The same word read twice in one frame is one observation of it.
            continue
        seen.add(key)
        entities.append(
            ObservedEntity(
                label=detection.label,
                entity_type=_entity_type_for(detection),
                entity_id=detection.object_id,
                bbox=detection.bbox,
                confidence=detection.confidence,
                attributes=_attributes_for(detection),
                source=detection.source or "detection",
            )
        )
    covered = {_box_key(item.label, item.bbox) for item in entities}
    for tracked_object in tracked.values():
        if _box_key(tracked_object.label, tracked_object.bbox) in covered:
            continue
        entities.append(tracked_object)
    return tuple(entities)


def _box_key(label: str, bbox: BBox | None) -> tuple[str, int, int, int, int]:
    """A duplicate key for one detection: its label plus its box, or -1s without one.

    A detection with no geometry is still a fact, and two of them with the same
    label are treated as one — without a box there is nothing to tell them apart,
    and inventing a distinction would be worse than folding them.
    """
    if bbox is None:
        return (label, -1, -1, -1, -1)
    return (label, bbox.x, bbox.y, bbox.width, bbox.height)


def _entity_type_for(item: Any) -> str:
    """What kind of thing a detection is, from what the provider said.

    ``class_id`` is the only class signal this build has, and it is a number with
    no vocabulary attached — so it selects between two generic types rather than
    inventing a name for it.
    """
    metadata = getattr(item, "metadata", None)
    if isinstance(metadata, Mapping):
        declared = str(metadata.get("entity_type", "") or "").strip()
        if declared:
            return declared
        if str(metadata.get("kind", "") or "").strip() == "text":
            return "text"
    if getattr(item, "class_id", None) is not None:
        return "detected_object"
    return "object"


def _attributes_for(item: Any) -> tuple[EntityAttribute, ...]:
    """The attributes a detection supports — its label, and nothing invented."""
    attributes: list[EntityAttribute] = []
    label = str(getattr(item, "label", "") or "")
    if label:
        attributes.append(
            EntityAttribute.observed(
                "label",
                label,
                confidence=as_optional_float(getattr(item, "confidence", None)),
                source=str(getattr(item, "source", "") or ""),
            )
        )
    class_id = getattr(item, "class_id", None)
    if class_id is not None:
        attributes.append(
            EntityAttribute.observed(
                "class_id",
                int(class_id),
                source=str(getattr(item, "source", "") or ""),
            )
        )
    return tuple(attributes)


def facts_from_scene(
    scene: SceneRepresentation, *, include_text: bool = False
) -> tuple[EntityAttribute, ...]:
    """Observation-level facts a scene supports: counts, geometry, and no text.

    The counts are derived arithmetic over what was detected, so they are marked
    ``INFERRED`` — a reader can tell "the scene had 4 objects" from "a source
    reported 4 objects". Text is the one field that is content rather than shape,
    so it is only carried when the caller explicitly opted in.
    """
    facts: list[EntityAttribute] = []
    evidence = (scene.scene_id,) if scene.scene_id else ()
    facts.append(
        EntityAttribute.inferred(
            "object_count",
            len(scene.objects),
            evidence=evidence,
            observed_at=scene.timestamp,
        )
    )
    facts.append(
        EntityAttribute.inferred(
            "text_line_count",
            len(scene.text),
            evidence=evidence,
            observed_at=scene.timestamp,
        )
    )
    if scene.width is not None and scene.height is not None:
        facts.append(
            EntityAttribute.observed(
                "frame_size",
                {"width": scene.width, "height": scene.height},
                evidence=evidence,
                observed_at=scene.timestamp,
                source="perception",
            )
        )
    if scene.abstraction is not None:
        for name, value in scene.abstraction.counts.items():
            facts.append(
                EntityAttribute.inferred(
                    f"count.{name}",
                    value,
                    evidence=evidence,
                    observed_at=scene.timestamp,
                )
            )
    if include_text and scene.text_block:
        facts.append(
            EntityAttribute.observed(
                "text",
                scene.text_block[:2000],
                evidence=evidence,
                observed_at=scene.timestamp,
                source="perception",
            )
        )
    return tuple(facts)


def scene_relationships(scene: SceneRepresentation) -> tuple[dict[str, Any], ...]:
    """The scene's geometric relations, kept as evidence-linked hints.

    Bounded, and every entry names the object ids it relates so the estimator can
    translate them into entity ids. The arithmetic Phase 21 recorded travels in
    ``evidence`` — this layer does not re-derive geometry it was handed.
    """
    rows: list[dict[str, Any]] = []
    for item in scene.relationships:
        rows.append(
            {
                "kind": item.kind.value,
                "subject_hint": item.subject_id,
                "object_hint": item.object_id,
                "confidence": item.confidence,
                "provenance": item.provenance,
                "evidence": item.evidence,
                "scene_id": scene.scene_id,
            }
        )
    return tuple(rows)


def observation_mapping(value: Any) -> dict[str, Any]:
    """A safe mapping for a caller that wants to store or echo an observation.

    Observation ids, source names and counts — never entity metadata or text, so
    an audit trail can reference an ingest without becoming a copy of it.
    """
    if isinstance(value, Observation):
        return {
            "observation_id": value.observation_id,
            "source": value.source.value,
            "source_id": value.source_id,
            "timestamp": value.timestamp,
            "scope": value.scope,
            "complete": value.complete,
            "entities": value.entity_count,
            "facts": len(value.facts),
            "correlation_id": value.correlation_id,
        }
    return {}


def observation_sequence(values: Any) -> tuple[Observation, ...]:
    """Normalize a sequence of observations, skipping the ones that are rejected."""
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    results: list[Observation] = []
    for item in values:
        try:
            results.append(normalize(item))
        except ObservationRejected:
            continue
    return tuple(results)


def is_complete_claim(payload: Mapping[str, Any]) -> bool:
    """Whether a caller claimed whole-scope coverage — read once, in one place."""
    return as_bool(payload.get("complete"), DEFAULT_COMPLETE)
