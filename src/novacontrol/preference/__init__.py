"""Phase 17: preference optimization (DPO / ORPO) for NovaControl.

This package turns the Phase 15 record of what NovaControl DID into *preference
pairs* — of these two observed behaviours for one prompt, prefer the one that led
to the better verified outcome — and optimises a model toward the preferred side.
It builds directly on Phase 16: the splitter, the checkpoint manager, the resource
estimator, the evaluation rules, the run record and the model registry are reused,
not rebuilt.

It does NOT implement RLHF, RLAIF, RLVR, critique learning or agentic RL. No
reward model is trained and no policy gradient is estimated; the two objectives
here consume PAIRS, and the preference figure a run produces is never allowed to
approve a model on its own — measured task behaviour is.

It does not train on hidden chain-of-thought: a pair holds observable outputs,
outcomes, corrections and structured evaluation readings, and a row carrying a
reasoning trace is refused rather than trimmed. It does not train anything
automatically: the default configuration is ``dry_run``, and a real run needs the
optional dependencies, the deployment's permission AND an explicit confirmation.

The pieces, in the order a preference-optimized model flows through them:

    PreferenceExample        one pair: prompt, chosen, rejected, and the evidence
    PreferenceStrength       confidence / evidence quality / verification, separately
    PreferenceQualityFilter  accepted, rejected, or held for a person
    PreferenceDatasetBuilder observed behaviour → versioned pairs
    PreferenceReviewQueue    the pairs a person settles, on the record
    PreferenceTrainer        the backend interface (dry-run, DPO, ORPO)
    PreferenceResourceEstimator  what a run costs, reference model included
    PreferenceEvaluator      base vs SFT vs candidate, with regressions
    PreferenceManager        the order those run in, on Phase 16's orchestrator
    PreferenceModule         the bus seam for read-only status
"""

from __future__ import annotations

from novacontrol.preference.backends import (
    OBJECTIVES,
    DPOTrainer,
    DryRunPreferenceTrainer,
    ORPOTrainer,
    PreferenceRunner,
    PreferenceTrainer,
    objective_for,
    preference_trainer_for,
)
from novacontrol.preference.config import (
    MAX_BETA,
    MIN_BETA,
    PreferenceTrainingConfig,
    resolve_sequence,
)
from novacontrol.preference.datasets import PreferenceDatasetBuilder
from novacontrol.preference.evaluation import (
    PREFERENCE_METRICS,
    REGRESSION_AREAS,
    PreferenceComparison,
    PreferenceEvaluator,
    PreferenceReading,
    RegressionReport,
    check_regressions,
)
from novacontrol.preference.manager import (
    ALGORITHMS,
    PREFERENCE_COMPARISON,
    PREFERENCE_DATASET_BUILT,
    PREFERENCE_DRY_RUN,
    PREFERENCE_REVIEW_DECIDED,
    PREFERENCE_REVIEW_QUEUED,
    PreferenceManager,
    PreferenceTrainerFactory,
)
from novacontrol.preference.models import (
    EVIDENCE_BENCHMARK_EXPECTATION,
    EVIDENCE_EVALUATION_DIMENSIONS,
    EVIDENCE_EXPLICIT_USER_PREFERENCE,
    EVIDENCE_HUMAN_REVIEW,
    EVIDENCE_SYNTHETIC_RULE,
    EVIDENCE_TEACHER_AGREEMENT,
    EVIDENCE_VERIFIED_SUCCESS,
    PAIR_SPLIT_NAMES,
    PREFERENCE_DATASET_SCHEMA_VERSION,
    PREFERENCE_PREPROCESSING_VERSION,
    PREFERENCE_SCHEMA_VERSION,
    SOURCE_STRENGTH,
    PreferenceAlgorithm,
    PreferenceDatasetType,
    PreferenceDatasetVersion,
    PreferenceEvidence,
    PreferenceExample,
    PreferenceQualityStatus,
    PreferenceReviewDecision,
    PreferenceRules,
    PreferenceSource,
    PreferenceStatistics,
    PreferenceStrength,
    next_preference_version,
    preference_dataset_version_id,
    preference_source_data_version,
)
from novacontrol.preference.quality import (
    CHECKS_RUN,
    DEFAULT_SEVERITIES,
    MAX_EVIDENCE_FLOOR,
    MAX_PREFERENCE_CONFIDENCE,
    PreferenceIssue,
    PreferenceQualityConfig,
    PreferenceQualityFilter,
    PreferenceQualityVerdict,
    PreferenceRuleCode,
)
from novacontrol.preference.resources import (
    PREFERENCE_PAIR_BYTES_PER_TOKEN,
    PreferenceResourceEstimator,
    preference_components,
)
from novacontrol.preference.review import (
    DECISIONS,
    PreferenceReviewQueue,
    ReviewItem,
)
from novacontrol.preference.runtime import PreferenceModule
from novacontrol.preference.storage import (
    PREFERENCE_DATASET_FILENAME,
    PREFERENCE_REVIEW_FILENAME,
    PreferenceDatasetRepository,
    PreferenceReviewRepository,
    build_preference_repositories,
)

__all__ = [
    "ALGORITHMS",
    "CHECKS_RUN",
    "DECISIONS",
    "DEFAULT_SEVERITIES",
    "DPOTrainer",
    "DryRunPreferenceTrainer",
    "EVIDENCE_BENCHMARK_EXPECTATION",
    "EVIDENCE_EVALUATION_DIMENSIONS",
    "EVIDENCE_EXPLICIT_USER_PREFERENCE",
    "EVIDENCE_HUMAN_REVIEW",
    "EVIDENCE_SYNTHETIC_RULE",
    "EVIDENCE_TEACHER_AGREEMENT",
    "EVIDENCE_VERIFIED_SUCCESS",
    "MAX_BETA",
    "MAX_EVIDENCE_FLOOR",
    "MAX_PREFERENCE_CONFIDENCE",
    "MIN_BETA",
    "OBJECTIVES",
    "ORPOTrainer",
    "PAIR_SPLIT_NAMES",
    "PREFERENCE_COMPARISON",
    "PREFERENCE_DATASET_BUILT",
    "PREFERENCE_DATASET_FILENAME",
    "PREFERENCE_DATASET_SCHEMA_VERSION",
    "PREFERENCE_DRY_RUN",
    "PREFERENCE_METRICS",
    "PREFERENCE_PAIR_BYTES_PER_TOKEN",
    "PREFERENCE_PREPROCESSING_VERSION",
    "PREFERENCE_REVIEW_DECIDED",
    "PREFERENCE_REVIEW_FILENAME",
    "PREFERENCE_REVIEW_QUEUED",
    "PREFERENCE_SCHEMA_VERSION",
    "REGRESSION_AREAS",
    "SOURCE_STRENGTH",
    "PreferenceAlgorithm",
    "PreferenceComparison",
    "PreferenceDatasetBuilder",
    "PreferenceDatasetRepository",
    "PreferenceDatasetType",
    "PreferenceDatasetVersion",
    "PreferenceEvaluator",
    "PreferenceEvidence",
    "PreferenceExample",
    "PreferenceIssue",
    "PreferenceManager",
    "PreferenceModule",
    "PreferenceQualityConfig",
    "PreferenceQualityFilter",
    "PreferenceQualityStatus",
    "PreferenceQualityVerdict",
    "PreferenceReading",
    "PreferenceResourceEstimator",
    "PreferenceReviewDecision",
    "PreferenceReviewQueue",
    "PreferenceReviewRepository",
    "PreferenceRuleCode",
    "PreferenceRules",
    "PreferenceRunner",
    "PreferenceSource",
    "PreferenceStatistics",
    "PreferenceStrength",
    "PreferenceTrainer",
    "PreferenceTrainerFactory",
    "PreferenceTrainingConfig",
    "RegressionReport",
    "ReviewItem",
    "build_preference_repositories",
    "check_regressions",
    "next_preference_version",
    "objective_for",
    "preference_components",
    "preference_dataset_version_id",
    "preference_source_data_version",
    "preference_trainer_for",
    "resolve_sequence",
]
