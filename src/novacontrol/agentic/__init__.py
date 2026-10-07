"""Phase 20 — Agentic Reinforcement Learning: the infrastructure, and only that.

NovaControl learns from complete multi-step tasks here, not from isolated
responses: an episode is a goal, a sequence of states, observations, decisions,
actions, outcomes, verifications and rewards — and the whole pipeline around it
(rollout, credit assignment, exploration, curriculum, evaluation, promotion
gates, resource governance, dry run) is built on top of Phases 15–19 rather than
beside them.

What this package IS:

  * OPTIONAL. Nothing imports it at application startup, and nothing in it starts
    by itself. A build with no interest in agentic RL is unaffected.
  * DETERMINISTIC where it must be. The environments are in-memory, read-only and
    seed-free reproducible; every optimizer that runs today is a mock that says
    so; the dry run exercises the whole pipeline without training anything.
  * REUSING. Phase 15's trajectory and reward records, Phase 16's trainer
    interface, run record, checkpoints and resource estimator, Phase 18's rollout
    and policy-update records, Phase 19's critique vocabulary, Phase 8's
    verification/recovery/permission layers, the event bus and the audit trail —
    all reused, none duplicated.
  * HONEST. A figure that was not measured is ``None``; a simulated optimizer is
    labelled simulated; a policy that has not been promoted is EXPERIMENTAL; and
    there is no field anywhere for a model's private chain of thought.

What this package is NOT: Phases 21–26. There is no real-time perception layer,
no world model, no interactive exploration framework, no embodied or game agent
and no ARC-style evaluation here. Those phases consume THIS infrastructure.

Normal NovaControl operation is untouched: with no learned policy enabled, the
existing DecisionEngine and Planner answer exactly as before.
"""

from novacontrol.agentic.actions import (
    MASK_CONFIRMATION_REQUIRED,
    MASK_INCOMPATIBLE,
    MASK_REASONS,
    MASK_UNAUTHORIZED,
    MASK_UNAVAILABLE,
    MASK_UNSAFE,
    ActionMask,
    ActionMasker,
    ActionSpace,
    MaskedAction,
    action_to_plan_step,
    candidate_tools,
    effect_for,
)
from novacontrol.agentic.config import (
    AGENTIC_ALGORITHMS,
    AGENTIC_CONFIG_VERSION,
    IMPLEMENTED_AGENTIC_ALGORITHMS,
    PLANNED_AGENTIC_ALGORITHMS,
    AgenticAlgorithm,
    AgenticConfigValidation,
    AgenticRewardWeights,
    AgenticRLConfig,
    CurriculumConfig,
    ExplorationConfig,
    PromotionThresholds,
)
from novacontrol.agentic.curriculum import (
    CURRICULUM_ACTIONS,
    CURRICULUM_ADVANCE,
    CURRICULUM_HOLD,
    CURRICULUM_REGRESS,
    DEFAULT_LEVEL_ENVIRONMENTS,
    CurriculumDecision,
    CurriculumManager,
    CurriculumStats,
)
from novacontrol.agentic.environments import (
    ENVIRONMENT_FACTORIES,
    ENVIRONMENT_LEVELS,
    AgenticEnvironment,
    ContextualEnvironment,
    EnvironmentStep,
    MultiStepEnvironment,
    PlanningEnvironment,
    RecoveryEnvironment,
    SimpleEnvironment,
    ToolSelectionEnvironment,
    build_environment,
    environment_names,
    environment_specs,
    environments_for_level,
)
from novacontrol.agentic.evaluation import (
    BASELINE_KINDS,
    BASELINE_UNAVAILABLE_REASON,
    RUNNABLE_BASELINES,
    ABPolicyEvaluator,
    ABResult,
    AgenticPolicyEvaluator,
    EvaluationConfig,
    EvaluationTask,
    ShadowComparison,
    ShadowPolicyEvaluator,
    baseline_table,
    compare_results,
    default_tasks,
    reward_improvement_is_not_evidence,
    summarise_episode,
)
from novacontrol.agentic.exploration import (
    STOP_BUDGET,
    STOP_COST,
    STOP_DISABLED,
    STOP_FAILURES,
    STOP_RATE,
    STOP_SAFETY,
    ExplorationBudget,
    ExplorationDecision,
    ExplorationPolicy,
)
from novacontrol.agentic.models import (
    AGENTIC_SCHEMA_VERSION,
    AGENTIC_VERSION,
    CANDIDATE_STATUSES,
    LIVE_STATUSES,
    MAX_CURRICULUM_LEVEL,
    MIN_CURRICULUM_LEVEL,
    REWARD_DIMENSIONS,
    SAFETY_DIMENSION,
    ActionType,
    AgentAction,
    AgenticEvaluationResult,
    AgentState,
    CreditAssignmentResult,
    CreditMethod,
    Episode,
    ExplorationStrategy,
    MultiObjectiveReward,
    PolicyDecision,
    PolicyFeedback,
    PolicyReasonCode,
    PolicyRecord,
    PolicyStatus,
    PromotionDecision,
    RewardDimension,
    StateTransition,
    StepPenalty,
    StepReward,
    StepSignal,
    TaskDifficulty,
    TaskDifficultyLevel,
    TerminationReason,
)
from novacontrol.agentic.policies import (
    POLICY_KINDS,
    AgentPolicy,
    LLMPolicyAdapter,
    MockPolicy,
    PolicyUnavailable,
    RuleBasedPolicy,
    build_policy,
    policy_descriptions,
)
from novacontrol.agentic.promotion import (
    PROMOTION_CHECKS,
    PolicyRegistry,
    PromotionGate,
    PromotionRefused,
    gate_report,
)
from novacontrol.agentic.rewards import (
    AgenticRewardEngine,
    CreditAssigner,
    StepContext,
    dimension_totals,
    episode_success,
    step_failed,
    terminal_step,
)
from novacontrol.agentic.rollout import (
    AgenticRolloutManager,
    RolloutLimits,
    RolloutOutcome,
    run_episode,
)
from novacontrol.agentic.trainer import (
    AGENTIC_OPTIMIZERS,
    DRY_RUN_STAGES,
    AgenticPolicyOptimizer,
    AgenticResourceEstimator,
    AgenticRLTrainer,
    AgenticTrainingSummary,
    DryRunReport,
    MockAgenticPolicyOptimizer,
    agentic_optimizer_for,
    agentic_trainer_for,
    checkpoint_digest,
    checkpoint_integrity_ok,
    checkpoint_payload,
    optimizer_descriptions,
    rollback_target,
    run_agentic_dry_run,
)

#: The phase this package implements.
PHASE = "phase20"

#: What this phase deliberately does NOT implement.
DEFERRED_PHASES: tuple[str, ...] = (
    "Phase 21: real-time perception and abstraction",
    "Phase 22: world model, memory and state reasoning",
    "Phase 23: interactive learning and exploration environments",
    "Phase 24: planning, reasoning and action policy",
    "Phase 25: embodied and game agents",
    "Phase 26: generalization, ARC and intelligence evaluation",
)


def overview() -> dict[str, object]:
    """What this phase ships, in the shape a status report uses.

    Read-only and cheap: it lists the machinery, the environments, the policies
    and the optimizers, and it states plainly that nothing here trains or loads
    anything by itself.
    """
    return {
        "phase": PHASE,
        "schema_version": AGENTIC_SCHEMA_VERSION,
        "version": AGENTIC_VERSION,
        "environments": list(environment_names()),
        "environment_levels": dict(ENVIRONMENT_LEVELS),
        "policies": list(POLICY_KINDS),
        "algorithms": {
            "implemented": list(IMPLEMENTED_AGENTIC_ALGORITHMS),
            "planned": list(PLANNED_AGENTIC_ALGORITHMS),
        },
        "reward_dimensions": list(REWARD_DIMENSIONS),
        "termination_reasons": [member.value for member in TerminationReason],
        "policy_statuses": [member.value for member in PolicyStatus],
        "dry_run_stages": list(DRY_RUN_STAGES),
        "cuda_required": False,
        "automatic_training": False,
        "automatic_model_loading": False,
        "stores_hidden_reasoning": False,
        "deferred": list(DEFERRED_PHASES),
    }


__all__ = [
    "ABPolicyEvaluator",
    "ABResult",
    "AGENTIC_ALGORITHMS",
    "AGENTIC_CONFIG_VERSION",
    "AGENTIC_OPTIMIZERS",
    "AGENTIC_SCHEMA_VERSION",
    "AGENTIC_VERSION",
    "ActionMask",
    "ActionMasker",
    "ActionSpace",
    "ActionType",
    "AgentAction",
    "AgentPolicy",
    "AgentState",
    "AgenticAlgorithm",
    "AgenticConfigValidation",
    "AgenticEnvironment",
    "AgenticEvaluationResult",
    "AgenticPolicyEvaluator",
    "AgenticPolicyOptimizer",
    "AgenticRLConfig",
    "AgenticRLTrainer",
    "AgenticResourceEstimator",
    "AgenticRewardEngine",
    "AgenticRewardWeights",
    "AgenticRolloutManager",
    "AgenticTrainingSummary",
    "BASELINE_KINDS",
    "BASELINE_UNAVAILABLE_REASON",
    "CANDIDATE_STATUSES",
    "CURRICULUM_ACTIONS",
    "CURRICULUM_ADVANCE",
    "CURRICULUM_HOLD",
    "CURRICULUM_REGRESS",
    "ContextualEnvironment",
    "CreditAssigner",
    "CreditAssignmentResult",
    "CreditMethod",
    "CurriculumConfig",
    "CurriculumDecision",
    "CurriculumManager",
    "CurriculumStats",
    "DEFAULT_LEVEL_ENVIRONMENTS",
    "DEFERRED_PHASES",
    "DRY_RUN_STAGES",
    "DryRunReport",
    "ENVIRONMENT_FACTORIES",
    "ENVIRONMENT_LEVELS",
    "EnvironmentStep",
    "Episode",
    "EvaluationConfig",
    "EvaluationTask",
    "ExplorationBudget",
    "ExplorationConfig",
    "ExplorationDecision",
    "ExplorationPolicy",
    "ExplorationStrategy",
    "IMPLEMENTED_AGENTIC_ALGORITHMS",
    "LIVE_STATUSES",
    "LLMPolicyAdapter",
    "MASK_CONFIRMATION_REQUIRED",
    "MASK_INCOMPATIBLE",
    "MASK_REASONS",
    "MASK_UNAVAILABLE",
    "MASK_UNAUTHORIZED",
    "MASK_UNSAFE",
    "MAX_CURRICULUM_LEVEL",
    "MIN_CURRICULUM_LEVEL",
    "MaskedAction",
    "MockAgenticPolicyOptimizer",
    "MockPolicy",
    "MultiObjectiveReward",
    "MultiStepEnvironment",
    "PHASE",
    "PLANNED_AGENTIC_ALGORITHMS",
    "POLICY_KINDS",
    "PROMOTION_CHECKS",
    "PlanningEnvironment",
    "PolicyDecision",
    "PolicyFeedback",
    "PolicyReasonCode",
    "PolicyRecord",
    "PolicyRegistry",
    "PolicyStatus",
    "PolicyUnavailable",
    "PromotionDecision",
    "PromotionGate",
    "PromotionRefused",
    "PromotionThresholds",
    "RUNNABLE_BASELINES",
    "RecoveryEnvironment",
    "RewardDimension",
    "RolloutLimits",
    "RolloutOutcome",
    "RuleBasedPolicy",
    "STOP_BUDGET",
    "STOP_COST",
    "STOP_DISABLED",
    "STOP_FAILURES",
    "STOP_RATE",
    "STOP_SAFETY",
    "SAFETY_DIMENSION",
    "ShadowComparison",
    "ShadowPolicyEvaluator",
    "SimpleEnvironment",
    "StateTransition",
    "StepContext",
    "StepPenalty",
    "StepReward",
    "StepSignal",
    "TaskDifficulty",
    "TaskDifficultyLevel",
    "TerminationReason",
    "ToolSelectionEnvironment",
    "action_to_plan_step",
    "agentic_optimizer_for",
    "agentic_trainer_for",
    "baseline_table",
    "build_environment",
    "build_policy",
    "candidate_tools",
    "checkpoint_digest",
    "checkpoint_integrity_ok",
    "checkpoint_payload",
    "compare_results",
    "default_tasks",
    "dimension_totals",
    "effect_for",
    "environment_names",
    "environment_specs",
    "environments_for_level",
    "episode_success",
    "gate_report",
    "optimizer_descriptions",
    "overview",
    "policy_descriptions",
    "reward_improvement_is_not_evidence",
    "rollback_target",
    "run_agentic_dry_run",
    "run_episode",
    "step_failed",
    "summarise_episode",
    "terminal_step",
]
