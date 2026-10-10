"""State queries: the structured questions the state store answers, and how it declines (§14).

Two rules shape every answer:

**A historical question is never answered with the current state.** ``state_at``
returns the newest retained version that actually covers the moment. When none
does, the result is ``NOT_FOUND`` with the reason and a stated limitation — the
current state wearing an old timestamp would be the single most misleading thing
this layer could do (§22D).

**A query cannot ask for everything.** Every read is bounded by ``limit`` (capped
at 200), and the rows returned are summaries: an entity query returns the entity
records, not its whole observation history, and an evidence query returns
references rather than the referenced content. There is no query kind that walks
the store unbounded, which is what §14's "no unrestricted query execution" means
in practice.

Input validation is part of the contract: a query that names no entity for a
question that is about one entity is ``INVALID`` with the missing field named,
rather than a confident empty answer that looks like "no such thing".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from novacontrol.world.changes import ChangeThresholds, detect_changes
from novacontrol.world.memory import TemporalMemory
from novacontrol.world.models import (
    EntityStatus,
    QueryKind,
    QueryStatus,
    RelationshipKind,
    RelationStatus,
    StateQuery,
    StateQueryResult,
    WorldEntity,
    WorldRelationship,
    WorldState,
    as_optional_float,
)
from novacontrol.world.timeutil import parse_timestamp

__all__ = [
    "MAX_LIMIT",
    "MIN_LIMIT",
    "run_query",
]

#: The bound one query may return. Small on purpose: a state question is answered
#: with the current facts, and a caller that wants a walk asks for a smaller slice
#: several times rather than one unbounded read.
MAX_LIMIT = 200
MIN_LIMIT = 1


def run_query(
    query: StateQuery,
    *,
    memory: TemporalMemory,
    world_id: str = "default",
    thresholds: ChangeThresholds | None = None,
) -> StateQueryResult:
    """Answer one structured question about current or historical state."""
    limit = max(MIN_LIMIT, min(MAX_LIMIT, int(query.limit or MIN_LIMIT)))
    current = memory.state
    if current is None:
        return StateQueryResult(
            kind=query.kind,
            status=QueryStatus.EMPTY,
            reason="this world has never been observed, so there is no state to query",
            limitations=("no state exists yet",),
            query=query,
        )
    if query.kind is QueryKind.STATE_AT:
        return _state_at(query, memory=memory)
    if query.kind is QueryKind.DIFF:
        return _diff(query, memory=memory, thresholds=thresholds)
    needs_entity = query.kind in {
        QueryKind.ENTITY,
        QueryKind.ENTITY_HISTORY,
        QueryKind.ENTITY_SEEN,
    }
    if needs_entity and not (query.entity_id or query.label):
        return StateQueryResult(
            kind=query.kind,
            status=QueryStatus.INVALID,
            state_id=current.state_id,
            version=current.version,
            timestamp=current.timestamp,
            reason=f"{query.kind.value} needs an 'entity_id' or a 'label' to answer",
            query=query,
        )
    if query.kind is QueryKind.EVIDENCE and not (
        query.subject or query.entity_id or query.label
    ):
        return StateQueryResult(
            kind=query.kind,
            status=QueryStatus.INVALID,
            state_id=current.state_id,
            version=current.version,
            timestamp=current.timestamp,
            reason=(
                "an evidence query needs a 'subject', an 'entity_id' or a 'label' to be about"
            ),
            query=query,
        )
    if query.kind is QueryKind.ENTITIES:
        return _entities(query, current, limit=limit)
    if query.kind is QueryKind.ENTITY:
        return _entity(query, current)
    if query.kind is QueryKind.ENTITY_HISTORY:
        return _entity_history(query, current, memory=memory, limit=limit)
    if query.kind is QueryKind.ENTITY_SEEN:
        return _entity_seen(query, current, memory=memory)
    if query.kind is QueryKind.RELATIONSHIPS:
        return _relationships(query, current, limit=limit)
    if query.kind is QueryKind.CHANGES_SINCE:
        return _changes_since(query, current, memory=memory, limit=limit)
    if query.kind is QueryKind.UNCERTAIN:
        return _uncertain(query, current, limit=limit)
    if query.kind is QueryKind.STALE:
        return _stale(query, current, limit=limit)
    if query.kind is QueryKind.EVIDENCE:
        return _evidence(query, current, memory=memory, limit=limit)
    return StateQueryResult(  # pragma: no cover - every kind is handled above
        kind=query.kind,
        status=QueryStatus.INVALID,
        reason=f"the query kind {query.kind.value!r} is not implemented",
        query=query,
    )


def _entities(query: StateQuery, state: WorldState, *, limit: int) -> StateQueryResult:
    rows = [
        item
        for item in state.entities
        if _matches_entity(item, query)
    ]
    rows.sort(key=lambda item: (item.status.value, item.label, item.entity_id))
    return StateQueryResult(
        kind=query.kind,
        status=QueryStatus.OK if rows else QueryStatus.EMPTY,
        rows=tuple(item.to_dict() for item in rows[:limit]),
        count=len(rows),
        state_id=state.state_id,
        version=state.version,
        timestamp=state.timestamp,
        reason="" if rows else "no entity matches those filters",
        limitations=(
            (f"showing {limit} of {len(rows)} matching entities",) if len(rows) > limit else ()
        ),
        query=query,
    )


def _entity(query: StateQuery, state: WorldState) -> StateQueryResult:
    found = _resolve_entity(query, state)
    if found is None:
        return StateQueryResult(
            kind=query.kind,
            status=QueryStatus.NOT_FOUND,
            state_id=state.state_id,
            version=state.version,
            timestamp=state.timestamp,
            reason="no entity matches that id or label in the current state",
            query=query,
        )
    return StateQueryResult(
        kind=query.kind,
        status=QueryStatus.OK,
        rows=(found.to_dict(),),
        count=1,
        state_id=state.state_id,
        version=state.version,
        timestamp=state.timestamp,
        evidence=tuple(found.evidence),
        limitations=(
            ("this entity's identity is provisional",) if found.provisional else ()
        ),
        query=query,
    )


def _entity_history(
    query: StateQuery, state: WorldState, *, memory: TemporalMemory, limit: int
) -> StateQueryResult:
    found = _resolve_entity(query, state)
    if found is None:
        return StateQueryResult(
            kind=query.kind,
            status=QueryStatus.NOT_FOUND,
            state_id=state.state_id,
            version=state.version,
            timestamp=state.timestamp,
            reason="no entity matches that id or label in the current state",
            query=query,
        )
    rows: list[Mapping[str, Any]] = [
        {
            "record": "entity",
            "entity_id": found.entity_id,
            "first_seen": found.first_seen,
            "last_seen": found.last_seen,
            "last_confirmed": found.last_confirmed,
            "observed_count": found.observed_count,
            "status": found.status.value,
        }
    ]
    for item in memory.transitions_for(found.entity_id):
        rows.append(
            {
                "record": "transition",
                "transition_id": item.transition_id,
                "kind": item.kind.value,
                "timestamp": item.timestamp,
                "detail": item.detail,
                "previous": item.previous,
                "current": item.current,
                "evidence": list(item.evidence),
            }
        )
    limited = rows[:limit]
    return StateQueryResult(
        kind=query.kind,
        status=QueryStatus.OK,
        rows=tuple(limited),
        count=len(rows),
        state_id=state.state_id,
        version=state.version,
        timestamp=state.timestamp,
        evidence=tuple(found.evidence),
        limitations=(
            (f"showing {limit} of {len(rows)} history rows",) if len(rows) > limit else ()
        ),
        query=query,
    )


def _entity_seen(
    query: StateQuery, state: WorldState, *, memory: TemporalMemory
) -> StateQueryResult:
    found = _resolve_entity(query, state)
    history = memory.transitions_for(found.entity_id) if found is not None else ()
    if found is None:
        return StateQueryResult(
            kind=query.kind,
            status=QueryStatus.NOT_FOUND,
            rows=(
                {
                    "seen": False,
                    "entity_id": query.entity_id,
                    "label": query.label,
                    "reason": "this state has never held an entity matching that id or label",
                },
            ),
            count=1,
            state_id=state.state_id,
            version=state.version,
            timestamp=state.timestamp,
            reason="not present in the current state and not in the retained history",
            query=query,
        )
    return StateQueryResult(
        kind=query.kind,
        status=QueryStatus.OK,
        rows=(
            {
                "seen": True,
                "entity_id": found.entity_id,
                "label": found.label,
                "status": found.status.value,
                "first_seen": found.first_seen,
                "last_seen": found.last_seen,
                "observed_count": found.observed_count,
                "transitions": len(history),
                "evidence": list(found.evidence),
            },
        ),
        count=1,
        state_id=state.state_id,
        version=state.version,
        timestamp=state.timestamp,
        evidence=tuple(found.evidence),
        query=query,
    )


def _relationships(query: StateQuery, state: WorldState, *, limit: int) -> StateQueryResult:
    rows = [item for item in state.relationships if _matches_relationship(item, query)]
    return StateQueryResult(
        kind=query.kind,
        status=QueryStatus.OK if rows else QueryStatus.EMPTY,
        rows=tuple(item.to_dict() for item in rows[:limit]),
        count=len(rows),
        state_id=state.state_id,
        version=state.version,
        timestamp=state.timestamp,
        reason="" if rows else "no relationship matches those filters",
        limitations=(
            (f"showing {limit} of {len(rows)} relationships",) if len(rows) > limit else ()
        ),
        query=query,
    )


def _changes_since(
    query: StateQuery, state: WorldState, *, memory: TemporalMemory, limit: int
) -> StateQueryResult:
    if query.since and parse_timestamp(query.since) is None:
        return StateQueryResult(
            kind=query.kind,
            status=QueryStatus.INVALID,
            state_id=state.state_id,
            version=state.version,
            timestamp=state.timestamp,
            reason=f"'since' must be a timestamp; {query.since!r} could not be read",
            query=query,
        )
    rows = memory.changes_between(query.since, query.until)
    if query.entity_id:
        rows = tuple(item for item in rows if item.entity_id == query.entity_id)
    if query.status:
        rows = tuple(item for item in rows if item.kind.value == query.status)
    indexed = [
        {
            "transition_id": item.transition_id,
            "kind": item.kind.value,
            "entity_id": item.entity_id,
            "detail": item.detail,
            "timestamp": item.timestamp,
            "previous_state_id": item.previous_state_id,
            "current_state_id": item.current_state_id,
            "evidence": list(item.evidence),
        }
        for item in rows
    ]
    return StateQueryResult(
        kind=query.kind,
        status=QueryStatus.OK if indexed else QueryStatus.EMPTY,
        rows=tuple(indexed[:limit]),
        count=len(indexed),
        state_id=state.state_id,
        version=state.version,
        timestamp=state.timestamp,
        reason="" if indexed else "no transition falls inside that window",
        limitations=(
            (f"showing {limit} of {len(indexed)} transitions",) if len(indexed) > limit else ()
        ),
        query=query,
    )


def _state_at(query: StateQuery, *, memory: TemporalMemory) -> StateQueryResult:
    if not query.since:
        return StateQueryResult(
            kind=query.kind,
            status=QueryStatus.INVALID,
            reason="state_at needs a 'since' timestamp naming the moment to look at",
            query=query,
        )
    state, reason = memory.state_at(query.since)
    if state is None:
        return StateQueryResult(
            kind=query.kind,
            status=QueryStatus.NOT_FOUND,
            reason=reason,
            limitations=(
                "the current state was NOT substituted for the requested moment",
            ),
            query=query,
        )
    return StateQueryResult(
        kind=query.kind,
        status=QueryStatus.OK,
        rows=(
            {
                "state_id": state.state_id,
                "version": state.version,
                "timestamp": state.timestamp,
                "entities": [item.label or item.entity_type for item in state.entities][:32],
                "entity_count": len(state.entities),
                "relationships": len(state.relationships),
                "uncertainty": len(state.uncertainty),
                "sources": list(state.sources),
            },
        ),
        count=1,
        state_id=state.state_id,
        version=state.version,
        timestamp=state.timestamp,
        limitations=(
            "answered from the newest retained version at or before the requested "
            "moment, which may be older than the moment itself",
        ),
        query=query,
    )


def _uncertain(query: StateQuery, state: WorldState, *, limit: int) -> StateQueryResult:
    rows = [
        item
        for item in state.uncertainty
        if not query.subject or query.subject in item.subject or query.subject in item.detail
    ]
    return StateQueryResult(
        kind=query.kind,
        status=QueryStatus.OK if rows else QueryStatus.EMPTY,
        rows=tuple(item.to_dict() for item in rows[:limit]),
        count=len(rows),
        state_id=state.state_id,
        version=state.version,
        timestamp=state.timestamp,
        reason="" if rows else "this state holds no open uncertainty records",
        limitations=(
            (f"showing {limit} of {len(rows)} uncertainty records",) if len(rows) > limit else ()
        ),
        query=query,
    )


def _stale(query: StateQuery, state: WorldState, *, limit: int) -> StateQueryResult:
    stale_entities = [
        item
        for item in state.entities
        if item.status in {EntityStatus.NOT_OBSERVED, EntityStatus.MISSING, EntityStatus.EXPIRED}
        and (not query.entity_id or item.entity_id == query.entity_id)
    ]
    stale_relationships = [
        item
        for item in state.relationships
        if item.status is not RelationStatus.ACTIVE
        and (not query.entity_id or query.entity_id in item.pair())
    ]
    rows: list[Mapping[str, Any]] = [
        {
            "record": "entity",
            "entity_id": item.entity_id,
            "label": item.label,
            "status": item.status.value,
            "last_seen": item.last_seen,
            "missed_observations": item.missed_observations,
            "detail": (
                "not observed in the latest complete observation"
                if item.status is EntityStatus.NOT_OBSERVED
                else (
                    "confirmed missing by repeated complete observations"
                    if item.status is EntityStatus.MISSING
                    else "retired after the retention window"
                )
            ),
        }
        for item in stale_entities
    ]
    rows.extend(
        {
            "record": "relationship",
            "relationship_id": item.relationship_id,
            "kind": item.kind.value,
            "source_entity_id": item.source_entity_id,
            "target_entity_id": item.target_entity_id,
            "status": item.status.value,
            "valid_until": item.valid_until,
            "detail": "no longer asserted",
        }
        for item in stale_relationships
    )
    return StateQueryResult(
        kind=query.kind,
        status=QueryStatus.OK if rows else QueryStatus.EMPTY,
        rows=tuple(rows[:limit]),
        count=len(rows),
        state_id=state.state_id,
        version=state.version,
        timestamp=state.timestamp,
        reason="" if rows else "nothing in this state is stale",
        limitations=(
            "a stale fact is not a false fact: it means the evidence stopped "
            "supporting it",
        )
        if rows
        else (),
        query=query,
    )


def _evidence(
    query: StateQuery, state: WorldState, *, memory: TemporalMemory, limit: int
) -> StateQueryResult:
    entity = _resolve_entity(query, state)
    wanted = query.entity_id or query.subject
    if entity is None and not wanted:
        # A label that resolves to nothing is still a subject the caller NAMED.
        # Treating it as "no filter" answered a question about a claim that does
        # not exist with unrelated evidence, which is worse than not answering.
        wanted = query.label
    # When the subject resolved to an entity, the question is about THAT entity,
    # so relations and uncertainty are anchored to its id rather than left open.
    anchor = entity.entity_id if entity is not None else wanted
    rows: list[Mapping[str, Any]] = []
    if entity is not None:
        rows.append(
            {
                "record": "entity",
                "entity_id": entity.entity_id,
                "basis": "observed",
                "evidence": list(entity.evidence),
                "identity_confidence": entity.identity_confidence,
                "provisional": entity.provisional,
                "detail": (
                    "provisional identity: the evidence did not choose between candidates"
                    if entity.provisional
                    else "identity resolved from the source's own id or from overlapping geometry"
                ),
            }
        )
        for attribute in entity.attributes:
            rows.append(
                {
                    "record": "attribute",
                    "name": attribute.name,
                    "value": attribute.value,
                    "basis": attribute.basis.value,
                    "confidence": attribute.confidence,
                    "observed_at": attribute.observed_at,
                    "source": attribute.source,
                    "evidence": list(attribute.evidence),
                }
            )
    for relation in state.relationships:
        if anchor and anchor not in relation.pair() and anchor != relation.relationship_id:
            continue
        rows.append(
            {
                "record": "relationship",
                "relationship_id": relation.relationship_id,
                "kind": relation.kind.value,
                "status": relation.status.value,
                "relation_source": relation.relation_source.value,
                "detail": relation.detail,
                "evidence": list(relation.evidence),
            }
        )
    for uncertain in state.uncertainty:
        if anchor and anchor not in uncertain.subject and anchor not in uncertain.detail:
            continue
        rows.append(
            {
                "record": "uncertainty",
                "subject": uncertain.subject,
                "kind": uncertain.kind.value,
                "detail": uncertain.detail,
                "evidence": list(uncertain.evidence),
            }
        )
    references = [
        item.to_dict()
        for item in state.evidence
        # A resolved entity's evidence trail is the state's reference list; an
        # unresolved subject is filtered, so a name nobody has seen returns
        # nothing rather than somebody else's observations.
        if entity is not None
        or not anchor
        or item.evidence_id == anchor
        or anchor in (item.source, item.detail)
    ]
    rows.extend({"record": "reference", **item} for item in references)
    if not rows:
        return StateQueryResult(
            kind=query.kind,
            status=QueryStatus.NOT_FOUND,
            state_id=state.state_id,
            version=state.version,
            timestamp=state.timestamp,
            reason=(
                f"nothing in the current state is about {wanted!r}"
                if wanted
                else "no evidence is recorded for that query"
            ),
            query=query,
        )
    return StateQueryResult(
        kind=query.kind,
        status=QueryStatus.OK,
        rows=tuple(rows[:limit]),
        count=len(rows),
        state_id=state.state_id,
        version=state.version,
        timestamp=state.timestamp,
        evidence=tuple(entity.evidence) if entity is not None else (),
        limitations=(
            "evidence references name observations; the referenced content is not stored here",
        ),
        query=query,
    )


def _diff(
    query: StateQuery, *, memory: TemporalMemory, thresholds: ChangeThresholds | None
) -> StateQueryResult:
    current = memory.state
    if current is None:  # pragma: no cover - run_query guards this
        return StateQueryResult(kind=query.kind, status=QueryStatus.EMPTY, query=query)
    after = memory.state_by_id(query.other_state_id) if query.other_state_id else current
    before = memory.state_by_id(query.state_id) if query.state_id else None
    if before is None:
        before = memory.state_by_id(after.previous_state_id) if after is not None else None
    if after is None or before is None:
        return StateQueryResult(
            kind=query.kind,
            status=QueryStatus.NOT_FOUND,
            state_id=current.state_id,
            version=current.version,
            timestamp=current.timestamp,
            reason=(
                "both states must be retained to diff them; one of them is not in memory "
                f"({len(memory.snapshots)} version(s) retained)"
            ),
            limitations=("a diff is only offered between retained versions",),
            query=query,
        )
    events = detect_changes(before, after, thresholds=thresholds, timestamp=after.timestamp)
    return StateQueryResult(
        kind=query.kind,
        status=QueryStatus.OK if events else QueryStatus.EMPTY,
        rows=tuple(item.to_dict() for item in events),
        count=len(events),
        state_id=before.state_id,
        version=before.version,
        timestamp=before.timestamp,
        reason="" if events else "the two versions are identical in every compared field",
        limitations=(f"compared version {before.version} with version {after.version}",),
        query=query,
    )


def _matches_entity(entity: WorldEntity, query: StateQuery) -> bool:
    if query.entity_id and entity.entity_id != query.entity_id:
        return False
    if query.label and query.label.lower() not in [item.lower() for item in entity.labels]:
        return False
    if query.entity_type and entity.entity_type != query.entity_type:
        return False
    if query.status and entity.status.value != query.status:
        return False
    if query.source and query.source not in entity.provenance:
        return False
    if query.min_confidence is not None:
        measured = as_optional_float(entity.identity_confidence)
        if measured is None or measured < query.min_confidence:
            return False
    return True


def _matches_relationship(item: WorldRelationship, query: StateQuery) -> bool:
    if query.relationship_kind:
        wanted = query.relationship_kind.lower()
        mapped: RelationshipKind | None = None
        for kind in RelationshipKind:
            if kind.value == wanted:
                mapped = kind
                break
        if mapped is None:
            return False
        if item.kind is not mapped:
            return False
    if query.status and item.status.value != query.status:
        return False
    if query.entity_id and query.entity_id not in item.pair():
        return False
    if query.min_confidence is not None:
        measured = as_optional_float(item.confidence)
        if measured is None or measured < query.min_confidence:
            return False
    return True


def _resolve_entity(query: StateQuery, state: WorldState) -> WorldEntity | None:
    """One entity from a query, by id first and by label second.

    A label that matches several entities returns the first in a stable order; the
    caller that needs all of them uses the ``entities`` kind, which is what keeps
    this function from quietly inventing a disambiguation rule.
    """
    if query.entity_id:
        return state.entity(query.entity_id)
    if query.label:
        matches = state.find(query.label)
        if matches:
            return sorted(matches, key=lambda item: item.entity_id)[0]
    return None


def history_kinds(memory: TemporalMemory) -> tuple[str, ...]:
    """Every transition kind that actually occurred, in order of appearance."""
    seen: list[str] = []
    for item in memory.transitions:
        if item.kind.value not in seen:
            seen.append(item.kind.value)
    return tuple(seen)

