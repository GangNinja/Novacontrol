"""State estimation: reconcile one observation with the state the world already has (§9).

The estimator is deterministic and explainable, and it is deliberately not a
model. Every decision it makes is a comparison a person can re-run by hand:

    two boxes overlap and the labels agree   -> the same entity
    the same label, different place          -> a different entity, provisionally
    a declared-complete observation          -> absence means something
    a partial observation                    -> absence means nothing at all

Its output is a new :class:`~novacontrol.world.models.WorldState` version plus the
transitions, changes and uncertainty rows that produced it — the four things a
reader needs to disagree with it.

Four policies are load-bearing, and each one is a refusal:

**Partial observations never erase state.** Absence is only evaluated against an
observation that declared itself complete, and only for entities of the same
scope. A screenshot of one window is not evidence about another (§5).

**An out-of-order fact does not overwrite a newer one.** The newer value survives
and the disagreement becomes an uncertainty row and a conflict change (§8).

**An ambiguous match stays ambiguous.** Two candidate entities that score within
the margin produce a provisional identity, not a merge (§6).

**An inference is never stored as an observation.** Derived facts
(``object_count``, and any value this module computes) are built with
``EntityAttribute.inferred``, which is what makes it possible to tell later what
was seen from what was worked out (§5).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.perception.models import BBox
from novacontrol.world import changes as change_detection
from novacontrol.world import transitions as transition_factory
from novacontrol.world.entities import (
    IdentityPolicy,
    confirm_absence,
    entity_from_observation,
    expire_stale,
    merge_entity,
    resolve_identity,
)
from novacontrol.world.models import (
    ChangeEvent,
    EntityAttribute,
    EntityStatus,
    EvidenceReference,
    FactBasis,
    Observation,
    ObservedEntity,
    StateEstimate,
    UncertaintyKind,
    UncertaintyRecord,
    WorldEntity,
    WorldState,
    WorldStateTransition,
)
from novacontrol.world.relationships import RelationshipOutcome, relations_from_observation

__all__ = [
    "MAX_ENTITIES",
    "MAX_UNCERTAINTY",
    "EstimationOutcome",
    "EstimationPolicy",
    "estimate",
]

#: A ceiling on the state. Reached only by a pathological stream; when it is, the
#: oldest expired entity is retired first and the caller is told.
MAX_ENTITIES = 256

#: How many open uncertainties a state carries. Old ones fall off the end; the
#: resolved ones are dropped the moment the entity is observed again.
MAX_UNCERTAINTY = 50

#: Conditions that describe the LATEST look rather than a durable fact about the
#: environment. They are re-measured by every observation and dropped when an
#: observation does not report them, so a failed look cannot linger as a stale
#: success (or the reverse).
EPHEMERAL_CONDITIONS: frozenset[str] = frozenset({"perception_status"})


@dataclass(frozen=True, slots=True)
class EstimationPolicy:
    """Every number the estimator uses — one table, so a deployment sets one thing.

    The defaults are the ``balanced`` posture: strict enough that a wobbling
    detection does not invent an identity, loose enough that a moving object is
    followed rather than duplicated every frame.
    """

    identity: IdentityPolicy = field(default_factory=IdentityPolicy)
    thresholds: change_detection.ChangeThresholds = field(
        default_factory=change_detection.ChangeThresholds
    )
    missing_after: int = 2
    stale_after_seconds: int = 60
    entity_ttl_seconds: int = 900
    max_entities: int = MAX_ENTITIES
    max_uncertainty: int = MAX_UNCERTAINTY
    derive_relationships: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> EstimationPolicy:
        """A policy from the configuration mapping, with every value clamped."""
        defaults = cls()
        if not isinstance(data, Mapping):
            return defaults

        def count(key: str, default: int, *, low: int = 0, high: int = 100000) -> int:
            try:
                value = int(data.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        return cls(
            identity=IdentityPolicy.from_mapping(data),
            thresholds=change_detection.ChangeThresholds.from_mapping(data),
            missing_after=max(1, count("missing_after", defaults.missing_after, low=1, high=100)),
            stale_after_seconds=count(
                "stale_after_seconds", defaults.stale_after_seconds, high=86400
            ),
            entity_ttl_seconds=count(
                "entity_ttl_seconds", defaults.entity_ttl_seconds, high=604800
            ),
            max_entities=max(1, count("max_entities", defaults.max_entities, low=1, high=10000)),
            max_uncertainty=max(
                1, count("max_uncertainty", defaults.max_uncertainty, low=1, high=1000)
            ),
            derive_relationships=bool(
                data.get("derive_relationships", defaults.derive_relationships)
            ),
        )


@dataclass(frozen=True, slots=True)
class EstimationOutcome:
    """The reconciled state, and everything the estimator decided on the way to it."""

    state: WorldState
    transitions: tuple[WorldStateTransition, ...] = ()
    changes: tuple[ChangeEvent, ...] = ()
    estimates: tuple[StateEstimate, ...] = ()
    uncertainty: tuple[UncertaintyRecord, ...] = ()
    relationships: RelationshipOutcome = field(default_factory=RelationshipOutcome)
    entities_added: int = 0
    entities_updated: int = 0
    entities_not_observed: int = 0
    entities_expired: int = 0
    conditions_changed: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.to_dict(),
            "transitions": [item.to_dict() for item in self.transitions],
            "changes": [item.to_dict() for item in self.changes],
            "estimates": [item.to_dict() for item in self.estimates],
            "uncertainty": [item.to_dict() for item in self.uncertainty],
            "relationships": self.relationships.to_dict(),
            "entities_added": self.entities_added,
            "entities_updated": self.entities_updated,
            "entities_not_observed": self.entities_not_observed,
            "entities_expired": self.entities_expired,
            "conditions_changed": list(self.conditions_changed),
        }


def estimate(
    *,
    previous: WorldState,
    observation: Observation,
    policy: EstimationPolicy | None = None,
    evidence_id: str = "",
    extra_uncertainty: Sequence[UncertaintyRecord] = (),
) -> EstimationOutcome:
    """The next state version, after one observation.

    Pure: the same previous state and the same observation produce the same result,
    including the same transition ids' shapes and the same ordering. That is what
    makes the state testable without a clock or a model.
    """
    settings = policy or EstimationPolicy()
    timestamp = observation.timestamp or previous.timestamp
    evidence = evidence_id or observation.observation_id

    # The entity list STARTS from what the state already holds: an observation
    # adds and updates, and an entity it does not mention is governed by the
    # absence rules below rather than disappearing because it was not rebuilt.
    entities: list[WorldEntity] = list(previous.entities)
    transitions: list[WorldStateTransition] = []
    estimates: list[StateEstimate] = []
    uncertainty: list[UncertaintyRecord] = []
    id_by_hint: dict[str, str] = {}
    resolved_pairs: list[tuple[ObservedEntity, str]] = []
    observed_ids: set[str] = set()
    claimed: set[str] = set()

    for observed in observation.entities:
        candidates = [item for item in entities if item.entity_id not in claimed]
        decision = resolve_identity(
            observed, candidates, policy=settings.identity, timestamp=timestamp
        )
        if decision.decision == "new_entity":
            entity = entity_from_observation(
                observed, timestamp=timestamp, evidence_id=evidence, scope=observation.scope
            )
            estimates.append(
                StateEstimate(
                    decision="new_entity",
                    entity_id=entity.entity_id,
                    label=entity.label,
                    reason=decision.reason,
                    confidence=None,
                )
            )
            transitions.append(
                transition_factory.entity_added(
                    entity,
                    previous_state_id=previous.state_id,
                    current_state_id="",
                    timestamp=timestamp,
                    world_id=previous.world_id,
                    evidence=(evidence,),
                )
            )
            entities.append(entity)
        else:
            incumbent = _find(entities, decision.entity_id)
            if incumbent is None:  # pragma: no cover - defensive: candidates came from here
                continue
            entity, conflicts = merge_entity(
                incumbent,
                observed,
                estimate=decision,
                timestamp=timestamp,
                evidence_id=evidence,
                scope=observation.scope,
            )
            entities[entities.index(incumbent)] = entity
            uncertainty.extend(conflicts)
            estimates.append(decision)
            if decision.decision == "ambiguous":
                uncertainty.append(
                    UncertaintyRecord(
                        subject=entity.entity_id,
                        kind=UncertaintyKind.AMBIGUOUS_IDENTITY,
                        detail=decision.reason,
                        confidence=decision.confidence,
                        evidence=(evidence,),
                        recorded_at=timestamp,
                    )
                )
            transitions.extend(
                _update_transitions(
                    previous=incumbent,
                    current=entity,
                    state=previous,
                    timestamp=timestamp,
                    evidence=evidence,
                    thresholds=settings.thresholds,
                )
            )
        claimed.add(entity.entity_id)
        observed_ids.add(entity.entity_id)
        resolved_pairs.append((observed, entity.entity_id))
        hint = observed.entity_id or observed.track_id
        if hint:
            id_by_hint[hint] = entity.entity_id
        # The entity's own id is a resolvable key too, so an observation that
        # carried no source ids can still have its geometry computed against the
        # identities this layer assigned.
        id_by_hint.setdefault(entity.entity_id, entity.entity_id)

    # ── absence: only a complete observation can conclude anything (§5/§10) ──
    not_observed = 0
    final: list[WorldEntity] = []
    for entity in entities:
        if entity.entity_id in observed_ids or entity.status is EntityStatus.EXPIRED:
            final.append(entity)
            continue
        replaced, kind, _ = confirm_absence(
            entity,
            complete=observation.complete,
            scope=observation.scope,
            missing_after=settings.missing_after,
            timestamp=timestamp,
            evidence_id=evidence,
        )
        final.append(replaced)
        if kind == "entity_not_observed":
            not_observed += 1
            transitions.append(
                transition_factory.entity_not_observed(
                    replaced,
                    missed=replaced.missed_observations,
                    scope=observation.scope or "unscoped",
                    previous_state_id=previous.state_id,
                    current_state_id="",
                    timestamp=timestamp,
                    world_id=previous.world_id,
                    evidence=(evidence,),
                )
            )
            uncertainty.append(
                UncertaintyRecord(
                    subject=replaced.entity_id,
                    kind=UncertaintyKind.STALE,
                    detail=(
                        f"{replaced.label or replaced.entity_type} was absent from the latest "
                        "complete observation; the state keeps what it knew rather than "
                        "assuming it is gone"
                    ),
                    confidence=None,
                    evidence=(evidence,),
                    recorded_at=timestamp,
                )
            )
        elif kind == "entity_confirmed_missing":
            not_observed += 1
            transitions.append(
                transition_factory.entity_confirmed_missing(
                    replaced,
                    missed=replaced.missed_observations,
                    scope=observation.scope or "unscoped",
                    previous_state_id=previous.state_id,
                    current_state_id="",
                    timestamp=timestamp,
                    world_id=previous.world_id,
                    evidence=(evidence,),
                )
            )
    entities = list(final)

    # ── expiration: retirement is a status, and the evidence stays ───────────
    before_expiry = {item.entity_id: item for item in entities}
    retained, expired_ids = expire_stale(
        entities,
        ttl_seconds=settings.entity_ttl_seconds,
        now=timestamp,
        evidence_id=evidence,
    )
    entities = list(retained)
    for entity_id in expired_ids:
        item = _find(entities, entity_id)
        if item is None:  # pragma: no cover - defensive
            continue
        transitions.append(
            transition_factory.entity_expired(
                item,
                ttl_seconds=settings.entity_ttl_seconds,
                previous_state_id=previous.state_id,
                current_state_id="",
                timestamp=timestamp,
                world_id=previous.world_id,
                evidence=(evidence,),
            )
        )

    # ── relationships: Phase 21 geometry, reconciled over time ───────────────
    relationships = relations_from_observation(
        observation,
        id_by_hint=id_by_hint,
        resolved=tuple(resolved_pairs),
        existing=previous.relationships,
        timestamp=timestamp,
        derive_missing=settings.derive_relationships,
    )
    transitions.extend(
        _relationship_transitions(
            previous=previous,
            outcome=relationships,
            timestamp=timestamp,
            evidence=evidence,
        )
    )
    uncertainty.extend(relationships.conflicts)

    # ── conditions and the scene-level comparison ────────────────────────────
    conditions = _conditions(
        previous, entities, observation, timestamp=timestamp, evidence=evidence
    )
    conditions_changed = _changed_conditions(previous.conditions, conditions)
    if conditions_changed:
        transitions.append(
            transition_factory.scene_changed(
                changed=conditions_changed,
                previous_state_id=previous.state_id,
                current_state_id="",
                timestamp=timestamp,
                world_id=previous.world_id,
                evidence=(evidence,),
            )
        )
    for row in (*uncertainty, *extra_uncertainty):
        if row.kind is UncertaintyKind.CONFLICTING:
            transitions.append(
                transition_factory.state_conflict(
                    subject=row.subject,
                    detail=row.detail,
                    previous_state_id=previous.state_id,
                    current_state_id="",
                    timestamp=timestamp,
                    world_id=previous.world_id,
                    evidence=row.evidence or (evidence,),
                    confidence=row.confidence,
                )
            )

    entity_rows, retired, over_ceiling = _bounded(
        entities, private=before_expiry, ceiling=settings.max_entities
    )
    uncertainty_rows = _live_uncertainty(
        previous.uncertainty,
        (*uncertainty, *extra_uncertainty),
        observed_ids,
        ceiling=settings.max_uncertainty,
    )
    version = previous.version + 1
    state = WorldState(
        world_id=previous.world_id,
        version=version,
        timestamp=timestamp,
        previous_state_id=previous.state_id,
        entities=entity_rows,
        relationships=relationships.relationships,
        conditions=conditions,
        uncertainty=uncertainty_rows,
        evidence=_evidence(previous.evidence, observation),
        sources=_sources(previous.sources, observation),
        confidence=change_detection.entity_confidence(entity_rows),
        metadata={
            **dict(previous.metadata),
            "last_observation_id": observation.observation_id,
            "last_source": observation.source.value,
            "last_scope": observation.scope,
            "last_complete": observation.complete,
            "retired_entities": retired,
            "entities_over_ceiling": over_ceiling,
        },
    )
    dated = tuple(
        _stamped(item, previous_state_id=previous.state_id, current_state_id=state.state_id)
        for item in transitions
    )
    detected = change_detection.detect_changes(
        previous,
        state,
        thresholds=settings.thresholds,
        evidence=(evidence,),
        timestamp=timestamp,
    )
    return EstimationOutcome(
        state=state,
        transitions=dated,
        changes=detected,
        estimates=tuple(estimates),
        uncertainty=uncertainty_rows,
        relationships=relationships,
        entities_added=sum(1 for item in estimates if item.decision == "new_entity"),
        entities_updated=sum(1 for item in estimates if item.decision != "new_entity"),
        entities_not_observed=not_observed,
        entities_expired=len(expired_ids),
        conditions_changed=tuple(conditions_changed),
    )


def _update_transitions(
    *,
    previous: WorldEntity,
    current: WorldEntity,
    state: WorldState,
    timestamp: str,
    evidence: str,
    thresholds: change_detection.ChangeThresholds,
) -> list[WorldStateTransition]:
    """Attribute and movement transitions for one entity that was re-observed."""
    rows: list[WorldStateTransition] = []
    for attribute in current.attributes:
        was = previous.attribute(attribute.name)
        if was is not None and was.value == attribute.value:
            continue
        rows.append(
            transition_factory.attribute_changed(
                current,
                attribute,
                was,
                previous_state_id=state.state_id,
                current_state_id="",
                timestamp=timestamp,
                world_id=state.world_id,
                evidence=(evidence,),
            )
        )
    distance = _distance(previous.bbox, current.bbox)
    if distance is not None and distance >= thresholds.position_px:
        rows.append(
            transition_factory.entity_moved(
                current,
                previous_bbox=previous.bbox.to_dict() if previous.bbox is not None else None,
                distance=distance,
                previous_state_id=state.state_id,
                current_state_id="",
                timestamp=timestamp,
                world_id=state.world_id,
                evidence=(evidence,),
            )
        )
    return rows


def _relationship_transitions(
    *,
    previous: WorldState,
    outcome: RelationshipOutcome,
    timestamp: str,
    evidence: str,
) -> list[WorldStateTransition]:
    """One transition per relation whose asserted state changed."""
    before = {item.relationship_id: item for item in previous.relationships}
    rows: list[WorldStateTransition] = []
    for item in outcome.relationships:
        was = before.get(item.relationship_id)
        if was is not None and was.status is item.status:
            continue
        rows.append(
            transition_factory.relationship_changed(
                item,
                previous_status=was.status.value if was is not None else "none",
                previous_state_id=previous.state_id,
                current_state_id="",
                timestamp=timestamp,
                world_id=previous.world_id,
                evidence=(evidence,),
            )
        )
    return rows


def _conditions(
    previous: WorldState,
    entities: Sequence[WorldEntity],
    observation: Observation,
    *,
    timestamp: str,
    evidence: str,
) -> tuple[EntityAttribute, ...]:
    """Environment-level facts: this observation's, plus the derived totals.

    The observation's own facts are carried through with their basis intact — a
    fact a source reported stays ``OBSERVED``, and the totals computed here are
    ``INFERRED``. Re-deriving them here rather than trusting the source's count is
    what keeps "the source said 4" and "there are 4" telling the same story.
    """
    rows: dict[str, EntityAttribute] = {item.name: item for item in observation.facts}
    measured = [item for item in previous.conditions if item.name not in rows]
    for item in measured:
        if item.name.startswith("count.") or item.name in EPHEMERAL_CONDITIONS:
            # A count from a previous observation is not carried forward: an
            # environment total is a claim about NOW, and repeating a stale one
            # would be the fabricated figure this phase refuses. The same is
            # true of a look's own outcome.
            continue
        rows[item.name] = item
    # The count is taken from the state being built, not from the state it is
    # replacing: a count one version behind is a figure that is simply wrong.
    rows["entity_count"] = EntityAttribute.inferred(
        "entity_count",
        len([item for item in entities if item.status is EntityStatus.PRESENT]),
        evidence=(evidence,),
        observed_at=timestamp,
    )
    return tuple(rows[name] for name in sorted(rows))


def _changed_conditions(
    previous: Sequence[EntityAttribute], current: Sequence[EntityAttribute]
) -> tuple[str, ...]:
    """Names of the condition rows whose value differs, ignoring bookkeeping rows."""
    before = {item.name: item.value for item in previous}
    after = {item.name: item.value for item in current}
    # Counting rows and frame geometry are bookkeeping: the entity transitions
    # already say what appeared and disappeared, and a frame size that is simply
    # re-measured is not the environment changing.
    ignored = {"object_count", "entity_count", "frame_size", *EPHEMERAL_CONDITIONS}
    names: list[str] = []
    for name in sorted(set(before) | set(after)):
        if name in ignored:
            continue
        if before.get(name) != after.get(name):
            names.append(name)
    return tuple(names)


def _evidence(
    previous: Sequence[EvidenceReference], observation: Observation
) -> tuple[EvidenceReference, ...]:
    """The evidence trail of the state: the newest references, bounded.

    Ids and kinds only — the state never becomes a copy of what was ingested.
    """
    rows = list(previous)
    rows.extend(observation.evidence)
    if not observation.evidence:
        rows.append(
            EvidenceReference(
                evidence_id=observation.observation_id,
                kind="observation",
                source=observation.source_id or observation.source.value,
                timestamp=observation.timestamp,
            )
        )
    deduped: dict[str, EvidenceReference] = {}
    for item in rows:
        if item.evidence_id:
            deduped[item.evidence_id] = item
    return tuple(list(deduped.values())[-64:])


def _sources(previous: Sequence[str], observation: Observation) -> tuple[str, ...]:
    """Every source that has fed this world, in order of first appearance."""
    values = [item for item in previous if item]
    candidate = observation.source_id or observation.source.value
    if candidate and candidate not in values:
        values.append(candidate)
    return tuple(values[-16:])


def _live_uncertainty(
    previous: Sequence[UncertaintyRecord],
    fresh: Sequence[UncertaintyRecord],
    observed_ids: set[str],
    *,
    ceiling: int,
) -> tuple[UncertaintyRecord, ...]:
    """Open uncertainty: resolved rows drop, new rows append, the whole is bounded.

    A row is RESOLVED when its subject is an entity that was observed again — the
    question it recorded has been answered, so keeping it would make the state
    report a doubt it no longer holds. Rows about anything else (a fact, a relation)
    survive until the bound pushes them off the end.
    """
    kept = [
        item
        for item in previous
        if not (item.subject in observed_ids and item.kind is not UncertaintyKind.CONFLICTING)
    ]
    seen = {(item.subject, item.kind.value, item.detail) for item in kept}
    for item in fresh:
        key = (item.subject, item.kind.value, item.detail)
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    return tuple(kept[-ceiling:])


def _bounded(
    entities: Sequence[WorldEntity],
    *,
    private: Mapping[str, WorldEntity],
    ceiling: int,
) -> tuple[tuple[WorldEntity, ...], int, int]:
    """Apply the entity ceiling by retiring expired history first.

    The current world is never trimmed to make room for itself: an expired entity
    is dropped before an active one, and the count of what was dropped is returned
    so the state does not look complete when it is not.

    That choice means a stream of genuinely NEW entities can leave the state above
    ``max_entities`` — and reaching it silently was itself a defect (report §25.9):
    live facts are never destroyed to satisfy a policy number, but the overage is
    returned and the state carries it, so ``status()`` cannot present a declared
    ceiling beside a count that contradicts it without saying so.
    """
    if len(entities) <= ceiling:
        return tuple(entities), 0, 0
    expired = [item for item in entities if item.status is EntityStatus.EXPIRED]
    active = [item for item in entities if item.status is not EntityStatus.EXPIRED]
    room = max(0, ceiling - len(active))
    dropped = min(len(expired), max(0, len(expired) - room))
    # Only the SURPLUS above the ceiling counts as over: an entity whose expiry was
    # consumed to make room is not an overage.
    kept = len(active) + (len(expired) - dropped)
    over = max(0, kept - ceiling)
    if dropped <= 0:
        return tuple(entities), 0, over
    remaining = expired[dropped:]
    return (*active, *remaining), dropped, over


def _find(entities: Sequence[WorldEntity], entity_id: str) -> WorldEntity | None:
    for item in entities:
        if item.entity_id == entity_id:
            return item
    return None


def _distance(before: BBox | None, after: BBox | None) -> float | None:
    """Centre-to-centre movement, or ``None`` when either box is unmeasurable."""
    if before is None or after is None:
        return None
    if not before.has_extent or not after.has_extent:
        return None
    dx = after.center[0] - before.center[0]
    dy = after.center[1] - before.center[1]
    return math.hypot(dx, dy)


def _stamped(
    transition: WorldStateTransition,
    *,
    previous_state_id: str,
    current_state_id: str,
) -> WorldStateTransition:
    """Fill in the state ids a factory could not know at build time.

    The factories run before the new version exists (its id is generated inside
    the state constructor), so the two ids are stamped once, here, rather than
    each factory guessing.
    """
    return WorldStateTransition(
        transition_id=transition.transition_id,
        kind=transition.kind,
        previous_state_id=previous_state_id,
        current_state_id=current_state_id,
        timestamp=transition.timestamp,
        entity_id=transition.entity_id,
        attribute=transition.attribute,
        previous=transition.previous,
        current=transition.current,
        detail=transition.detail,
        confidence=transition.confidence,
        evidence=transition.evidence,
        world_id=transition.world_id,
    )


def inferred_fact(name: str, value: Any, *, evidence: Sequence[str], at: str) -> EntityAttribute:
    """A derived fact, built only here — so an inference can never be spelled as an observation."""
    return EntityAttribute(
        name=name,
        value=value,
        basis=FactBasis.INFERRED,
        evidence=tuple(item for item in evidence if item),
        observed_at=at,
    )
