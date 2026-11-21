"""PI-05 forecast evaluation metrics.

The metrics are computed from explicit prediction rows.  They are never computed
against training data, never aggregate different instruments or horizons into one
headline number, and never invent a percentage error when the denominator is zero
or negative.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

DIRECTION_LABELS = ("up", "down", "neutral")


def direction_label(actual_return: float, neutral_band_pct: float) -> str:
    band = abs(float(neutral_band_pct)) / 100.0
    if actual_return > band:
        return "up"
    if actual_return < -band:
        return "down"
    return "neutral"


def direction_from_forecast(expected_return: float, neutral_band_pct: float) -> str:
    return direction_label(expected_return, neutral_band_pct)


@dataclass(frozen=True, slots=True)
class PredictionRow:
    """One out-of-sample comparison, with every input needed by every metric."""

    actual_price: float
    predicted_price: float
    actual_return: float
    predicted_return: float
    lower_return: float | None
    upper_return: float | None
    actual_direction: str
    predicted_direction: str
    regime: str = "unclassified"
    horizon: str = ""
    instrument_id: str = ""
    model_id: str = ""
    interval_supported: bool = False


def _safe_mean(values: Iterable[float]) -> float | None:
    cleaned = [float(item) for item in values if math.isfinite(float(item))]
    return statistics.fmean(cleaned) if cleaned else None


def price_mae(rows: Sequence[PredictionRow]) -> float | None:
    return _safe_mean(abs(row.actual_price - row.predicted_price) for row in rows)


def price_rmse(rows: Sequence[PredictionRow]) -> float | None:
    values = [
        (row.actual_price - row.predicted_price) ** 2
        for row in rows
        if math.isfinite(row.actual_price) and math.isfinite(row.predicted_price)
    ]
    return math.sqrt(statistics.fmean(values)) if values else None


def price_mape(rows: Sequence[PredictionRow]) -> float | None:
    values = [
        abs(row.actual_price - row.predicted_price) / abs(row.actual_price)
        for row in rows
        if math.isfinite(row.actual_price) and row.actual_price != 0
    ]
    return _safe_mean(values)


def price_smape(rows: Sequence[PredictionRow]) -> float | None:
    values: list[float] = []
    for row in rows:
        denominator = (abs(row.actual_price) + abs(row.predicted_price)) / 2.0
        if denominator > 0:
            values.append(abs(row.actual_price - row.predicted_price) / denominator)
    return _safe_mean(values)


def return_mae(rows: Sequence[PredictionRow]) -> float | None:
    return _safe_mean(abs(row.actual_return - row.predicted_return) for row in rows)


def return_rmse(rows: Sequence[PredictionRow]) -> float | None:
    values = [
        (row.actual_return - row.predicted_return) ** 2
        for row in rows
        if math.isfinite(row.actual_return) and math.isfinite(row.predicted_return)
    ]
    return math.sqrt(statistics.fmean(values)) if values else None


def mean_forecast_error(rows: Sequence[PredictionRow]) -> float | None:
    """Mean actual minus predicted return; positive means under-forecasting."""
    return _safe_mean(row.actual_return - row.predicted_return for row in rows)


def forecast_bias(rows: Sequence[PredictionRow]) -> float | None:
    """Mean predicted minus actual return; positive means over-forecasting."""
    return _safe_mean(row.predicted_return - row.actual_return for row in rows)


def confusion_matrix(rows: Sequence[PredictionRow]) -> dict[str, dict[str, int]]:
    matrix = {
        actual: {predicted: 0 for predicted in DIRECTION_LABELS}
        for actual in DIRECTION_LABELS
    }
    for row in rows:
        actual = row.actual_direction if row.actual_direction in DIRECTION_LABELS else "neutral"
        predicted = (
            row.predicted_direction
            if row.predicted_direction in DIRECTION_LABELS
            else "neutral"
        )
        matrix[actual][predicted] += 1
    return matrix


def directional_accuracy(rows: Sequence[PredictionRow]) -> float | None:
    if not rows:
        return None
    return sum(
        1
        for row in rows
        if row.actual_direction == row.predicted_direction
    ) / len(rows)


def _precision_recall_f1(
    matrix: Mapping[str, Mapping[str, int]], label: str
) -> tuple[float | None, float | None, float | None]:
    true_positive = int(matrix[label][label])
    predicted_total = sum(int(matrix[actual][label]) for actual in DIRECTION_LABELS)
    actual_total = sum(int(matrix[label][predicted]) for predicted in DIRECTION_LABELS)
    precision = true_positive / predicted_total if predicted_total else None
    recall = true_positive / actual_total if actual_total else None
    if precision is None or recall is None or precision + recall == 0:
        f1 = None
    else:
        f1 = 2.0 * precision * recall / (precision + recall)
    return precision, recall, f1


def precision_recall_f1(
    rows: Sequence[PredictionRow],
) -> tuple[dict[str, float | None], dict[str, float | None], dict[str, float | None]]:
    matrix = confusion_matrix(rows)
    precision: dict[str, float | None] = {}
    recall: dict[str, float | None] = {}
    f1: dict[str, float | None] = {}
    for label in DIRECTION_LABELS:
        p, r, f = _precision_recall_f1(matrix, label)
        precision[label], recall[label], f1[label] = p, r, f
    return precision, recall, f1


def interval_coverage(rows: Sequence[PredictionRow]) -> float | None:
    usable = [
        row
        for row in rows
        if row.interval_supported
        and row.lower_return is not None
        and row.upper_return is not None
    ]
    if not usable:
        return None
    return sum(
        1
        for row in usable
        if row.lower_return is not None
        and row.upper_return is not None
        and row.lower_return <= row.actual_return <= row.upper_return
    ) / len(usable)


def mean_interval_width(rows: Sequence[PredictionRow]) -> float | None:
    widths = [
        row.upper_return - row.lower_return
        for row in rows
        if row.interval_supported
        and row.lower_return is not None
        and row.upper_return is not None
    ]
    return _safe_mean(widths)


def coverage_by(rows: Sequence[PredictionRow], key: str) -> dict[str, float | None]:
    groups: dict[str, list[PredictionRow]] = {}
    for row in rows:
        value = str(getattr(row, key, "") or "unclassified")
        groups.setdefault(value, []).append(row)
    return {name: interval_coverage(group) for name, group in sorted(groups.items())}


def samples_by(rows: Sequence[PredictionRow], key: str) -> dict[str, int]:
    groups: dict[str, int] = {}
    for row in rows:
        value = str(getattr(row, key, "") or "unclassified")
        groups[value] = groups.get(value, 0) + 1
    return dict(sorted(groups.items()))


@dataclass(frozen=True, slots=True)
class StatisticalMetrics:
    samples: int
    model_id: str = ""
    instrument_id: str = ""
    horizon: str = ""
    regime: str = ""
    price_mae: float | None = None
    price_rmse: float | None = None
    price_mape: float | None = None
    price_smape: float | None = None
    return_mae: float | None = None
    return_rmse: float | None = None
    mean_forecast_error: float | None = None
    forecast_bias: float | None = None
    directional_accuracy: float | None = None
    precision: Mapping[str, float | None] = field(default_factory=dict)
    recall: Mapping[str, float | None] = field(default_factory=dict)
    f1: Mapping[str, float | None] = field(default_factory=dict)
    confusion: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    bullish_accuracy: float | None = None
    bearish_accuracy: float | None = None
    neutral_accuracy: float | None = None
    interval_coverage: float | None = None
    mean_interval_width: float | None = None
    coverage_by_regime: Mapping[str, float | None] = field(default_factory=dict)
    samples_by_regime: Mapping[str, int] = field(default_factory=dict)
    coverage_by_horizon: Mapping[str, float | None] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        def rounded(value: float | None) -> float | None:
            return None if value is None else round(float(value), 8)

        return {
            "samples": self.samples,
            "model_id": self.model_id,
            "instrument_id": self.instrument_id,
            "horizon": self.horizon,
            "regime": self.regime,
            "price_mae": rounded(self.price_mae),
            "price_rmse": rounded(self.price_rmse),
            "price_mape": rounded(self.price_mape),
            "price_smape": rounded(self.price_smape),
            "return_mae": rounded(self.return_mae),
            "return_rmse": rounded(self.return_rmse),
            "mean_forecast_error": rounded(self.mean_forecast_error),
            "forecast_bias": rounded(self.forecast_bias),
            "directional_accuracy": rounded(self.directional_accuracy),
            "precision": {key: rounded(value) for key, value in self.precision.items()},
            "recall": {key: rounded(value) for key, value in self.recall.items()},
            "f1": {key: rounded(value) for key, value in self.f1.items()},
            "confusion": {actual: dict(row) for actual, row in self.confusion.items()},
            "bullish_prediction_accuracy": rounded(self.bullish_accuracy),
            "bearish_prediction_accuracy": rounded(self.bearish_accuracy),
            "neutral_classification_performance": rounded(self.neutral_accuracy),
            "interval_coverage": rounded(self.interval_coverage),
            "mean_interval_width": rounded(self.mean_interval_width),
            "coverage_by_regime": {
                key: rounded(value) for key, value in self.coverage_by_regime.items()
            },
            "samples_by_regime": dict(self.samples_by_regime),
            "coverage_by_horizon": {
                key: rounded(value) for key, value in self.coverage_by_horizon.items()
            },
            "notes": list(self.notes),
        }


def evaluate_rows(
    rows: Sequence[PredictionRow],
    *,
    model_id: str = "",
    instrument_id: str = "",
    horizon: str = "",
    regime: str = "",
) -> StatisticalMetrics:
    precision, recall, f1 = precision_recall_f1(rows)
    matrix = confusion_matrix(rows)
    bullish = (
        matrix["up"]["up"] / sum(matrix["up"].values())
        if sum(matrix["up"].values())
        else None
    )
    bearish = (
        matrix["down"]["down"] / sum(matrix["down"].values())
        if sum(matrix["down"].values())
        else None
    )
    neutral = (
        matrix["neutral"]["neutral"] / sum(matrix["neutral"].values())
        if sum(matrix["neutral"].values())
        else None
    )
    notes: list[str] = []
    if not any(row.interval_supported for row in rows):
        notes.append("no prediction interval was supported for these rows")
    if len(rows) < 20:
        notes.append("fewer than twenty out-of-sample predictions; treat as provisional")
    return StatisticalMetrics(
        samples=len(rows),
        model_id=model_id or (rows[0].model_id if rows else ""),
        instrument_id=instrument_id or (rows[0].instrument_id if rows else ""),
        horizon=horizon or (rows[0].horizon if rows else ""),
        regime=regime,
        price_mae=price_mae(rows),
        price_rmse=price_rmse(rows),
        price_mape=price_mape(rows),
        price_smape=price_smape(rows),
        return_mae=return_mae(rows),
        return_rmse=return_rmse(rows),
        mean_forecast_error=mean_forecast_error(rows),
        forecast_bias=forecast_bias(rows),
        directional_accuracy=directional_accuracy(rows),
        precision=precision,
        recall=recall,
        f1=f1,
        confusion=matrix,
        bullish_accuracy=bullish,
        bearish_accuracy=bearish,
        neutral_accuracy=neutral,
        interval_coverage=interval_coverage(rows),
        mean_interval_width=mean_interval_width(rows),
        coverage_by_regime=coverage_by(rows, "regime"),
        samples_by_regime=samples_by(rows, "regime"),
        coverage_by_horizon=coverage_by(rows, "horizon"),
        notes=tuple(notes),
    )


def aggregate_scores(
    scores: Mapping[str, StatisticalMetrics],
) -> dict[str, Any]:
    """A comparison table; deliberately no pooled cross-horizon headline."""
    return {
        "models": {key: value.to_dict() for key, value in sorted(scores.items())},
        "best_by_mae": (
            min(
                (
                    (key, value.price_mae)
                    for key, value in scores.items()
                    if value.price_mae is not None
                ),
                key=lambda item: (item[1], item[0]),
            )[0]
            if any(value.price_mae is not None for value in scores.values())
            else None
        ),
        "pooled_across_horizons": False,
    }


__all__ = [
    "DIRECTION_LABELS",
    "PredictionRow",
    "StatisticalMetrics",
    "aggregate_scores",
    "coverage_by",
    "direction_from_forecast",
    "direction_label",
    "directional_accuracy",
    "evaluate_rows",
    "forecast_bias",
    "interval_coverage",
    "mean_forecast_error",
    "mean_interval_width",
    "precision_recall_f1",
]
