"""Change detection: what differs between two state versions, and whether it matters (§13).

This module is a pure function of two :class:`~novacontrol.world.models.WorldState`
records. That is deliberate: the same code answers "what changed since the last
observation" during ingestion and "what changed between versions A and B" as a
query, and neither path can drift from the other because there is only one.

The thresholds are the whole point of the module. A world model that reported
every coordinate fluctuation would be a log of noise, so:

* a position change below ``position_px`` is not reported at all — the boxes did
  not move, the detection wobbled;
* a confidence change below ``confidence_delta`` is not reported — a re-read of
  the same thing at 0.99 instead of 1.00 is not news;
* two identical observations produce NO events, because every comparison here is
  between values and equal values do not differ.

Visual scene change is not re-detected either. Phase 21 already reports observed
change between frames (``perception.scene_changed`` and its temporal events), and
this layer consumes the STRUCTURED difference instead of re-running a pixel
comparison: a scene-level change here means the environment's own facts changed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from novacontrol.world.models import (
    ChangeEvent,
    ChangeKind,
    EntityAttribute,
    EntityStatus,
    FactBasis,
    RelationshipSource,
    RelationStatus,
    UncertaintyKind,
    WorldEntity,
    WorldRelationship,
    WorldState,
    as_optional_float,
)

__all__ = [
    "MAX_CHANGES",
    "ChangeThresholds",
    "detect_changes",
]

#: One ingest or one diff never returns more than this many events. The count is
#: bounded because a change list is read by people and models, not just stored.
MAX_CHANGES = 64


@dataclass(frozen=True, slots=True)
class ChangeThresholds:
    """What counts as a change. One table, so no two call sites can disagree."""

    position_px: int = 8
    confidence_delta: float = 0.1

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> ChangeThresholds:
        defaults = cls()
        if not isinstance(data, Mapping):
            return defaults
        raw_position = data.get("position_change_px", defaults.position_px)
        try:
            position = max(0, int(raw_position))
        except (TypeError, ValueError):
            position = defaults.position_px
        delta = data.get("confidence_change", defaults.confidence_delta)
        try:
            confidence = max(0.0, min(1.0, float(delta)))
        except (TypeError, ValueError):
            confidence = defaults.confidence_delta
        return cls(position_px=position, confidence_delta=confidence)


def detect_changes(
    previous: WorldState,
    current: WorldState,
    *,
    thresholds: ChangeThresholds | None = None,
    evidence: Sequence[str] = (),
    timestamp: str = "",
    limit: int = MAX_CHANGES,
) -> tuple[ChangeEvent, ...]:
    """Every meaningful difference between two versions, in a stable order."""
    settings = thresholds or ChangeThresholds()
    at = timestamp or current.timestamp
    ids = tuple(evidence)
    events: list[ChangeEvent] = []

    old_entities = {item.entity_id: item for item in previous.entities}
    new_entities = {item.entity_id: item for item in current.entities}

    for entity_id in sorted(set(new_entities) - set(old_entities)):
        item = new_entities[entity_id]
        if item.status is EntityStatus.EXPIRED:
            continue
        events.append(
            ChangeEvent(
                kind=ChangeKind.ENTITY_APPEARED,
                entity_id=entity_id,
                detail=f"{item.label or item.entity_type} was observed for the first time",
                current=item.label,
                confidence=item.identity_confidence,
                timestamp=at,
                evidence=ids,
            )
        )

    for entity_id in sorted(set(old_entities) - set(new_entities)):
        item = old_entities[entity_id]
        events.append(
            ChangeEvent(
                kind=ChangeKind.ENTITY_DISAPPEARED,
                entity_id=entity_id,
                detail=f"{item.label or item.entity_type} is no longer part of the state",
                previous=item.label,
                timestamp=at,
                evidence=ids,
            )
        )

    for entity_id in sorted(set(old_entities) & set(new_entities)):
        before = old_entities[entity_id]
        after = new_entities[entity_id]
        events.extend(_entity_events(before, after, settings, at, ids))
        events.extend(_status_events(before, after, settings, at, ids))

    events.extend(_relationship_events(previous, current, at, ids))
    events.extend(_environment_events(previous, current, settings, at, ids))
    events.extend(_conflict_events(previous, current, at, ids))

    return tuple(events[: max(0, limit)])


def _entity_events(
    before: WorldEntity,
    after: WorldEntity,
    thresholds: ChangeThresholds,
    at: str,
    evidence: tuple[str, ...],
) -> list[ChangeEvent]:
    """Attribute, position and confidence changes for one entity."""
    events: list[ChangeEvent] = []
    old_attributes = {item.name: item for item in before.attributes}
    new_attributes = {item.name: item for item in after.attributes}
    for name in sorted(set(old_attributes) | set(new_attributes)):
        old = old_attributes.get(name)
        new = new_attributes.get(name)
        if old is not None and new is not None and old.value == new.value:
            continue
        events.append(
            ChangeEvent(
                kind=ChangeKind.ATTRIBUTE_CHANGED,
                entity_id=after.entity_id,
                detail=(
                    f"{name} changed from {_short(old.value if old else None)} to "
                    f"{_short(new.value if new else None)}"
                ),
                previous=old.value if old is not None else None,
                current=new.value if new is not None else None,
                confidence=new.confidence if new is not None else None,
                timestamp=at,
                evidence=evidence,
            )
        )
    distance = _movement(before, after)
    if distance is not None and distance >= thresholds.position_px:
        events.append(
            ChangeEvent(
                kind=ChangeKind.POSITION_CHANGED,
                entity_id=after.entity_id,
                detail=f"moved {distance:.0f}px",
                previous=before.bbox.to_dict() if before.bbox is not None else None,
                current=after.bbox.to_dict() if after.bbox is not None else None,
                magnitude=distance,
                timestamp=at,
                evidence=evidence,
            )
        )
    previous_confidence = before.identity_confidence
    current_confidence = after.identity_confidence
    if (
        previous_confidence is not None
        and current_confidence is not None
        and abs(current_confidence - previous_confidence) >= thresholds.confidence_delta
    ):
        events.append(
            ChangeEvent(
                kind=ChangeKind.CONFIDENCE_CHANGED,
                entity_id=after.entity_id,
                detail=(
                    f"identity confidence moved from {previous_confidence:.2f} to "
                    f"{current_confidence:.2f}"
                ),
                previous=previous_confidence,
                current=current_confidence,
                magnitude=abs(current_confidence - previous_confidence),
                timestamp=at,
                evidence=evidence,
            )
        )
    return events


def _status_events(
    before: WorldEntity,
    after: WorldEntity,
    thresholds: ChangeThresholds,
    at: str,
    evidence: tuple[str, ...],
) -> list[ChangeEvent]:
    """Absence and retirement, which are changes about the state rather than a value."""
    events: list[ChangeEvent] = []
    if after.status is EntityStatus.EXPIRED and before.status is not EntityStatus.EXPIRED:
        events.append(
            ChangeEvent(
                kind=ChangeKind.ENTITY_DISAPPEARED,
                entity_id=after.entity_id,
                detail=(
                    "retired from the current state after the retention window "
                    "passed with no further observation (its history is kept)"
                ),
                previous=before.status.value,
                current=after.status.value,
                timestamp=at,
                evidence=evidence,
            )
        )
        return events
    if (
        after.status in {EntityStatus.NOT_OBSERVED, EntityStatus.MISSING}
        and before.status is EntityStatus.PRESENT
    ):
        events.append(
            ChangeEvent(
                kind=ChangeKind.STATE_STALE,
                entity_id=after.entity_id,
                detail=(
                    f"{after.label or after.entity_type} was not in the latest "
                    f"observation ({after.status.value})"
                ),
                previous=before.status.value,
                current=after.status.value,
                timestamp=at,
                evidence=evidence,
            )
        )
    return events


def _relationship_events(
    previous: WorldState, current: WorldState, at: str, evidence: tuple[str, ...]
) -> list[ChangeEvent]:
    """Active relations added, retired or revived between the two versions."""
    before = {
        (item.kind.value, item.source_entity_id, item.target_entity_id): item
        for item in previous.relationships
    }
    after = {
        (item.kind.value, item.source_entity_id, item.target_entity_id): item
        for item in current.relationships
    }
    events: list[ChangeEvent] = []
    for key in sorted(set(after) - set(before)):
        item = after[key]
        if not item.active:
            continue
        events.append(
            ChangeEvent(
                kind=ChangeKind.RELATIONSHIP_CHANGED,
                entity_id=item.source_entity_id,
                detail=(
                    f"{item.kind.value} now holds between {item.source_entity_id} and "
                    f"{item.target_entity_id}"
                ),
                current=item.kind.value,
                confidence=item.confidence,
                timestamp=at,
                evidence=evidence or item.evidence,
            )
        )
    for key in sorted(set(before) & set(after)):
        was = before[key]
        now = after[key]
        if was.status is RelationStatus.ACTIVE and now.status is not RelationStatus.ACTIVE:
            events.append(
                ChangeEvent(
                    kind=ChangeKind.RELATIONSHIP_CHANGED,
                    entity_id=now.source_entity_id,
                    detail=(
                        f"{now.kind.value} between {now.source_entity_id} and "
                        f"{now.target_entity_id} is no longer asserted ({now.status.value})"
                    ),
                    previous=was.status.value,
                    current=now.status.value,
                    timestamp=at,
                    evidence=evidence,
                )
            )
    return events


def _environment_events(
    previous: WorldState,
    current: WorldState,
    thresholds: ChangeThresholds,
    at: str,
    evidence: tuple[str, ...],
) -> list[ChangeEvent]:
    """Environment-level facts (counts, frame size) that differ between versions."""
    before = {item.name: item for item in previous.conditions}
    after = {item.name: item for item in current.conditions}
    events: list[ChangeEvent] = []
    for name in sorted(set(after) | set(before)):
        old = before.get(name)
        new = after.get(name)
        if old is not None and new is not None and old.value == new.value:
            continue
        if old is None and new is None:
            continue
        events.append(
            ChangeEvent(
                kind=ChangeKind.ENVIRONMENT_CHANGED,
                detail=f"{name} changed from {_short(old.value if old else None)} to "
                f"{_short(new.value if new else None)}",
                previous=old.value if old is not None else None,
                current=new.value if new is not None else None,
                timestamp=at,
                evidence=evidence,
            )
        )
    return events


def _conflict_events(
    previous: WorldState, current: WorldState, at: str, evidence: tuple[str, ...]
) -> list[ChangeEvent]:
    """Uncertainty rows that were RECORDED by this version — conflicts, out-of-order facts."""
    seen = {
        (item.subject, item.kind.value, item.detail) for item in previous.uncertainty
    }
    events: list[ChangeEvent] = []
    for item in current.uncertainty:
        key = (item.subject, item.kind.value, item.detail)
        if key in seen:
            continue
        if item.kind not in {UncertaintyKind.CONFLICTING, UncertaintyKind.OUT_OF_ORDER}:
            continue
        events.append(
            ChangeEvent(
                kind=ChangeKind.OBSERVATION_CONFLICT,
                entity_id=item.subject if len(item.subject) <= 32 else "",
                detail=item.detail,
                confidence=item.confidence,
                timestamp=at,
                evidence=item.evidence or evidence,
            )
        )
    return events


def _movement(before: WorldEntity, after: WorldEntity) -> float | None:
    """Centre-to-centre distance between two boxes, or ``None`` when unmeasurable."""
    if before.bbox is None or after.bbox is None:
        return None
    if not before.bbox.has_extent or not after.bbox.has_extent:
        return None
    first = before.bbox.center
    second = after.bbox.center
    return math.hypot(second[0] - first[0], second[1] - first[1])


def _short(value: Any) -> str:
    """A short, safe rendering of a fact for a change line."""
    if value is None:
        return "nothing"
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


def attributes_by_basis(attributes: Sequence[EntityAttribute]) -> dict[str, FactBasis]:
    """The basis of each attribute — used by callers that must not read an inferred fact as seen."""
    return {item.name: item.basis for item in attributes if item.name}


def relationship_snapshot(relationships: Sequence[WorldRelationship]) -> dict[str, Any]:
    """Counts of the graph by status — a compact summary for a status surface."""
    counts: dict[str, int] = {status.value: 0 for status in RelationStatus}
    sources: dict[str, int] = {source.value: 0 for source in RelationshipSource}
    for item in relationships:
        counts[item.status.value] += 1
        sources[item.relation_source.value] += 1
    return {"by_status": counts, "by_source": sources, "total": len(relationships)}


def entity_confidence(entities: Sequence[WorldEntity]) -> float | None:
    """The mean of the identity confidences that were actually measured.

    ``None`` when nothing measured one — never 0.0, which would read as "we are
    certain this is wrong" rather than "nobody said".
    """
    total = 0.0
    measured = 0
    for item in entities:
        value = as_optional_float(item.identity_confidence)
        if value is None:
            continue
        total += value
        measured += 1
    if not measured:
        return None
    return round(total / measured, 4)
