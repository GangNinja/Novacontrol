"""The bridge to the agent layers: an observation for Phase 20, and the Phase 21 door.

Two adapters, both deliberately thin.

**Phase 20 receives an observation, not a state machine.** ``AgentState`` already
has the shape a policy reads (``observations`` and ``environment_state``), and this
module fills those two fields from a world state — counted, bounded and free of
attribute values. Nothing here imports a policy, chooses an action or advances an
episode: a world model that could advance a state machine would be running the loop
this phase explicitly does not own (§19). The Phase 20 import is deferred to the
one function that needs it, so importing this package never drags the agentic stack
in.

**Phase 21 feeds the state through the real engine.** ``observe_perception`` awaits
``PerceptionEngine.perceive`` and ingests its result — there is no second perception
path here, and no re-implementation of what Phase 21 already does. The conversion
lives in :mod:`novacontrol.world.ingest`, so this function only decides WHEN to look
and what to do with the answer.

What this deliberately does not provide: a reward, a predicted next state, an action
or a policy. Those are Phases 23–24, and inventing their shape here would pre-empt
them with something worse designed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from novacontrol.world.engine import WorldModelEngine
from novacontrol.world.ingest import observation_from_perception
from novacontrol.world.models import (
    EntityStatus,
    UpdateReport,
    WorldState,
    WorldStateTransition,
    as_text,
)

__all__ = [
    "MAX_OBSERVED_ENTITIES",
    "MAX_OBSERVED_RELATIONSHIPS",
    "MAX_OBSERVED_TRANSITIONS",
    "WORLD_OBSERVATION_KIND",
    "attach_world_state",
    "environment_state_for",
    "observe_perception",
    "world_observation",
]

#: The `kind` a world observation carries, so a consumer can tell what it is.
WORLD_OBSERVATION_KIND = "world.state"

#: Hard ceilings on every list in an observation: an observation that grows with
#: the world is one that eventually breaks a context window.
MAX_OBSERVED_ENTITIES = 32
MAX_OBSERVED_RELATIONSHIPS = 24
MAX_OBSERVED_TRANSITIONS = 16


def world_observation(
    state: WorldState | None,
    *,
    transitions: Sequence[WorldStateTransition] = (),
    limitations: Sequence[str] = (),
) -> dict[str, Any]:
    """One world state as a Phase 20 observation mapping — bounded, content-light.

    Carries identity, kind, status, position, counts and the newest transitions.
    It does NOT carry attribute values: those are what a query is for, and copying
    them into every observation is how a memory becomes a transcript.
    """
    if state is None:
        return {"kind": WORLD_OBSERVATION_KIND, "present": False}
    active = [item for item in state.entities if item.status is not EntityStatus.EXPIRED]
    return {
        "kind": WORLD_OBSERVATION_KIND,
        "present": True,
        "world_id": state.world_id,
        "state_id": state.state_id,
        "version": state.version,
        "timestamp": state.timestamp,
        "entities": [
            {
                "entity_id": item.entity_id,
                "label": item.label,
                "entity_type": item.entity_type,
                "status": item.status.value,
                "bbox": item.bbox.to_dict() if item.bbox is not None else None,
                "provisional": item.provisional,
                "identity_confidence": item.identity_confidence,
            }
            for item in active[:MAX_OBSERVED_ENTITIES]
        ],
        "entity_count": len(active),
        "relationships": [
            {
                "kind": item.kind.value,
                "source_entity_id": item.source_entity_id,
                "target_entity_id": item.target_entity_id,
                "status": item.status.value,
            }
            for item in state.active_relationships()[:MAX_OBSERVED_RELATIONSHIPS]
        ],
        "relationship_count": len(state.active_relationships()),
        "uncertainty_count": len(state.uncertainty),
        "transitions": [
            {
                "kind": item.kind.value,
                "entity_id": item.entity_id,
                "detail": item.detail,
                "timestamp": item.timestamp,
            }
            for item in tuple(transitions)[-MAX_OBSERVED_TRANSITIONS:]
        ],
        "sources": list(state.sources),
        "confidence": state.confidence,
        "limitations": list(limitations)[:8],
    }


def environment_state_for(state: WorldState | None) -> dict[str, Any]:
    """The ``environment_state`` Phase 20's ``AgentState`` already accepts.

    Small on purpose — a summary a policy can include in a fingerprint without the
    world state becoming part of the agent's identity.
    """
    if state is None:
        return {"world": {"observed": False}}
    active = [item for item in state.entities if item.status is not EntityStatus.EXPIRED]
    return {
        "world": {
            "observed": True,
            "world_id": state.world_id,
            "state_id": state.state_id,
            "version": state.version,
            "entities": len(active),
            "relationships": len(state.active_relationships()),
            "uncertainty": len(state.uncertainty),
            "labels": sorted({item.label for item in active if item.label})[:16],
        }
    }


def attach_world_state(
    agent_state: Any,
    state: WorldState | None,
    *,
    transitions: Sequence[WorldStateTransition] = (),
) -> Any:
    """Advance any state that speaks Phase 20's ``advanced`` contract.

    Duck-typed on ONE method, exactly like the perception bridge: requiring the
    concrete ``AgentState`` would mean importing the agentic package for a type
    annotation nobody reads, and a caller without that method gets the observation
    back rather than a broken state.
    """
    observation = world_observation(state, transitions=transitions)
    advanced = getattr(agent_state, "advanced", None)
    if callable(advanced):
        return advanced(
            observation=observation, environment_state=environment_state_for(state)
        )
    return observation


async def observe_perception(
    engine: WorldModelEngine,
    perception: Any,
    request: Any,
    *,
    correlation_id: str = "",
    complete: bool = False,
    include_text: bool = False,
    scope: str = "",
) -> tuple[Any, UpdateReport]:
    """Look through the REAL perception engine, then remember what it saw (§22).

    Returns the perception result AND the ingest report, because a caller needs both
    readings: did the looking work, and did the remembering work. They are separate
    facts — a perception that failed is still ingested as "nothing could look", which
    is itself information the state should hold.
    """
    result = await perception.perceive(request)
    observation = observation_from_perception(
        result, include_text=include_text, complete=complete, scope=scope
    )
    if correlation_id:
        observation = _with_correlation(observation, correlation_id)
    report = engine.observe(observation)
    return result, report


def _with_correlation(observation: Any, correlation_id: str) -> Any:
    """The same observation with a correlation id stamped on it."""
    from dataclasses import replace

    return replace(observation, correlation_id=as_text(correlation_id))


def bounded_labels(state: WorldState | None, limit: int = 16) -> tuple[str, ...]:
    """The active labels of a world, sorted and bounded — for a short summary line."""
    if state is None:
        return ()
    labels = sorted(
        {
            item.label
            for item in state.entities
            if item.label and item.status is not EntityStatus.EXPIRED
        }
    )
    return tuple(labels[: max(0, limit)])


def observation_summary(mapping: Mapping[str, Any]) -> str:
    """One short line describing a world observation — for a log or a status surface."""
    if not mapping.get("present"):
        return "the world has not been observed"
    return (
        f"world {mapping.get('world_id', '')} v{mapping.get('version', 0)}: "
        f"{mapping.get('entity_count', 0)} entit(ies), "
        f"{mapping.get('relationship_count', 0)} relation(s), "
        f"{mapping.get('uncertainty_count', 0)} open uncertainty"
    )
