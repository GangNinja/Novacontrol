"""Rolling forecast-accountability metrics over persisted decision outcomes.

The metrics are read-only and computed only from outcomes that have already been
measured against a real subsequent market observation.  No future bar, synthetic
value or unmeasured prediction is included.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import sqrt
from typing import Any


def _number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _confidence(forecast: Mapping[str, Any]) -> float | None:
    value = _number(forecast.get("confidence"))
    if value is None:
        return None
    return min(1.0, max(0.0, value))


def forecast_metrics(
    outcomes: Sequence[Mapping[str, Any]], *, window: int = 200
) -> dict[str, Any]:
    """Calculate MAE, RMSE, directional accuracy, Brier and calibration.

    ``window`` applies to the most recent measured outcomes only.  Rows without
    the required fields are counted as excluded rather than imputed.
    """

    capped = max(1, min(int(window), 2000))
    rows = list(outcomes)[:capped]
    errors: list[float] = []
    squared: list[float] = []
    directional: list[bool] = []
    brier: list[float] = []
    calibrated: list[tuple[float, float]] = []
    excluded = 0
    for row in rows:
        forecast = row.get("forecast")
        if not isinstance(forecast, Mapping):
            excluded += 1
            continue
        expected = _number(forecast.get("expected_return"))
        actual = _number(row.get("actual_return"))
        if expected is None or actual is None:
            excluded += 1
            continue
        error = expected - actual
        errors.append(error)
        squared.append(error * error)
        correct = forecast.get("correct")
        if isinstance(correct, bool):
            directional.append(correct)
            confidence = _confidence(forecast)
            if confidence is not None:
                brier.append((confidence - (1.0 if correct else 0.0)) ** 2)
                calibrated.append((confidence, 1.0 if correct else 0.0))

    count = len(errors)
    directional_accuracy = (
        sum(1 for item in directional if item) / len(directional)
        if directional
        else None
    )
    brier_score = sum(brier) / len(brier) if brier else None
    buckets: list[dict[str, Any]] = []
    for low, high in (
        (0.0, 0.2),
        (0.2, 0.4),
        (0.4, 0.6),
        (0.6, 0.8),
        (0.8, 1.0000001),
    ):
        members = [
            correct
            for confidence, correct in calibrated
            if low <= confidence < high
        ]
        if members:
            buckets.append(
                {
                    "bucket": f"{low:.1f}-{min(high, 1.0):.1f}",
                    "count": len(members),
                    "observed_accuracy": round(sum(members) / len(members), 6),
                }
            )
    return {
        "window": capped,
        "measured": len(rows),
        "evaluated": count,
        "excluded": excluded,
        "mae": round(sum(abs(item) for item in errors) / count, 8) if count else None,
        "rmse": round(sqrt(sum(squared) / count), 8) if count else None,
        "mean_error": round(sum(errors) / count, 8) if count else None,
        "directional_accuracy": (
            round(directional_accuracy, 6) if directional_accuracy is not None else None
        ),
        "directional_sample": len(directional),
        "brier_score": round(brier_score, 8) if brier_score is not None else None,
        "brier_sample": len(brier),
        "calibration": buckets,
        "calculated_from": "persisted prediction_decision_outcomes only",
        "automatic_promotion": False,
        "learning_applied": False,
    }


__all__ = ["forecast_metrics"]
