"""State transitions: the factories that build them, and the readers that query them (§10).

A transition is written by exactly one factory per kind, so the vocabulary and the
shapes cannot drift: ``ENTITY_NOT_OBSERVED`` always comes from
:func:`entity_not_observed` and carries the miss count it was decided at, and
``ENTITY_CONFIRMED_MISSING`` always comes from :func:`entity_confirmed_missing`
with the evidence that concluded it. Nothing else constructs a transition, which
is what makes "was the laptop ever confirmed gone?" a question with a definite
answer.

Two refusals are encoded in the signatures:

* there is no factory for a transition with an unstated kind — the kind is always
  the first decision;
* there is no factory that infers causality ("the window moved because the laptop
  moved"): two changes that happened together are two transitions, and nothing in
  this module relates them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from novacontrol.world.models import (
    EntityAttribute,
    RelationshipKind,
    TransitionKind,
    WorldEntity,
    WorldRelationship,
    WorldState,
    WorldStateTransition,
)

__all__ = [
    "MAX_TRANSITIONS",
    "attribute_changed",
    "between_states",
    "entity_added",
    "entity_confirmed_missing",
    "entity_expired",
    "entity_moved",
    "entity_not_observed",
    "for_entity",
    "for_kind",
    "relationship_changed",
    "scene_changed",
    "state_conflict",
    "summary",
]

#: How many transitions one world keeps. The oldest are pruned first, and pruning
#: is reported by the store rather than hidden.
MAX_TRANSITIONS = 500


def _base(
    kind: TransitionKind,
    *,
    previous_state_id: str,
    current_state_id: str,
    timestamp: str,
    world_id: str,
    evidence: Sequence[str] = (),
    confidence: float | None = None,
) -> dict[str, Any]:
    """The envelope every transition shares — one place, so no factory forgets a field."""
    return {
        "kind": kind,
        "previous_state_id": previous_state_id,
        "current_state_id": current_state_id,
        "timestamp": timestamp,
        "world_id": world_id,
        "evidence": tuple(item for item in evidence if item),
        "confidence": confidence,
    }


def entity_added(
    entity: WorldEntity,
    *,
    previous_state_id: str,
    current_state_id: str,
    timestamp: str,
    world_id: str,
    evidence: Sequence[str] = (),
) -> WorldStateTransition:
    """A new identity entered the state."""
    return WorldStateTransition(
        **_base(
            TransitionKind.ENTITY_ADDED,
            previous_state_id=previous_state_id,
            current_state_id=current_state_id,
            timestamp=timestamp,
            world_id=world_id,
            evidence=evidence or entity.evidence,
            confidence=entity.identity_confidence,
        ),
        entity_id=entity.entity_id,
        current=entity.label or entity.entity_type,
        detail=(
            f"{entity.label or entity.entity_type} was added"
            + ("" if not entity.provisional else " with a provisional identity")
        ),
    )


def attribute_changed(
    entity: WorldEntity,
    attribute: EntityAttribute,
    previous: EntityAttribute | None,
    *,
    previous_state_id: str,
    current_state_id: str,
    timestamp: str,
    world_id: str,
    evidence: Sequence[str] = (),
) -> WorldStateTransition:
    """One fact about an existing entity changed value (or was first recorded)."""
    detail = (
        f"{attribute.name} was recorded as {_short(attribute.value)}"
        if previous is None
        else (
            f"{attribute.name} changed from {_short(previous.value)} to "
            f"{_short(attribute.value)}"
        )
    )
    return WorldStateTransition(
        **_base(
            TransitionKind.ATTRIBUTE_CHANGED,
            previous_state_id=previous_state_id,
            current_state_id=current_state_id,
            timestamp=timestamp,
            world_id=world_id,
            evidence=evidence or attribute.evidence,
            confidence=attribute.confidence,
        ),
        entity_id=entity.entity_id,
        attribute=attribute.name,
        previous=previous.value if previous is not None else None,
        current=attribute.value,
        detail=detail,
    )


def entity_moved(
    entity: WorldEntity,
    *,
    previous_bbox: dict[str, int] | None,
    distance: float | None,
    previous_state_id: str,
    current_state_id: str,
    timestamp: str,
    world_id: str,
    evidence: Sequence[str] = (),
) -> WorldStateTransition:
    """An entity's position changed by more than the configured threshold."""
    return WorldStateTransition(
        **_base(
            TransitionKind.ENTITY_MOVED,
            previous_state_id=previous_state_id,
            current_state_id=current_state_id,
            timestamp=timestamp,
            world_id=world_id,
            evidence=evidence or entity.evidence,
            confidence=entity.identity_confidence,
        ),
        entity_id=entity.entity_id,
        previous=previous_bbox,
        current=entity.bbox.to_dict() if entity.bbox is not None else None,
        detail=(
            f"{entity.label or entity.entity_type} moved {distance:.0f}px"
            if distance is not None
            else f"{entity.label or entity.entity_type} changed position"
        ),
    )


def relationship_changed(
    relationship: WorldRelationship,
    *,
    previous_status: str,
    previous_state_id: str,
    current_state_id: str,
    timestamp: str,
    world_id: str,
    evidence: Sequence[str] = (),
) -> WorldStateTransition:
    """A relation was asserted, stopped being asserted, or was retracted."""
    return WorldStateTransition(
        **_base(
            TransitionKind.RELATIONSHIP_CHANGED,
            previous_state_id=previous_state_id,
            current_state_id=current_state_id,
            timestamp=timestamp,
            world_id=world_id,
            evidence=evidence or relationship.evidence,
            confidence=relationship.confidence,
        ),
        entity_id=relationship.source_entity_id,
        attribute=relationship.kind.value,
        previous=previous_status,
        current=relationship.status.value,
        detail=(
            f"{relationship.kind.value} between {relationship.source_entity_id} and "
            f"{relationship.target_entity_id} is {relationship.status.value}"
        ),
    )


def entity_not_observed(
    entity: WorldEntity,
    *,
    missed: int,
    scope: str,
    previous_state_id: str,
    current_state_id: str,
    timestamp: str,
    world_id: str,
    evidence: Sequence[str] = (),
) -> WorldStateTransition:
    """The entity was absent from a complete observation — NOT a deletion (§10)."""
    return WorldStateTransition(
        **_base(
            TransitionKind.ENTITY_NOT_OBSERVED,
            previous_state_id=previous_state_id,
            current_state_id=current_state_id,
            timestamp=timestamp,
            world_id=world_id,
            evidence=evidence,
            confidence=None,
        ),
        entity_id=entity.entity_id,
        previous="present",
        current="not_observed",
        detail=(
            f"{entity.label or entity.entity_type} was not in a complete observation "
            f"of scope {scope!r} (miss {missed}); absence is not yet a conclusion"
        ),
    )


def entity_confirmed_missing(
    entity: WorldEntity,
    *,
    missed: int,
    scope: str,
    previous_state_id: str,
    current_state_id: str,
    timestamp: str,
    world_id: str,
    evidence: Sequence[str] = (),
) -> WorldStateTransition:
    """Absence of the entity was CONFIRMED by repeated complete observations (§10)."""
    return WorldStateTransition(
        **_base(
            TransitionKind.ENTITY_CONFIRMED_MISSING,
            previous_state_id=previous_state_id,
            current_state_id=current_state_id,
            timestamp=timestamp,
            world_id=world_id,
            evidence=evidence,
            confidence=None,
        ),
        entity_id=entity.entity_id,
        previous="present",
        current="missing",
        detail=(
            f"{entity.label or entity.entity_type} is missing: absent from {missed} "
            f"complete observations of scope {scope!r}"
        ),
    )


def entity_expired(
    entity: WorldEntity,
    *,
    ttl_seconds: int,
    previous_state_id: str,
    current_state_id: str,
    timestamp: str,
    world_id: str,
    evidence: Sequence[str] = (),
) -> WorldStateTransition:
    """The entity passed the retention window and was retired from the current state."""
    return WorldStateTransition(
        **_base(
            TransitionKind.ENTITY_EXPIRED,
            previous_state_id=previous_state_id,
            current_state_id=current_state_id,
            timestamp=timestamp,
            world_id=world_id,
            evidence=evidence,
            confidence=None,
        ),
        entity_id=entity.entity_id,
        previous=entity.status.value,
        current="expired",
        detail=(
            f"{entity.label or entity.entity_type} was last seen at "
            f"{entity.last_seen or 'an unknown time'} and passed the {ttl_seconds}s "
            "retention window; its history is kept"
        ),
    )


def scene_changed(
    *,
    changed: Sequence[str],
    previous_state_id: str,
    current_state_id: str,
    timestamp: str,
    world_id: str,
    evidence: Sequence[str] = (),
) -> WorldStateTransition:
    """Environment-level facts differed — the scene, not one entity, changed."""
    names = [item for item in changed if item]
    return WorldStateTransition(
        **_base(
            TransitionKind.SCENE_CHANGED,
            previous_state_id=previous_state_id,
            current_state_id=current_state_id,
            timestamp=timestamp,
            world_id=world_id,
            evidence=evidence,
            confidence=None,
        ),
        current=names,
        detail="environment facts changed: " + ", ".join(names[:6]),
    )


def state_conflict(
    *,
    subject: str,
    detail: str,
    previous_state_id: str,
    current_state_id: str,
    timestamp: str,
    world_id: str,
    evidence: Sequence[str] = (),
    confidence: float | None = None,
) -> WorldStateTransition:
    """Two observations disagree about a fact — recorded, never resolved by fiat."""
    return WorldStateTransition(
        **_base(
            TransitionKind.STATE_CONFLICT,
            previous_state_id=previous_state_id,
            current_state_id=current_state_id,
            timestamp=timestamp,
            world_id=world_id,
            evidence=evidence,
            confidence=confidence,
        ),
        entity_id=subject if len(subject) <= 32 else "",
        detail=detail,
    )


# ── readers ──────────────────────────────────────────────────────────────────


def for_kind(
    transitions: Iterable[WorldStateTransition], kind: TransitionKind | str
) -> tuple[WorldStateTransition, ...]:
    """Every transition of one kind, in the order they happened."""
    wanted = kind.value if isinstance(kind, TransitionKind) else str(kind)
    return tuple(item for item in transitions if item.kind.value == wanted)


def for_entity(
    transitions: Iterable[WorldStateTransition], entity_id: str
) -> tuple[WorldStateTransition, ...]:
    """Every transition about one entity — its history, in order."""
    wanted = str(entity_id)
    return tuple(item for item in transitions if item.entity_id == wanted)


def between_states(
    transitions: Iterable[WorldStateTransition], previous_state_id: str, current_state_id: str
) -> tuple[WorldStateTransition, ...]:
    """The transitions that moved one version to another."""
    return tuple(
        item
        for item in transitions
        if item.previous_state_id == previous_state_id
        and item.current_state_id == current_state_id
    )


def summary(transitions: Sequence[WorldStateTransition]) -> dict[str, Any]:
    """Counts by kind — what a status surface shows instead of the rows themselves."""
    counts: dict[str, int] = {}
    for item in transitions:
        counts[item.kind.value] = counts.get(item.kind.value, 0) + 1
    return {
        "total": len(transitions),
        "by_kind": counts,
        "newest": transitions[-1].transition_id if transitions else "",
        "oldest": transitions[0].transition_id if transitions else "",
    }


def _short(value: Any) -> str:
    if value is None:
        return "nothing"
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


def relationship_kind_of(transition: WorldStateTransition) -> RelationshipKind | None:
    """The relation kind a ``RELATIONSHIP_CHANGED`` transition is about, or nothing."""
    if transition.kind is not TransitionKind.RELATIONSHIP_CHANGED:
        return None
    for kind in RelationshipKind:
        if kind.value == transition.attribute:
            return kind
    return None


def touched_state_ids(transitions: Iterable[WorldStateTransition]) -> tuple[str, ...]:
    """Every state id a transition set mentions, in order — for a history walk."""
    seen: list[str] = []
    for item in transitions:
        for state_id in (item.previous_state_id, item.current_state_id):
            if state_id and state_id not in seen:
                seen.append(state_id)
    return tuple(seen)


def latest_for(
    transitions: Sequence[WorldStateTransition], kind: TransitionKind | str
) -> WorldStateTransition | None:
    """The most recent transition of a kind, or nothing when none happened."""
    wanted = kind.value if isinstance(kind, TransitionKind) else str(kind)
    for item in reversed(transitions):
        if item.kind.value == wanted:
            return item
    return None


def state_pair_is_known(
    transitions: Sequence[WorldStateTransition], state_id: str
) -> bool:
    """Whether a state id appears in any recorded transition."""
    return any(
        state_id in {item.previous_state_id, item.current_state_id} for item in transitions
    )


def states_seen(transitions: Sequence[WorldStateTransition]) -> tuple[str, ...]:
    """Every state id the transition log knows — the walkable history."""
    return touched_state_ids(transitions)


def world_of(transitions: Sequence[WorldStateTransition]) -> str:
    """The world id the transitions belong to (the first one that states it)."""
    for item in transitions:
        if item.world_id:
            return item.world_id
    return ""


def state_version_of(state: WorldState) -> int:
    """A state's version, clamped to something printable."""
    return max(0, int(state.version))
