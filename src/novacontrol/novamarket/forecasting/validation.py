"""Chronological walk-forward validation (NM-06).

There is no random split anywhere: each origin only sees closes before it, the
test slice is strictly after the training slice, and origins step by one horizon
so the evaluation samples do not overlap. A model that cannot be scored on at
least ``min_validation_samples`` origins is not compared at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from novacontrol.novamarket.forecasting import estimators
from novacontrol.novamarket.forecasting.calibration import CalibrationSample
from novacontrol.novamarket.forecasting.metrics import (
    DirectionRow,
    ModelScore,
    brier_score,
    calibration_error,
    confusion_matrix,
    direction_of,
    directional_accuracy,
    interval_coverage,
    log_loss,
    mae,
    mean_interval_width,
    median_absolute_error,
    rmse,
)
from novacontrol.novamarket.forecasting.models import ForecastPolicy


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    horizon_sessions: int
    origins: int
    scores: dict[str, ModelScore]
    calibration: dict[str, tuple[CalibrationSample, ...]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "horizon_sessions": self.horizon_sessions,
            "origins": self.origins,
            "scores": {key: value.to_dict() for key, value in self.scores.items()},
        }


def walk_forward_origins(
    count: int, min_train: int, horizon: int, *, step: int | None = None
) -> list[tuple[int, int]]:
    """(origin, target_index) pairs: train is ``closes[:origin]``."""
    step = max(1, int(step or horizon))
    origins: list[tuple[int, int]] = []
    origin = int(min_train)
    while origin + horizon <= count:
        origins.append((origin, origin + horizon - 1))
        origin += step
    return origins


def evaluate_models(
    closes: Sequence[float],
    horizon: int,
    policy: ForecastPolicy,
    *,
    step: int | None = None,
    models: Sequence[str] = estimators.MODEL_IDS,
) -> WalkForwardResult:
    """Score every model on the same chronological origins."""
    origins = walk_forward_origins(
        len(closes), policy.min_training_bars, horizon, step=step
    )
    threshold = policy.directional_threshold_pct / 100.0
    scores: dict[str, ModelScore] = {}
    calibration: dict[str, tuple[CalibrationSample, ...]] = {}
    if len(origins) < policy.min_validation_samples:
        return WalkForwardResult(horizon, len(origins), scores, calibration)

    for model_id in models:
        point_rows: list[tuple[float, float]] = []
        direction_rows: list[DirectionRow] = []
        interval_rows: list[tuple[float, float, float]] = []
        calibration_rows: list[CalibrationSample] = []
        for origin, target in origins:
            actual = (closes[target] / closes[origin - 1]) - 1.0
            try:
                output = estimators.forecast(
                    model_id, closes[:origin], horizon, policy
                )
            except ValueError:
                continue
            point_rows.append((actual, output.predicted_return))
            if output.p_up is None or output.lower_return is None:
                continue
            assert output.p_down is not None
            assert output.p_sideways is not None
            assert output.upper_return is not None
            direction_rows.append(
                DirectionRow(
                    actual_return=actual,
                    p_up=output.p_up,
                    p_down=output.p_down,
                    p_sideways=output.p_sideways,
                )
            )
            interval_rows.append((actual, output.lower_return, output.upper_return))
            calibration_rows.append(
                CalibrationSample(
                    raw_probability=output.p_up,
                    outcome=direction_of(actual, threshold) == "up",
                )
            )
        if not point_rows:
            continue
        probabilistic = bool(direction_rows)
        scores[model_id] = ModelScore(
            model_id=model_id,
            samples=len(point_rows),
            mae=mae(point_rows),
            rmse=rmse(point_rows),
            median_absolute_error=median_absolute_error(point_rows),
            directional_accuracy=(
                directional_accuracy(direction_rows, threshold)
                if probabilistic
                else None
            ),
            brier=(
                brier_score(direction_rows, threshold) if probabilistic else None
            ),
            log_loss=log_loss(direction_rows, threshold) if probabilistic else None,
            interval_coverage=(
                interval_coverage(interval_rows) if probabilistic else None
            ),
            interval_width=(
                mean_interval_width(interval_rows) if probabilistic else None
            ),
            calibration_error=(
                calibration_error(
                    [(sample.raw_probability, sample.outcome) for sample in calibration_rows]
                )
                if probabilistic
                else None
            ),
            confusion=(
                confusion_matrix(direction_rows, threshold) if probabilistic else {}
            ),
            notes=(
                ()
                if probabilistic
                else ("point baseline: no distribution or probability is claimed",)
            ),
        )
        if probabilistic:
            calibration[model_id] = tuple(calibration_rows)
    return WalkForwardResult(horizon, len(origins), scores, calibration)


def best_model(
    result: WalkForwardResult, *, minimum_samples: int
) -> tuple[str | None, ModelScore | None]:
    """Lowest Brier score among probabilistic models, MAE as the tie-break."""
    eligible = [
        score
        for score in result.scores.values()
        if score.brier is not None and score.samples >= minimum_samples
    ]
    if not eligible:
        return None, None
    best = min(
        eligible,
        key=lambda score: (score.brier or 1e9, score.mae if score.mae is not None else 1e9),
    )
    return best.model_id, best


def baseline_score(result: WalkForwardResult) -> ModelScore | None:
    return result.scores.get("random_walk")


__all__ = [
    "WalkForwardResult",
    "baseline_score",
    "best_model",
    "evaluate_models",
    "walk_forward_origins",
]
