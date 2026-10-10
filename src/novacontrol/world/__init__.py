"""World model, memory and state reasoning — Phase 22.

One sentence for what this package is: **it turns observations into a structured,
uncertainty-aware state of the environment that can be updated, remembered,
queried and reasoned about — and it says plainly when it cannot predict.**

It is built ON the layers before it rather than beside them. Phase 21's
``PerceptionResult`` and ``SceneRepresentation`` become normalized observations, and
Phase 21's own spatial arithmetic is what decides a geometric relation.
NovaControl's existing JSON state store is what persists this world, its existing
event bus carries the announcements, its existing capability registry holds the
capability table, and its existing resource governor stands in front of any
model-backed prediction. What is new here is everything the phase is about:

    observation      ->  normalization, duplicate and ordering checks
    state estimation ->  identity, attributes, absence, expiration
    world state      ->  entities, relationships, conditions, uncertainty
    transitions      ->  what changed between versions, with evidence
    temporal memory  ->  bounded snapshots and observation references
    change detection ->  differences above stated thresholds
    queries          ->  bounded structured reads, current and historical
    reasoning        ->  named deterministic rules, with limitations
    prediction       ->  a provider that may not exist, and says so

The honest positions are stated at the same volume as the features:

  * a fact that was not observed is never stored as an observed fact
  * a figure that was not measured is ``None``, never a zero
  * absence from a partial observation proves nothing
  * an ambiguous identity stays provisional rather than being asserted
  * a stale relation is kept, labelled, not deleted
  * a historical question is never answered with the current state
  * no predictive model ships; a rule projection is labelled as a rule
  * nothing here decides to act, and nothing here trains or downloads anything

``overview()`` is the machine-readable form of those statements, and the live
capability table is :func:`novacontrol.world.capabilities.capability_rows`.
"""

from novacontrol.world.agentic import (
    WORLD_OBSERVATION_KIND,
    attach_world_state,
    environment_state_for,
    observe_perception,
    world_observation,
)
from novacontrol.world.capabilities import (
    CAPABILITY_SCHEMA_VERSION,
    CAPABILITY_TABLE,
    WorldCapabilityDefinition,
    capability_rows,
    register_world_capabilities,
)
from novacontrol.world.changes import MAX_CHANGES, ChangeThresholds, detect_changes
from novacontrol.world.engine import (
    CONFLICT_DETECTED,
    ENTITY_STATE_CHANGED,
    EVENT_TYPES,
    OBSERVATION_INGESTED,
    PHASE,
    PREDICTION_COMPLETED,
    PREDICTION_FAILED,
    RELATIONSHIP_CHANGED,
    SCHEMA_VERSION,
    STATE_RESTORED,
    STATE_TRANSITION_CREATED,
    STATE_UPDATED,
    WorldModelEngine,
    WorldTelemetry,
)
from novacontrol.world.entities import (
    IdentityPolicy,
    active_entities,
    expire_stale,
    merge_entity,
    resolve_identity,
)
from novacontrol.world.estimation import EstimationOutcome, EstimationPolicy, estimate
from novacontrol.world.ingest import (
    ObservationRejected,
    entities_from_scene,
    normalize,
    observation_from_perception,
    scene_relationships,
    validate,
)
from novacontrol.world.memory import (
    TemporalMemory,
    WorldMemoryView,
    WorldRetentionPolicy,
)
from novacontrol.world.models import (
    AttributeChange,
    ChangeEvent,
    ChangeKind,
    EntityAttribute,
    EntityStatus,
    EvidenceReference,
    FactBasis,
    IngestStatus,
    Observation,
    ObservationSource,
    ObservedEntity,
    PredictionRequest,
    PredictionResult,
    PredictionStatus,
    QueryKind,
    QueryStatus,
    ReasoningConclusion,
    RelationshipKind,
    RelationshipSource,
    RelationStatus,
    RestoreReport,
    StateEstimate,
    StateQuery,
    StateQueryResult,
    TransitionKind,
    UncertaintyKind,
    UncertaintyRecord,
    UpdateReport,
    WorldEntity,
    WorldRelationship,
    WorldState,
    WorldStateTransition,
)
from novacontrol.world.prediction import (
    NoPredictionProvider,
    PredictionContext,
    PredictionProvider,
    PredictionService,
    RuleProjectionProvider,
)
from novacontrol.world.queries import run_query
from novacontrol.world.reasoning import REASONING_ACTIONS, reasoning_rules
from novacontrol.world.relationships import (
    RelationshipOutcome,
    derive_geometric_relations,
    relations_from_observation,
)
from novacontrol.world.store import (
    InMemoryWorldRepository,
    JsonWorldRepository,
    WorldRepository,
    sanitize_world_id,
)
from novacontrol.world.timeutil import is_newer, parse_timestamp, seconds_between
from novacontrol.world.transitions import (
    MAX_TRANSITIONS,
    between_states,
    for_entity,
    for_kind,
    summary,
)

#: What this phase deliberately does NOT implement, named so a reader never has to
#: infer the boundary from absence. Published by ``overview()["deferred"]``.
DEFERRED_PHASES: tuple[str, ...] = (
    "Phase 23: interactive learning and exploration environments",
    "Phase 24: planning, reasoning and action policy",
    "Phase 25: embodied and game agents",
    "Phase 26: generalization, ARC and intelligence evaluation",
)


def overview() -> dict[str, object]:
    """What this phase ships and what it refuses to claim — cheap and read-only.

    The flags are the machine-readable form of "no fake intelligence", so a report,
    a test or a dashboard can assert the phase's posture instead of trusting prose.
    """
    return {
        "phase": PHASE,
        "schema_version": SCHEMA_VERSION,
        "capability_schema_version": CAPABILITY_SCHEMA_VERSION,
        "capabilities": {row.capability_id: row.state.value for row in CAPABILITY_TABLE},
        "capability_details": [row.to_dict() for row in CAPABILITY_TABLE],
        "fact_bases": [member.value for member in FactBasis],
        "observation_sources": [member.value for member in ObservationSource],
        "ingest_statuses": [member.value for member in IngestStatus],
        "entity_statuses": [member.value for member in EntityStatus],
        "relationship_kinds": [member.value for member in RelationshipKind],
        "relation_statuses": [member.value for member in RelationStatus],
        "transition_kinds": [member.value for member in TransitionKind],
        "change_kinds": [member.value for member in ChangeKind],
        "uncertainty_kinds": [member.value for member in UncertaintyKind],
        "query_kinds": [member.value for member in QueryKind],
        "query_statuses": [member.value for member in QueryStatus],
        "prediction_statuses": [member.value for member in PredictionStatus],
        "reasoning_actions": list(REASONING_ACTIONS),
        "event_types": list(EVENT_TYPES),
        "prediction_providers": {
            "default": NoPredictionProvider.name,
            "rule_based": RuleProjectionProvider.name,
            "learned": "provider_dependent (no predictive model ships)",
        },
        "integration": {
            "perception": "Phase 21 PerceptionResult / SceneRepresentation -> Observation",
            "phase20": "WorldState -> AgentState observation and environment_state (adapter only)",
            "persistence": "the application's JsonStateStore, one key per world",
            "events": "the application's ONE event bus, through an injected seam",
            "capabilities": "the ONE CapabilityRegistry",
            "governor": "the SAME ResourceGovernor, asked before any model-backed prediction",
        },
        # The honesty flags: each one is a thing this build does NOT do.
        "cuda_required": False,
        "automatic_model_loading": False,
        "automatic_model_downloads": False,
        "stores_raw_frames": False,
        "stores_observation_content": False,
        "stores_hidden_reasoning": False,
        "exposes_chain_of_thought": False,
        "action_execution": False,
        "predicts_future_state": False,
        "predictive_model_available": False,
        "rule_projection_available": True,
        "trains_anything": False,
        "deferred": list(DEFERRED_PHASES),
    }


__all__ = [
    "CAPABILITY_SCHEMA_VERSION",
    "CAPABILITY_TABLE",
    "CONFLICT_DETECTED",
    "DEFERRED_PHASES",
    "ENTITY_STATE_CHANGED",
    "EVENT_TYPES",
    "MAX_CHANGES",
    "MAX_TRANSITIONS",
    "OBSERVATION_INGESTED",
    "PHASE",
    "PREDICTION_COMPLETED",
    "PREDICTION_FAILED",
    "REASONING_ACTIONS",
    "RELATIONSHIP_CHANGED",
    "SCHEMA_VERSION",
    "STATE_RESTORED",
    "STATE_TRANSITION_CREATED",
    "STATE_UPDATED",
    "WORLD_OBSERVATION_KIND",
    "AttributeChange",
    "ChangeEvent",
    "ChangeKind",
    "ChangeThresholds",
    "EntityAttribute",
    "EntityStatus",
    "EstimationOutcome",
    "EstimationPolicy",
    "EvidenceReference",
    "FactBasis",
    "IdentityPolicy",
    "InMemoryWorldRepository",
    "IngestStatus",
    "JsonWorldRepository",
    "NoPredictionProvider",
    "Observation",
    "ObservationRejected",
    "ObservationSource",
    "ObservedEntity",
    "PredictionContext",
    "PredictionProvider",
    "PredictionRequest",
    "PredictionResult",
    "PredictionService",
    "PredictionStatus",
    "QueryKind",
    "QueryStatus",
    "ReasoningConclusion",
    "RelationStatus",
    "RelationshipKind",
    "RelationshipOutcome",
    "RelationshipSource",
    "RestoreReport",
    "WorldRetentionPolicy",
    "RuleProjectionProvider",
    "StateEstimate",
    "StateQuery",
    "StateQueryResult",
    "TemporalMemory",
    "TransitionKind",
    "UncertaintyKind",
    "UncertaintyRecord",
    "UpdateReport",
    "WorldCapabilityDefinition",
    "WorldEntity",
    "WorldMemoryView",
    "WorldModelEngine",
    "WorldRelationship",
    "WorldRepository",
    "WorldState",
    "WorldStateTransition",
    "WorldTelemetry",
    "active_entities",
    "attach_world_state",
    "between_states",
    "capability_rows",
    "derive_geometric_relations",
    "detect_changes",
    "entities_from_scene",
    "environment_state_for",
    "estimate",
    "expire_stale",
    "for_entity",
    "for_kind",
    "is_newer",
    "merge_entity",
    "normalize",
    "observation_from_perception",
    "observe_perception",
    "overview",
    "parse_timestamp",
    "reasoning_rules",
    "register_world_capabilities",
    "relations_from_observation",
    "resolve_identity",
    "run_query",
    "sanitize_world_id",
    "scene_relationships",
    "seconds_between",
    "summary",
    "validate",
    "world_observation",
]
