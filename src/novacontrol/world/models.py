"""The world-model contracts: what Phase 22 stores, and what it refuses to claim.

Everything a world model knows becomes one of these records, and every record
follows the three rules the rest of this build already holds itself to:

**A fact that was not observed is not an observed fact.** Every attribute carries
a :class:`FactBasis` — ``OBSERVED``, ``INFERRED``, ``PREDICTED`` or ``UNKNOWN`` —
and nothing in this package promotes one into another. A value derived from two
observations is ``INFERRED`` and says so; a value projected forward is
``PREDICTED`` and the prediction's own result says it was a rule, not a model.

**A figure that was not measured is ``None``, never a zero.** Confidence and
uncertainty are ``None`` when nobody measured them. An unreadable timestamp
stays an empty string rather than becoming "now", because "now" is a claim.

**Provenance travels with the claim.** Every entity, relationship and transition
names the observation ids behind it, so "the laptop is on the desk" can be
checked against the frame it came from rather than believed.

Two vocabularies are deliberately distinct from their neighbours in this build.
``WorldStateTransition`` is an *environmental* transition (a fact about the world
changed), never the agentic ``StateTransition`` of Phase 20 (an action was
taken); conflating them would let an observation masquerade as a decision. And
:class:`RelationshipKind` here is the *world* vocabulary — the durable, temporal
relations a state graph holds — while ``perception.RelationshipKind`` is the
per-frame geometry vocabulary. The spatial layer derives geometry; this layer
decides what geometry is worth remembering and for how long.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4

# The geometry type is Phase 21's, reused rather than re-declared: a box in a
# frame means the same thing to both layers, and a second BBox would be a second
# rounding rule for the same pixel. Imported from the pure contracts module so
# this package never drags the perception engine (or a vision provider) in.
from novacontrol.perception.models import BBox

__all__ = [
    "AttributeChange",
    "ChangeEvent",
    "ChangeKind",
    "EntityAttribute",
    "EntityStatus",
    "EvidenceReference",
    "FactBasis",
    "IngestStatus",
    "Observation",
    "ObservationSource",
    "ObservedEntity",
    "PredictionRequest",
    "PredictionResult",
    "PredictionStatus",
    "QueryKind",
    "QueryStatus",
    "ReasoningConclusion",
    "RelationStatus",
    "RelationshipKind",
    "RelationshipSource",
    "RestoreReport",
    "StateEstimate",
    "StateQuery",
    "StateQueryResult",
    "UncertaintyKind",
    "UncertaintyRecord",
    "UpdateReport",
    "WorldEntity",
    "WorldRelationship",
    "WorldState",
    "WorldStateTransition",
    "TransitionKind",
    "as_bool",
    "as_optional_float",
    "as_optional_int",
    "as_text",
    "iso_now",
]


# ── vocabularies ─────────────────────────────────────────────────────────────


class FactBasis(StrEnum):
    """How a fact came to be known. Never silently upgraded (§5).

    ``OBSERVED`` — a source reported it directly.
    ``INFERRED`` — derived from other facts by a rule in this build.
    ``PREDICTED`` — projected forward; not a fact about what happened.
    ``UNKNOWN`` — recorded with no basis, which is itself information.
    """

    OBSERVED = "observed"
    INFERRED = "inferred"
    PREDICTED = "predicted"
    UNKNOWN = "unknown"


class ObservationSource(StrEnum):
    """Where an observation came from — the sources this build can actually read."""

    PERCEPTION = "perception"
    USER = "user"
    TOOL_RESULT = "tool_result"
    SYSTEM_EVENT = "system_event"
    EXTERNAL = "external"


class IngestStatus(StrEnum):
    """What ingestion did with one observation, and each value means something.

    ``ACCEPTED`` — applied to the state (possibly with partial facts).
    ``DUPLICATE`` — already ingested; the state did not change.
    ``STALE`` — older than the state it would update; applied, marked, and an
    uncertainty row was recorded rather than the observation being dropped.
    ``OUT_OF_ORDER`` — arrived after a newer observation from the same clock.
    ``PARTIAL`` — some facts were unreadable; the rest were applied.
    ``REJECTED`` — malformed or unsupported; nothing was applied.
    """

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    STALE = "stale"
    OUT_OF_ORDER = "out_of_order"
    PARTIAL = "partial"
    REJECTED = "rejected"


class EntityStatus(StrEnum):
    """Where an entity is in its life cycle — with absence kept honest (§6).

    ``PRESENT`` — observed in the most recent observation.
    ``NOT_OBSERVED`` — absent from the latest observation, which is NOT proof it
    is gone (the frame may not have covered it).
    ``MISSING`` — absence confirmed by evidence (a declared-complete observation
    of the same scope, once the miss threshold was reached).
    ``EXPIRED`` — no longer observed for longer than the retention window; kept
    as history, retired from the current state.
    """

    PRESENT = "present"
    NOT_OBSERVED = "not_observed"
    MISSING = "missing"
    EXPIRED = "expired"


class RelationshipKind(StrEnum):
    """Durable relations a state graph holds — the world vocabulary.

    Only kinds the evidence can support exist here. The four spatial ones
    (``LOCATED_NEAR``, ``INSIDE``, ``CONTAINS``, ``OBSERVED_WITH``) are decided
    by the geometry Phase 21 already derives; ``CONNECTED_TO``, ``OWNED_BY`` and
    ``ASSOCIATED_WITH`` only ever appear when a source *stated* them, because
    nothing in a frame can prove ownership.
    """

    LOCATED_NEAR = "located_near"
    INSIDE = "inside"
    CONTAINS = "contains"
    OBSERVED_WITH = "observed_with"
    CONNECTED_TO = "connected_to"
    OWNED_BY = "owned_by"
    ASSOCIATED_WITH = "associated_with"


class RelationshipSource(StrEnum):
    """Where a relationship came from — the four kinds §7 asks to distinguish."""

    GEOMETRIC = "geometric"
    STATED = "stated"
    INFERRED = "inferred"
    EXTERNAL = "external"


class RelationStatus(StrEnum):
    """A relation is true while it is supported, and stops being asserted when it is not."""

    ACTIVE = "active"
    STALE = "stale"
    RETRACTED = "retracted"


class TransitionKind(StrEnum):
    """What changed between two state versions — evidence-supported only (§10).

    ``ENTITY_CONFIRMED_MISSING`` is deliberately separate from
    ``ENTITY_NOT_OBSERVED``: the first is a conclusion the evidence supports, the
    second is the absence of a detection, which proves nothing on its own.
    """

    ENTITY_ADDED = "entity_added"
    ATTRIBUTE_CHANGED = "attribute_changed"
    ENTITY_MOVED = "entity_moved"
    RELATIONSHIP_CHANGED = "relationship_changed"
    ENTITY_NOT_OBSERVED = "entity_not_observed"
    ENTITY_CONFIRMED_MISSING = "entity_confirmed_missing"
    ENTITY_EXPIRED = "entity_expired"
    STATE_CONFLICT = "state_conflict"
    SCENE_CHANGED = "scene_changed"


class ChangeKind(StrEnum):
    """The changes the change-detection layer reports (§13)."""

    ENTITY_APPEARED = "entity_appeared"
    ENTITY_DISAPPEARED = "entity_disappeared"
    ATTRIBUTE_CHANGED = "attribute_changed"
    POSITION_CHANGED = "position_changed"
    RELATIONSHIP_CHANGED = "relationship_changed"
    STATE_STALE = "state_stale"
    OBSERVATION_CONFLICT = "observation_conflict"
    CONFIDENCE_CHANGED = "confidence_changed"
    ENVIRONMENT_CHANGED = "environment_changed"


class UncertaintyKind(StrEnum):
    """Why a fact is uncertain — named so a reader knows which question to ask."""

    LOW_CONFIDENCE = "low_confidence"
    CONFLICTING = "conflicting"
    STALE = "stale"
    AMBIGUOUS_IDENTITY = "ambiguous_identity"
    UNMEASURED = "unmeasured"
    OUT_OF_ORDER = "out_of_order"


class QueryKind(StrEnum):
    """The structured questions the state store answers (§14)."""

    ENTITIES = "entities"
    ENTITY = "entity"
    ENTITY_HISTORY = "entity_history"
    ENTITY_SEEN = "entity_seen"
    RELATIONSHIPS = "relationships"
    CHANGES_SINCE = "changes_since"
    STATE_AT = "state_at"
    UNCERTAIN = "uncertain"
    STALE = "stale"
    EVIDENCE = "evidence"
    DIFF = "diff"


class QueryStatus(StrEnum):
    """How a query ended: ``OK``, ``NOT_FOUND``, ``INVALID`` or ``EMPTY``.

    ``NOT_FOUND`` is the honest answer for a historical question the store
    cannot answer — it is never satisfied with the current state instead (§22D).
    """

    OK = "ok"
    EMPTY = "empty"
    NOT_FOUND = "not_found"
    INVALID = "invalid"


class PredictionStatus(StrEnum):
    """Every ending a prediction request may have, including the honest ones."""

    PREDICTED = "predicted"
    UNSUPPORTED = "unsupported"
    MODEL_UNAVAILABLE = "model_unavailable"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    RESOURCE_BLOCKED = "resource_blocked"
    FAILED = "failed"


# ── small helpers ────────────────────────────────────────────────────────────


def iso_now() -> str:
    """Now, as an ISO-8601 string. The one place this package reads a clock."""
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def as_text(value: Any, default: str = "") -> str:
    """A string, or the default — never the literal ``"None"``."""
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def as_bool(value: Any, default: bool = False) -> bool:
    """A boolean from whatever a caller sent (JSON booleans or config strings)."""
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


def as_optional_float(value: Any) -> float | None:
    """A probability, or ``None`` — never a fabricated zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def as_optional_int(value: Any) -> int | None:
    """An integer, or ``None`` — never a fabricated zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _strings(value: Any) -> tuple[str, ...]:
    """A tuple of non-empty strings from a list, a tuple or one bare string."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _mapping(value: Any) -> dict[str, Any]:
    """A plain dict from a mapping — an empty one from anything else."""
    return dict(value) if isinstance(value, Mapping) else {}


def _rows(value: Any) -> tuple[Any, ...]:
    """A tuple from a sequence — empty from anything else."""
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


# ── evidence and facts ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """A pointer to what supports a claim — an id, never the payload (§21).

    This is the privacy half of provenance: the state names the observation, the
    scene or the tool result a fact came from, and a reader that wants the detail
    asks the subsystem that owns it. Raw frames, masks and text blocks are never
    copied into the state store.
    """

    evidence_id: str = ""
    kind: str = "observation"
    source: str = ""
    detail: str = ""
    timestamp: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> EvidenceReference:
        return cls(
            evidence_id=as_text(data.get("evidence_id") or data.get("id")),
            kind=as_text(data.get("kind"), "observation"),
            source=as_text(data.get("source")),
            detail=as_text(data.get("detail")),
            timestamp=as_text(data.get("timestamp")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "source": self.source,
            "detail": self.detail,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True, slots=True)
class EntityAttribute:
    """One named fact about an entity, with its basis and its support.

    ``value`` is JSON-safe data (a label, a number, a nested mapping from a tool
    result). ``basis`` is what keeps an inference from being read as an
    observation; ``confidence`` is ``None`` when nothing measured it.
    """

    name: str
    value: Any = None
    basis: FactBasis = FactBasis.OBSERVED
    confidence: float | None = None
    evidence: tuple[str, ...] = ()
    observed_at: str = ""
    source: str = ""

    @classmethod
    def observed(
        cls,
        name: str,
        value: Any,
        *,
        confidence: float | None = None,
        evidence: Sequence[str] = (),
        observed_at: str = "",
        source: str = "",
    ) -> EntityAttribute:
        """A fact a source reported — the constructor callers should reach for."""
        return cls(
            name=as_text(name),
            value=value,
            basis=FactBasis.OBSERVED,
            confidence=confidence,
            evidence=tuple(str(item) for item in evidence if str(item)),
            observed_at=observed_at,
            source=source,
        )

    @classmethod
    def inferred(
        cls,
        name: str,
        value: Any,
        *,
        confidence: float | None = None,
        evidence: Sequence[str] = (),
        observed_at: str = "",
        source: str = "state_estimation",
    ) -> EntityAttribute:
        """A fact derived from other facts — labelled as derived, never as seen."""
        return cls(
            name=as_text(name),
            value=value,
            basis=FactBasis.INFERRED,
            confidence=confidence,
            evidence=tuple(str(item) for item in evidence if str(item)),
            observed_at=observed_at,
            source=source,
        )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> EntityAttribute:
        try:
            basis = FactBasis(as_text(data.get("basis"), FactBasis.UNKNOWN.value))
        except ValueError:
            basis = FactBasis.UNKNOWN
        return cls(
            name=as_text(data.get("name")),
            value=data.get("value"),
            basis=basis,
            confidence=as_optional_float(data.get("confidence")),
            evidence=_strings(data.get("evidence")),
            observed_at=as_text(data.get("observed_at")),
            source=as_text(data.get("source")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "basis": self.basis.value,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "observed_at": self.observed_at,
            "source": self.source,
        }


# ── entities ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WorldEntity:
    """One thing the world model believes exists, with how sure it is (§6).

    ``entity_id`` is THIS layer's stable identity, resolved from evidence rather
    than assigned by a source: a Phase 21 track id is a hint, not an identity,
    because a track is "the thing that was there last frame" while an entity is
    "the thing we have been watching". ``provisional`` is ``True`` when the
    match was ambiguous — the entity exists, but its continuity is a guess the
    state says out loud instead of hiding.
    """

    entity_id: str = field(default_factory=lambda: uuid4().hex[:12])
    entity_type: str = "object"
    labels: tuple[str, ...] = ()
    attributes: tuple[EntityAttribute, ...] = ()
    bbox: BBox | None = None
    status: EntityStatus = EntityStatus.PRESENT
    first_seen: str = ""
    last_seen: str = ""
    last_confirmed: str = ""
    missed_observations: int = 0
    observed_count: int = 0
    evidence: tuple[str, ...] = ()
    identity_confidence: float | None = None
    provisional: bool = False
    provenance: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    #: The scope this entity was last observed in. Absence is only evaluated
    #: against an observation of the SAME scope, so a screenshot of one window
    #: is never read as evidence about another (§5).
    scope: str = ""

    @property
    def label(self) -> str:
        """The first label, or an empty string — never an invented name."""
        return self.labels[0] if self.labels else ""

    @property
    def visible(self) -> bool:
        return self.status is EntityStatus.PRESENT

    def attribute(self, name: str) -> EntityAttribute | None:
        """One attribute by name, or nothing when it was never observed."""
        for item in self.attributes:
            if item.name == name:
                return item
        return None

    def value_of(self, name: str, default: Any = None) -> Any:
        item = self.attribute(name)
        return item.value if item is not None else default

    def evidence_for(self, name: str) -> tuple[str, ...]:
        """The observation ids behind one attribute — for an evidence query."""
        item = self.attribute(name)
        return item.evidence if item is not None else ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "label": self.label,
            "labels": list(self.labels),
            "attributes": [item.to_dict() for item in self.attributes],
            "bbox": self.bbox.to_dict() if self.bbox is not None else None,
            "status": self.status.value,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "last_confirmed": self.last_confirmed,
            "missed_observations": self.missed_observations,
            "observed_count": self.observed_count,
            "evidence": list(self.evidence),
            "identity_confidence": self.identity_confidence,
            "provisional": self.provisional,
            "provenance": list(self.provenance),
            "source_ids": list(self.source_ids),
            "scope": self.scope,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> WorldEntity:
        """An entity from a stored record — tolerant of a partial one."""
        bbox_row = data.get("bbox")
        labels = _strings(data.get("labels"))
        if not labels:
            single = as_text(data.get("label"))
            labels = (single,) if single else ()
        try:
            status = EntityStatus(as_text(data.get("status"), EntityStatus.PRESENT.value))
        except ValueError:
            status = EntityStatus.PRESENT
        return cls(
            entity_id=as_text(data.get("entity_id")) or uuid4().hex[:12],
            entity_type=as_text(data.get("entity_type"), "object"),
            labels=labels,
            attributes=tuple(
                EntityAttribute.from_mapping(row)
                for row in _rows(data.get("attributes"))
                if isinstance(row, Mapping)
            ),
            bbox=(
                BBox.from_mapping(bbox_row) if isinstance(bbox_row, Mapping) else None
            ),
            status=status,
            first_seen=as_text(data.get("first_seen")),
            last_seen=as_text(data.get("last_seen")),
            last_confirmed=as_text(data.get("last_confirmed")),
            missed_observations=max(0, as_optional_int(data.get("missed_observations")) or 0),
            observed_count=max(0, as_optional_int(data.get("observed_count")) or 0),
            evidence=_strings(data.get("evidence")),
            identity_confidence=as_optional_float(data.get("identity_confidence")),
            provisional=as_bool(data.get("provisional")),
            provenance=_strings(data.get("provenance")),
            source_ids=_strings(data.get("source_ids")),
            scope=as_text(data.get("scope")),
        )


# ── relationships ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WorldRelationship:
    """One relation between two entities, true while the evidence supports it (§7).

    ``valid_until`` is set when the relation stops being asserted, and the record
    is kept: a relation that WAS true is history, and deleting it would make the
    state claim it never happened. ``evidence`` is the arithmetic or the source
    that decided it — never blank for a geometric relation.
    """

    relationship_id: str = field(default_factory=lambda: uuid4().hex[:12])
    kind: RelationshipKind = RelationshipKind.LOCATED_NEAR
    source_entity_id: str = ""
    target_entity_id: str = ""
    confidence: float | None = None
    evidence: tuple[str, ...] = ()
    detail: str = ""
    relation_source: RelationshipSource = RelationshipSource.GEOMETRIC
    status: RelationStatus = RelationStatus.ACTIVE
    valid_from: str = ""
    valid_until: str | None = None

    @property
    def active(self) -> bool:
        return self.status is RelationStatus.ACTIVE

    def pair(self) -> tuple[str, str]:
        return (self.source_entity_id, self.target_entity_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "relationship_id": self.relationship_id,
            "kind": self.kind.value,
            "source_entity_id": self.source_entity_id,
            "target_entity_id": self.target_entity_id,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "detail": self.detail,
            "relation_source": self.relation_source.value,
            "status": self.status.value,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> WorldRelationship | None:
        """A relationship from a stored record, or ``None`` when it is unusable.

        A stored row with no endpoints is not a relationship: returning ``None``
        lets the store report a skipped record instead of inventing a pair.
        """
        source_id = as_text(data.get("source_entity_id"))
        target_id = as_text(data.get("target_entity_id"))
        if not source_id or not target_id:
            return None
        try:
            kind = RelationshipKind(as_text(data.get("kind"), RelationshipKind.LOCATED_NEAR.value))
        except ValueError:
            kind = RelationshipKind.ASSOCIATED_WITH
        try:
            relation_source = RelationshipSource(
                as_text(data.get("relation_source"), RelationshipSource.GEOMETRIC.value)
            )
        except ValueError:
            relation_source = RelationshipSource.EXTERNAL
        try:
            status = RelationStatus(as_text(data.get("status"), RelationStatus.ACTIVE.value))
        except ValueError:
            status = RelationStatus.ACTIVE
        return cls(
            relationship_id=as_text(data.get("relationship_id")) or uuid4().hex[:12],
            kind=kind,
            source_entity_id=source_id,
            target_entity_id=target_id,
            confidence=as_optional_float(data.get("confidence")),
            evidence=_strings(data.get("evidence")),
            detail=as_text(data.get("detail")),
            relation_source=relation_source,
            status=status,
            valid_from=as_text(data.get("valid_from")),
            valid_until=as_text(data.get("valid_until")) or None,
        )


# ── observations ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ObservedEntity:
    """One thing a source reported seeing — before identity is resolved.

    ``entity_id`` and ``track_id`` are HINTS from the source (a Phase 21 track,
    an object id); the estimator decides whether a hint means continuity. Both
    are empty when the source cannot say, which is the normal case for a person
    typing what they see.
    """

    label: str = ""
    entity_type: str = "object"
    entity_id: str = ""
    track_id: str = ""
    bbox: BBox | None = None
    confidence: float | None = None
    attributes: tuple[EntityAttribute, ...] = ()
    source: str = ""
    text: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ObservedEntity:
        bbox_row = data.get("bbox")
        return cls(
            label=as_text(data.get("label") or data.get("name")),
            entity_type=as_text(data.get("entity_type"), "object"),
            entity_id=as_text(data.get("entity_id")),
            track_id=as_text(data.get("track_id")),
            bbox=BBox.from_mapping(bbox_row) if isinstance(bbox_row, Mapping) else None,
            confidence=as_optional_float(data.get("confidence")),
            attributes=tuple(
                EntityAttribute.from_mapping(row)
                for row in _rows(data.get("attributes"))
                if isinstance(row, Mapping)
            ),
            source=as_text(data.get("source")),
            text=as_text(data.get("text")),
            metadata=_mapping(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "track_id": self.track_id,
            "bbox": self.bbox.to_dict() if self.bbox is not None else None,
            "confidence": self.confidence,
            "attributes": [item.to_dict() for item in self.attributes],
            "source": self.source,
            "text": self.text,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class Observation:
    """One normalized report about the world, with everything needed to rank it.

    ``complete`` is the field that keeps absence honest: a source that declares
    it saw the WHOLE scope (e.g. "this is the full window tree") lets the
    estimator treat a missing entity as a real miss; a source that did not (a
    screenshot of one region) gets no such conclusion. ``clock`` names the clock
    domain the timestamp came from, because two unrelated clocks are not
    comparable and pretending otherwise is how a replayed file corrupts a state.
    """

    observation_id: str = field(default_factory=lambda: uuid4().hex[:12])
    source: ObservationSource = ObservationSource.EXTERNAL
    source_id: str = ""
    timestamp: str = ""
    #: The region or channel this observation covered. Empty means "unscoped",
    #: which is the honest default and lets a complete observation evaluate
    #: absence for everything that is not region-scoped elsewhere.
    scope: str = ""
    complete: bool = False
    entities: tuple[ObservedEntity, ...] = ()
    facts: tuple[EntityAttribute, ...] = ()
    confidence: float | None = None
    evidence: tuple[EvidenceReference, ...] = ()
    correlation_id: str = ""
    clock: str = "wall"
    text: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def entity_count(self) -> int:
        return len(self.entities)

    def labels(self) -> tuple[str, ...]:
        return tuple(item.label for item in self.entities if item.label)

    def content_key(self) -> str:
        """A stable fingerprint of what this observation CLAIMS.

        Used for duplicate detection: two reports with the same source, scope,
        timestamp and content are one report delivered twice, not two facts. The
        fingerprint covers claims and never the metadata, so a retry with a new
        id but the same content is still a duplicate.
        """
        import json

        payload = {
            "source": self.source.value,
            "source_id": self.source_id,
            "scope": self.scope,
            "timestamp": self.timestamp,
            "complete": self.complete,
            "entities": [
                {
                    "label": item.label,
                    "entity_type": item.entity_type,
                    "entity_id": item.entity_id,
                    "track_id": item.track_id,
                    "bbox": item.bbox.to_dict() if item.bbox is not None else None,
                    "attributes": [
                        {"name": attr.name, "value": attr.value} for attr in item.attributes
                    ],
                }
                for item in self.entities
            ],
            "facts": [{"name": item.name, "value": item.value} for item in self.facts],
        }
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        import hashlib

        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "source": self.source.value,
            "source_id": self.source_id,
            "timestamp": self.timestamp,
            "scope": self.scope,
            "complete": self.complete,
            "entities": [item.to_dict() for item in self.entities],
            "facts": [item.to_dict() for item in self.facts],
            "confidence": self.confidence,
            "evidence": [item.to_dict() for item in self.evidence],
            "correlation_id": self.correlation_id,
            "clock": self.clock,
            "text": self.text,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Observation:
        """An observation from an API body or a stored record.

        An unknown source falls back to ``EXTERNAL`` rather than raising: the
        source label is metadata, and refusing an observation because a caller
        invented a source name would trade a usable fact for a spelling lesson.
        """
        raw_source = as_text(data.get("source"), ObservationSource.EXTERNAL.value)
        try:
            source = ObservationSource(raw_source)
        except ValueError:
            source = ObservationSource.EXTERNAL
        return cls(
            observation_id=as_text(data.get("observation_id")) or uuid4().hex[:12],
            source=source,
            source_id=as_text(data.get("source_id")),
            timestamp=as_text(data.get("timestamp")),
            scope=as_text(data.get("scope")),
            complete=as_bool(data.get("complete")),
            entities=tuple(
                ObservedEntity.from_mapping(row)
                for row in _rows(data.get("entities"))
                if isinstance(row, Mapping)
            ),
            facts=tuple(
                EntityAttribute.from_mapping(row)
                for row in _rows(data.get("facts"))
                if isinstance(row, Mapping)
            ),
            confidence=as_optional_float(data.get("confidence")),
            evidence=tuple(
                EvidenceReference.from_mapping(row)
                for row in _rows(data.get("evidence"))
                if isinstance(row, Mapping)
            ),
            correlation_id=as_text(data.get("correlation_id")),
            clock=as_text(data.get("clock"), "wall"),
            text=as_text(data.get("text")),
            metadata=_mapping(data.get("metadata")),
        )


# ── state ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WorldState:
    """The environment at one version, in one serializable record (§5).

    ``version`` is monotonic within a world, and ``previous_state_id`` links the
    chain so history is walkable without holding every state in memory.
    ``sources`` names the observation sources that fed this version, which is
    what makes "derived conditions" auditable: every condition in ``conditions``
    was either observed by a named source or inferred by a named rule.
    """

    world_id: str = "default"
    state_id: str = field(default_factory=lambda: uuid4().hex[:12])
    version: int = 0
    timestamp: str = ""
    previous_state_id: str = ""
    entities: tuple[WorldEntity, ...] = ()
    relationships: tuple[WorldRelationship, ...] = ()
    conditions: tuple[EntityAttribute, ...] = ()
    uncertainty: tuple[UncertaintyRecord, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()
    sources: tuple[str, ...] = ()
    confidence: float | None = None
    schema_version: str = "phase22.1"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def entity(self, entity_id: str) -> WorldEntity | None:
        for item in self.entities:
            if item.entity_id == entity_id:
                return item
        return None

    def find(self, label: str) -> tuple[WorldEntity, ...]:
        """Entities carrying a label — case-insensitive, never fuzzy."""
        wanted = str(label).strip().lower()
        return tuple(
            item
            for item in self.entities
            if any(name.lower() == wanted for name in item.labels)
        )

    def active_relationships(self) -> tuple[WorldRelationship, ...]:
        return tuple(item for item in self.relationships if item.active)

    def stale_entities(self) -> tuple[WorldEntity, ...]:
        return tuple(
            item
            for item in self.entities
            if item.status in {EntityStatus.NOT_OBSERVED, EntityStatus.MISSING}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "world_id": self.world_id,
            "state_id": self.state_id,
            "version": self.version,
            "timestamp": self.timestamp,
            "previous_state_id": self.previous_state_id,
            "entities": [item.to_dict() for item in self.entities],
            "relationships": [item.to_dict() for item in self.relationships],
            "conditions": [item.to_dict() for item in self.conditions],
            "uncertainty": [item.to_dict() for item in self.uncertainty],
            "evidence": [item.to_dict() for item in self.evidence],
            "sources": list(self.sources),
            "confidence": self.confidence,
            "schema_version": self.schema_version,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> WorldState:
        """A state from a stored record, skipping rows that cannot be read.

        A single malformed entity must not cost the whole state: the readable
        rows are kept and the rest are simply absent, which the store reports as
        skipped records rather than hiding behind a blanket failure.
        """
        return cls(
            world_id=as_text(data.get("world_id"), "default"),
            state_id=as_text(data.get("state_id")) or uuid4().hex[:12],
            version=max(0, as_optional_int(data.get("version")) or 0),
            timestamp=as_text(data.get("timestamp")),
            previous_state_id=as_text(data.get("previous_state_id")),
            entities=tuple(
                WorldEntity.from_mapping(row)
                for row in _rows(data.get("entities"))
                if isinstance(row, Mapping)
            ),
            relationships=tuple(
                item
                for item in (
                    WorldRelationship.from_mapping(row)
                    for row in _rows(data.get("relationships"))
                    if isinstance(row, Mapping)
                )
                if item is not None
            ),
            conditions=tuple(
                EntityAttribute.from_mapping(row)
                for row in _rows(data.get("conditions"))
                if isinstance(row, Mapping)
            ),
            uncertainty=tuple(
                UncertaintyRecord.from_mapping(row)
                for row in _rows(data.get("uncertainty"))
                if isinstance(row, Mapping)
            ),
            evidence=tuple(
                EvidenceReference.from_mapping(row)
                for row in _rows(data.get("evidence"))
                if isinstance(row, Mapping)
            ),
            sources=_strings(data.get("sources")),
            confidence=as_optional_float(data.get("confidence")),
            schema_version=as_text(data.get("schema_version"), "phase22.1"),
            metadata=_mapping(data.get("metadata")),
        )


# ── uncertainty, transitions and changes ─────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UncertaintyRecord:
    """One thing the state is unsure about, and which kind of unsure it is (§5/§13)."""

    subject: str = ""
    kind: UncertaintyKind = UncertaintyKind.UNMEASURED
    detail: str = ""
    confidence: float | None = None
    evidence: tuple[str, ...] = ()
    recorded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "kind": self.kind.value,
            "detail": self.detail,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> UncertaintyRecord:
        try:
            kind = UncertaintyKind(as_text(data.get("kind"), UncertaintyKind.UNMEASURED.value))
        except ValueError:
            kind = UncertaintyKind.UNMEASURED
        return cls(
            subject=as_text(data.get("subject")),
            kind=kind,
            detail=as_text(data.get("detail")),
            confidence=as_optional_float(data.get("confidence")),
            evidence=_strings(data.get("evidence")),
            recorded_at=as_text(data.get("recorded_at")),
        )


@dataclass(frozen=True, slots=True)
class WorldStateTransition:
    """One environmental change between two state versions (§10).

    Not Phase 20's ``StateTransition``: that one records an ACTION and its
    outcome, and this one records a fact about the environment changing. Keeping
    them separate is what stops an observation from being read as a decision.
    """

    transition_id: str = field(default_factory=lambda: uuid4().hex[:12])
    kind: TransitionKind = TransitionKind.SCENE_CHANGED
    previous_state_id: str = ""
    current_state_id: str = ""
    timestamp: str = ""
    entity_id: str = ""
    attribute: str = ""
    previous: Any = None
    current: Any = None
    detail: str = ""
    confidence: float | None = None
    evidence: tuple[str, ...] = ()
    world_id: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "kind": self.kind.value,
            "previous_state_id": self.previous_state_id,
            "current_state_id": self.current_state_id,
            "timestamp": self.timestamp,
            "entity_id": self.entity_id,
            "attribute": self.attribute,
            "previous": self.previous,
            "current": self.current,
            "detail": self.detail,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "world_id": self.world_id,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> WorldStateTransition:
        try:
            kind = TransitionKind(as_text(data.get("kind"), TransitionKind.SCENE_CHANGED.value))
        except ValueError:
            kind = TransitionKind.SCENE_CHANGED
        return cls(
            transition_id=as_text(data.get("transition_id")) or uuid4().hex[:12],
            kind=kind,
            previous_state_id=as_text(data.get("previous_state_id")),
            current_state_id=as_text(data.get("current_state_id")),
            timestamp=as_text(data.get("timestamp")),
            entity_id=as_text(data.get("entity_id")),
            attribute=as_text(data.get("attribute")),
            previous=data.get("previous"),
            current=data.get("current"),
            detail=as_text(data.get("detail")),
            confidence=as_optional_float(data.get("confidence")),
            evidence=_strings(data.get("evidence")),
            world_id=as_text(data.get("world_id"), "default"),
        )


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """One meaningful difference the change detector reported (§13).

    A change event is emitted for a MEANINGFUL difference only: thresholds decide
    what counts, and an identical repeat of the previous observation produces no
    events at all rather than a stream of no-ops.
    """

    change_id: str = field(default_factory=lambda: uuid4().hex[:12])
    kind: ChangeKind = ChangeKind.ENVIRONMENT_CHANGED
    entity_id: str = ""
    detail: str = ""
    previous: Any = None
    current: Any = None
    magnitude: float | None = None
    confidence: float | None = None
    timestamp: str = ""
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "kind": self.kind.value,
            "entity_id": self.entity_id,
            "detail": self.detail,
            "previous": self.previous,
            "current": self.current,
            "magnitude": self.magnitude,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class AttributeChange:
    """One attribute that differs between two versions of an entity."""

    name: str
    previous: Any = None
    current: Any = None
    previous_basis: FactBasis = FactBasis.UNKNOWN
    current_basis: FactBasis = FactBasis.UNKNOWN
    magnitude: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "previous": self.previous,
            "current": self.current,
            "previous_basis": self.previous_basis.value,
            "current_basis": self.current_basis.value,
            "magnitude": self.magnitude,
        }


# ── estimation and update results ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StateEstimate:
    """What the estimator decided about ONE observed entity (§9).

    ``decision`` is the honest verb: ``new_entity``, ``matched``, ``ambiguous``,
    ``uncertain`` or ``no_change``. ``ambiguous`` means candidates existed and the
    evidence did not choose, which produces a provisional identity rather than a
    confident merge.
    """

    decision: str = "new_entity"
    entity_id: str = ""
    matched_entity_id: str = ""
    label: str = ""
    reason: str = ""
    confidence: float | None = None
    candidates: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "entity_id": self.entity_id,
            "matched_entity_id": self.matched_entity_id,
            "label": self.label,
            "reason": self.reason,
            "confidence": self.confidence,
            "candidates": list(self.candidates),
        }


@dataclass(frozen=True, slots=True)
class UpdateReport:
    """The whole outcome of ingesting one observation (§8/§23).

    A rejection is a report, not an exception: a malformed observation is data
    about a caller's mistake, and a state layer that raised would let one bad
    caller stop the world. ``status`` says what happened, ``reason`` why, and the
    rest counts what moved.
    """

    status: IngestStatus = IngestStatus.REJECTED
    reason: str = ""
    observation_id: str = ""
    state_id: str = ""
    version: int = 0
    entities_added: int = 0
    entities_updated: int = 0
    entities_not_observed: int = 0
    entities_expired: int = 0
    relationships_added: int = 0
    relationships_retracted: int = 0
    transitions: tuple[WorldStateTransition, ...] = ()
    changes: tuple[ChangeEvent, ...] = ()
    estimates: tuple[StateEstimate, ...] = ()
    uncertainty: tuple[UncertaintyRecord, ...] = ()
    duplicate: bool = False
    stale: bool = False
    elapsed_ms: float | None = None

    @property
    def accepted(self) -> bool:
        return self.status is not IngestStatus.REJECTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "observation_id": self.observation_id,
            "state_id": self.state_id,
            "version": self.version,
            "entities_added": self.entities_added,
            "entities_updated": self.entities_updated,
            "entities_not_observed": self.entities_not_observed,
            "entities_expired": self.entities_expired,
            "relationships_added": self.relationships_added,
            "relationships_retracted": self.relationships_retracted,
            "transitions": [item.to_dict() for item in self.transitions],
            "changes": [item.to_dict() for item in self.changes],
            "estimates": [item.to_dict() for item in self.estimates],
            "uncertainty": [item.to_dict() for item in self.uncertainty],
            "duplicate": self.duplicate,
            "stale": self.stale,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True, slots=True)
class RestoreReport:
    """What a restore actually recovered, including what it could not (§12).

    ``restored`` is False when there was nothing to restore, and a store that
    half-read a file says so — "state restored" is never claimed on the strength
    of a file having existed.
    """

    restored: bool = False
    reason: str = ""
    state_id: str = ""
    version: int = 0
    transitions: int = 0
    snapshots: int = 0
    observations: int = 0
    skipped_records: int = 0
    skipped_reasons: tuple[str, ...] = ()
    pruned: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "restored": self.restored,
            "reason": self.reason,
            "state_id": self.state_id,
            "version": self.version,
            "transitions": self.transitions,
            "snapshots": self.snapshots,
            "observations": self.observations,
            "skipped_records": self.skipped_records,
            "skipped_reasons": list(self.skipped_reasons),
            "pruned": self.pruned,
        }


# ── queries and reasoning ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StateQuery:
    """One structured question about current or historical state (§14).

    Every field is a filter the store applies, and ``limit`` is bounded by the
    caller's own ceiling on read: an unbounded history question is a denial of
    service dressed as curiosity.
    """

    kind: QueryKind = QueryKind.ENTITIES
    entity_id: str = ""
    label: str = ""
    entity_type: str = ""
    relationship_kind: str = ""
    source: str = ""
    status: str = ""
    since: str = ""
    until: str = ""
    state_id: str = ""
    other_state_id: str = ""
    min_confidence: float | None = None
    limit: int = 20
    subject: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> StateQuery:
        """A query from an API body; an unknown kind becomes ``ENTITIES``.

        Not an error: the kind is the question's shape, and answering the safest
        question (what is known) is more useful than refusing the whole call over
        one unrecognised word. The result's ``kind`` says which question ran.
        """
        raw_kind = as_text(data.get("kind"), QueryKind.ENTITIES.value)
        try:
            kind = QueryKind(raw_kind)
        except ValueError:
            kind = QueryKind.ENTITIES
        limit = as_optional_int(data.get("limit"))
        return cls(
            kind=kind,
            entity_id=as_text(data.get("entity_id")),
            label=as_text(data.get("label")),
            entity_type=as_text(data.get("entity_type")),
            relationship_kind=as_text(data.get("relationship_kind")),
            source=as_text(data.get("source")),
            status=as_text(data.get("status")),
            since=as_text(data.get("since")),
            until=as_text(data.get("until")),
            state_id=as_text(data.get("state_id")),
            other_state_id=as_text(data.get("other_state_id")),
            min_confidence=as_optional_float(data.get("min_confidence")),
            limit=max(1, min(200, limit if limit is not None else 20)),
            subject=as_text(data.get("subject") or data.get("query")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "entity_id": self.entity_id,
            "label": self.label,
            "entity_type": self.entity_type,
            "relationship_kind": self.relationship_kind,
            "source": self.source,
            "status": self.status,
            "since": self.since,
            "until": self.until,
            "state_id": self.state_id,
            "other_state_id": self.other_state_id,
            "min_confidence": self.min_confidence,
            "limit": self.limit,
            "subject": self.subject,
        }


@dataclass(frozen=True, slots=True)
class StateQueryResult:
    """The answer to one query, with what it could NOT say (§14).

    ``limitations`` is not decoration: a history question that was answered from
    the oldest retained snapshot says so here, and an evidence query that found
    the claim but no support says so too.
    """

    kind: QueryKind = QueryKind.ENTITIES
    status: QueryStatus = QueryStatus.EMPTY
    rows: tuple[Mapping[str, Any], ...] = ()
    count: int = 0
    state_id: str = ""
    version: int = 0
    timestamp: str = ""
    reason: str = ""
    evidence: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    query: StateQuery | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "status": self.status.value,
            "rows": [dict(row) for row in self.rows],
            "count": self.count,
            "state_id": self.state_id,
            "version": self.version,
            "timestamp": self.timestamp,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "limitations": list(self.limitations),
            "query": self.query.to_dict() if self.query is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ReasoningConclusion:
    """One bounded conclusion about the state, with the rule that produced it (§15).

    There is deliberately no field for private reasoning: ``rule`` names the
    deterministic rule, ``conclusion`` is the short statement a person reads, and
    ``evidence`` points at the observations behind it. A conclusion whose support
    was insufficient says so in ``limitations`` rather than being withheld — the
    reader is told what the state cannot answer.
    """

    rule: str = ""
    conclusion: str = ""
    category: str = "state"
    evidence: tuple[str, ...] = ()
    confidence: float | None = None
    limitations: tuple[str, ...] = ()
    state_id: str = ""
    subject: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "conclusion": self.conclusion,
            "category": self.category,
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "limitations": list(self.limitations),
            "state_id": self.state_id,
            "subject": self.subject,
            "payload": dict(self.payload),
        }


# ── prediction ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PredictionRequest:
    """What is being predicted, over what horizon, under what budget (§16)."""

    target: str = ""
    horizon_seconds: float | None = None
    constraints: Mapping[str, Any] = field(default_factory=dict)
    resource_budget: Mapping[str, Any] = field(default_factory=dict)
    state_id: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> PredictionRequest:
        horizon = data.get("horizon_seconds")
        try:
            seconds = float(horizon) if horizon is not None else None
        except (TypeError, ValueError):
            seconds = None
        return cls(
            target=as_text(data.get("target") or data.get("entity_id")),
            horizon_seconds=seconds,
            constraints=_mapping(data.get("constraints")),
            resource_budget=_mapping(data.get("resource_budget")),
            state_id=as_text(data.get("state_id")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "horizon_seconds": self.horizon_seconds,
            "constraints": dict(self.constraints),
            "resource_budget": dict(self.resource_budget),
            "state_id": self.state_id,
        }


@dataclass(frozen=True, slots=True)
class PredictionResult:
    """The honest outcome of a prediction request — including "not available" (§16).

    ``rule_based`` is the field that keeps a projection from being read as
    learned intelligence: when it is True the ``provider`` names a rule and the
    ``limitations`` say in words that no predictive model was involved.
    """

    status: PredictionStatus = PredictionStatus.MODEL_UNAVAILABLE
    reason: str = ""
    provider: str = ""
    predicted: Mapping[str, Any] = field(default_factory=dict)
    confidence: float | None = None
    state_id: str = ""
    evidence: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    rule_based: bool = False
    elapsed_ms: float | None = None
    request: PredictionRequest | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "provider": self.provider,
            "predicted": dict(self.predicted),
            "confidence": self.confidence,
            "state_id": self.state_id,
            "evidence": list(self.evidence),
            "limitations": list(self.limitations),
            "rule_based": self.rule_based,
            "elapsed_ms": self.elapsed_ms,
            "request": self.request.to_dict() if self.request is not None else None,
        }
