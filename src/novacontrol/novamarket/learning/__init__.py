"""NM-13 controlled adaptive learning.

Evidence from NM-10, NM-11 and NM-12 is evaluated with its provenance intact;
bounded experiments propose candidate strategy configurations; approval,
optional shadow validation, promotion and rollback are explicit and versioned.
Nothing here executes a trade, moves a wallet or changes a risk limit.
"""

from novacontrol.novamarket.learning import experiments, monitor
from novacontrol.novamarket.learning.models import (
    AlertKind,
    AlertSeverity,
    CandidateStatus,
    EvidenceKind,
    EvidenceRecord,
    EvidenceStatus,
    ExperimentStatus,
    LearningAlert,
    LearningCandidate,
    LearningExperiment,
    PerformanceSnapshot,
    RollbackRecord,
    ShadowValidationStatus,
    StrategyVersionRecord,
    VersionStatus,
)
from novacontrol.novamarket.learning.service import (
    LearningError,
    LearningIsolationError,
    LearningNotFoundError,
    LearningService,
    LearningStateError,
    LearningValidationError,
)
from novacontrol.novamarket.learning.store import LearningStore

__all__ = [
    "AlertKind",
    "AlertSeverity",
    "CandidateStatus",
    "EvidenceKind",
    "EvidenceRecord",
    "EvidenceStatus",
    "ExperimentStatus",
    "LearningAlert",
    "LearningCandidate",
    "LearningError",
    "LearningExperiment",
    "LearningIsolationError",
    "LearningNotFoundError",
    "LearningService",
    "LearningStateError",
    "LearningStore",
    "LearningValidationError",
    "PerformanceSnapshot",
    "RollbackRecord",
    "ShadowValidationStatus",
    "StrategyVersionRecord",
    "VersionStatus",
    "experiments",
    "monitor",
]
