"""NM-06 — measured probabilistic forecasting.

The package is a pipeline with one owner per stage:

``features``     cutoff-safe, reproducible features over NM-02 daily bars
``estimators``   deterministic predictive distributions (baselines + statistical)
``validation``   chronological walk-forward comparison, never a random split
``calibration``  isotonic calibration fitted on the earlier portion only
``metrics``      numerical, directional and probabilistic scores
``store``        immutable forecasts, mutable model state, outcomes, history
``engine``       generation, lifecycle, evaluation, training and reliability

Reachable through ``NovaMarketService`` (``/market/forecasts*``) and the
``novamarket.forecast.*`` events. Nothing here places an order, moves NovaCoin
or promises a price.
"""

from novacontrol.novamarket.forecasting.calibration import (
    CalibrationReport,
    CalibrationSample,
    IsotonicCalibrator,
    calibrate_probability,
)
from novacontrol.novamarket.forecasting.engine import ForecastEngine
from novacontrol.novamarket.forecasting.estimators import (
    MODEL_IDS,
    MODEL_METADATA,
    PROBABILISTIC_MODELS,
    ForecastOutput,
    forecast,
)
from novacontrol.novamarket.forecasting.features import (
    FeatureSet,
    PriceBar,
    classify_regime,
    compute_features,
)
from novacontrol.novamarket.forecasting.metrics import (
    ModelScore,
    brier_score,
    calibration_error,
    interval_coverage,
    log_loss,
    mae,
    rmse,
)
from novacontrol.novamarket.forecasting.models import (
    FEATURE_SET_VERSION,
    HORIZON_SESSIONS,
    METHODOLOGY_VERSION,
    MODEL_CATALOG_VERSION,
    MODEL_TRANSITIONS,
    ForecastOutcome,
    ForecastPolicy,
    ForecastRecord,
    ForecastStatus,
    Horizon,
    ModelRecord,
    ModelState,
    Regime,
    UncertaintyLevel,
    ensure_model_transition,
)
from novacontrol.novamarket.forecasting.store import SCHEMA_VERSION, ForecastStore
from novacontrol.novamarket.forecasting.validation import (
    WalkForwardResult,
    best_model,
    evaluate_models,
    walk_forward_origins,
)

__all__ = [
    "FEATURE_SET_VERSION",
    "HORIZON_SESSIONS",
    "METHODOLOGY_VERSION",
    "MODEL_CATALOG_VERSION",
    "MODEL_IDS",
    "MODEL_METADATA",
    "MODEL_TRANSITIONS",
    "PROBABILISTIC_MODELS",
    "SCHEMA_VERSION",
    "CalibrationReport",
    "CalibrationSample",
    "FeatureSet",
    "ForecastEngine",
    "ForecastOutcome",
    "ForecastOutput",
    "ForecastPolicy",
    "ForecastRecord",
    "ForecastStatus",
    "ForecastStore",
    "Horizon",
    "IsotonicCalibrator",
    "ModelRecord",
    "ModelScore",
    "ModelState",
    "PriceBar",
    "Regime",
    "UncertaintyLevel",
    "WalkForwardResult",
    "best_model",
    "brier_score",
    "calibrate_probability",
    "calibration_error",
    "classify_regime",
    "compute_features",
    "ensure_model_transition",
    "evaluate_models",
    "forecast",
    "interval_coverage",
    "log_loss",
    "mae",
    "rmse",
    "walk_forward_origins",
]
