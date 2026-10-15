"""Interactive learning and exploration — Phase 23.

One sentence for what this package is: **it turns a bounded question into a bounded
experiment, compares what it expected with what it observed, and keeps the answer
only when the evidence supports it — and says so plainly when it cannot.**

The cycle it runs is the one the phase names, and each arrow is a recorded step:

    observe → identify uncertainty → form hypothesis → select experiment
    → check safety and permissions → execute bounded action → observe result
    → compare prediction with actual result → update knowledge
    → evaluate learning → select next experiment

It is built ON the layers before it rather than beside them. Phase 22's world state is
read through a read-only environment adapter (never written), Phase 21's perception is
what feeds that state, NovaControl's existing JSON state store persists the objective
and its evidence, its existing event bus carries the announcements, its existing
capability registry holds the capability table, and its existing permission, risk and
resource layers are what stand in front of any real action.

The honest positions are stated at the same volume as the features:

  * a hypothesis is never invented — candidate explanations come from the environment's
    declared space
  * a prediction is never fabricated — with no rule and no model the run is recorded as
    prediction-unavailable, and it is not scored for agreement
  * one successful experiment never establishes a universal rule; a rule is scoped,
    versioned, thresholded, and contradicted evidence is kept rather than dropped
  * repeated observations from one source are not counted as independent evidence
  * a blocked, rejected, timed-out or unexecuted experiment is never a success
  * an interrupted experiment is never completed by a restart
  * the shipped environments are simulators: their results are evidence about the
    simulator, and that is reported on every report and metric
  * nothing here trains a model, downloads a model, or acts outside a registered
    environment

``overview()`` is the machine-readable form of those statements; the live per-machine
report is :meth:`novacontrol.interactive.manager.InteractiveLearningManager.status`.
"""

from novacontrol.interactive.comparison import (
    compare,
    evidence_for,
    evidence_role,
    match_hypotheses,
    match_one,
)
from novacontrol.interactive.curiosity import (
    CuriosityWeights,
    curiosity_score,
)
from novacontrol.interactive.environments import (
    BridgedEnvironment,
    DeterministicEnvironment,
    Environment,
    EnvironmentRegistry,
    WorldStateEnvironment,
    builtin_environments,
    environment_from_spec,
)
from novacontrol.interactive.experiments import (
    DispositionPolicy,
    ExperimentLedger,
    candidate_experiments,
    experiment_signature,
    validate_experiment,
)
from novacontrol.interactive.governance import (
    BudgetTracker,
    LearningPolicy,
    SafetyGate,
    budget_from_mapping,
    cost_of,
)
from novacontrol.interactive.hypotheses import (
    ConfidencePolicy,
    HypothesisBook,
    candidate_hypotheses,
    predictions_differ,
)
from novacontrol.interactive.information import (
    InformationWeights,
    discrimination_ratio,
    information_value,
)
from novacontrol.interactive.knowledge import (
    DEFAULT_CONTRADICTION_THRESHOLD,
    DEFAULT_SUPPORT_THRESHOLD,
    RuleBook,
    confidence_of,
    support_ratio,
)
from novacontrol.interactive.manager import (
    REPLICATE_PARAMETER,
    TERMINAL_OBJECTIVE_STATUSES,
    InteractiveLearningManager,
    knowledge_changes,
    usage_of,
)
from novacontrol.interactive.metrics import (
    LearningEvaluator,
    LearningMetrics,
    StrategyComparison,
    StrategyOutcome,
    TransferResult,
    compare_strategies,
    transfer_result,
)
from novacontrol.interactive.models import (
    MAX_EVIDENCE_PER_HYPOTHESIS,
    MAX_EXPERIMENTS_PER_OBJECTIVE,
    MAX_HYPOTHESES_PER_OBJECTIVE,
    MAX_RULES,
    MAX_STATEMENT,
    PHASE,
    SCHEMA_VERSION,
    ActionResult,
    ActionSpec,
    BudgetUsage,
    BudgetVerdict,
    CandidateExperiment,
    ComparisonResult,
    ConfidenceKind,
    CuriosityScore,
    EnvironmentKind,
    EnvironmentObservation,
    EvidenceKind,
    EvidenceReference,
    EvidenceRole,
    ExperimentRun,
    ExperimentSpec,
    ExperimentStatus,
    Hypothesis,
    HypothesisStatus,
    HypothesisVersion,
    InformationValue,
    KnowledgeChange,
    KnowledgeUpdate,
    LearnedRule,
    LearningBudget,
    LearningCycleReport,
    LearningCycleStep,
    LearningObjective,
    MatchingPolicy,
    ObjectiveStatus,
    OutcomeKind,
    Prediction,
    PredictionBasis,
    RuleStatus,
    SafetyDecision,
    StepKind,
)
from novacontrol.interactive.prediction import (
    EnvironmentRuleProvider,
    LearnedRuleProvider,
    NoPredictionProvider,
    PredictionProvider,
    PredictionService,
    WorldModelProvider,
)
from novacontrol.interactive.selection import (
    BoundedRandomStrategy,
    ExperimentSelector,
    InformationValueStrategy,
    SelectionContext,
    SelectionOutcome,
    SelectionStrategy,
    UncertaintyFirstStrategy,
    available_strategies,
    strategy_by_name,
)
from novacontrol.interactive.store import (
    InMemoryLearningRepository,
    JsonLearningRepository,
    LearningRepository,
    sanitize_key,
)

#: What this phase deliberately does NOT implement, named so a reader never has to
#: infer the boundary from absence. Published by ``overview()["deferred"]``.
DEFERRED_PHASES: tuple[str, ...] = (
    "Phase 24: planning, reasoning and action policy",
    "Phase 25: embodied and game agents",
    "Phase 26: generalization, ARC and intelligence evaluation",
)

#: The lifecycle tables, so a report, a test or a dashboard reads the same words the
#: implementation uses instead of restating them.
LIFECYCLE: dict[str, list[str]] = {
    "objective": [member.value for member in ObjectiveStatus],
    "hypothesis": [member.value for member in HypothesisStatus],
    "experiment": [member.value for member in ExperimentStatus],
    "outcome": [member.value for member in OutcomeKind],
    "prediction_basis": [member.value for member in PredictionBasis],
    "confidence_kind": [member.value for member in ConfidenceKind],
    "evidence_role": [member.value for member in EvidenceRole],
    "evidence_kind": [member.value for member in EvidenceKind],
    "knowledge_change": [member.value for member in KnowledgeChange],
    "rule_status": [member.value for member in RuleStatus],
    "environment_kind": [member.value for member in EnvironmentKind],
    "step": [member.value for member in StepKind],
}

#: The event vocabulary this package publishes on the application's ONE bus.
EVENT_TYPES: tuple[str, ...] = (
    "learning.objective_created",
    "learning.hypothesis_proposed",
    "learning.hypothesis_updated",
    "learning.experiment_proposed",
    "learning.experiment_approved",
    "learning.experiment_rejected",
    "learning.experiment_started",
    "learning.observation_collected",
    "learning.prediction_evaluated",
    "learning.knowledge_updated",
    "learning.experiment_completed",
    "learning.experiment_failed",
    "learning.experiment_interrupted",
    "learning.experiment_cancelled",
    "learning.budget_exhausted",
)


def overview() -> dict[str, object]:
    """What this phase ships and what it refuses to claim — cheap and read-only."""
    return {
        "phase": PHASE,
        "schema_version": SCHEMA_VERSION,
        "cycle": [
            "observe",
            "identify_uncertainty",
            "form_hypothesis",
            "select_experiment",
            "check_safety_and_permissions",
            "execute_bounded_action",
            "observe_result",
            "compare_prediction_with_actual",
            "update_knowledge",
            "evaluate_learning",
            "select_next_experiment",
        ],
        "lifecycle": {key: list(value) for key, value in LIFECYCLE.items()},
        "strategies": list(available_strategies()),
        "event_types": list(EVENT_TYPES),
        "selection": {
            "default": "uncertainty_first",
            "algorithm": (
                "deterministic ordering by a documented heuristic score; information value "
                "is a ranking hint, never a statistical claim"
            ),
            "random_seed_reproducible": True,
        },
        "prediction_providers": {
            "environment_rule": "when the environment declares the transition (rule-based)",
            "learned_rule": "rules this build learned from evidence (provisional)",
            "world_model": "provider_dependent; Phase 22's boundary, available only when wired",
            "none": "the honest default: prediction unavailable, and the run is not scored",
        },
        "integration": {
            "phase21": "perception feeds world state; this package reads it, never writes it",
            "phase22": "read-only world environment; no world state is mutated by a hypothesis",
            "phase15": "trajectory-ready reports; no second recorder is created here",
            "phase18": "curiosity is one documented heuristic, not a second reward system",
            "persistence": "the application's JsonStateStore, one key per objective",
            "events": "the application's ONE event bus, through an injected seam",
            "capabilities": "the ONE CapabilityRegistry",
            "permissions": "the existing permission/risk layer, asked before every real action",
            "governor": "the SAME ResourceGovernor; nothing here loads a model on its own",
        },
        # The honesty flags: each one is a thing this build does NOT do.
        "cuda_required": False,
        "automatic_model_loading": False,
        "automatic_model_downloads": False,
        "trains_anything": False,
        "changes_model_weights": False,
        "fabricates_predictions": False,
        "fabricates_confidence": False,
        "counts_repeats_as_independent_evidence": False,
        "discards_contradictory_evidence": False,
        "marks_unexecuted_experiments_successful": False,
        "completes_interrupted_experiments": False,
        "rewards_action_count": False,
        "bypasses_permissions": False,
        "exports_chain_of_thought": False,
        "real_world_environments_shipped": False,
        "simulator_results_are_real_world_evidence": False,
        "deferred": list(DEFERRED_PHASES),
    }


__all__ = [
    "DEFAULT_CONTRADICTION_THRESHOLD",
    "DEFAULT_SUPPORT_THRESHOLD",
    "DEFERRED_PHASES",
    "EVENT_TYPES",
    "LIFECYCLE",
    "MAX_EVIDENCE_PER_HYPOTHESIS",
    "MAX_EXPERIMENTS_PER_OBJECTIVE",
    "MAX_HYPOTHESES_PER_OBJECTIVE",
    "MAX_RULES",
    "MAX_STATEMENT",
    "PHASE",
    "REPLICATE_PARAMETER",
    "SCHEMA_VERSION",
    "TERMINAL_OBJECTIVE_STATUSES",
    "ActionResult",
    "ActionSpec",
    "BoundedRandomStrategy",
    "BridgedEnvironment",
    "BudgetTracker",
    "BudgetUsage",
    "BudgetVerdict",
    "CandidateExperiment",
    "ComparisonResult",
    "ConfidenceKind",
    "ConfidencePolicy",
    "CuriosityScore",
    "CuriosityWeights",
    "DeterministicEnvironment",
    "DispositionPolicy",
    "Environment",
    "EnvironmentKind",
    "EnvironmentObservation",
    "EnvironmentRegistry",
    "EnvironmentRuleProvider",
    "EvidenceKind",
    "EvidenceReference",
    "EvidenceRole",
    "ExperimentLedger",
    "ExperimentRun",
    "ExperimentSelector",
    "ExperimentSpec",
    "ExperimentStatus",
    "Hypothesis",
    "HypothesisBook",
    "HypothesisStatus",
    "HypothesisVersion",
    "InMemoryLearningRepository",
    "InformationValue",
    "InformationValueStrategy",
    "InformationWeights",
    "InteractiveLearningManager",
    "JsonLearningRepository",
    "KnowledgeChange",
    "KnowledgeUpdate",
    "LearnedRule",
    "LearnedRuleProvider",
    "LearningBudget",
    "LearningCycleReport",
    "LearningCycleStep",
    "LearningEvaluator",
    "LearningMetrics",
    "LearningObjective",
    "LearningPolicy",
    "LearningRepository",
    "MatchingPolicy",
    "NoPredictionProvider",
    "ObjectiveStatus",
    "OutcomeKind",
    "Prediction",
    "PredictionBasis",
    "PredictionProvider",
    "PredictionService",
    "RuleBook",
    "RuleStatus",
    "SafetyDecision",
    "SafetyGate",
    "SelectionContext",
    "SelectionOutcome",
    "SelectionStrategy",
    "StepKind",
    "StrategyComparison",
    "StrategyOutcome",
    "TransferResult",
    "UncertaintyFirstStrategy",
    "WorldModelProvider",
    "WorldStateEnvironment",
    "available_strategies",
    "budget_from_mapping",
    "builtin_environments",
    "candidate_experiments",
    "candidate_hypotheses",
    "compare",
    "compare_strategies",
    "confidence_of",
    "cost_of",
    "curiosity_score",
    "discrimination_ratio",
    "environment_from_spec",
    "evidence_for",
    "evidence_role",
    "experiment_signature",
    "information_value",
    "knowledge_changes",
    "match_hypotheses",
    "match_one",
    "overview",
    "predictions_differ",
    "sanitize_key",
    "strategy_by_name",
    "support_ratio",
    "transfer_result",
    "usage_of",
    "validate_experiment",
]
