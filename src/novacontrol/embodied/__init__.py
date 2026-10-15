"""Embodied agents and controlled environments — Phase 25.

One sentence for what this package is: **it puts an agent in an environment and
makes it observe, decide, act, verify and recover — through the layers that
already exist, and without ever claiming something it did not observe.**

The loop it runs is the one the phase names, and each arrow is a recorded step:

    observe → interpret → update the world model → identify the objective
    → select an action through Phase 24 → check preconditions and safety
    → send the input through the ONE approved door → observe the result
    → verify whether it worked → update memory and the trajectory
    → recover or replan

It is built ON the layers before it rather than beside them:

  * Phase 4/24 plan the step and the application's executor runs it — this
    package never sends an input itself, it registers a step handler
  * Phase 5 tool selection and the tool registry are how a step becomes a call
  * Phase 8's permission layer, verification engine, recovery advisor and retry
    ceilings are asked, not reimplemented
  * Phase 9's event bus and capability registry carry the announcements and the
    capability table
  * Phase 13's audit trail records every input; Phase 14's governor decides
    whether a model may load, so nothing here loads one
  * Phase 22's world model takes the observations as observed facts
  * Phase 15's trajectory/recorder vocabulary is what the session evaluation uses

The honest positions are stated at the same volume as the features:

  * the only environment shipped here is a SIMULATION this build owns; a real
    game or window adapter is not shipped and is not claimed
  * a region nobody identified is never given a name — no enemy, item or player
    is invented from pixels
  * an observation that is stale or not structured never authorizes a move
  * an input nobody could verify is not a success; a timeout is re-observed
    rather than blindly re-sent
  * an objective is completed only when its conditions are SEEN holding
  * a refused input stops the session rather than being retried through
  * a restored session sends nothing until it is revalidated against a live
    environment
  * nothing here trains a model, downloads one, or drives an environment the
    operator has not allowed

``overview()`` is the machine-readable form of those statements; the live
per-machine state is :meth:`novacontrol.embodied.manager.EmbodiedManager.status`.
"""

from novacontrol.embodied.agent import (
    AGENT_ACTION_EXECUTED,
    AGENT_ACTION_FAILED,
    AGENT_ACTION_SELECTED,
    AGENT_BLOCKED,
    AGENT_OBJECTIVE_FAILED,
    AGENT_OBJECTIVE_VERIFIED,
    AGENT_OBSERVATION,
    AGENT_RECOVERY,
    AGENT_SESSION_CANCELLED,
    AGENT_SESSION_ENDED,
    AGENT_SESSION_PAUSED,
    AGENT_SESSION_RESUMED,
    AGENT_SESSION_STARTED,
    DEFAULT_MAX_ACTIONS,
    DEFAULT_MAX_TRAJECTORY,
    DEFAULT_REFRESH_ATTEMPTS,
    EmbodiedAgent,
)
from novacontrol.embodied.capabilities import (
    CAPABILITY_SCHEMA_VERSION,
    CAPABILITY_TABLE,
    EmbodiedCapabilityDefinition,
    capability_rows,
    capability_table_rows,
    register_embodied_capabilities,
)
from novacontrol.embodied.environment import (
    EnvironmentAdapter,
    EnvironmentRefused,
    EnvironmentRegistry,
    ReleaseReport,
)
from novacontrol.embodied.manager import (
    ENVIRONMENT_INPUT,
    ENVIRONMENT_SCOPE,
    HARD_MAX_ACTIONS,
    HARD_MAX_INPUT_MS,
    HARD_MAX_SECONDS,
    HARD_MAX_SESSIONS,
    STORE_KEY,
    EmbodiedManager,
    EmbodiedSettingsView,
)
from novacontrol.embodied.models import (
    PHASE,
    RESUMABLE_STATUSES,
    SCHEMA_VERSION,
    ActionKind,
    ActionResult,
    ActionStatus,
    EntityRead,
    EnvironmentAction,
    EnvironmentCapability,
    EnvironmentInfo,
    EnvironmentKind,
    EnvironmentStatus,
    Evaluation,
    Objective,
    ObjectiveInvalid,
    ObjectiveVerdict,
    Observation,
    ObservationBasis,
    ObservationFreshness,
    SessionState,
    SessionStatus,
    TrajectoryEntry,
    validate_objective,
)
from novacontrol.embodied.perception import (
    FACT_PREFIX,
    SEMANTIC_LABEL_CLAIMS,
    FactSet,
    effect_observed,
    facts_from_observation,
    same_value,
    to_world_observation,
)
from novacontrol.embodied.simulation import (
    FAULT_NAMES,
    MAX_MOVE_STEPS,
    Faults,
    GridWorldSimulation,
    adapter_for,
    scenarios,
)
from novacontrol.embodied.tactical import (
    TacticalChoice,
    TacticalMemory,
    choose,
    tasks_for,
)

#: What this phase deliberately does NOT implement, named so a reader never has
#: to infer the boundary from absence. Published by ``overview()["deferred"]``.
DEFERRED_PHASES: tuple[str, ...] = (
    "Phase 26: generalization, ARC and intelligence evaluation",
    "a real game or window adapter (this phase ships a simulation only)",
    "learning a policy from recorded sessions",
    "batching several inputs into one authorized macro action",
)

#: The lifecycle tables, so a report, a test or a dashboard reads the same words
#: the implementation uses instead of restating them.
LIFECYCLE: dict[str, list[str]] = {
    "session": [member.value for member in SessionStatus],
    "action": [member.value for member in ActionStatus],
    "action_kind": [member.value for member in ActionKind],
    "environment": [member.value for member in EnvironmentStatus],
    "environment_kind": [member.value for member in EnvironmentKind],
    "observation_basis": [member.value for member in ObservationBasis],
    "observation_freshness": [member.value for member in ObservationFreshness],
}

#: The event vocabulary this package publishes on the application's ONE bus.
EVENT_TYPES: tuple[str, ...] = (
    AGENT_SESSION_STARTED,
    AGENT_SESSION_PAUSED,
    AGENT_SESSION_RESUMED,
    AGENT_SESSION_CANCELLED,
    AGENT_SESSION_ENDED,
    AGENT_OBSERVATION,
    AGENT_ACTION_SELECTED,
    AGENT_ACTION_EXECUTED,
    AGENT_ACTION_FAILED,
    AGENT_RECOVERY,
    AGENT_BLOCKED,
    AGENT_OBJECTIVE_VERIFIED,
    AGENT_OBJECTIVE_FAILED,
)


def overview() -> dict[str, object]:
    """What this phase ships and what it refuses to claim — cheap and read-only."""
    return {
        "phase": PHASE,
        "schema_version": SCHEMA_VERSION,
        "cycle": [
            "observe",
            "interpret",
            "update_world_model",
            "identify_objective",
            "select_action_through_planning",
            "check_preconditions_and_safety",
            "send_input_through_the_one_door",
            "observe_result",
            "verify_action_and_objective",
            "update_memory_and_trajectory",
            "recover_or_replan",
        ],
        "lifecycle": {key: list(value) for key, value in LIFECYCLE.items()},
        "environments": ["gridworld"],
        "scenarios": list(scenarios()),
        "faults": list(FAULT_NAMES),
        "event_types": list(EVENT_TYPES),
        "capabilities": [row.capability_id for row in CAPABILITY_TABLE],
        "input_door": {
            "action": ENVIRONMENT_INPUT,
            "scope": ENVIRONMENT_SCOPE,
            "rule": (
                "one handler sends environment inputs; it checks the session, the "
                "allowlist, the connection, the duration clamp, the permission layer "
                "and writes the audit record"
            ),
        },
        "budgets": {
            "max_sessions": HARD_MAX_SESSIONS,
            "max_actions": HARD_MAX_ACTIONS,
            "max_seconds": HARD_MAX_SECONDS,
            "max_input_ms": HARD_MAX_INPUT_MS,
            "note": "a configuration may ask for less; it can never ask for more",
        },
        "integration": {
            "phase4": (
                "the application's executor runs every step; "
                "this package only plans and reads"
            ),
            "phase5": "a step reaches a tool through the existing tool registry",
            "phase8": (
                "the permission layer, verification engine, recovery advisor "
                "and retry ceilings are asked"
            ),
            "phase9": "the ONE event bus and the ONE capability registry",
            "phase13": "every input is written to the shared automation audit trail",
            "phase14": "the governor decides whether a model may load; nothing here loads one",
            "phase22": "observations are ingested as world-model facts, provenance intact",
            "phase24": (
                "observe → plan → authorize → execute → verify → reassess "
                "is the planning cycle"
            ),
            "persistence": "the application's JsonStateStore, one key for every session",
        },
        # The honesty flags: each one is a thing this build does NOT do.
        "real_environment_shipped": False,
        "requires_gpu": False,
        "trains_anything": False,
        "downloads_models": False,
        "fabricates_object_identity": False,
        "acts_on_stale_state": False,
        "counts_unverified_as_success": False,
        "resends_timed_out_inputs": False,
        "retries_a_refusal": False,
        "completes_an_objective_without_a_fresh_reading": False,
        "sends_input_before_revalidation_after_restart": False,
        "bypasses_permissions": False,
        "drives_unallowlisted_environments": False,
        "stores_frames_or_prompts_in_the_trajectory": False,
        "deferred": list(DEFERRED_PHASES),
    }


__all__ = [
    "AGENT_ACTION_EXECUTED",
    "AGENT_ACTION_FAILED",
    "AGENT_ACTION_SELECTED",
    "AGENT_BLOCKED",
    "AGENT_OBSERVATION",
    "AGENT_OBJECTIVE_FAILED",
    "AGENT_OBJECTIVE_VERIFIED",
    "AGENT_RECOVERY",
    "AGENT_SESSION_CANCELLED",
    "AGENT_SESSION_ENDED",
    "AGENT_SESSION_PAUSED",
    "AGENT_SESSION_RESUMED",
    "AGENT_SESSION_STARTED",
    "CAPABILITY_SCHEMA_VERSION",
    "CAPABILITY_TABLE",
    "DEFAULT_MAX_ACTIONS",
    "DEFAULT_MAX_TRAJECTORY",
    "DEFAULT_REFRESH_ATTEMPTS",
    "DEFERRED_PHASES",
    "ENVIRONMENT_INPUT",
    "ENVIRONMENT_SCOPE",
    "EVENT_TYPES",
    "FACT_PREFIX",
    "FAULT_NAMES",
    "HARD_MAX_ACTIONS",
    "HARD_MAX_INPUT_MS",
    "HARD_MAX_SECONDS",
    "HARD_MAX_SESSIONS",
    "LIFECYCLE",
    "MAX_MOVE_STEPS",
    "PHASE",
    "RESUMABLE_STATUSES",
    "SCHEMA_VERSION",
    "SEMANTIC_LABEL_CLAIMS",
    "STORE_KEY",
    "ActionResult",
    "ActionKind",
    "ActionStatus",
    "EmbodiedAgent",
    "EmbodiedCapabilityDefinition",
    "EmbodiedManager",
    "EmbodiedSettingsView",
    "EntityRead",
    "EnvironmentAction",
    "EnvironmentAdapter",
    "EnvironmentCapability",
    "EnvironmentInfo",
    "EnvironmentKind",
    "EnvironmentRefused",
    "EnvironmentRegistry",
    "EnvironmentStatus",
    "Evaluation",
    "FactSet",
    "Faults",
    "GridWorldSimulation",
    "Observation",
    "ObservationBasis",
    "ObservationFreshness",
    "Objective",
    "ObjectiveInvalid",
    "ObjectiveVerdict",
    "ReleaseReport",
    "SessionState",
    "SessionStatus",
    "TacticalChoice",
    "TacticalMemory",
    "TrajectoryEntry",
    "adapter_for",
    "capability_rows",
    "capability_table_rows",
    "choose",
    "effect_observed",
    "facts_from_observation",
    "overview",
    "register_embodied_capabilities",
    "same_value",
    "scenarios",
    "tasks_for",
    "to_world_observation",
    "validate_objective",
]
