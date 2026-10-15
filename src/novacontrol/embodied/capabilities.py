"""What Phase 25 can do, classified — and registered with the ONE registry.

The classification says what each capability IS in this build rather than what
the phase is called. It reuses the same vocabulary Phase 24 used, so the two
cannot disagree about what "IMPLEMENTED" means, and it states the absences as
loudly as the presences:

    session_lifecycle        IMPLEMENTED       create/pause/resume/cancel/restore,
                                               with revalidation before any input
    environment_adapter      IMPLEMENTED       one interface, one owned adapter
    gridworld_environment    IMPLEMENTED       the deterministic simulation
    screen_environment       DEFERRED          no game/window adapter ships here
    structured_observation   IMPLEMENTED       the environment's own state, with
                                               provenance and freshness
    semantic_perception      PROVIDER_DEPENDENT  pixels need a model; regions stay
                                               regions and are never named
    world_model_update       IMPLEMENTED       observations become observed facts
    tactical_navigation      IMPLEMENTED       bounded subgoals, remember a blockage
    action_selection         IMPLEMENTED       through Phase 24, never a private loop
    precondition_safety      IMPLEMENTED       re-grounded immediately before input
    input_gating             IMPLEMENTED       one door: session, allowlist, clamp,
                                               permission layer, audit
    action_verification      IMPLEMENTED       a fresh reading after the input
    outcome_verification     IMPLEMENTED       a condition only from a fresh reading
    recovery_replanning      IMPLEMENTED       bounded, and a refusal is never retried
    trajectory_evaluation    IMPLEMENTED       bounded, with per-session resource use
    batching                 DEFERRED          one input per cycle; no macro actions
    learning_from_sessions   DEFERRED          Phase 26 territory
    generalization           DEFERRED          Phase 26 territory

Availability is a property of the MACHINE and is read live from the manager on
every call, never from a boot snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from novacontrol.embodied.models import EnvironmentStatus
from novacontrol.intelligence.intent import CapabilityAvailability
from novacontrol.perception.models import CapabilityState, CapabilityStatus

__all__ = [
    "CAPABILITY_SCHEMA_VERSION",
    "CAPABILITY_TABLE",
    "EmbodiedCapabilityDefinition",
    "capability_rows",
    "register_embodied_capabilities",
]

CAPABILITY_SCHEMA_VERSION = "phase25.1"


@dataclass(frozen=True, slots=True)
class EmbodiedCapabilityDefinition:
    """One capability as this build ships it, plus how to probe it."""

    capability_id: str
    state: CapabilityState
    description: str
    #: ``manager`` reads the live session manager; ``deferred`` is a deliberate
    #: absence; ``provider`` names a model or a service that must be wired.
    probe: str = "deterministic"
    requires_model: bool = False
    permissions: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    notes: str = ""

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


CAPABILITY_TABLE: tuple[EmbodiedCapabilityDefinition, ...] = (
    EmbodiedCapabilityDefinition(
        capability_id="embodied.session_lifecycle",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Hold a session as data — its environment, objective, budgets and "
            "trajectory — and create, pause, resume, cancel and restore it "
            "without ever sending an input to a restored one before revalidation."
        ),
        probe="manager",
        outputs=("session_id", "status", "objective", "evaluation"),
    ),
    EmbodiedCapabilityDefinition(
        capability_id="embodied.environment_adapter",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "One adapter interface (status/connect/observe/send_input/release/"
            "shutdown) with the capability it declares and the capability it has "
            "VERIFIED reported separately."
        ),
        probe="manager",
        outputs=("environment_id", "status", "capabilities"),
    ),
    EmbodiedCapabilityDefinition(
        capability_id="embodied.gridworld_environment",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "The owned deterministic simulation: a grid world whose whole state "
            "is readable, with scripted faults for the failure paths."
        ),
        probe="manager",
        permissions=("environment:control",),
        outputs=("structured_state", "grid", "scenario"),
    ),
    EmbodiedCapabilityDefinition(
        capability_id="embodied.screen_environment",
        state=CapabilityState.FUTURE,
        description=("Drive a real game or window by screen capture and synthetic input."),
        probe="deferred",
        notes=(
            "no screen/game adapter ships in this build: it needs an "
            "owner-permitted environment, and pretending to have one would make "
            "every measurement here meaningless"
        ),
    ),
    EmbodiedCapabilityDefinition(
        capability_id="embodied.structured_observation",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Read the environment's own state with provenance, a basis and a "
            "freshness attached, and refuse to authorize a decision on a stale "
            "or non-structured reading."
        ),
        probe="manager",
        outputs=("observation_id", "basis", "freshness", "structured"),
    ),
    EmbodiedCapabilityDefinition(
        capability_id="embodied.semantic_perception",
        state=CapabilityState.PROVIDER_DEPENDENT,
        description=("Identify objects (enemy, item, player) in a visual reading."),
        probe="provider",
        requires_model=True,
        notes=(
            "without a vision-language model the adapter reports REGIONS with "
            "positions and confidences and no identity is invented from them"
        ),
    ),
    EmbodiedCapabilityDefinition(
        capability_id="embodied.world_model_update",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Turn an observation into facts on the Phase 22 world model: "
            "structured state becomes an observed fact, an unnamed region stays "
            "an unnamed region."
        ),
        probe="manager",
        outputs=("facts", "limitations"),
    ),
    EmbodiedCapabilityDefinition(
        capability_id="embodied.tactical_navigation",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Name the next bounded subgoal from a fresh reading, route around a "
            "blockage that was actually observed, and stop repeating an action "
            "that did not work."
        ),
        probe="manager",
        outputs=("kind", "direction", "reason"),
    ),
    EmbodiedCapabilityDefinition(
        capability_id="embodied.action_selection",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Select and authorize each input through the Phase 24 goal planner "
            "and its action policy — never through a private loop beside it."
        ),
        probe="manager",
        permissions=("environment:control",),
        outputs=("goal_id", "plan", "steps"),
    ),
    EmbodiedCapabilityDefinition(
        capability_id="embodied.precondition_safety",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Re-ground the preconditions against a fresh reading immediately "
            "before the input goes out, and refuse (DENIED) rather than act on a "
            "state that no longer supports the step."
        ),
        probe="manager",
        outputs=("preconditions", "revalidation"),
    ),
    EmbodiedCapabilityDefinition(
        capability_id="embodied.input_gating",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Send every environment input through one door: the session must be "
            "live, the environment allowlisted and connected, the duration "
            "clamped, the action declared on the permission layer and the result "
            "written to the audit trail."
        ),
        probe="manager",
        permissions=("environment:control",),
        outputs=("actions_sent", "audit"),
    ),
    EmbodiedCapabilityDefinition(
        capability_id="embodied.action_verification",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Take a fresh reading after each input and compare it with the effect "
            "the action declared. An unconfirmable result is reported as "
            "unverified, which is never counted as a success."
        ),
        probe="manager",
        outputs=("verified", "verification"),
    ),
    EmbodiedCapabilityDefinition(
        capability_id="embodied.outcome_verification",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Complete a session only when its stated success conditions are seen "
            "holding in a fresh reading; otherwise report exactly which condition "
            "was not observed."
        ),
        probe="manager",
        outputs=("met", "missing", "observed"),
    ),
    EmbodiedCapabilityDefinition(
        capability_id="embodied.recovery_replanning",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Recover inside the session's own ceiling, remember what was blocked, "
            "re-observe before re-sending a timed-out input, and stop on a refusal "
            "instead of retrying through it."
        ),
        probe="manager",
        outputs=("recoveries", "replans", "reason"),
    ),
    EmbodiedCapabilityDefinition(
        capability_id="embodied.trajectory_evaluation",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Keep a bounded, content-free trajectory and evaluate completion, "
            "invalid actions, recoveries and resource use from it."
        ),
        probe="manager",
        outputs=("trajectory", "evaluation"),
    ),
    EmbodiedCapabilityDefinition(
        capability_id="embodied.batching",
        state=CapabilityState.FUTURE,
        description="Send several inputs as one authorized macro action.",
        probe="deferred",
        notes="out of this phase's scope; one input per cycle keeps verification exact",
    ),
    EmbodiedCapabilityDefinition(
        capability_id="embodied.learning_from_sessions",
        state=CapabilityState.FUTURE,
        description="Learn a policy from the sessions this phase records.",
        probe="deferred",
        notes="Phase 26 territory",
    ),
    EmbodiedCapabilityDefinition(
        capability_id="embodied.generalization",
        state=CapabilityState.FUTURE,
        description="Measure transfer to an environment nobody trained on.",
        probe="deferred",
        notes="Phase 26 territory",
    ),
)


def _availability(
    manager: Any | None, definition: EmbodiedCapabilityDefinition
) -> tuple[bool | None, str, str]:
    """Whether the MACHINE has this capability right now, and on what."""
    if definition.probe == "deferred":
        return False, "none", definition.notes or "deferred to a later phase"
    if definition.probe == "provider":
        if manager is None:
            return None, "", "no manager is wired, so no provider could be asked"
        environments = manager.environments()
        # An adapter DECLARES its capabilities; the probe reads the declaration
        # rather than assuming every adapter can name what it sees.
        provides = any(
            capability.name == "semantic_perception"
            and capability.status is EnvironmentStatus.OPERATIONAL
            for item in environments
            for capability in item.capabilities
        )
        return (
            provides,
            "environment adapter",
            (
                "an environment adapter reports semantic perception (a labeled reading "
                "with confidence)"
                if provides
                else "no environment adapter reports semantic perception, so regions stay regions"
            ),
        )
    if definition.probe == "manager":
        if manager is None:
            return (
                False,
                "none",
                "no embodied session manager is wired in this build",
            )
        enabled = bool(getattr(manager.settings, "enabled", False))
        return (
            enabled,
            "embodied manager",
            "sessions are enabled and the manager is wired"
            if enabled
            else "embodied sessions are disabled by configuration",
        )
    return None, "", f"the probe {definition.probe!r} is not one this build knows"


def capability_rows(manager: Any | None = None) -> tuple[CapabilityStatus, ...]:
    """The table with LIVE availability, read from the manager itself."""
    rows: list[CapabilityStatus] = []
    for definition in CAPABILITY_TABLE:
        available, provider, reason = _availability(manager, definition)
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


def register_embodied_capabilities(registry: Any, manager: Any | None = None) -> int:
    """Declare the embodied capabilities on the ONE capability registry."""
    registered = 0
    for definition in CAPABILITY_TABLE:
        available, provider, reason = _availability(manager, definition)
        if available is None:
            availability = CapabilityAvailability.UNKNOWN
        else:
            availability = (
                CapabilityAvailability.AVAILABLE
                if available
                else CapabilityAvailability.UNAVAILABLE
            )
        registry.register_action(
            definition.capability_id,
            description=definition.description,
            capability_id=definition.capability_id,
            executor="embodied",
            verifier="plan_verifier",
            category="embodied",
            outputs=definition.outputs,
            permissions=definition.permissions,
            required_models=("vision",) if definition.requires_model else (),
            tags=("embodied", "environment", "session"),
            examples=_examples(definition.capability_id),
            availability=availability,
            availability_reason=reason or f"provider: {provider}",
        )
        registered += 1
    return registered


def _examples(capability_id: str) -> tuple[str, ...]:
    """A phrase a person would use for each capability — for discovery."""
    return {
        "embodied.session_lifecycle": ("start a session in the grid world",),
        "embodied.environment_adapter": ("what environments can you drive",),
        "embodied.gridworld_environment": ("play the grid world simulation",),
        "embodied.screen_environment": ("play this game on my screen",),
        "embodied.structured_observation": ("what is the current game state",),
        "embodied.semantic_perception": ("find the enemy on screen",),
        "embodied.world_model_update": ("remember where the target is",),
        "embodied.tactical_navigation": ("get to the target",),
        "embodied.action_selection": ("choose the next move",),
        "embodied.precondition_safety": ("is it safe to move now",),
        "embodied.input_gating": ("what is holding the controls",),
        "embodied.action_verification": ("did that move work",),
        "embodied.outcome_verification": ("did I finish the objective",),
        "embodied.recovery_replanning": ("try a different route",),
        "embodied.trajectory_evaluation": ("how did the session go",),
        "embodied.batching": ("run a combo",),
        "embodied.learning_from_sessions": ("learn to play better",),
        "embodied.generalization": ("play a game you never saw",),
    }.get(capability_id, ())


def capability_table_rows(manager: Any | None = None) -> tuple[dict[str, Any], ...]:
    """The table as plain rows, for a status payload."""
    rows: list[dict[str, Any]] = []
    for definition, status in zip(CAPABILITY_TABLE, capability_rows(manager), strict=False):
        payload = definition.to_dict()
        payload["available"] = status.available
        payload["availability_reason"] = status.reason
        rows.append(payload)
    return tuple(rows)
