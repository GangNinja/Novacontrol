"""Split-conformal uncertainty calibration for the PI-08 ensemble.

The calibration quantile is fitted on validation-only residuals.  The final test
block is never used to fit the quantile; it is scored once for coverage and
width.  Finite-sample correction follows the split-conformal convention
``ceil((n + 1) * level) / n`` with the largest residual at the boundary.
"""

from __future__ import annotations

import math
import statistics
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from novacontrol.novamarket.prediction.models import (
    HORIZON_SPECS,
    ForecastHorizon,
    direction_for_return,
)
from novacontrol.novamarket.statistical.metrics import (
    PredictionRow,
    StatisticalMetrics,
    evaluate_rows,
)

from .models import (
    ENSEMBLE_CALIBRATION_PREFIX,
    ENSEMBLE_CALIBRATION_VERSION,
    ConformalCalibration,
    EnsembleConfig,
    EnsembleObservation,
    EnsembleValidationError,
    ensemble_id,
)


def conformal_quantile(residuals: Sequence[float], level: float) -> float:
    """Return the finite-sample split-conformal absolute residual quantile."""
    values = sorted(abs(float(value)) for value in residuals if math.isfinite(float(value)))
    if not values:
        raise EnsembleValidationError("calibration needs at least one finite residual")
    if not 0.05 <= level < 1.0:
        raise EnsembleValidationError("calibration level must be within [0.05, 1)")
    count = len(values)
    rank = min(count, max(1, int(math.ceil((count + 1) * level))))
    return float(values[rank - 1])


def fit_calibration(
    observations: Sequence[EnsembleObservation],
    *,
    instrument_id: str,
    horizon: ForecastHorizon,
    config: EnsembleConfig,
    dataset_version: str = "unspecified",
    created_at: datetime | None = None,
    minimum_half_width: float = 0.0,
) -> ConformalCalibration:
    """Fit a validation-only symmetric conformal interval."""
    # The caller passes the validation block explicitly; accepting it without
    # re-filtering keeps synthetic/imported validation rows usable while the
    # service still guarantees test rows never reach this function.
    validation = list(observations)
    if any(not item.validation for item in validation):
        raise EnsembleValidationError(
            "calibration accepts validation observations only; test rows must "
            "never be fitted"
        )
    if len(validation) < config.min_validation_samples:
        raise EnsembleValidationError(
            f"{len(validation)} validation observations; "
            f"{config.min_validation_samples} are required for calibration"
        )
    residuals = [item.actual_return - item.predicted_return for item in validation]
    quantile = conformal_quantile(residuals, config.interval_level)
    quantile = max(quantile, float(minimum_half_width))
    if not math.isfinite(quantile) or quantile < 0.0:
        raise EnsembleValidationError("calibration quantile is not finite")
    created = created_at or datetime.now(UTC)
    if created.tzinfo is None or created.utcoffset() is None:
        raise EnsembleValidationError("calibration created_at must be timezone-aware")
    calibration_id = ensemble_id(
        ENSEMBLE_CALIBRATION_PREFIX,
        instrument_id,
        horizon.value,
        config.digest,
        config.interval_level,
        created.isoformat(),
        dataset_version,
        len(validation),
    )
    return ConformalCalibration(
        calibration_id=calibration_id,
        instrument_id=instrument_id,
        horizon=horizon,
        level=config.interval_level,
        quantile=quantile,
        samples=len(validation),
        method=ENSEMBLE_CALIBRATION_VERSION,
        fitted_on="validation_only",
        dataset_version=dataset_version,
        config_digest=config.digest,
        created_at=created,
        minimum_half_width=float(minimum_half_width),
        status="provisional",
    )


def calibrated_interval(
    predicted_return: float,
    calibration: ConformalCalibration,
) -> tuple[float, float]:
    lower, upper = calibration.apply(predicted_return)
    return lower, upper


def _row(
    observation: EnsembleObservation,
    *,
    lower: float | None = None,
    upper: float | None = None,
    neutral_band_pct: float | None = None,
) -> PredictionRow:
    horizon = observation.horizon
    band = (
        neutral_band_pct
        if neutral_band_pct is not None
        else HORIZON_SPECS[horizon].neutral_band_pct
    )
    predicted_direction = (
        direction_for_return(observation.predicted_return, band).value
        if neutral_band_pct is not None
        else observation.predicted_direction
    )
    actual_direction = (
        direction_for_return(observation.actual_return, band).value
        if neutral_band_pct is not None
        else observation.actual_direction
    )
    return PredictionRow(
        actual_price=observation.actual_price,
        predicted_price=observation.predicted_price,
        actual_return=observation.actual_return,
        predicted_return=observation.predicted_return,
        lower_return=observation.lower_return if lower is None else lower,
        upper_return=observation.upper_return if upper is None else upper,
        actual_direction=actual_direction,
        predicted_direction=predicted_direction,
        regime=observation.regime,
        horizon=observation.horizon.value,
        instrument_id=observation.instrument_id,
        model_id=observation.model_id,
        interval_supported=True,
    )


def metrics_for(
    observations: Sequence[EnsembleObservation],
    *,
    calibration: ConformalCalibration | None = None,
    neutral_band_pct: float | None = None,
) -> StatisticalMetrics:
    if not observations:
        return StatisticalMetrics(samples=0, model_id="ensemble")
    rows: list[PredictionRow] = []
    for item in observations:
        if calibration is not None:
            lower, upper = calibrated_interval(item.predicted_return, calibration)
            rows.append(
                _row(
                    item,
                    lower=lower,
                    upper=upper,
                    neutral_band_pct=neutral_band_pct,
                )
            )
        else:
            rows.append(_row(item, neutral_band_pct=neutral_band_pct))
    return evaluate_rows(
        rows,
        model_id="ensemble",
        instrument_id=observations[0].instrument_id,
        horizon=observations[0].horizon.value,
    )


def coverage_by_regime(
    observations: Sequence[EnsembleObservation],
    *,
    calibration: ConformalCalibration | None = None,
    neutral_band_pct: float | None = None,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    groups: dict[str, list[EnsembleObservation]] = {}
    for item in observations:
        groups.setdefault(item.regime or "unclassified", []).append(item)
    for name, group in sorted(groups.items()):
        metrics[name] = {
            "samples": len(group),
            "metrics": metrics_for(
                group,
                calibration=calibration,
                neutral_band_pct=neutral_band_pct,
            ).to_dict(),
        }
    return metrics


def mean_calibration_error(
    observations: Sequence[EnsembleObservation],
    calibration: ConformalCalibration,
) -> float | None:
    """Difference between nominal and empirical coverage (signed)."""
    metrics = metrics_for(observations, calibration=calibration)
    if metrics.interval_coverage is None:
        return None
    return float(metrics.interval_coverage) - float(calibration.level)


__all__ = [
    "calibrated_interval",
    "conformal_quantile",
    "coverage_by_regime",
    "fit_calibration",
    "mean_calibration_error",
    "metrics_for",
]
