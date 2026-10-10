"""The relationship graph: durable relations, and the rules for when they stop (§7).

Phase 21 already derives per-frame spatial relations from box geometry, with the
arithmetic attached. This layer does not repeat that work — it decides which of
those relations is worth REMEMBERING, and for how long, by giving every relation a
life cycle:

    ACTIVE      the evidence supports it now
    STALE       it was geometric, and a complete observation of the same scope
                did not repeat it — not a lie, just no longer supported
    RETRACTED   a later observation contradicted it (an ``inside`` that became a
                ``contains`` between the same pair)

Three deliberate distinctions, all of them from §7:

**A stated relation does not expire.** "The licence belongs to the laptop" is not
something a screenshot repeats, so an observation that does not mention it leaves
it ACTIVE. Only GEOMETRIC relations can go STALE, because only they are claims
about a moment.

**A relation is never deleted.** ``valid_until`` records when it stopped being
asserted and the record stays, so the state can answer "was the phone ever on the
desk?" instead of pretending the answer was always no.

**Ownership is never inferred.** ``owned_by``/``associated_with``/``connected_to``
only ever appear when a source STATED them — nothing in a frame can prove them,
so the geometric path cannot reach them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.perception.models import DetectedObject
from novacontrol.perception.spatial import derive_relationships
from novacontrol.world.models import (
    Observation,
    ObservedEntity,
    RelationshipKind,
    RelationshipSource,
    RelationStatus,
    UncertaintyKind,
    UncertaintyRecord,
    WorldRelationship,
    as_optional_float,
    as_text,
)

__all__ = [
    "RELATION_ORDER",
    "MAX_RELATIONSHIPS",
    "RelationshipOutcome",
    "derive_geometric_relations",
    "relations_from_observation",
    "same_relation",
]

#: A hard ceiling on the graph. Reached only by a pathological source, and
#: reported through ``RelationshipOutcome.capped`` rather than truncating silently.
MAX_RELATIONSHIPS = 512

#: The order relations are reported in, so the same evidence always produces the
#: same graph — which is what a diff and a test both need.
RELATION_ORDER: tuple[RelationshipKind, ...] = (
    RelationshipKind.CONTAINS,
    RelationshipKind.INSIDE,
    RelationshipKind.LOCATED_NEAR,
    RelationshipKind.OBSERVED_WITH,
    RelationshipKind.CONNECTED_TO,
    RelationshipKind.OWNED_BY,
    RelationshipKind.ASSOCIATED_WITH,
)

#: Phase 21's per-frame geometry vocabulary, mapped onto the durable one. Five
#: directional kinds collapse into one durable ``located_near``: "left of" at one
#: instant is not a fact about the world, it is a fact about a frame, and the
#: arithmetic that decided it travels in ``detail``.
_PERCEPTION_KINDS: Mapping[str, RelationshipKind] = {
    "left_of": RelationshipKind.LOCATED_NEAR,
    "right_of": RelationshipKind.LOCATED_NEAR,
    "above": RelationshipKind.LOCATED_NEAR,
    "below": RelationshipKind.LOCATED_NEAR,
    "near": RelationshipKind.LOCATED_NEAR,
    "inside": RelationshipKind.INSIDE,
    "contains": RelationshipKind.CONTAINS,
    "overlaps": RelationshipKind.OBSERVED_WITH,
}

#: Pairs that cannot both be true of the same two entities. Kept to the one pair
#: that genuinely contradicts: containment in both directions.
_INCOMPATIBLE: frozenset[frozenset[RelationshipKind]] = frozenset(
    {frozenset({RelationshipKind.INSIDE, RelationshipKind.CONTAINS})}
)


@dataclass(frozen=True, slots=True)
class RelationshipOutcome:
    """The reconciled graph, plus what reconciling it cost (§7/§13)."""

    relationships: tuple[WorldRelationship, ...] = ()
    added: int = 0
    reactivated: int = 0
    staled: int = 0
    retracted: int = 0
    capped: bool = False
    derived_from_geometry: bool = False
    conflicts: tuple[UncertaintyRecord, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": self.added,
            "reactivated": self.reactivated,
            "staled": self.staled,
            "retracted": self.retracted,
            "capped": self.capped,
            "derived_from_geometry": self.derived_from_geometry,
            "relationships": [item.to_dict() for item in self.relationships],
        }


def same_relation(left: WorldRelationship, right: WorldRelationship) -> bool:
    """Whether two records describe the same claim about the same pair.

    Identity of a relation is ``(kind, source, target)`` — not the record id. Two
    observations of the same "phone on the desk" must refresh ONE row rather than
    accumulate a new one per frame, or the graph becomes a log.
    """
    return (
        left.kind is right.kind
        and left.source_entity_id == right.source_entity_id
        and left.target_entity_id == right.target_entity_id
    )


def relations_from_observation(
    observation: Observation,
    *,
    id_by_hint: Mapping[str, str],
    resolved: Sequence[tuple[ObservedEntity, str]] = (),
    existing: Sequence[WorldRelationship],
    timestamp: str,
    derive_missing: bool = True,
) -> RelationshipOutcome:
    """Reconcile this observation's relations against the graph the state holds.

    ``id_by_hint`` is the estimator's translation table from the source's own ids
    (a Phase 21 object id, a track id) to this layer's entity ids — relations
    arrive naming the first and must be stored naming the second. ``resolved`` is
    the same translation in observation order, which is what the geometric
    fallback needs: an observation with no source ids still has identities, they
    were just assigned by this layer rather than reported by the source.
    """
    candidates = _candidates_from(observation, id_by_hint)
    derived = False
    if derive_missing and not candidates:
        geometric, derived = derive_geometric_relations(
            resolved, timestamp=timestamp
        )
        candidates.extend(geometric)

    active = [item for item in existing if item.status is RelationStatus.ACTIVE]
    inactive = [item for item in existing if item.status is not RelationStatus.ACTIVE]
    kept: list[WorldRelationship] = []
    added = 0
    reactivated = 0
    retracted = 0
    conflicts: list[UncertaintyRecord] = []
    matched_existing: set[str] = set()

    for candidate in candidates:
        if len(kept) >= MAX_RELATIONSHIPS:
            break
        incumbent = next((item for item in active if same_relation(item, candidate)), None)
        if incumbent is not None:
            matched_existing.add(incumbent.relationship_id)
            kept.append(_refreshed(incumbent, candidate, timestamp))
            continue
        contradicted = _contradiction(candidate, active)
        if contradicted is not None:
            matched_existing.add(contradicted.relationship_id)
            retracted += 1
            kept.append(_retired(contradicted, timestamp))
            conflicts.append(
                UncertaintyRecord(
                    subject=candidate.relationship_id,
                    kind=UncertaintyKind.CONFLICTING,
                    detail=(
                        f"{candidate.kind.value} between {candidate.source_entity_id} and "
                        f"{candidate.target_entity_id} contradicts the {contradicted.kind.value} "
                        "already held for the same pair; the older claim was retracted"
                    ),
                    confidence=candidate.confidence,
                    recorded_at=timestamp,
                )
            )
        revived = next((item for item in inactive if same_relation(item, candidate)), None)
        if revived is not None:
            reactivated += 1
            kept.append(_reactivated(revived, candidate, timestamp))
            continue
        kept.append(candidate)
        added += 1

    # A geometric relation the latest complete observation did not repeat stops
    # being asserted. A STATED one is left alone: ownership does not lapse because
    # a frame did not mention it.
    staled = 0
    for item in active:
        if item.relationship_id in matched_existing:
            continue
        if item.relation_source is RelationshipSource.GEOMETRIC and observation.complete:
            staled += 1
            kept.append(_retired(item, timestamp, status=RelationStatus.STALE))
            continue
        kept.append(item)

    # Retracted and stale rows stay in the graph as history, bounded.
    history = [item for item in inactive if item.relationship_id not in matched_existing]
    combined = _ordered((*kept, *history))
    capped = len(combined) > MAX_RELATIONSHIPS
    return RelationshipOutcome(
        relationships=combined[:MAX_RELATIONSHIPS],
        added=added,
        reactivated=reactivated,
        staled=staled,
        retracted=retracted,
        capped=capped,
        derived_from_geometry=derived,
        conflicts=tuple(conflicts),
    )


def _candidates_from(
    observation: Observation, id_by_hint: Mapping[str, str]
) -> list[WorldRelationship]:
    """World relations this observation supports, from hints or explicit statements."""
    rows: list[WorldRelationship] = []
    hints = observation.metadata.get("relationship_hints")
    if isinstance(hints, (list, tuple)):
        for row in hints:
            if not isinstance(row, Mapping):
                continue
            kind = _world_kind(row.get("kind"))
            if kind is None:
                continue
            subject = id_by_hint.get(as_text(row.get("subject_hint")))
            target = id_by_hint.get(as_text(row.get("object_hint")))
            if not subject or not target or subject == target:
                continue
            rows.append(
                WorldRelationship(
                    kind=kind,
                    source_entity_id=subject,
                    target_entity_id=target,
                    confidence=as_optional_float(row.get("confidence")),
                    evidence=_hint_evidence(row),
                    detail=as_text(row.get("evidence")),
                    relation_source=RelationshipSource.GEOMETRIC,
                    valid_from=observation.timestamp,
                )
            )
    stated = observation.metadata.get("relationships")
    if isinstance(stated, (list, tuple)):
        for row in stated:
            if not isinstance(row, Mapping):
                continue
            kind = _world_kind(row.get("kind"))
            if kind is None:
                continue
            subject = as_text(row.get("source_entity_id") or row.get("subject"))
            target = as_text(row.get("target_entity_id") or row.get("object"))
            if not subject or not target or subject == target:
                continue
            rows.append(
                WorldRelationship(
                    kind=kind,
                    source_entity_id=subject,
                    target_entity_id=target,
                    confidence=as_optional_float(row.get("confidence")),
                    evidence=(observation.observation_id,),
                    detail=as_text(row.get("detail")),
                    relation_source=RelationshipSource.STATED,
                    valid_from=observation.timestamp,
                )
            )
    return rows


def _hint_evidence(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Evidence ids for a hint: the scene it came from, and the arithmetic."""
    scene_id = as_text(row.get("scene_id"))
    return (scene_id,) if scene_id else ()


def _world_kind(value: Any) -> RelationshipKind | None:
    """A durable relationship kind from either vocabulary, or nothing.

    Accepts the world vocabulary directly and Phase 21's geometry names through
    the mapping above, so a caller may state ``owns``-shaped facts with either
    spelling this build has used.
    """
    text = as_text(value).lower()
    if not text:
        return None
    mapped = _PERCEPTION_KINDS.get(text)
    if mapped is not None:
        return mapped
    for kind in RelationshipKind:
        if kind.value == text:
            return kind
    return None


def derive_geometric_relations(
    resolved: Sequence[tuple[ObservedEntity, str]],
    *,
    timestamp: str,
    max_pairs: int = 120,
) -> tuple[list[WorldRelationship], bool]:
    """Geometry for an observation that carried boxes but no derived relations.

    This is the ONE reuse point for Phase 21's spatial layer: relations are derived
    by ``perception.spatial.derive_relationships`` rather than by a second copy of
    the arithmetic, and the entity ids are supplied as the ``DetectedObject`` ids
    so the returned relations already name THEM.
    """
    objects: list[DetectedObject] = []
    for item, entity_id in resolved:
        if not entity_id or item.bbox is None or not item.bbox.has_extent:
            continue
        objects.append(
            DetectedObject(
                label=item.label,
                bbox=item.bbox,
                confidence=item.confidence,
                source=item.source,
                object_id=entity_id,
            )
        )
    if len(objects) < 2:
        return [], False
    report = derive_relationships(objects, max_pairs=max_pairs)
    rows: list[WorldRelationship] = []
    for relation in report.relationships:
        kind = _PERCEPTION_KINDS.get(relation.kind.value)
        if kind is None:
            continue
        rows.append(
            WorldRelationship(
                kind=kind,
                source_entity_id=relation.subject_id,
                target_entity_id=relation.object_id,
                confidence=relation.confidence,
                evidence=(),
                detail=relation.evidence,
                relation_source=RelationshipSource.GEOMETRIC,
                valid_from=timestamp,
            )
        )
    return rows, bool(rows)


def _refreshed(
    incumbent: WorldRelationship, candidate: WorldRelationship, timestamp: str
) -> WorldRelationship:
    """The same claim, re-observed: ``valid_from`` stays, confidence and detail move."""
    return WorldRelationship(
        relationship_id=incumbent.relationship_id,
        kind=incumbent.kind,
        source_entity_id=incumbent.source_entity_id,
        target_entity_id=incumbent.target_entity_id,
        confidence=(
            candidate.confidence if candidate.confidence is not None else incumbent.confidence
        ),
        evidence=_bounded_evidence((*incumbent.evidence, *candidate.evidence)),
        detail=candidate.detail or incumbent.detail,
        relation_source=incumbent.relation_source,
        status=RelationStatus.ACTIVE,
        valid_from=incumbent.valid_from or timestamp,
        valid_until=None,
    )


def _reactivated(
    previous: WorldRelationship, candidate: WorldRelationship, timestamp: str
) -> WorldRelationship:
    """A relation that was stale or retracted and is supported again."""
    return WorldRelationship(
        relationship_id=previous.relationship_id,
        kind=previous.kind,
        source_entity_id=previous.source_entity_id,
        target_entity_id=previous.target_entity_id,
        confidence=candidate.confidence,
        evidence=_bounded_evidence((*previous.evidence, *candidate.evidence)),
        detail=candidate.detail or previous.detail,
        relation_source=previous.relation_source,
        status=RelationStatus.ACTIVE,
        valid_from=previous.valid_from or timestamp,
        valid_until=None,
    )


def _retired(
    previous: WorldRelationship,
    timestamp: str,
    *,
    status: RelationStatus = RelationStatus.RETRACTED,
) -> WorldRelationship:
    """A relation that stopped being asserted — kept, with the moment recorded."""
    return WorldRelationship(
        relationship_id=previous.relationship_id,
        kind=previous.kind,
        source_entity_id=previous.source_entity_id,
        target_entity_id=previous.target_entity_id,
        confidence=previous.confidence,
        evidence=previous.evidence,
        detail=previous.detail,
        relation_source=previous.relation_source,
        status=status,
        valid_from=previous.valid_from,
        valid_until=previous.valid_until or timestamp,
    )


def _contradiction(
    candidate: WorldRelationship, active: Sequence[WorldRelationship]
) -> WorldRelationship | None:
    """An ACTIVE relation on the same pair that cannot be true alongside this one."""
    for item in active:
        if {item.kind, candidate.kind} in _INCOMPATIBLE:
            same_pair = item.pair() == candidate.pair() or item.pair() == (
                candidate.target_entity_id,
                candidate.source_entity_id,
            )
            if same_pair:
                return item
    return None


def _bounded_evidence(evidence: Sequence[str], ceiling: int = 8) -> tuple[str, ...]:
    cleaned = [item for item in evidence if item]
    return tuple(dict.fromkeys(cleaned))[-ceiling:]


def _ordered(rows: Sequence[WorldRelationship]) -> tuple[WorldRelationship, ...]:
    """A stable order: by kind, then pair, then id — so output is reproducible."""
    weights = {kind: index for index, kind in enumerate(RELATION_ORDER)}
    return tuple(
        sorted(
            rows,
            key=lambda item: (
                weights.get(item.kind, len(weights)),
                item.source_entity_id,
                item.target_entity_id,
                item.relationship_id,
            ),
        )
    )
