"""What Phase 21 can do, classified — and registered with the ONE registry.

The classification is the honest half of this module. Each capability states
what it IS in this build, not what the phase is called:

    ocr             IMPLEMENTED          the existing engine, measured boxes
    detection       PARTIALLY_IMPLEMENTED text geometry and classical regions;
                                         no semantic object detector ships
    segmentation    PARTIALLY_IMPLEMENTED real classical masks; no class labels
    tracking        IMPLEMENTED          SPATIAL continuity, not recognition
    relationships   IMPLEMENTED          derived from box geometry
    temporal        IMPLEMENTED          observed change between two scenes
    abstraction     IMPLEMENTED          deterministic summary, not reasoning
    vlm             PROVIDER_DEPENDENT   a model must be installed and allowed

Availability is a property of the MACHINE and is read live from the engine's own
providers on every call, so installing an OCR engine or wiring a vision model
changes the answer without a restart. A capability this build cannot probe
reports UNKNOWN rather than either answer.

Registration goes through :class:`CapabilityRegistry`, the single registry the
decision layer, the tool selector and every status surface already consult —
never a second table beside it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from novacontrol.intelligence.intent import CapabilityAvailability
from novacontrol.perception.models import CapabilityState, CapabilityStatus, PerceptionCapability

__all__ = [
    "CAPABILITY_TABLE",
    "CapabilityDefinition",
    "capability_rows",
    "register_perception_capabilities",
]

#: The schema version of this table, so a stored report can say what it read.
CAPABILITY_SCHEMA_VERSION = "phase21.1"


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    """One capability as this build ships it, plus how to ask if it can run."""

    name: PerceptionCapability
    state: CapabilityState
    description: str
    #: Which provider decides the LIVE availability: ``ocr``/``detection``/
    #: ``segmentation``/``vlm`` name engine slots, ``deterministic`` means the
    #: capability is code in this build and needs no provider to exist.
    probe: str = "deterministic"
    requires_model: bool = False
    notes: str = ""

    @property
    def capability_id(self) -> str:
        return f"vision.{self.name.value}"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> CapabilityDefinition:
        """A definition from a stored table — tolerant, and never a guess."""
        name = str(data.get("name", "") or "").strip()
        try:
            capability = PerceptionCapability(name)
        except ValueError:
            capability = PerceptionCapability.ABSTRACTION
        return cls(
            name=capability,
            state=CapabilityState(str(data.get("state", CapabilityState.FUTURE.value))),
            description=str(data.get("description", "")),
            probe=str(data.get("probe", "deterministic")),
            requires_model=bool(data.get("requires_model", False)),
            notes=str(data.get("notes", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name.value,
            "capability_id": self.capability_id,
            "state": self.state.value,
            "description": self.description,
            "probe": self.probe,
            "requires_model": self.requires_model,
            "notes": self.notes,
        }


CAPABILITY_TABLE: tuple[CapabilityDefinition, ...] = (
    CapabilityDefinition(
        name=PerceptionCapability.OCR,
        state=CapabilityState.IMPLEMENTED,
        description="Read the text in a frame, with the position of every word.",
        probe="ocr",
        notes="Windows' own engine on this platform; a text file needs no engine.",
    ),
    CapabilityDefinition(
        name=PerceptionCapability.DETECTION,
        state=CapabilityState.PARTIALLY_IMPLEMENTED,
        description="Locate objects: OCR word geometry and classical regions.",
        probe="detection",
        notes=(
            "Region-level and text-level only: no semantic object detector ships, "
            "so no label like 'person' is produced by the fast path."
        ),
    ),
    CapabilityDefinition(
        name=PerceptionCapability.SEGMENTATION,
        state=CapabilityState.PARTIALLY_IMPLEMENTED,
        description="Outline regions as pixel masks (classical connected components).",
        probe="segmentation",
        notes="Real masks at preview resolution; no semantic classes.",
    ),
    CapabilityDefinition(
        name=PerceptionCapability.TRACKING,
        state=CapabilityState.IMPLEMENTED,
        description="Follow objects across frames by spatial continuity.",
        probe="deterministic",
        notes="SPATIAL continuity only — not identity recognition.",
    ),
    CapabilityDefinition(
        name=PerceptionCapability.RELATIONSHIPS,
        state=CapabilityState.IMPLEMENTED,
        description="Derive spatial relations from box geometry, with evidence.",
        probe="deterministic",
    ),
    CapabilityDefinition(
        name=PerceptionCapability.TEMPORAL,
        state=CapabilityState.IMPLEMENTED,
        description="Report observed change between two scenes.",
        probe="deterministic",
        notes="Observed change only: nothing here predicts a next state.",
    ),
    CapabilityDefinition(
        name=PerceptionCapability.ABSTRACTION,
        state=CapabilityState.IMPLEMENTED,
        description="Summarize a scene deterministically, with provenance.",
        probe="deterministic",
        notes="Counting and quoting, not model reasoning.",
    ),
    CapabilityDefinition(
        name=PerceptionCapability.VLM,
        state=CapabilityState.PROVIDER_DEPENDENT,
        description="Deep understanding through a vision model, via the Phase 6 manager.",
        probe="vlm",
        requires_model=True,
        notes="Unavailable unless a multimodal model is installed and allowed.",
    ),
)


def _availability(engine: Any | None, definition: CapabilityDefinition) -> tuple[bool | None,
    str, str]:
    """Whether the capability can run right now: (available, provider, reason).

    ``None`` means this build cannot tell — a capability is never reported as
    available because nobody checked.
    """
    if engine is None:
        return None, "", ""
    if definition.probe == "deterministic":
        return True, "perception", ""
    provider = getattr(engine, definition.probe, None)
    if provider is None:
        return None, "", f"no {definition.probe} provider is wired"
    available = getattr(provider, "available", None)
    name = str(getattr(provider, "name", "") or definition.probe)
    reason = str(getattr(provider, "unavailable_reason", "") or "")
    if not isinstance(available, bool):
        return None, name, reason or f"the {definition.probe} provider cannot be probed"
    return available, name, reason


def capability_rows(engine: Any | None = None) -> tuple[CapabilityStatus, ...]:
    """The capability table with LIVE availability, read from the engine itself."""
    rows: list[CapabilityStatus] = []
    for definition in CAPABILITY_TABLE:
        available, provider, reason = _availability(engine, definition)
        if definition.requires_model and provider == "none":
            available = False
            reason = reason or "no vision model is wired"
        rows.append(
            CapabilityStatus(
                name=definition.name.value,
                state=definition.state,
                available=available,
                provider=provider,
                reason=reason,
            )
        )
    return tuple(rows)


def register_perception_capabilities(registry: Any, engine: Any | None = None) -> int:
    """Declare the perception capabilities on the ONE capability registry.

    Every id is the phase's own name (``vision.ocr``, ``vision.detection``, …)
    and every availability comes from the same live probe the status surface
    uses, so the registry and the engine cannot disagree about this machine.
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
            # A deterministic capability is code in this build and needs no
            # provider at all, so saying "provider: perception" for it would
            # read as a dependency that does not exist.
            reason = (
                "implemented in this build; no provider needed"
                if definition.probe == "deterministic"
                else f"provider: {provider}"
            )
        registry.register_action(
            definition.capability_id,
            description=definition.description,
            capability_id=definition.capability_id,
            executor="perception_engine",
            verifier="scene_verifier",
            category="perception",
            outputs=("scene", "objects", "text", "relationships", "events"),
            permissions=("filesystem:read",) if definition.probe in {"ocr", "detection"} else (),
            required_models=("vision",) if definition.requires_model else (),
            tags=("perception", "vision"),
            examples=_examples(definition.name),
            availability=availability,
            availability_reason=reason,
        )
        registered += 1
    return registered


def _examples(name: PerceptionCapability) -> tuple[str, ...]:
    """A phrase a person would use for each capability — for discovery."""
    return {
        PerceptionCapability.OCR: ("read the text in this image",), 
        PerceptionCapability.DETECTION: ("what objects are visible", "what is on the screen"),
        PerceptionCapability.SEGMENTATION: ("outline the regions in this image",),
        PerceptionCapability.TRACKING: ("follow the moving object across frames",),
        PerceptionCapability.RELATIONSHIPS: ("what is near the laptop",),
        PerceptionCapability.TEMPORAL: ("what changed since the last frame",),
        PerceptionCapability.ABSTRACTION: ("summarize the scene",),
        PerceptionCapability.VLM: ("describe what is happening in this scene",),
    }.get(name, ())

