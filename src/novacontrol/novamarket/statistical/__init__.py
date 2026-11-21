"""PI-05 statistical baseline forecasting.

This package is intentionally additive.  It contains the fitting algorithms,
contracts and leakage-safe comparison harness; the application-facing service
persists through the existing PI-01 prediction store and NM-06 forecast store.
"""

from novacontrol.novamarket.statistical.arima import (
    ARIMAResult,
    adf_test,
    difference,
    fit_arima,
    forecast_arima,
)
from novacontrol.novamarket.statistical.estimators import (
    MODEL_IDS,
    MODEL_METADATA,
    forecast_model,
)
from novacontrol.novamarket.statistical.metrics import (
    PredictionRow,
    StatisticalMetrics,
    direction_label,
    evaluate_rows,
)
from novacontrol.novamarket.contracts import ForecastDataError
from novacontrol.novamarket.statistical.models import (
    PI05_PHASE,
    STATISTICAL_CONTRACT_VERSION,
    STATISTICAL_METHODOLOGY_VERSION,
    STATISTICAL_MODEL_VERSION,
    STATISTICAL_SCHEMA_VERSION,
    FitStatus,
    IntervalMethod,
    ModelConfig,
    ModelFit,
    ModelForecast,
    ModelKind,
    StatisticalForecast,
    StatisticalForecastRequest,
    StatisticalModelError,
    StatisticalValidationError,
    SuitabilityReport,
    UnsupportedHorizonError,
    classify_direction,
    config_candidates,
    decimal_price,
    normalise_model_id,
    price_from_return,
)
from novacontrol.novamarket.statistical.service import StatisticalForecastingService
from novacontrol.novamarket.statistical.validation import (
    EvaluationReport,
    default_resolution,
    evaluate_models,
    horizon_steps,
    walk_forward_origins,
)

__all__ = [
    "ARIMAResult",
    "EvaluationReport",
    "FitStatus",
    "ForecastDataError",
    "IntervalMethod",
    "MODEL_IDS",
    "MODEL_METADATA",
    "ModelConfig",
    "ModelFit",
    "ModelForecast",
    "ModelKind",
    "PI05_PHASE",
    "PredictionRow",
    "STATISTICAL_CONTRACT_VERSION",
    "STATISTICAL_METHODOLOGY_VERSION",
    "STATISTICAL_MODEL_VERSION",
    "STATISTICAL_SCHEMA_VERSION",
    "StatisticalForecast",
    "StatisticalForecastRequest",
    "StatisticalForecastingService",
    "StatisticalMetrics",
    "StatisticalModelError",
    "StatisticalValidationError",
    "SuitabilityReport",
    "UnsupportedHorizonError",
    "adf_test",
    "classify_direction",
    "config_candidates",
    "decimal_price",
    "default_resolution",
    "difference",
    "direction_label",
    "evaluate_models",
    "evaluate_rows",
    "fit_arima",
    "forecast_arima",
    "forecast_model",
    "horizon_steps",
    "normalise_model_id",
    "price_from_return",
    "walk_forward_origins",
]
