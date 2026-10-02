"""Phase 15: data collection, evaluation and the reward foundation.

This package records what NovaControl DID — as structured decisions, actions,
observations, verification and outcomes — scores it across nine dimensions, and
turns the scores into a weighted, fully explained reward. It does NOT train
anything: there is no fine-tuning, no preference optimisation and no
reinforcement learning anywhere in it. What it produces is the dataset those
future phases would consume, stored locally, filterable and versioned, together
with the rules by which it would be judged.

The pieces, in the order a finished task flows through them:

    TrajectoryRecorder   observes the existing EventBus lifecycle → AgentTrajectory
    DataQualityFilter    classifies it (ACCEPTED / REJECTED / NEEDS_REVIEW)
    EvaluationEngine     scores nine dimensions separately
    RewardEngine         turns them into a weighted total with its reasons
    repositories         store trajectories, evaluations, rewards, datasets
    EvaluationService    the order those run in
    EvaluationModule     the bus seam that attaches the recorder
"""

from __future__ import annotations

from novacontrol.evaluation.datasets import (
    BUILTIN_DATASET_ID,
    BUILTIN_DATASET_VERSION,
    GoldenDataset,
    GoldenExample,
    builtin_golden_dataset,
)
from novacontrol.evaluation.evaluator import (
    EVALUATOR_VERSION,
    DimensionScore,
    EvaluationConfig,
    EvaluationEngine,
    EvaluationIssue,
    EvaluationResult,
)
from novacontrol.evaluation.metrics import MetricsCalculator, percentile
from novacontrol.evaluation.models import (
    EVALUATION_SCHEMA_VERSION,
    GOLDEN_DATASET_SCHEMA_VERSION,
    REWARD_SCHEMA_VERSION,
    TRAJECTORY_SCHEMA_VERSION,
    AgentTrajectory,
    DimensionStatus,
    EvaluationDimension,
    ExecutionStep,
    LatencyMetrics,
    Observation,
    OverallStatus,
    QualityVerdictValue,
    RecoveryRecord,
    ResourceUsage,
    ToolCallRecord,
    TrajectoryStatus,
    UserFeedback,
    VerificationRecord,
)
from novacontrol.evaluation.quality import (
    DataQualityFilter,
    QualityConfig,
    QualityIssue,
    QualityRuleCode,
    QualityVerdict,
)
from novacontrol.evaluation.recorder import TRAJECTORY_EVENT_TYPES, TrajectoryRecorder
from novacontrol.evaluation.reward import (
    COMPONENT_NAMES,
    PENALTY_NAMES,
    REWARD_VERSION,
    RewardComponent,
    RewardConfig,
    RewardEngine,
    RewardPenalty,
    RewardResult,
)
from novacontrol.evaluation.runtime import EvaluationModule
from novacontrol.evaluation.service import EvaluationService
from novacontrol.evaluation.storage import (
    DEFAULT_REPOSITORY_CAP,
    DatasetRepository,
    EvaluationRepository,
    InMemoryRecordStore,
    JsonlRecordStore,
    RewardRepository,
    TrajectoryRepository,
    build_repositories,
)

__all__ = [
    "BUILTIN_DATASET_ID",
    "BUILTIN_DATASET_VERSION",
    "COMPONENT_NAMES",
    "DEFAULT_REPOSITORY_CAP",
    "EVALUATION_SCHEMA_VERSION",
    "EVALUATOR_VERSION",
    "GOLDEN_DATASET_SCHEMA_VERSION",
    "PENALTY_NAMES",
    "REWARD_SCHEMA_VERSION",
    "REWARD_VERSION",
    "TRAJECTORY_EVENT_TYPES",
    "TRAJECTORY_SCHEMA_VERSION",
    "AgentTrajectory",
    "DataQualityFilter",
    "DatasetRepository",
    "DimensionScore",
    "DimensionStatus",
    "EvaluationConfig",
    "EvaluationDimension",
    "EvaluationEngine",
    "EvaluationIssue",
    "EvaluationModule",
    "EvaluationRepository",
    "EvaluationResult",
    "EvaluationService",
    "ExecutionStep",
    "GoldenDataset",
    "GoldenExample",
    "InMemoryRecordStore",
    "JsonlRecordStore",
    "LatencyMetrics",
    "MetricsCalculator",
    "Observation",
    "OverallStatus",
    "QualityConfig",
    "QualityIssue",
    "QualityRuleCode",
    "QualityVerdict",
    "QualityVerdictValue",
    "RecoveryRecord",
    "ResourceUsage",
    "RewardComponent",
    "RewardConfig",
    "RewardEngine",
    "RewardPenalty",
    "RewardRepository",
    "RewardResult",
    "ToolCallRecord",
    "TrajectoryRecorder",
    "TrajectoryRepository",
    "TrajectoryStatus",
    "UserFeedback",
    "VerificationRecord",
    "build_repositories",
    "builtin_golden_dataset",
    "percentile",
]
