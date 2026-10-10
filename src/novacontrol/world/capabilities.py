"""What Phase 22 can do, classified — and registered with the ONE registry.

The classification states what each capability IS in this build rather than what
the phase is called. The vocabulary is Phase 21's
:class:`~novacontrol.perception.models.CapabilityState` reused verbatim, because
two vocabularies for the same question is how a capability table starts to lie:

    observe              IMPLEMENTED        deterministic reconciliation, no model
    state_query          IMPLEMENTED        bounded structured reads
    entity_tracking      IMPLEMENTED        identity from ids or geometry
    relationships        IMPLEMENTED        Phase 21 geometry, durable over time
    transitions          IMPLEMENTED        observed change between versions
    temporal_memory      IMPLEMENTED        bounded snapshots and history
    change_detection     IMPLEMENTED        thresholded, deterministic
    state_reasoning      IMPLEMENTED        named rules, not a model
    persistence          IMPLEMENTED        the shared JSON state store
    perception_ingest    IMPLEMENTED        Phase 21 results become observations
    rule_projection      IMPLEMENTED        deterministic motion extrapolation
    world.prediction     PROVIDER_DEPENDENT no predictive model ships; a provider
                                          may be wired, and until it is, the
                                          honest answer is "unavailable"

Availability is a property of the MACHINE and is read live: whether a store is
wired, and whether any prediction provider is installed, are answered from the
engine's own collaborators on every call rather than from a boot snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from novacontrol.intelligence.intent import CapabilityAvailability
from novacontrol.perception.models import CapabilityState, CapabilityStatus
from novacontrol.world.models import as_text

__all__ = [
    "CAPABILITY_SCHEMA_VERSION",
    "CAPABILITY_TABLE",
    "WorldCapabilityDefinition",
    "capability_rows",
    "register_world_capabilities",
]

#: The schema version of this table, so a stored report can say what it read.
CAPABILITY_SCHEMA_VERSION = "phase22.1"


@dataclass(frozen=True, slots=True)
class WorldCapabilityDefinition:
    """One capability as this build ships it, plus how to probe it."""

    capability_id: str
    state: CapabilityState
    description: str
    #: ``deterministic`` needs no provider at all; ``store`` and ``prediction``
    #: name engine collaborators that may or may not be wired.
    probe: str = "deterministic"
    requires_model: bool = False
    notes: str = ""
    permissions: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> WorldCapabilityDefinition:
        """A definition from a stored table — tolerant, and never a guess."""
        return cls(
            capability_id=as_text(data.get("capability_id"), "world.unknown"),
            state=CapabilityState(
                as_text(data.get("state"), CapabilityState.FUTURE.value)
            ),
            description=as_text(data.get("description")),
            probe=as_text(data.get("probe"), "deterministic"),
            requires_model=bool(data.get("requires_model", False)),
            notes=as_text(data.get("notes")),
            permissions=tuple(str(item) for item in data.get("permissions", ()) or ()),
            outputs=tuple(str(item) for item in data.get("outputs", ()) or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "state": self.state.value,
            "description": self.description,
            "probe": self.probe,
            "requires_model": self.requires_model,
            "notes": self.notes,
            "permissions": list(self.permissions),
            "outputs": list(self.outputs),
        }


CAPABILITY_TABLE: tuple[WorldCapabilityDefinition, ...] = (
    WorldCapabilityDefinition(
        capability_id="world.observe",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Ingest a normalized observation and advance the world state with its "
            "transitions, changes and uncertainty."
        ),
        outputs=("world_state", "transitions", "changes", "uncertainty"),
    ),
    WorldCapabilityDefinition(
        capability_id="world.state_query",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Answer bounded structured questions about current and historical state, "
            "including evidence, uncertainty and staleness."
        ),
        outputs=("rows", "evidence", "limitations"),
    ),
    WorldCapabilityDefinition(
        capability_id="world.entity_tracking",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Maintain stable entity identities from source ids or overlapping geometry, "
            "keeping an ambiguous match provisional."
        ),
        notes="Identity is resolved from evidence, not from a label alone.",
        outputs=("entities",),
    ),
    WorldCapabilityDefinition(
        capability_id="world.relationships",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Hold durable relations over time, derived from Phase 21 geometry or stated "
            "by a source, with staleness and retraction."
        ),
        notes="Geometric relations go stale when a complete observation stops repeating them.",
        outputs=("relationships",),
    ),
    WorldCapabilityDefinition(
        capability_id="world.state_transitions",
        state=CapabilityState.IMPLEMENTED,
        description="Record what changed between state versions, with the evidence.",
        outputs=("transitions",),
    ),
    WorldCapabilityDefinition(
        capability_id="world.temporal_memory",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Keep a bounded history of versions, transitions and observation references "
            "with retention applied on write."
        ),
        notes="Observation references are ids, sources and times — never contents.",
        outputs=("snapshots", "transitions", "references"),
    ),
    WorldCapabilityDefinition(
        capability_id="world.change_detection",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Report meaningful differences between versions above stated thresholds, "
            "including conflicts and staleness."
        ),
        outputs=("changes",),
    ),
    WorldCapabilityDefinition(
        capability_id="world.state_reasoning",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Answer questions through named deterministic rules, with evidence, "
            "confidence and stated limitations."
        ),
        notes="No chain-of-thought, no agent, no free-form model output.",
        outputs=("conclusions",),
    ),
    WorldCapabilityDefinition(
        capability_id="world.persistence",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Persist the state, transitions and references through the shared JSON state "
            "store, and restore them without letting one bad record cost the state."
        ),
        probe="store",
        permissions=("filesystem:read", "filesystem:write"),
        notes=(
            "Uses the application's existing store; a world with no store "
            "configured reports no store."
        ),
        outputs=("snapshot_file",),
    ),
    WorldCapabilityDefinition(
        capability_id="world.perception_ingest",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Convert Phase 21 perception results and scenes into normalized observations, "
            "including their geometry and identity hints."
        ),
        notes="Built on Phase 21 rather than beside it; text is not copied by default.",
        outputs=("observation",),
    ),
    WorldCapabilityDefinition(
        capability_id="world.rule_projection",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Project one measured displacement forward over a requested horizon, labelled "
            "as a rule-based projection rather than a learned prediction."
        ),
        notes="Deterministic arithmetic; confidence is never fabricated.",
        outputs=("prediction",),
    ),
    WorldCapabilityDefinition(
        capability_id="world.prediction",
        state=CapabilityState.PROVIDER_DEPENDENT,
        description=(
            "Predict a future state through a wired predictive provider, respecting "
            "resource admission before any model is loaded."
        ),
        probe="prediction",
        notes=(
            "No predictive model ships with this build: until a provider is wired, a "
            "prediction request answers MODEL_UNAVAILABLE rather than guessing."
        ),
        outputs=("prediction",),
    ),
)


def _availability(engine: Any | None, definition: WorldCapabilityDefinition) -> tuple[
    bool | None, str, str
]:
    """Whether the capability can run right now: ``(available, provider, reason)``.

    ``None`` means this build cannot tell. A capability is never reported available
    because nobody checked.
    """
    if engine is None:
        return None, "", ""
    if definition.probe == "deterministic":
        return True, "world_model", ""
    if definition.probe == "store":
        store = getattr(engine, "repository", None)
        if store is None:
            return False, "none", (
                "no world store is configured, so nothing is persisted; the state still "
                "works in memory"
            )
        return True, str(getattr(store, "prefix", "json") or "json"), ""
    if definition.probe == "prediction":
        service = getattr(engine, "prediction", None)
        if service is None:
            return None, "", "no prediction service is wired"
        availability = service.availability()
        name = as_text(availability.get("provider"), "none")
        available = bool(availability.get("available"))
        reason = as_text(availability.get("reason"))
        if not available and not reason:
            reason = "no predictive provider is wired"
        return available, name, reason
    return None, "", f"the probe {definition.probe!r} is not one this build knows"


def capability_rows(engine: Any | None = None) -> tuple[CapabilityStatus, ...]:
    """The table with LIVE availability, read from the engine itself."""
    rows: list[CapabilityStatus] = []
    for definition in CAPABILITY_TABLE:
        available, provider, reason = _availability(engine, definition)
        rows.append(
            CapabilityStatus(
                name=definition.capability_id,
                state=definition.state,
                available=available,
                provider=provider,
                reason=reason,
            )
        )
    return tuple(rows)


def register_world_capabilities(registry: Any, engine: Any | None = None) -> int:
    """Declare the world capabilities on the ONE capability registry.

    Same door Phase 21 uses, same live probe: the registry and the engine cannot
    disagree about this machine.
    """
    registered = 0
    for definition in CAPABILITY_TABLE:
        available, provider, reason = _availability(engine, definition)
        if available is None:
            availability = CapabilityAvailability.UNKNOWN
        else:
            availability = (
                CapabilityAvailability.AVAILABLE
                if available
                else CapabilityAvailability.UNAVAILABLE
            )
        if not reason:
            reason = (
                "implemented in this build; no provider needed"
                if definition.probe == "deterministic"
                else f"provider: {provider}"
            )
        registry.register_action(
            definition.capability_id,
            description=definition.description,
            capability_id=definition.capability_id,
            executor="world_model",
            verifier="state_verifier",
            category="world",
            outputs=definition.outputs,
            permissions=definition.permissions,
            required_models=("prediction",) if definition.requires_model else (),
            tags=("world", "state", "memory"),
            examples=_examples(definition.capability_id),
            availability=availability,
            availability_reason=reason,
        )
        registered += 1
    return registered


def _examples(capability_id: str) -> tuple[str, ...]:
    """A phrase a person would use for each capability — for discovery."""
    return {
        "world.observe": ("remember what is on the desk", "note what is visible now"),
        "world.state_query": ("what is currently visible", "what do we know about the room"),
        "world.entity_tracking": ("has this laptop been seen before",),
        "world.relationships": ("what is near the laptop", "was the phone ever on the desk"),
        "world.state_transitions": ("what changed since the last observation",),
        "world.temporal_memory": ("what did the room look like an hour ago",),
        "world.change_detection": ("what changed",),
        "world.state_reasoning": ("why do we believe the laptop is here",),
        "world.persistence": ("remember this across restarts",),
        "world.perception_ingest": ("remember the scene you just looked at",),
        "world.rule_projection": ("where will the object be in ten seconds",),
        "world.prediction": ("predict what happens next",),
    }.get(capability_id, ())
