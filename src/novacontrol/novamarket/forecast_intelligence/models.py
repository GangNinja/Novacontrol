"""PI-23 forecast-intelligence contracts and bounded configuration.

The module deliberately contains no model implementation. It names the evidence,
status and policy vocabulary used by the optimization coordinator. A selection
decision is advisory: it never changes a production model, ensemble weight, risk
decision or paper order.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

PI23_PHASE = "PI-23"
PI23_VERSION = "1.0.0"
PI23_CONTRACT_VERSION = "forecast-intelligence.1.0"
PI23_SCHEMA_VERSION = 1
SELECTION_POLICY_VERSION = "pi23.selection-evidence.1.0"
CALIBRATION_VERSION = "pi23.calibration.1.0"
DRIFT_METHOD_VERSION = "pi23.drift.1.0"
RELIABILITY_POLICY_VERSION = "pi23.reliability.1.0"

RISK_AUTHORITY = "NM-08"
EXECUTION_AUTHORITY = "NM-10"
AUTOMATIC_PROMOTION = False
PRODUCTION_WEIGHT_MUTATION = False

HORIZONS = ("1m", "5m", "15m", "20m", "1h", "1d", "3d", "5d", "30d", "90d")
HORIZON_FAMILY = {
    "1m": "intraday",
    "5m": "intraday",
    "15m": "intraday",
    "20m": "intraday",
    "1h": "intraday",
    "1d": "swing",
    "3d": "swing",
    "5d": "swing",
    "30d": "long_term",
    "90d": "long_term",
}
HORIZON_DURATION = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "20m": timedelta(minutes=20),
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "5d": timedelta(days=5),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}
#: The general market-data refresh is 15 minutes. PI-23 never opens a faster
#: acquisition path; 5m/15m/20m forecasts are evaluated only when their source
#: records already exist.
GENERAL_REFRESH_FLOOR_SECONDS = 900

TASKS = (
    "price_regression",
    "return_regression",
    "direction_classification",
    "probability_estimation",
    "interval_estimation",
)
TASK_FAMILY_COMPATIBILITY = {
    "price_regression": {
        "naive",
        "moving_average",
        "exponential_smoothing",
        "arima",
        "xgboost",
        "lightgbm",
        "lstm",
        "ensemble",
    },
    "return_regression": {
        "naive",
        "moving_average",
        "exponential_smoothing",
        "arima",
        "xgboost",
        "lightgbm",
        "lstm",
        "ensemble",
    },
    "direction_classification": {"xgboost", "lightgbm", "lstm", "ensemble"},
    "probability_estimation": {"ensemble"},
    "interval_estimation": {
        "naive",
        "moving_average",
        "exponential_smoothing",
        "arima",
        "xgboost",
        "lightgbm",
        "lstm",
        "ensemble",
    },
}
BASELINE_FAMILIES = ("naive", "random_walk", "moving_average", "exponential_smoothing")
STATISTICAL_FAMILIES = (*BASELINE_FAMILIES, "arima")
ML_FAMILIES = ("xgboost", "lightgbm", "lstm")
ALL_FAMILIES = (*STATISTICAL_FAMILIES, "xgboost", "lightgbm", "lstm", "ensemble")

REGIMES = (
    "strong_uptrend",
    "weak_uptrend",
    "strong_downtrend",
    "weak_downtrend",
    "sideways",
    "high_volatility",
    "low_volatility",
    "potential_reversal",
    "breakout",
    "breakdown",
    "uncertain",
)

STATUS_COMPLETED = "completed"
STATUS_WARNING = "completed_with_warnings"
STATUS_INSUFFICIENT = "insufficient_data"
STATUS_STALE = "stale_data"
STATUS_MODEL_UNAVAILABLE = "model_unavailable"
STATUS_CALIBRATION_UNAVAILABLE = "calibration_unavailable"
STATUS_DRIFT_UNCONFIRMED = "drift_unconfirmed"
STATUS_FALLBACK = "fallback_selected"
STATUS_ABSTAINED = "abstained"
STATUS_FAILED = "evaluation_failed"
STATUSES = (
    STATUS_COMPLETED,
    STATUS_WARNING,
    STATUS_INSUFFICIENT,
    STATUS_STALE,
    STATUS_MODEL_UNAVAILABLE,
    STATUS_CALIBRATION_UNAVAILABLE,
    STATUS_DRIFT_UNCONFIRMED,
    STATUS_FALLBACK,
    STATUS_ABSTAINED,
    STATUS_FAILED,
)

SELECTED = "selected"
CONDITIONALLY_SELECTED = "conditionally_selected"
BASELINE_FALLBACK = "baseline_fallback"
INSUFFICIENT_EVIDENCE = "insufficient_evidence"
NO_ELIGIBLE_MODEL = "no_eligible_model"
SELECTION_OUTCOMES = (
    SELECTED,
    CONDITIONALLY_SELECTED,
    BASELINE_FALLBACK,
    INSUFFICIENT_EVIDENCE,
    NO_ELIGIBLE_MODEL,
)

STRONG_SUITABILITY = "strongly_suitable"
CONDITIONAL_SUITABILITY = "conditionally_suitable"
WEAK_SUITABILITY = "weakly_suitable"
INSUFFICIENT_SUITABILITY = "insufficiently_evaluated"

HIGH_RELIABILITY = "high_reliability"
MODERATE_RELIABILITY = "moderate_reliability"
LOW_RELIABILITY = "low_reliability"
UNRELIABLE = "unreliable"
INSUFFICIENT_RELIABILITY = "insufficient_evidence"

HEALTHY = "healthy"
WATCH = "watch"
DEGRADED = "degraded"
UNSUITABLE = "unsuitable"
INSUFFICIENT_HEALTH = "insufficient_evidence"

INCREASE_WEIGHT = "increase_weight"
DECREASE_WEIGHT = "decrease_weight"
RETAIN_WEIGHT = "retain_weight"
REMOVE_ELIGIBILITY = "remove_from_eligibility"
RESTORE_ELIGIBILITY = "restore_eligibility"
WEIGHT_INSUFFICIENT = "insufficient_evidence"

PRIMARY_MODEL = "primary_model"
ALTERNATIVE_MODEL = "alternative_model"
ENSEMBLE_FALLBACK = "ensemble_fallback"
BASELINE_FALLBACK_OUTCOME = "baseline_fallback"
ABSTAINED_OUTCOME = "abstained"
INSUFFICIENT_DATA_OUTCOME = "insufficient_data"

UNCERTAINTY_LOW = "low"
UNCERTAINTY_MODERATE = "moderate"
UNCERTAINTY_HIGH = "high"
UNCERTAINTY_VERY_HIGH = "very_high"
UNCERTAINTY_UNAVAILABLE = "unavailable"

AGREEMENT_STRONG = "strong_agreement"
AGREEMENT_MODERATE = "moderate_disagreement"
AGREEMENT_HIGH = "high_disagreement"
AGREEMENT_CRITICAL = "critical_disagreement"
AGREEMENT_INSUFFICIENT = "insufficient_model_coverage"

MODEL_STATES = (
    "experimental",
    "validated",
    "approved",
    "production",
    "deprecated",
    "rejected",
)


class ForecastIntelligenceError(Exception):
    """Base error with the HTTP status used by the API adapter."""

    http_status = 500
    code = "forecast_intelligence_error"

    def __init__(self, message: str, *, code: str = "", http_status: int = 0) -> None:
        super().__init__(message)
        self.message = str(message)
        if code:
            self.code = code
        if http_status:
            self.http_status = int(http_status)


class ForecastIntelligenceValidationError(ForecastIntelligenceError, ValueError):
    http_status = 422
    code = "forecast_intelligence_validation_error"


class ForecastIntelligenceNotFoundError(ForecastIntelligenceError, LookupError):
    http_status = 404
    code = "forecast_intelligence_not_found"


class ForecastIntelligenceUnavailableError(ForecastIntelligenceError):
    http_status = 503
    code = "forecast_intelligence_unavailable"


class ForecastIntelligenceStateError(ForecastIntelligenceError):
    http_status = 409
    code = "forecast_intelligence_state_error"


def horizon_family(horizon: str) -> str:
    value = str(horizon or "").strip()
    if value not in HORIZON_FAMILY:
        raise ForecastIntelligenceValidationError(
            f"unknown horizon {horizon!r}; known: {', '.join(HORIZONS)}"
        )
    return HORIZON_FAMILY[value]


def family_supports_task(family: str, task: str) -> bool:
    if task not in TASKS:
        raise ForecastIntelligenceValidationError(
            f"unknown forecasting task {task!r}; known: {', '.join(TASKS)}"
        )
    aliases = {"random_walk": "naive", "neutral": "sideways"}
    normalised = aliases.get(str(family or "").strip().lower(), str(family or "").strip().lower())
    return normalised in TASK_FAMILY_COMPATIBILITY[task]


def normalise_family(value: object) -> str:
    family = str(value or "").strip().lower()
    aliases = {
        "random-walk": "random_walk",
        "randomwalk": "random_walk",
        "moving-average": "moving_average",
        "exponential-smoothing": "exponential_smoothing",
        "light_gbm": "lightgbm",
        "xg_boost": "xgboost",
        "sequence": "lstm",
    }
    return aliases.get(family, family)


def valid_horizon(value: object) -> str:
    horizon = str(value or "").strip()
    if horizon not in HORIZON_FAMILY:
        raise ForecastIntelligenceValidationError(
            f"unknown horizon {value!r}; known: {', '.join(HORIZONS)}"
        )
    return horizon


def finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True, slots=True)
class ForecastIntelligenceConfig:
    """Bounded, documented thresholds for every PI-23 decision."""

    min_evaluation_samples: int = 20
    min_selection_samples: int = 30
    min_regime_samples: int = 12
    min_calibration_samples: int = 30
    min_drift_samples: int = 20
    min_stability_windows: int = 2
    max_models: int = 24
    max_records: int = 600
    max_evaluations: int = 8
    max_instruments: int = 20
    max_horizons: int = 9
    max_interval_width: float = 0.25
    calibration_bins: int = 10
    calibration_min_improvement: float = 0.005
    calibration_test_fraction: float = 0.30
    platt_iterations: int = 100
    platt_learning_rate: float = 0.20
    temperature_min: float = 0.50
    temperature_max: float = 3.00
    temperature_step: float = 0.05
    directional_threshold: float = 0.005
    drift_psi_threshold: float = 0.20
    drift_ks_p_threshold: float = 0.01
    drift_persistence_windows: int = 2
    drift_min_samples: int = 20
    degradation_mae_increase: float = 0.25
    degradation_accuracy_drop: float = 0.10
    degradation_min_windows: int = 2
    weight_min: float = 0.05
    weight_max: float = 0.60
    weight_step: float = 0.10
    weight_smoothing: float = 0.50
    min_weight_samples: int = 30
    reference_tolerance_seconds: int = 300
    stale_hours_intraday: float = 6.0
    stale_hours_swing: float = 72.0
    stale_hours_long_term: float = 336.0
    max_disagreement_return: float = 0.03
    critical_disagreement_return: float = 0.08
    max_evidence_age_days: int = 3650
    allow_test_set_selection: bool = False
    allow_automatic_promotion: bool = False
    allow_production_weight_mutation: bool = False

    def __post_init__(self) -> None:
        for name in (
            "min_evaluation_samples",
            "min_selection_samples",
            "min_regime_samples",
            "min_calibration_samples",
            "min_drift_samples",
            "min_stability_windows",
            "max_models",
            "max_records",
            "max_evaluations",
            "max_instruments",
            "max_horizons",
            "calibration_bins",
            "platt_iterations",
            "drift_persistence_windows",
            "drift_min_samples",
            "degradation_min_windows",
            "min_weight_samples",
            "reference_tolerance_seconds",
            "max_evidence_age_days",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ForecastIntelligenceValidationError(f"{name} must be an integer")
            if value < 1:
                raise ForecastIntelligenceValidationError(f"{name} must be positive")
        if self.min_selection_samples < self.min_evaluation_samples:
            raise ForecastIntelligenceValidationError(
                "min_selection_samples must cover min_evaluation_samples"
            )
        if not 2 <= self.calibration_bins <= 50:
            raise ForecastIntelligenceValidationError("calibration_bins must be 2..50")
        if not 0.05 <= self.calibration_test_fraction <= 0.50:
            raise ForecastIntelligenceValidationError(
                "calibration_test_fraction must be within 0.05..0.50"
            )
        if not 0.0 <= self.drift_ks_p_threshold <= 1.0:
            raise ForecastIntelligenceValidationError("drift_ks_p_threshold must be within [0, 1]")
        if not 0.0 < self.weight_min < self.weight_max <= 1.0:
            raise ForecastIntelligenceValidationError(
                "weight bounds must satisfy 0 < min < max <= 1"
            )
        if not 0.0 < self.weight_step <= 0.5:
            raise ForecastIntelligenceValidationError("weight_step must be within (0, 0.5]")
        if not 0.0 <= self.weight_smoothing <= 1.0:
            raise ForecastIntelligenceValidationError("weight_smoothing must be within [0, 1]")
        if self.allow_test_set_selection:
            raise ForecastIntelligenceValidationError("test-set model selection is forbidden")
        if self.allow_automatic_promotion:
            raise ForecastIntelligenceValidationError("automatic model promotion is forbidden")
        if self.allow_production_weight_mutation:
            raise ForecastIntelligenceValidationError("PI-23 weight changes must remain advisory")
        for name in (
            "max_interval_width",
            "degradation_mae_increase",
            "degradation_accuracy_drop",
            "max_disagreement_return",
            "critical_disagreement_return",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ForecastIntelligenceValidationError(f"{name} must be numeric")
            if not 0.0 < float(value) <= 1.0:
                raise ForecastIntelligenceValidationError(f"{name} must be within (0, 1]")
        if self.max_disagreement_return >= self.critical_disagreement_return:
            raise ForecastIntelligenceValidationError(
                "max_disagreement_return must be below critical_disagreement_return"
            )

    def stale_seconds(self, horizon: str) -> int:
        family = horizon_family(horizon)
        if family == "intraday":
            return int(self.stale_hours_intraday * 3600)
        if family == "swing":
            return int(self.stale_hours_swing * 3600)
        return int(self.stale_hours_long_term * 3600)

    def to_dict(self) -> dict[str, Any]:
        return {
            key: getattr(self, key) for key in self.__dataclass_fields__ if not key.startswith("_")
        }


@dataclass(frozen=True, slots=True)
class ForecastIntelligenceSources:
    """Read-only adapters over the existing prediction infrastructure.

    Every callback is supplied by the application service and reads an existing
    store. A missing callback is reported as unavailable evidence; it is never
    replaced with a synthetic metric.
    """

    models: Any = None
    training_runs: Any = None
    performance: Any = None
    forecasts: Any = None
    calibration: Any = None
    evaluations: Any = None
    evaluation: Any = None
    regime: Any = None
    ensemble_models: Any = None
    ensemble_calibration: Any = None
    health: Any = None
    clock: Any = None

    def to_dict(self) -> dict[str, Any]:
        names = (
            "models",
            "training_runs",
            "performance",
            "forecasts",
            "calibration",
            "evaluations",
            "evaluation",
            "regime",
            "ensemble_models",
            "ensemble_calibration",
            "health",
            "clock",
        )
        return {name: getattr(self, name) is not None for name in names}


@dataclass(frozen=True, slots=True)
class ModelEvidence:
    model_id: str
    version: str
    family: str
    instrument_id: str
    horizon: str
    selection_metrics: Mapping[str, Any] = field(default_factory=dict)
    performance_metrics: Mapping[str, Any] = field(default_factory=dict)
    classifier_validation: Mapping[str, Any] = field(default_factory=dict)
    classifier_test: Mapping[str, Any] = field(default_factory=dict)
    dataset_version: str = ""
    feature_set_version: str = ""
    training_dataset_version: str = ""
    resolution: str = ""
    evaluation_start: str = ""
    evaluation_end: str = ""
    validation_samples: int = 0
    test_samples: int = 0
    regimes: Mapping[str, int] = field(default_factory=dict)
    regime_metrics: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    artifact_ref: str = ""
    state: str = "eligible"
    calibration_status: str = "uncalibrated"
    calibration: Mapping[str, Any] = field(default_factory=dict)
    source: str = "training_run"
    as_of: str = ""
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "version": self.version,
            "family": self.family,
            "instrument_id": self.instrument_id,
            "horizon": self.horizon,
            "selection_metrics": dict(self.selection_metrics),
            "performance_metrics": dict(self.performance_metrics),
            "classifier_validation": dict(self.classifier_validation),
            "classifier_test": dict(self.classifier_test),
            "dataset_version": self.dataset_version,
            "feature_set_version": self.feature_set_version,
            "training_dataset_version": self.training_dataset_version,
            "resolution": self.resolution,
            "evaluation_start": self.evaluation_start,
            "evaluation_end": self.evaluation_end,
            "validation_samples": self.validation_samples,
            "test_samples": self.test_samples,
            "regimes": dict(self.regimes),
            "regime_metrics": {key: dict(value) for key, value in self.regime_metrics.items()},
            "artifact_ref": self.artifact_ref,
            "state": self.state,
            "calibration_status": self.calibration_status,
            "calibration": dict(self.calibration),
            "source": self.source,
            "as_of": self.as_of,
            "warnings": list(self.warnings),
        }


__all__ = [name for name in globals() if not name.startswith("_")]
