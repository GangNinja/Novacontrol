"""Phase 18: RLHF and RLAIF infrastructure — the loops, not the algorithms.

This package records what NovaControl DID (Phase 15's trajectories), what PEOPLE
said about it (feedback), what EVALUATORS said about it (ratings), turns both
into structured, auditable rewards, and builds the pipeline a future
policy-optimization algorithm would learn from:

    trajectory → feedback/ratings → reward → reward validation → policy
    optimizer → candidate model → evaluation → model registry

It deliberately does NOT implement PPO, GRPO, RLVR, critique-based learning or
agentic RL. It ships the PLUG (`PolicyOptimizer`), one deterministic mock that is
loudly not training, the full dry-run pipeline, and the safeguards — reward
integrity checks and disagreement detection — that make learning from feedback
safe to add later.

The pieces, in the order a run flows through them:

    HumanFeedback        what a person said, short and structured
    FeedbackQualityFilter  screens it, never deletes it
    Evaluator            rule / local / external / human judges, replaceable
    AIRating             what an evaluator said, criterion by criterion
    RewardProvider       human, AI, evaluation and composite rewards
    RewardIntegrityChecker  detects reward hacking before it becomes training data
    FeedbackDisagreementDetector  records where sources contradict, never picks
    RewardDatasetBuilder  assembles, audits and splits the rows
    PolicyOptimizer      the plug: mock today, PPO/GRPO later
    RLTrainer            the schedule: dry run, checkpoints, honest refusals
    RLPipeline           the plan a dry run shows
    RLHFManager          orchestration on Phase 16's rails
    RLHFModule           the bus seam

Everything is optional: normal NovaControl operation imports nothing that needs
an RL dependency, no run starts automatically, and no model is loaded by a dry
run.
"""

from __future__ import annotations

from novacontrol.rlhf.backends import (
    POLICY_OPTIMIZERS,
    DryRunRLTrainer,
    MockPolicyOptimizer,
    PolicyOptimizer,
    PolicyOptimizerUnavailable,
    RewardAudit,
    RLAIFTrainer,
    RLHFTrainer,
    RLRunner,
    RLTrainer,
    optimizer_for,
    policy_optimizer_for,
    rl_trainer_for,
)
from novacontrol.rlhf.config import (
    DEFAULT_FEEDBACK_VALUES,
    DEFAULT_PROVIDER_WEIGHTS,
    EVALUATORS,
    REWARD_PROVIDERS,
    RewardPolicyConfig,
    RLTrainingConfig,
)
from novacontrol.rlhf.datasets import (
    REASONING_KEYS,
    SPLIT_NAMES,
    RewardDatasetBuilder,
    RewardDatasetRequest,
    RewardDatasetRules,
    reward_dataset_fingerprint,
)
from novacontrol.rlhf.evaluation import (
    BASELINE_ROLES,
    CRITICAL_AREAS,
    RLModelEvaluator,
    regression_report,
)
from novacontrol.rlhf.evaluators import (
    DEFAULT_CRITERIA,
    HIDDEN_REASONING_KEYS,
    EvaluationRequest,
    Evaluator,
    EvaluatorUnavailable,
    ExternalAIEvaluator,
    HumanEvaluator,
    LocalAIEvaluator,
    NoEvaluator,
    RuleBasedEvaluator,
    evaluator_for,
    outcome_facts,
)
from novacontrol.rlhf.feedback import (
    FeedbackQualityConfig,
    FeedbackQualityFilter,
    ScreenedFeedback,
    feedback_evidence,
    feedback_reward_value,
)
from novacontrol.rlhf.integrity import (
    FeedbackDisagreementDetector,
    IntegrityContext,
    RewardIntegrityChecker,
)
from novacontrol.rlhf.manager import (
    ALGORITHMS,
    RLHF_COMPARISON,
    RLHF_DATASET_BUILT,
    RLHF_DISAGREEMENT_FOUND,
    RLHF_DRY_RUN,
    RLHF_FEEDBACK_DECIDED,
    RLHF_FEEDBACK_RECEIVED,
    RLHF_RATING_CREATED,
    RLHFManager,
    RLTrainerFactory,
)
from novacontrol.rlhf.models import (
    FEEDBACK_ACCEPTED,
    FEEDBACK_NEEDS_REVIEW,
    FEEDBACK_REJECTED,
    FEEDBACK_STATUSES,
    FEEDBACK_TYPES,
    IMPLEMENTED_ALGORITHMS,
    INTEGRITY_STATUSES,
    MODES,
    PLANNED_ALGORITHMS,
    POLICY_ALGORITHMS,
    RATING_MAX,
    RATING_MIN,
    REWARD_DATASET_SCHEMA_VERSION,
    REWARD_SOURCES,
    RLHF_SCHEMA_VERSION,
    RLHF_VERSION,
    ROLLOUT_SCHEMA_VERSION,
    AIRating,
    CriterionScore,
    Disagreement,
    DisagreementKind,
    FeedbackIssue,
    FeedbackStatus,
    FeedbackVerdict,
    HumanFeedback,
    HumanFeedbackType,
    PolicyAlgorithm,
    PolicyUpdate,
    RewardBreakdown,
    RewardDatasetStatistics,
    RewardDatasetVersion,
    RewardExample,
    RewardIntegrityCheck,
    RewardIntegrityFinding,
    RewardIntegrityStatus,
    RewardSource,
    RLMode,
    Rollout,
    RolloutStatus,
    RolloutStep,
)
from novacontrol.rlhf.pipeline import (
    PIPELINE_STAGES,
    PipelineStage,
    RLPipeline,
    RLPipelinePlan,
    optimizer_status,
)
from novacontrol.rlhf.resources import RLResourceEstimator
from novacontrol.rlhf.rewards import (
    AIRatingRewardProvider,
    CompositeRewardProvider,
    EvaluationRewardProvider,
    HumanRewardProvider,
    RewardProvider,
    RewardProviderUnavailable,
    RewardRequest,
    clip_reward,
    default_composite,
    normalize_reward,
    provider_for,
)
from novacontrol.rlhf.rollout import (
    CallablePolicy,
    Environment,
    MockEnvironment,
    Policy,
    RewardPropagator,
    RolloutRunner,
    RolloutSummary,
    ScriptedPolicy,
    single_step_rollout,
    summarise,
)
from novacontrol.rlhf.runtime import RLHFModule
from novacontrol.rlhf.storage import (
    DisagreementRepository,
    FeedbackRepository,
    RatingRepository,
    RewardDatasetRepository,
    RLHFRepositories,
    build_rlhf_repositories,
)

__all__ = [
    "AIRating",
    "AIRatingRewardProvider",
    "ALGORITHMS",
    "BASELINE_ROLES",
    "CRITICAL_AREAS",
    "CallablePolicy",
    "CompositeRewardProvider",
    "CriterionScore",
    "DEFAULT_CRITERIA",
    "DEFAULT_FEEDBACK_VALUES",
    "DEFAULT_PROVIDER_WEIGHTS",
    "Disagreement",
    "DisagreementKind",
    "DisagreementRepository",
    "DryRunRLTrainer",
    "EVALUATORS",
    "Environment",
    "EvaluationRequest",
    "EvaluationRewardProvider",
    "Evaluator",
    "EvaluatorUnavailable",
    "ExternalAIEvaluator",
    "FEEDBACK_ACCEPTED",
    "FEEDBACK_NEEDS_REVIEW",
    "FEEDBACK_REJECTED",
    "FEEDBACK_STATUSES",
    "FEEDBACK_TYPES",
    "FeedbackDisagreementDetector",
    "FeedbackIssue",
    "FeedbackQualityConfig",
    "FeedbackQualityFilter",
    "FeedbackRepository",
    "FeedbackStatus",
    "FeedbackVerdict",
    "HIDDEN_REASONING_KEYS",
    "HumanEvaluator",
    "HumanFeedback",
    "HumanFeedbackType",
    "HumanRewardProvider",
    "IMPLEMENTED_ALGORITHMS",
    "INTEGRITY_STATUSES",
    "IntegrityContext",
    "LocalAIEvaluator",
    "MODES",
    "MockEnvironment",
    "MockPolicyOptimizer",
    "NoEvaluator",
    "PIPELINE_STAGES",
    "PLANNED_ALGORITHMS",
    "POLICY_ALGORITHMS",
    "POLICY_OPTIMIZERS",
    "PipelineStage",
    "Policy",
    "PolicyAlgorithm",
    "PolicyOptimizer",
    "PolicyOptimizerUnavailable",
    "PolicyUpdate",
    "RATING_MAX",
    "RATING_MIN",
    "REASONING_KEYS",
    "REWARD_DATASET_SCHEMA_VERSION",
    "REWARD_PROVIDERS",
    "REWARD_SOURCES",
    "RLAIFTrainer",
    "RLHFManager",
    "RLHFModule",
    "RLHFRepositories",
    "RLHFTrainer",
    "RLHF_COMPARISON",
    "RLHF_DATASET_BUILT",
    "RLHF_DISAGREEMENT_FOUND",
    "RLHF_DRY_RUN",
    "RLHF_FEEDBACK_DECIDED",
    "RLHF_FEEDBACK_RECEIVED",
    "RLHF_RATING_CREATED",
    "RLHF_SCHEMA_VERSION",
    "RLHF_VERSION",
    "RLMode",
    "RLModelEvaluator",
    "RLPipeline",
    "RLPipelinePlan",
    "RLResourceEstimator",
    "RLRunner",
    "RLTrainer",
    "RLTrainerFactory",
    "RLTrainingConfig",
    "ROLLOUT_SCHEMA_VERSION",
    "RatingRepository",
    "RewardAudit",
    "RewardBreakdown",
    "RewardDatasetBuilder",
    "RewardDatasetRepository",
    "RewardDatasetRequest",
    "RewardDatasetRules",
    "RewardDatasetStatistics",
    "RewardDatasetVersion",
    "RewardExample",
    "RewardIntegrityCheck",
    "RewardIntegrityChecker",
    "RewardIntegrityFinding",
    "RewardIntegrityStatus",
    "RewardPolicyConfig",
    "RewardPropagator",
    "RewardProvider",
    "RewardProviderUnavailable",
    "RewardRequest",
    "RewardSource",
    "Rollout",
    "RolloutRunner",
    "RolloutStatus",
    "RolloutStep",
    "RolloutSummary",
    "RuleBasedEvaluator",
    "SPLIT_NAMES",
    "ScreenedFeedback",
    "ScriptedPolicy",
    "build_rlhf_repositories",
    "clip_reward",
    "default_composite",
    "evaluator_for",
    "feedback_evidence",
    "feedback_reward_value",
    "normalize_reward",
    "optimizer_for",
    "optimizer_status",
    "outcome_facts",
    "policy_optimizer_for",
    "provider_for",
    "regression_report",
    "reward_dataset_fingerprint",
    "rl_trainer_for",
    "single_step_rollout",
    "summarise",
]
