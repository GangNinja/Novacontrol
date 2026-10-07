"""Phase 19: RLVR (verifiable rewards) and critique-based learning.

Two capabilities, one idea: a reward a machine can CHECK is worth more than an
opinion, and a structured critique is how a failure becomes training data.

    model → action/output → verifier → objective result → verifiable reward → signal
    model → action/output → verifier/evaluator → structured critique
          → correction / preference / reward → signal

The package is optional and experimental by construction: nothing here is
imported by normal NovaControl operation, nothing starts training, no model is
loaded, and a dry run works with no training dependencies installed. Verifiers
are deterministic adapters over evidence the system already records (files,
processes, tests, HTTP, databases, git, outputs, schemas), and the reward,
critique, dataset and security policy are explicit, versioned configuration —
never hidden weights in application code. No reasoning trace is stored anywhere:
the models have no field for one.
"""

from novacontrol.rlvr.config import (
    RLVR_MODE,
    CritiqueConfig,
    RLVRTrainingConfig,
    VerifiableRewardConfig,
    VerifierPolicyConfig,
)
from novacontrol.rlvr.critique import CritiqueEngine, CritiqueSummary
from novacontrol.rlvr.datasets import (
    CorrectedExampleBuilder,
    CritiqueDatasetBuilder,
    CritiqueDatasetRequest,
    CritiqueDatasetRules,
)
from novacontrol.rlvr.evaluation import RLVREvaluation, RLVREvaluator
from novacontrol.rlvr.manager import RLVRManager
from novacontrol.rlvr.models import (
    CorrectedExample,
    CritiqueCategory,
    CritiqueDatasetVersion,
    CritiqueExample,
    CritiqueResult,
    CritiqueSeverity,
    CritiqueSource,
    VerificationRequest,
    VerificationResult,
    VerificationStatus,
    VerificationSummary,
    VerifierMetadata,
    critique_dataset_version_id,
    expected_fingerprint,
)
from novacontrol.rlvr.pipeline import (
    RLVR_PIPELINE_STAGES,
    RLVRPipeline,
    RLVRPipelinePlan,
    RLVRStage,
    greenlight,
)
from novacontrol.rlvr.registry import (
    CATEGORY_PRIORITY,
    SelectionDecision,
    VerifierRecord,
    VerifierRegistry,
    VerifierSelector,
)
from novacontrol.rlvr.rewards import (
    VerifiableRewardProvider,
    VerifiableRewardValidator,
    VerificationEvidence,
    critique_reward,
)
from novacontrol.rlvr.runtime import RLVRModule
from novacontrol.rlvr.security import (
    RewardTamperGuard,
    VerificationSecurityPolicy,
)
from novacontrol.rlvr.storage import (
    CorrectionRepository,
    CritiqueDatasetRepository,
    CritiqueRepository,
    RLVRRepositories,
    build_rlvr_repositories,
)
from novacontrol.rlvr.trainer import (
    CRITIQUE_METHODS,
    CorrectionProposal,
    CritiqueTrainingMethod,
    RLVRTrainer,
    VerifiedReward,
    rlvr_trainer_for,
)
from novacontrol.rlvr.verifiers import (
    CustomVerifier,
    DatabaseVerifier,
    FileVerifier,
    GitVerifier,
    HTTPVerifier,
    OutputVerifier,
    ProcessVerifier,
    SchemaVerifier,
    TestVerifier,
    Verifier,
    default_verifiers,
)

__all__ = [
    "CATEGORY_PRIORITY",
    "CRITIQUE_METHODS",
    "RLVR_MODE",
    "RLVR_PIPELINE_STAGES",
    "CorrectedExample",
    "CorrectedExampleBuilder",
    "CorrectionProposal",
    "CorrectionRepository",
    "CritiqueCategory",
    "CritiqueConfig",
    "CritiqueDatasetBuilder",
    "CritiqueDatasetRepository",
    "CritiqueDatasetRequest",
    "CritiqueDatasetRules",
    "CritiqueDatasetVersion",
    "CritiqueEngine",
    "CritiqueExample",
    "CritiqueRepository",
    "CritiqueResult",
    "CritiqueSeverity",
    "CritiqueSource",
    "CritiqueSummary",
    "CritiqueTrainingMethod",
    "CustomVerifier",
    "DatabaseVerifier",
    "FileVerifier",
    "GitVerifier",
    "HTTPVerifier",
    "OutputVerifier",
    "ProcessVerifier",
    "RLVREvaluation",
    "RLVREvaluator",
    "RLVRManager",
    "RLVRModule",
    "RLVRPipeline",
    "RLVRPipelinePlan",
    "RLVRRepositories",
    "RLVRStage",
    "RLVRTrainer",
    "RLVRTrainingConfig",
    "RewardTamperGuard",
    "SchemaVerifier",
    "SelectionDecision",
    "TestVerifier",
    "VerifiableRewardConfig",
    "VerifiableRewardProvider",
    "VerifiableRewardValidator",
    "VerificationEvidence",
    "VerificationRequest",
    "VerificationResult",
    "VerificationSecurityPolicy",
    "VerificationStatus",
    "VerificationSummary",
    "VerifiedReward",
    "Verifier",
    "VerifierMetadata",
    "VerifierPolicyConfig",
    "VerifierRecord",
    "VerifierRegistry",
    "VerifierSelector",
    "build_rlvr_repositories",
    "critique_dataset_version_id",
    "critique_reward",
    "default_verifiers",
    "expected_fingerprint",
    "greenlight",
    "rlvr_trainer_for",
]
