"""PI-02 Historical Market Dataset Engine.

A versioned, reproducible dataset layer over the existing NM-02 market-data
authority.  It acquires bounded historical ranges through the approved provider
pipeline, validates and classifies observations, stores exact membership
references in the existing SQLite database, and constructs leakage-safe
chronological training datasets for later prediction phases.
"""

from novacontrol.novamarket.datasets.acquisition import (
    DatasetAcquisition,
    acquisition_summary,
)
from novacontrol.novamarket.datasets.corporate import (
    AdjustedBar,
    AdjustedSeries,
    adjust_bars,
)
from novacontrol.novamarket.datasets.models import (
    DATASET_CONTRACT_VERSION,
    DATASET_NORMALIZATION_VERSION,
    MAX_INSTRUMENTS_PER_BUILD,
    PI02_PHASE,
    PI02_SCHEMA_VERSION,
    TRAINING_CONTRACT_VERSION,
    AdjustmentBasis,
    AnnotationKind,
    CorporateAction,
    CorporateActionKind,
    CoverageSummary,
    DatasetBuildRequest,
    DatasetError,
    DatasetLeakageError,
    DatasetMember,
    DatasetNotFoundError,
    DatasetQuality,
    DatasetSegment,
    DatasetStateError,
    DatasetValidationError,
    DatasetVersion,
    GapKind,
    GapRecord,
    MarketAnnotation,
    MissingSummary,
    SelectionRecord,
    SplitName,
    SplitSummary,
    TrainingDataset,
    TrainingSample,
    ValidationSummary,
    horizon_supports,
    interval_duration,
    membership_digest,
)
from novacontrol.novamarket.datasets.service import HistoricalDatasetService
from novacontrol.novamarket.datasets.store import DatasetStore
from novacontrol.novamarket.datasets.training import (
    build_training_dataset,
    verify_stored_training,
)
from novacontrol.novamarket.datasets.validation import (
    SegmentAssessment,
    assess_segment,
    classify_gaps,
    select_bars,
)

__all__ = [
    "AdjustedBar",
    "AdjustedSeries",
    "AdjustmentBasis",
    "AnnotationKind",
    "CorporateAction",
    "CorporateActionKind",
    "CoverageSummary",
    "DATASET_CONTRACT_VERSION",
    "DATASET_NORMALIZATION_VERSION",
    "DatasetAcquisition",
    "DatasetBuildRequest",
    "DatasetError",
    "DatasetLeakageError",
    "DatasetMember",
    "DatasetNotFoundError",
    "DatasetQuality",
    "DatasetSegment",
    "DatasetStateError",
    "DatasetStore",
    "DatasetValidationError",
    "DatasetVersion",
    "GapKind",
    "GapRecord",
    "HistoricalDatasetService",
    "MAX_INSTRUMENTS_PER_BUILD",
    "MarketAnnotation",
    "MissingSummary",
    "PI02_PHASE",
    "PI02_SCHEMA_VERSION",
    "SegmentAssessment",
    "SelectionRecord",
    "SplitName",
    "SplitSummary",
    "TRAINING_CONTRACT_VERSION",
    "TrainingDataset",
    "TrainingSample",
    "ValidationSummary",
    "acquisition_summary",
    "adjust_bars",
    "assess_segment",
    "build_training_dataset",
    "classify_gaps",
    "horizon_supports",
    "interval_duration",
    "membership_digest",
    "select_bars",
    "verify_stored_training",
]
