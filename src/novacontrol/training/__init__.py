"""Phase 16: supervised fine-tuning infrastructure for NovaControl.

This package turns the Phase 15 record of what NovaControl DID into supervised
training datasets, runs a trainer against them (a dry-run trainer by default,
because the target machine has no CUDA device and no training dependencies are
required), checkpoints as it goes, evaluates the result against the base model,
and registers the candidate as an EXPERIMENTAL model that only an explicit,
evidence-backed approval can promote.

It does NOT implement preference optimisation or reinforcement learning — no
DPO, ORPO, RLHF, RLAIF, RLVR or agentic RL. It does not train on hidden
reasoning: a training example is a structured input and a structured target, and
a row carrying a chain-of-thought key is refused. It does not train anything
automatically: the default configuration is ``dry_run``.

The pieces, in the order a trained model flows through them:

    SFTDatasetBuilder    accepted Phase 15 rows → versioned examples
    SplitConfig          deterministic, group-safe train/validation/test
    ResourceEstimator    what a configuration would cost on THIS machine
    SFTTrainer           the backend interface (dry-run, PEFT/LoRA adapter)
    CheckpointManager    atomic checkpoints, validation, resume, retention
    TrainingEvaluator    base model versus candidate, with regressions
    SFTModelRegistry     experimental → approved → production, explicitly
    TrainingManager      the order those run in, failure-isolated
    TrainingModule       the bus seam for read-only status
"""

from __future__ import annotations

from novacontrol.training.backends import (
    DryRunTrainer,
    PeftLoraBackend,
    PeftRunner,
    SFTTrainer,
    TrainingBackendUnavailable,
    TrainingCallbacks,
)
from novacontrol.training.checkpoints import (
    CHECKPOINT_COMPLETE,
    CHECKPOINT_CORRUPT,
    CHECKPOINT_INCOMPLETE,
    CheckpointManager,
)
from novacontrol.training.config import (
    PRECISIONS,
    TrainingConfig,
    TrainingConfigValidation,
)
from novacontrol.training.datasets import SFTDatasetBuilder, next_version
from novacontrol.training.evaluation import (
    REGRESSION_TOLERANCE,
    CallablePredictor,
    ModelPredictor,
    TrainingEvaluator,
)
from novacontrol.training.hardware import (
    TRAINING_DEPENDENCIES,
    BackendChoice,
    HardwareCapabilities,
    detect_hardware,
    resolve_backend,
)
from novacontrol.training.manager import TrainingManager
from novacontrol.training.models import (
    ALLOWED_STATUS_TRANSITIONS,
    DATASET_SCHEMA_VERSION,
    FORBIDDEN_REASONING_KEYS,
    PREPROCESSING_VERSION,
    SPLIT_NAMES,
    TRACKED_METRICS,
    CheckpointRecord,
    DatasetStatistics,
    DatasetType,
    Difficulty,
    HardwarePolicy,
    ModelStatus,
    ResourceEstimate,
    ResourceVerdict,
    SelectionRules,
    SFTDatasetVersion,
    SFTTrainingExample,
    SplitConfig,
    TrainingEvaluation,
    TrainingMethod,
    TrainingModelRecord,
    TrainingRun,
    TrainingRunStatus,
    dataset_version_id,
    reasoning_violations,
    source_data_version,
)
from novacontrol.training.registry import SFTModelRegistry
from novacontrol.training.resources import DatasetSizeHint, ResourceEstimator
from novacontrol.training.runtime import TrainingModule
from novacontrol.training.storage import (
    CheckpointRepository,
    DatasetVersionRepository,
    TrainingEvaluationRepository,
    TrainingModelRepository,
    TrainingRunRepository,
    build_training_repositories,
)

__all__ = [
    "ALLOWED_STATUS_TRANSITIONS",
    "CHECKPOINT_COMPLETE",
    "CHECKPOINT_CORRUPT",
    "CHECKPOINT_INCOMPLETE",
    "DATASET_SCHEMA_VERSION",
    "FORBIDDEN_REASONING_KEYS",
    "PREPROCESSING_VERSION",
    "PRECISIONS",
    "REGRESSION_TOLERANCE",
    "SPLIT_NAMES",
    "TRACKED_METRICS",
    "TRAINING_DEPENDENCIES",
    "BackendChoice",
    "CallablePredictor",
    "CheckpointManager",
    "CheckpointRecord",
    "CheckpointRepository",
    "DatasetSizeHint",
    "DatasetStatistics",
    "DatasetType",
    "DatasetVersionRepository",
    "Difficulty",
    "DryRunTrainer",
    "HardwareCapabilities",
    "HardwarePolicy",
    "ModelPredictor",
    "ModelStatus",
    "PeftLoraBackend",
    "PeftRunner",
    "ResourceEstimate",
    "ResourceEstimator",
    "ResourceVerdict",
    "SFTDatasetBuilder",
    "SFTDatasetVersion",
    "SFTModelRegistry",
    "SFTTrainer",
    "SFTTrainingExample",
    "SelectionRules",
    "SplitConfig",
    "TRACKED_METRICS",
    "TrainingBackendUnavailable",
    "TrainingCallbacks",
    "TrainingConfig",
    "TrainingConfigValidation",
    "TrainingEvaluation",
    "TrainingEvaluationRepository",
    "TrainingEvaluator",
    "TrainingManager",
    "TrainingMethod",
    "TrainingModelRecord",
    "TrainingModelRepository",
    "TrainingModule",
    "TrainingRun",
    "TrainingRunRepository",
    "TrainingRunStatus",
    "build_training_repositories",
    "dataset_version_id",
    "detect_hardware",
    "next_version",
    "reasoning_violations",
    "resolve_backend",
    "source_data_version",
]
