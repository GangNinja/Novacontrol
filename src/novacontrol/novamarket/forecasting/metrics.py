"""Forecast performance metrics (NM-06).

Numerical, directional and probabilistic metrics, each computed from explicit
input rows so no metric can quietly borrow information the forecaster did not
have. MAE/RMSE are in return units (fractions); MAPE is deliberately absent —
a return can be zero or negative, where percentage error is undefined.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Sequence

DIRECTIONS = ("up", "down", "sideways")


@dataclass(frozen=True, slots=True)
class DirectionRow:
    actual_return: float
    p_up: float
    p_down: float
    p_sideways: float


def direction_of(actual_return: float, threshold: float) -> str:
    """Direction under the same explicit thresholds the probabilities use."""
    if actual_return > threshold:
        return "up"
    if actual_return < -threshold:
        return "down"
    return "sideways"


def predicted_direction(p_up: float, p_down: float, p_sideways: float) -> str:
    best = max(p_up, p_down, p_sideways)
    if best == p_up:
        return "up"
    if best == p_down:
        return "down"
    return "sideways"


def mae(rows: Sequence[tuple[float, float]]) -> float | None:
    return (
        statistics.fmean(abs(actual - predicted) for actual, predicted in rows)
        if rows
        else None
    )


def rmse(rows: Sequence[tuple[float, float]]) -> float | None:
    if not rows:
        return None
    return math.sqrt(
        statistics.fmean((actual - predicted) ** 2 for actual, predicted in rows)
    )


def median_absolute_error(rows: Sequence[tuple[float, float]]) -> float | None:
    if not rows:
        return None
    return statistics.median(abs(actual - predicted) for actual, predicted in rows)


def pinball_loss(actual: float, predicted: float, quantile: float) -> float:
    """Quantile (pinball) loss for one prediction at one quantile."""
    error = actual - predicted
    return max(quantile * error, (quantile - 1.0) * error)


def directional_accuracy(rows: Sequence[DirectionRow], threshold: float) -> float | None:
    if not rows:
        return None
    hits = sum(
        1
        for row in rows
        if predicted_direction(row.p_up, row.p_down, row.p_sideways)
        == direction_of(row.actual_return, threshold)
    )
    return hits / len(rows)


def confusion_matrix(rows: Sequence[DirectionRow], threshold: float) -> dict[str, dict[str, int]]:
    matrix = {actual: {predicted: 0 for predicted in DIRECTIONS} for actual in DIRECTIONS}
    for row in rows:
        actual = direction_of(row.actual_return, threshold)
        predicted = predicted_direction(row.p_up, row.p_down, row.p_sideways)
        matrix[actual][predicted] += 1
    return matrix


def brier_score(rows: Sequence[DirectionRow], threshold: float) -> float | None:
    """Multi-class Brier score: mean squared distance to the one-hot outcome."""
    if not rows:
        return None
    total = 0.0
    for row in rows:
        actual = direction_of(row.actual_return, threshold)
        for name, probability in (
            ("up", row.p_up),
            ("down", row.p_down),
            ("sideways", row.p_sideways),
        ):
            total += (probability - (1.0 if name == actual else 0.0)) ** 2
    return total / len(rows)


def log_loss(rows: Sequence[DirectionRow], threshold: float, *, floor: float = 1e-12) -> float | None:
    if not rows:
        return None
    total = 0.0
    for row in rows:
        actual = direction_of(row.actual_return, threshold)
        probability = {
            "up": row.p_up,
            "down": row.p_down,
            "sideways": row.p_sideways,
        }[actual]
        total -= math.log(max(probability, floor))
    return total / len(rows)


def interval_coverage(rows: Sequence[tuple[float, float, float]]) -> float | None:
    """Share of actual returns inside their [lower, upper] prediction interval."""
    if not rows:
        return None
    inside = sum(1 for actual, low, high in rows if low <= actual <= high)
    return inside / len(rows)


def mean_interval_width(rows: Sequence[tuple[float, float, float]]) -> float | None:
    if not rows:
        return None
    return statistics.fmean(high - low for _actual, low, high in rows)


def calibration_error(samples: Sequence[tuple[float, bool]], *, bins: int = 10) -> float | None:
    """Expected calibration error over equal-width probability bins."""
    if not samples:
        return None
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for probability, outcome in samples:
        index = min(bins - 1, max(0, int(probability * bins)))
        buckets[index].append((probability, outcome))
    total = 0.0
    for bucket in buckets:
        if not bucket:
            continue
        confidence = statistics.fmean(probability for probability, _ in bucket)
        accuracy = sum(1 for _, outcome in bucket if outcome) / len(bucket)
        total += (len(bucket) / len(samples)) * abs(accuracy - confidence)
    return total


@dataclass(frozen=True, slots=True)
class ModelScore:
    model_id: str
    samples: int
    mae: float | None
    rmse: float | None
    median_absolute_error: float | None
    directional_accuracy: float | None
    brier: float | None
    log_loss: float | None
    interval_coverage: float | None
    interval_width: float | None
    calibration_error: float | None
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    #: Multi-class Brier of a uniform up/down/sideways forecast (2/3).
    reference_brier: float | None = None
    #: 1 - brier/reference_brier; positive only when better than uniform.
    brier_skill_score: float | None = None
    #: Mean absolute change in the point forecast between consecutive origins.
    mean_forecast_change: float | None = None

    def to_dict(self) -> dict[str, object]:
        def rounded(value: float | None) -> float | None:
            return None if value is None else round(value, 6)

        return {
            "model_id": self.model_id,
            "samples": self.samples,
            "mae": rounded(self.mae),
            "rmse": rounded(self.rmse),
            "median_absolute_error": rounded(self.median_absolute_error),
            "directional_accuracy": rounded(self.directional_accuracy),
            "brier": rounded(self.brier),
            "log_loss": rounded(self.log_loss),
            "interval_coverage": rounded(self.interval_coverage),
            "interval_width": rounded(self.interval_width),
            "calibration_error": rounded(self.calibration_error),
            "confusion": self.confusion,
            "notes": list(self.notes),
            "reference_brier": rounded(self.reference_brier),
            "brier_skill_score": rounded(self.brier_skill_score),
            "mean_forecast_change": rounded(self.mean_forecast_change),
        }


__all__ = [
    "DIRECTIONS",
    "DirectionRow",
    "ModelScore",
    "brier_score",
    "calibration_error",
    "confusion_matrix",
    "direction_of",
    "directional_accuracy",
    "interval_coverage",
    "log_loss",
    "mae",
    "mean_interval_width",
    "median_absolute_error",
    "pinball_loss",
    "predicted_direction",
    "rmse",
]
