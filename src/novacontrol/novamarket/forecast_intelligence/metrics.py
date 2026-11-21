"""Numerical evidence for PI-23.

These are evaluation primitives only. They never train a forecasting model and
never alter an issued forecast. MAE/RMSE are in the units named by the metric;
return errors are fractions. MAPE is withheld whenever a target is zero or
non-positive, because the percentage is then undefined or misleading.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from novacontrol.novamarket.forecast_intelligence.models import (
    ForecastIntelligenceValidationError,
    finite,
)

DIRECTIONS = ("up", "down", "neutral")


def _value(row: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        if name in row:
            number = finite(row.get(name))
            if number is not None:
                return number
    return None


def _direction(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"up", "buy", "bullish"}:
        return "up"
    if text in {"down", "sell", "bearish"}:
        return "down"
    if text in {"neutral", "sideways", "hold", "flat"}:
        return "neutral"
    return ""


def actual_direction(actual_return: float, threshold: float) -> str:
    if actual_return > threshold:
        return "up"
    if actual_return < -threshold:
        return "down"
    return "neutral"


def predicted_direction(row: Mapping[str, Any], threshold: float) -> str:
    explicit = _direction(row.get("predicted_direction"))
    if explicit:
        return explicit
    p_up = finite(row.get("p_up"))
    p_down = finite(row.get("p_down"))
    p_neutral = finite(
        row.get("p_neutral", row.get("p_sideways", row.get("p_neutral_probability")))
    )
    if p_up is None and p_down is None and p_neutral is None:
        predicted_return = _value(row, "predicted_return", "forecast_return")
        if predicted_return is not None:
            return actual_direction(predicted_return, threshold)
        return ""
    values = {"up": p_up or 0.0, "down": p_down or 0.0, "neutral": p_neutral or 0.0}
    return max(values, key=lambda key: values[key])


def _mae(rows: Sequence[tuple[float, float]]) -> float | None:
    return statistics.fmean(abs(a - p) for a, p in rows) if rows else None


def _rmse(rows: Sequence[tuple[float, float]]) -> float | None:
    if not rows:
        return None
    return math.sqrt(statistics.fmean((a - p) ** 2 for a, p in rows))


def _smape(rows: Sequence[tuple[float, float]]) -> float | None:
    values: list[float] = []
    for actual, predicted in rows:
        denominator = abs(actual) + abs(predicted)
        if denominator <= 1e-12:
            continue
        values.append(2.0 * abs(actual - predicted) / denominator)
    return statistics.fmean(values) if values else None


def _mape(rows: Sequence[tuple[float, float]]) -> tuple[float | None, str]:
    if not rows:
        return None, "no observations"
    if any(abs(actual) <= 1e-9 for actual, _ in rows):
        return None, "withheld: at least one target is zero"
    if any(actual < 0 for actual, _ in rows):
        return None, "withheld: MAPE is not meaningful for negative targets"
    return statistics.fmean(abs((actual - predicted) / actual) for actual, predicted in rows), ""


def regression_metrics(
    records: Sequence[Mapping[str, Any]],
    *,
    task: str = "return_regression",
    threshold: float = 0.005,
) -> dict[str, Any]:
    price_rows: list[tuple[float, float]] = []
    return_rows: list[tuple[float, float]] = []
    for row in records:
        actual_price = _value(row, "actual_price")
        predicted_price = _value(row, "predicted_price")
        if actual_price is not None and predicted_price is not None:
            price_rows.append((actual_price, predicted_price))
        actual_return = _value(row, "actual_return")
        predicted_return = _value(row, "predicted_return", "forecast_return")
        if actual_return is not None and predicted_return is not None:
            return_rows.append((actual_return, predicted_return))
    primary = price_rows if task == "price_regression" and price_rows else return_rows
    mape_value, mape_note = _mape(primary)
    directional_errors = [abs(predicted - actual) for actual, predicted in return_rows]
    errors = [predicted - actual for actual, predicted in return_rows]
    return {
        "samples": len(primary),
        "price_samples": len(price_rows),
        "return_samples": len(return_rows),
        "mae": _mae(primary),
        "rmse": _rmse(primary),
        "smape": _smape(primary),
        "mape": mape_value,
        "mape_note": mape_note,
        "directional_return_error": (
            statistics.fmean(directional_errors) if directional_errors else None
        ),
        "bias": statistics.fmean(errors) if errors else None,
        "error_std": statistics.pstdev(errors) if len(errors) > 1 else None,
        "notes": [mape_note] if mape_note else [],
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def classification_metrics(
    records: Sequence[Mapping[str, Any]],
    *,
    threshold: float = 0.005,
) -> dict[str, Any]:
    matrix = {actual: dict.fromkeys(DIRECTIONS, 0) for actual in DIRECTIONS}
    usable = 0
    invalid = 0
    for row in records:
        actual = _direction(row.get("actual_direction"))
        if not actual:
            actual_return = _value(row, "actual_return")
            if actual_return is None:
                invalid += 1
                continue
            actual = actual_direction(actual_return, threshold)
        prediction = predicted_direction(row, threshold)
        if not prediction:
            invalid += 1
            continue
        matrix[actual][prediction] += 1
        usable += 1
    per_class: dict[str, dict[str, Any]] = {}
    for label in DIRECTIONS:
        true_positive = matrix[label][label]
        predicted_positive = sum(matrix[actual][label] for actual in DIRECTIONS)
        actual_positive = sum(matrix[label].values())
        precision = _ratio(true_positive, predicted_positive)
        recall = _ratio(true_positive, actual_positive)
        f1 = (
            2.0 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and precision + recall > 0
            else None
        )
        per_class[label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": actual_positive,
            "predicted": predicted_positive,
        }
    recalls = [row["recall"] for row in per_class.values() if row["recall"] is not None]
    f1s = [row["f1"] for row in per_class.values() if row["f1"] is not None]
    hits = sum(matrix[label][label] for label in DIRECTIONS)
    return {
        "samples": usable,
        "invalid_samples": invalid,
        "accuracy": _ratio(hits, usable),
        "balanced_accuracy": statistics.fmean(recalls) if recalls else None,
        "macro_precision": (
            statistics.fmean(
                row["precision"] for row in per_class.values() if row["precision"] is not None
            )
            if any(row["precision"] is not None for row in per_class.values())
            else None
        ),
        "macro_recall": statistics.fmean(recalls) if recalls else None,
        "macro_f1": statistics.fmean(f1s) if f1s else None,
        "precision": {key: row["precision"] for key, row in per_class.items()},
        "recall": {key: row["recall"] for key, row in per_class.items()},
        "f1": {key: row["f1"] for key, row in per_class.items()},
        "confusion": matrix,
    }


def reliability_bins(
    samples: Sequence[tuple[float, bool]], *, bins: int = 10
) -> list[dict[str, Any]]:
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for probability, outcome in samples:
        index = min(bins - 1, max(0, int(probability * bins)))
        buckets[index].append((probability, outcome))
    result: list[dict[str, Any]] = []
    for index, bucket in enumerate(buckets):
        if not bucket:
            result.append(
                {
                    "lower": index / bins,
                    "upper": (index + 1) / bins,
                    "samples": 0,
                    "mean_probability": None,
                    "observed_frequency": None,
                }
            )
            continue
        result.append(
            {
                "lower": index / bins,
                "upper": (index + 1) / bins,
                "samples": len(bucket),
                "mean_probability": statistics.fmean(p for p, _ in bucket),
                "observed_frequency": sum(1 for _, outcome in bucket if outcome) / len(bucket),
            }
        )
    return result


def expected_calibration_error(
    samples: Sequence[tuple[float, bool]], *, bins: int = 10
) -> float | None:
    if not samples:
        return None
    total = 0.0
    for row in reliability_bins(samples, bins=bins):
        if not row["samples"]:
            continue
        total += (row["samples"] / len(samples)) * abs(
            float(row["observed_frequency"]) - float(row["mean_probability"])
        )
    return total


def probability_metrics(
    records: Sequence[Mapping[str, Any]],
    *,
    threshold: float = 0.005,
    bins: int = 10,
) -> dict[str, Any]:
    multiclass: list[tuple[str, float, float, float]] = []
    binary: dict[str, list[tuple[float, bool]]] = {label: [] for label in DIRECTIONS}
    for row in records:
        p_up = finite(row.get("p_up"))
        p_down = finite(row.get("p_down"))
        p_neutral = finite(
            row.get("p_neutral", row.get("p_sideways", row.get("p_neutral_probability")))
        )
        if p_up is None or p_down is None or p_neutral is None:
            continue
        if any(value < 0.0 or value > 1.0 for value in (p_up, p_down, p_neutral)):
            continue
        total = p_up + p_down + p_neutral
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            continue
        actual = _direction(row.get("actual_direction"))
        if not actual:
            actual_return = _value(row, "actual_return")
            if actual_return is None:
                continue
            actual = actual_direction(actual_return, threshold)
        multiclass.append((actual, p_up, p_down, p_neutral))
        for label, probability in (("up", p_up), ("down", p_down), ("neutral", p_neutral)):
            binary[label].append((probability, actual == label))
    if not multiclass:
        return {
            "samples": 0,
            "brier": None,
            "log_loss": None,
            "calibration_error": None,
            "reliability": [],
            "per_class": {},
            "notes": ["no complete, normalised direction probabilities were available"],
        }
    brier = 0.0
    log_loss = 0.0
    for actual, p_up, p_down, p_neutral in multiclass:
        probabilities = {"up": p_up, "down": p_down, "neutral": p_neutral}
        for label, probability in probabilities.items():
            brier += (probability - (1.0 if label == actual else 0.0)) ** 2
        log_loss -= math.log(max(probabilities[actual], 1e-12))
    count = len(multiclass)
    all_binary = [
        (probability, outcome) for values in binary.values() for probability, outcome in values
    ]
    return {
        "samples": count,
        "brier": brier / count,
        "log_loss": log_loss / count,
        "calibration_error": expected_calibration_error(all_binary, bins=bins),
        "reliability": reliability_bins(all_binary, bins=bins),
        "per_class": {
            label: {
                "samples": len(values),
                "brier": (
                    statistics.fmean(
                        (probability - (1.0 if outcome else 0.0)) ** 2
                        for probability, outcome in values
                    )
                    if values
                    else None
                ),
                "calibration_error": expected_calibration_error(values, bins=bins),
                "reliability": reliability_bins(values, bins=bins),
            }
            for label, values in binary.items()
        },
        "notes": [],
    }


def quantile_loss(actual: float, predicted: float, quantile: float) -> float:
    error = actual - predicted
    return max(quantile * error, (quantile - 1.0) * error)


def uncertainty_metrics(
    records: Sequence[Mapping[str, Any]],
    *,
    nominal_coverage: float = 0.80,
) -> dict[str, Any]:
    rows: list[tuple[float, float]] = []
    inside: list[bool] = []
    widths: list[float] = []
    lower_losses: list[float] = []
    upper_losses: list[float] = []
    for row in records:
        actual = _value(row, "actual_return")
        lower = _value(row, "interval_lower", "lower_return")
        upper = _value(row, "interval_upper", "upper_return")
        if actual is None or lower is None or upper is None or lower > upper:
            continue
        rows.append((lower, upper))
        widths.append(upper - lower)
        inside.append(lower <= actual <= upper)
        lower_losses.append(quantile_loss(actual, lower, 0.10))
        upper_losses.append(quantile_loss(actual, upper, 0.90))
    if not rows:
        return {
            "samples": 0,
            "coverage": None,
            "mean_width": None,
            "median_width": None,
            "quantile_loss": None,
            "nominal_coverage": nominal_coverage,
            "coverage_gap": None,
            "notes": ["no complete prediction intervals were available"],
        }
    coverage = sum(inside) / len(inside)
    return {
        "samples": len(rows),
        "coverage": coverage,
        "mean_width": statistics.fmean(widths),
        "median_width": statistics.median(widths),
        "max_width": max(widths),
        "quantile_loss": statistics.fmean([*lower_losses, *upper_losses]),
        "nominal_coverage": nominal_coverage,
        "coverage_gap": coverage - nominal_coverage,
        "notes": (
            ["empirical coverage differs materially from nominal coverage"]
            if abs(coverage - nominal_coverage) > 0.15
            else []
        ),
    }


def conditional_coverage(
    records: Sequence[Mapping[str, Any]], *, field: str = "regime"
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        grouped[str(row.get(field) or "unknown")].append(row)
    return {key: uncertainty_metrics(values) for key, values in sorted(grouped.items())}


def _logit(probability: float) -> float:
    clipped = min(1.0 - 1e-6, max(1e-6, probability))
    return math.log(clipped / (1.0 - clipped))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def fit_platt(
    samples: Sequence[tuple[float, bool]], *, iterations: int = 100, learning_rate: float = 0.2
) -> dict[str, Any]:
    """Fit p' = sigmoid(a * logit(p) + b) with bounded gradient descent."""
    usable = [(min(1.0 - 1e-6, max(1e-6, p)), 1.0 if outcome else 0.0) for p, outcome in samples]
    if not usable:
        raise ForecastIntelligenceValidationError("Platt scaling needs samples")
    a, b = 1.0, 0.0
    for _ in range(max(1, iterations)):
        gradient_a = 0.0
        gradient_b = 0.0
        for probability, outcome in usable:
            x = _logit(probability)
            predicted = _sigmoid(a * x + b)
            error = predicted - outcome
            gradient_a += error * x
            gradient_b += error
        scale = learning_rate / len(usable)
        a -= scale * gradient_a
        b -= scale * gradient_b
        a = min(5.0, max(0.05, a))
        b = min(5.0, max(-5.0, b))
    return {"method": "platt", "a": a, "b": b, "samples": len(usable)}


def apply_platt(probability: float, parameters: Mapping[str, Any]) -> float:
    a = float(parameters.get("a", 1.0))
    b = float(parameters.get("b", 0.0))
    return min(1.0, max(0.0, _sigmoid(a * _logit(probability) + b)))


def fit_isotonic(samples: Sequence[tuple[float, bool]]) -> dict[str, Any]:
    ordered = sorted((float(p), 1.0 if outcome else 0.0) for p, outcome in samples)
    if not ordered:
        raise ForecastIntelligenceValidationError("isotonic calibration needs samples")
    blocks: list[dict[str, float]] = [
        {"sum": outcome, "weight": 1.0, "min": probability, "max": probability}
        for probability, outcome in ordered
    ]
    index = 0
    while index < len(blocks) - 1:
        left = blocks[index]
        right = blocks[index + 1]
        if left["sum"] / left["weight"] <= right["sum"] / right["weight"]:
            index += 1
            continue
        merged = {
            "sum": left["sum"] + right["sum"],
            "weight": left["weight"] + right["weight"],
            "min": left["min"],
            "max": right["max"],
        }
        blocks[index : index + 2] = [merged]
        if index > 0:
            index -= 1
    points = [
        {
            "lower": block["min"],
            "upper": block["max"],
            "probability": block["sum"] / block["weight"],
            "samples": int(block["weight"]),
        }
        for block in blocks
    ]
    return {"method": "isotonic", "points": points, "samples": len(ordered)}


def apply_isotonic(probability: float, parameters: Mapping[str, Any]) -> float:
    points = list(parameters.get("points") or ())
    if not points:
        return probability
    for point in points:
        if probability <= float(point.get("upper", 1.0)):
            return min(1.0, max(0.0, float(point.get("probability", probability))))
    return min(1.0, max(0.0, float(points[-1].get("probability", probability))))


def fit_temperature(
    rows: Sequence[tuple[float, float, float, str]],
    *,
    minimum: float = 0.5,
    maximum: float = 3.0,
    step: float = 0.05,
) -> dict[str, Any]:
    usable = []
    for p_up, p_down, p_neutral, actual in rows:
        values = [p_up, p_down, p_neutral]
        label = _direction(actual)
        if label and all(0.0 < value < 1.0 for value in values) and abs(sum(values) - 1.0) <= 1e-6:
            usable.append((values, DIRECTIONS.index(label)))
    if not usable:
        raise ForecastIntelligenceValidationError("temperature scaling needs class probabilities")

    def loss(temperature: float) -> float:
        total = 0.0
        for values, actual in usable:
            logits = [math.log(value) / temperature for value in values]
            maximum_value = max(logits)
            normaliser = maximum_value + math.log(
                sum(math.exp(value - maximum_value) for value in logits)
            )
            total -= logits[actual] - normaliser
        return total / len(usable)

    best = minimum
    best_loss = loss(best)
    value = minimum
    while value <= maximum + 1e-9:
        candidate = loss(value)
        if candidate < best_loss:
            best, best_loss = value, candidate
        value += step
    return {
        "method": "temperature",
        "temperature": best,
        "loss": best_loss,
        "samples": len(usable),
    }


def apply_temperature(
    p_up: float, p_down: float, p_neutral: float, parameters: Mapping[str, Any]
) -> tuple[float, float, float]:
    temperature = max(0.05, float(parameters.get("temperature", 1.0)))
    values = [p_up, p_down, p_neutral]
    logits = [math.log(max(1e-9, value)) / temperature for value in values]
    maximum_value = max(logits)
    denominator = sum(math.exp(value - maximum_value) for value in logits)
    scaled = [math.exp(value - maximum_value) / denominator for value in logits]
    return scaled[0], scaled[1], scaled[2]


def calibration_analysis(
    records: Sequence[Mapping[str, Any]],
    *,
    method: str = "auto",
    config: Any,
    dataset_version: str = "",
    model_version: str = "",
) -> dict[str, Any]:
    """Fit on an earlier calibration segment and report on a later holdout.

    No future observation is used to fit the transform. The holdout is never
    used to choose the calibration method: `auto` chooses only by sample size and
    the predeclared policy, then evaluates once.
    """
    chosen = str(method or "auto").strip().lower()
    if chosen not in {"auto", "platt", "isotonic", "temperature"}:
        raise ForecastIntelligenceValidationError(
            "calibration method must be auto, platt, isotonic or temperature"
        )
    usable: list[dict[str, Any]] = []
    for row in records:
        p_up = finite(row.get("p_up"))
        p_down = finite(row.get("p_down"))
        p_neutral = finite(
            row.get("p_neutral", row.get("p_sideways", row.get("p_neutral_probability")))
        )
        actual_return = finite(row.get("actual_return"))
        actual = _direction(row.get("actual_direction"))
        if not actual and actual_return is not None:
            actual = actual_direction(actual_return, float(config.directional_threshold))
        if (
            p_up is None
            or p_down is None
            or p_neutral is None
            or not actual
            or not all(0.0 <= value <= 1.0 for value in (p_up, p_down, p_neutral))
        ):
            continue
        usable.append(
            {
                **dict(row),
                "p_up": p_up,
                "p_down": p_down,
                "p_neutral": p_neutral,
                "actual_direction": actual,
            }
        )
    usable.sort(
        key=lambda row: (str(row.get("generated_at") or ""), str(row.get("forecast_id") or ""))
    )
    if len(usable) < int(config.min_calibration_samples):
        return {
            "status": "insufficient_data",
            "method": method,
            "samples": len(usable),
            "required_samples": int(config.min_calibration_samples),
            "calibration": None,
            "before": probability_metrics(usable, bins=config.calibration_bins),
            "after": None,
            "detail": (
                "insufficient complete probability/outcome pairs; outputs remain uncalibrated"
            ),
            "fitted_on": "not fitted",
            "evaluation_on": "not evaluated",
        }
    split = max(1, int(len(usable) * (1.0 - float(config.calibration_test_fraction))))
    split = min(len(usable) - 1, split)
    fit_rows, test_rows = usable[:split], usable[split:]
    if chosen == "auto":
        chosen = (
            "temperature"
            if len(fit_rows) >= max(60, config.min_calibration_samples * 2)
            else "platt"
        )
    before = probability_metrics(test_rows, bins=config.calibration_bins)
    calibrated_rows: list[dict[str, Any]] = []
    parameters: dict[str, Any]
    if chosen == "platt":
        fit_samples = [(row["p_up"], row["actual_direction"] == "up") for row in fit_rows]
        parameters = fit_platt(
            fit_samples,
            iterations=config.platt_iterations,
            learning_rate=config.platt_learning_rate,
        )
        for row in test_rows:
            p_up = apply_platt(row["p_up"], parameters)
            p_down = apply_platt(row["p_down"], parameters)
            p_neutral = apply_platt(row["p_neutral"], parameters)
            total = p_up + p_down + p_neutral
            if total <= 0:
                p_up, p_down, p_neutral, total = 1 / 3, 1 / 3, 1 / 3, 1.0
            calibrated_rows.append(
                {
                    **row,
                    "p_up": p_up / total,
                    "p_down": p_down / total,
                    "p_neutral": p_neutral / total,
                }
            )
    elif chosen == "isotonic":
        parameters = {
            "method": "isotonic",
            "classes": {},
            "samples": len(fit_rows),
        }
        for label in DIRECTIONS:
            fit = fit_isotonic(
                [(row[f"p_{label}"], row["actual_direction"] == label) for row in fit_rows]
            )
            parameters["classes"][label] = fit
        for row in test_rows:
            raw = {}
            for label in DIRECTIONS:
                raw[label] = apply_isotonic(row[f"p_{label}"], parameters["classes"][label])
            total = sum(raw.values())
            calibrated_rows.append(
                {
                    **row,
                    **{
                        f"p_{label}": raw[label] / total if total else 1 / 3 for label in DIRECTIONS
                    },
                }
            )
    elif chosen == "temperature":
        parameters = fit_temperature(
            [
                (
                    row["p_up"],
                    row["p_down"],
                    row["p_neutral"],
                    str(row["actual_direction"]),
                )
                for row in fit_rows
            ],
            minimum=config.temperature_min,
            maximum=config.temperature_max,
            step=config.temperature_step,
        )
        for row in test_rows:
            p_up, p_down, p_neutral = apply_temperature(
                row["p_up"], row["p_down"], row["p_neutral"], parameters
            )
            calibrated_rows.append({**row, "p_up": p_up, "p_down": p_down, "p_neutral": p_neutral})
    after = probability_metrics(calibrated_rows, bins=config.calibration_bins)
    before_ece = finite(before.get("calibration_error"))
    after_ece = finite(after.get("calibration_error"))
    improvement = (
        before_ece - after_ece if before_ece is not None and after_ece is not None else None
    )
    accepted = improvement is not None and improvement >= float(config.calibration_min_improvement)
    return {
        "status": "calibrated" if accepted else "uncalibrated",
        "method": chosen,
        "samples": len(usable),
        "fit_samples": len(fit_rows),
        "holdout_samples": len(test_rows),
        "required_samples": int(config.min_calibration_samples),
        "calibration": parameters,
        "before": before,
        "after": after,
        "ece_improvement": improvement,
        "accepted": accepted,
        "fitted_on": "earliest chronological segment",
        "evaluation_on": "later chronological holdout",
        "model_version": model_version,
        "dataset_version": dataset_version,
        "detail": (
            "calibrator accepted only after lower holdout calibration error"
            if accepted
            else (
                "calibrator did not improve holdout calibration; "
                "original probabilities remain authoritative"
            )
        ),
    }


def psi(reference: Sequence[float], current: Sequence[float], *, bins: int = 10) -> float | None:
    if len(reference) < 2 or len(current) < 2:
        return None
    ordered = sorted(reference)
    edges: list[float] = []
    for index in range(1, bins):
        position = (len(ordered) - 1) * index / bins
        lower = int(math.floor(position))
        upper = min(len(ordered) - 1, lower + 1)
        weight = position - lower
        edges.append(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)
    edges = sorted(set(edges))

    def bucket(value: float) -> int:
        for index, edge in enumerate(edges):
            if value <= edge:
                return index
        return len(edges)

    ref_counts = [0] * (len(edges) + 1)
    cur_counts = [0] * (len(edges) + 1)
    for value in reference:
        ref_counts[bucket(value)] += 1
    for value in current:
        cur_counts[bucket(value)] += 1
    total = 0.0
    for ref, cur in zip(ref_counts, cur_counts, strict=True):
        ref_share = max(ref / len(reference), 1e-6)
        cur_share = max(cur / len(current), 1e-6)
        total += (cur_share - ref_share) * math.log(cur_share / ref_share)
    return total


def ks_2samp(reference: Sequence[float], current: Sequence[float]) -> dict[str, Any]:
    if len(reference) < 2 or len(current) < 2:
        return {"statistic": None, "p_value": None, "samples": 0}
    ref = sorted(reference)
    cur = sorted(current)
    i = j = 0
    statistic = 0.0
    while i < len(ref) and j < len(cur):
        value = min(ref[i], cur[j])
        while i < len(ref) and ref[i] <= value:
            i += 1
        while j < len(cur) and cur[j] <= value:
            j += 1
        statistic = max(statistic, abs(i / len(ref) - j / len(cur)))
    effective = len(ref) * len(cur) / (len(ref) + len(cur))
    try:
        p_value = 2.0 * sum(
            (-1) ** (k - 1) * math.exp(-2.0 * k * k * (effective * statistic) ** 2)
            for k in range(1, 101)
        )
    except OverflowError:
        p_value = 0.0
    return {
        "statistic": statistic,
        "p_value": min(1.0, max(0.0, p_value)),
        "samples": len(reference) + len(current),
    }


def residual_series(records: Sequence[Mapping[str, Any]]) -> list[float]:
    values: list[float] = []
    for row in records:
        error = finite(row.get("error"))
        if error is not None:
            values.append(abs(error))
            continue
        actual = finite(row.get("actual_return"))
        predicted = finite(row.get("predicted_return", row.get("forecast_return")))
        if actual is not None and predicted is not None:
            values.append(abs(actual - predicted))
    return values


def drift_analysis(
    records: Sequence[Mapping[str, Any]],
    *,
    reference_features: Mapping[str, Sequence[float]] | None,
    current_features: Mapping[str, Sequence[float]] | None,
    config: Any,
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    unavailable: list[str] = []
    for name, reference in (reference_features or {}).items():
        current = (current_features or {}).get(name) or ()
        if len(reference) < int(config.drift_min_samples) or len(current) < int(
            config.drift_min_samples
        ):
            unavailable.append(f"{name}: below {config.drift_min_samples} observations")
            continue
        score = psi(reference, current, bins=config.calibration_bins)
        test = ks_2samp(reference, current)
        detected = bool(
            score is not None
            and score >= float(config.drift_psi_threshold)
            and test["p_value"] is not None
            and test["p_value"] <= float(config.drift_ks_p_threshold)
        )
        events.append(
            {
                "category": "input",
                "feature": name,
                "method": "psi+ks",
                "psi": score,
                "ks_statistic": test["statistic"],
                "ks_p_value": test["p_value"],
                "reference_samples": len(reference),
                "current_samples": len(current),
                "detected": detected,
                "severity": "high"
                if detected and (score or 0) >= config.drift_psi_threshold * 2
                else "watch"
                if detected
                else "none",
            }
        )
    residuals = residual_series(records)
    behaviour = None
    if len(residuals) >= max(int(config.drift_min_samples) * 2, int(config.min_stability_windows)):
        split = len(residuals) // 2
        score = psi(residuals[:split], residuals[split:], bins=config.calibration_bins)
        test = ks_2samp(residuals[:split], residuals[split:])
        detected = bool(
            score is not None
            and score >= float(config.drift_psi_threshold)
            and test["p_value"] is not None
            and test["p_value"] <= float(config.drift_ks_p_threshold)
        )
        behaviour = {
            "category": "behaviour",
            "feature": "absolute_error",
            "method": "psi+ks",
            "psi": score,
            "ks_statistic": test["statistic"],
            "ks_p_value": test["p_value"],
            "reference_samples": split,
            "current_samples": len(residuals) - split,
            "detected": detected,
            "severity": "high" if detected else "none",
        }
        events.append(behaviour)
    elif records:
        unavailable.append(
            f"residual distribution: below {max(int(config.drift_min_samples) * 2, 2)} observations"
        )
    detected_events = [event for event in events if event["detected"]]
    confirmation_required = max(1, int(config.drift_persistence_windows))
    confirmed = len(detected_events) >= confirmation_required
    return {
        "status": (
            "detected"
            if confirmed
            else "not_detected"
            if events and not detected_events
            else "drift_unconfirmed"
        ),
        "detected": confirmed,
        "window_detected": bool(detected_events),
        "detected_windows": len(detected_events),
        "events": events,
        "unavailable": unavailable,
        "method": (
            "population stability index and two-sample Kolmogorov-Smirnov; "
            "both must exceed their configured thresholds"
        ),
        "minimum_samples": int(config.drift_min_samples),
        "persistence_required": confirmation_required,
        "limitations": (
            "feature drift is evaluated only when both reference and current "
            "distributions are supplied; behaviour drift uses chronological "
            "absolute residuals and does not prove a market regime change"
        ),
    }


def degradation_analysis(
    records: Sequence[Mapping[str, Any]],
    *,
    config: Any,
    regime_changed: bool = False,
) -> dict[str, Any]:
    ordered = sorted(
        records,
        key=lambda row: (str(row.get("generated_at") or ""), str(row.get("forecast_id") or "")),
    )
    minimum = int(config.min_regime_samples)
    if len(ordered) < minimum * 2:
        return {
            "health": "insufficient_evidence",
            "status": "insufficient_data",
            "windows": [],
            "samples": len(ordered),
            "detail": (
                f"{len(ordered)} observations cannot support two comparison windows of {minimum}"
            ),
            "regime_changed": regime_changed,
        }
    window_size = max(minimum, len(ordered) // 3)
    windows: list[dict[str, Any]] = []
    for start in range(0, len(ordered) - window_size + 1, window_size):
        subset = ordered[start : start + window_size]
        regression = regression_metrics(subset, task="return_regression")
        classification = classification_metrics(subset)
        uncertainty = uncertainty_metrics(subset)
        windows.append(
            {
                "start": subset[0].get("generated_at"),
                "end": subset[-1].get("generated_at"),
                "samples": len(subset),
                "mae": regression["mae"],
                "directional_accuracy": classification["accuracy"],
                "coverage": uncertainty["coverage"],
            }
        )
    if len(windows) < 2:
        return {
            "health": "insufficient_evidence",
            "status": "insufficient_data",
            "windows": windows,
            "samples": len(ordered),
            "detail": "fewer than two complete comparison windows",
            "regime_changed": regime_changed,
        }
    baseline = windows[0]
    latest = windows[-1]
    mae_change = None
    if baseline["mae"] not in (None, 0) and latest["mae"] is not None:
        mae_change = (latest["mae"] - baseline["mae"]) / abs(baseline["mae"])
    accuracy_drop = None
    if baseline["directional_accuracy"] is not None and latest["directional_accuracy"] is not None:
        accuracy_drop = baseline["directional_accuracy"] - latest["directional_accuracy"]
    coverage_drop = None
    if baseline["coverage"] is not None and latest["coverage"] is not None:
        coverage_drop = baseline["coverage"] - latest["coverage"]
    persistence = int(config.degradation_min_windows)
    worsening = 0
    for previous, current in zip(windows, windows[1:], strict=False):
        previous_mae = previous["mae"]
        current_mae = current["mae"]
        previous_accuracy = previous["directional_accuracy"]
        current_accuracy = current["directional_accuracy"]
        if (
            previous_mae not in (None, 0)
            and current_mae is not None
            and (current_mae - previous_mae) / abs(previous_mae)
            >= float(config.degradation_mae_increase)
        ) or (
            previous_accuracy is not None
            and current_accuracy is not None
            and previous_accuracy - current_accuracy >= float(config.degradation_accuracy_drop)
        ):
            worsening += 1
    degraded = (
        worsening >= persistence
        and mae_change is not None
        and mae_change >= float(config.degradation_mae_increase)
        and (accuracy_drop is None or accuracy_drop >= float(config.degradation_accuracy_drop))
    )
    if regime_changed:
        health = "watch"
        detail = (
            "performance changed while the evaluated regime also changed; "
            "a regime effect is plausible"
        )
    elif degraded:
        health = "degraded"
        detail = "error and/or directional performance deteriorated across consecutive windows"
    elif worsening:
        health = "watch"
        detail = "one comparison window worsened; persistence is not yet established"
    else:
        health = "healthy"
        detail = "no configured degradation threshold was crossed with the required persistence"
    return {
        "health": health,
        "status": "completed" if health != "insufficient_evidence" else "insufficient_data",
        "samples": len(ordered),
        "windows": windows,
        "window_size": window_size,
        "mae_change_pct": mae_change,
        "accuracy_drop": accuracy_drop,
        "coverage_drop": coverage_drop,
        "worsening_windows": worsening,
        "persistence_required": persistence,
        "regime_changed": regime_changed,
        "detail": detail,
        "limitations": (
            "a single declining window never deactivates a model; market-regime and data-quality "
            "changes must be excluded before treating a change as model degradation"
        ),
    }


def _metric(metrics: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        value = finite(metrics.get(name))
        if value is not None:
            return value
    return None


def normalise_metrics(metrics: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(metrics or {})
    classification = source.get("classification") or source.get("classifier") or {}
    if not isinstance(classification, Mapping):
        classification = {}

    def _prefer(primary: Any, fallback: Any) -> Any:
        return primary if primary is not None else fallback

    result = {
        "samples": int(finite(source.get("samples", source.get("count"))) or 0),
        "mae": _metric(source, "mae", "price_mae", "return_mae"),
        "rmse": _metric(source, "rmse", "price_rmse", "return_rmse"),
        "smape": _metric(source, "smape", "price_smape"),
        "mape": _metric(source, "mape", "price_mape"),
        "directional_accuracy": _prefer(
            _metric(source, "directional_accuracy", "accuracy", "direction_accuracy"),
            _metric(classification, "accuracy", "directional_accuracy"),
        ),
        "balanced_accuracy": _prefer(
            _metric(source, "balanced_accuracy", "balanced_accuracy_score"),
            _metric(classification, "balanced_accuracy"),
        ),
        "macro_f1": _prefer(
            _metric(source, "macro_f1", "f1_macro"),
            _metric(classification, "macro_f1"),
        ),
        "brier": _metric(source, "brier", "brier_score"),
        "log_loss": _metric(source, "log_loss", "logloss"),
        "calibration_error": _metric(
            source, "calibration_error", "ece", "expected_calibration_error"
        ),
        "interval_coverage": _metric(source, "interval_coverage", "coverage", "coverage_rate"),
        "interval_width": _metric(source, "interval_width", "mean_interval_width", "width"),
        "quantile_loss": _metric(source, "quantile_loss", "pinball_loss"),
        "bias": _metric(source, "bias", "forecast_bias", "mean_forecast_error"),
        "directional_return_error": _metric(source, "directional_return_error"),
        "smape_note": str(source.get("smape_note") or ""),
    }
    return {key: value for key, value in result.items() if value is not None or key == "samples"}


def merge_metrics(*parts: Mapping[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for part in parts:
        for key, value in normalise_metrics(part).items():
            if value is not None and merged.get(key) is None:
                merged[key] = value
    return merged


def reliability_label(
    *,
    samples: int,
    min_evaluation_samples: int,
    direction_accuracy: float | None,
    interval_coverage: float | None,
    calibration_error: float | None,
    health: str,
    drift: bool,
    data_fresh: bool,
    regime_supported: bool,
) -> tuple[str, list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = [
        {"criterion": "samples", "value": samples, "required": min_evaluation_samples},
        {"criterion": "directional_accuracy", "value": direction_accuracy},
        {"criterion": "interval_coverage", "value": interval_coverage},
        {"criterion": "calibration_error", "value": calibration_error},
        {"criterion": "model_health", "value": health},
        {"criterion": "drift", "value": drift},
        {"criterion": "data_fresh", "value": data_fresh},
        {"criterion": "regime_supported", "value": regime_supported},
    ]
    if samples < min_evaluation_samples:
        return "insufficient_evidence", evidence
    if health in {"degraded", "unsuitable"} or drift or not data_fresh:
        return "unreliable", evidence
    passed = 0
    possible = 0
    if direction_accuracy is not None:
        possible += 1
        passed += direction_accuracy >= 0.50
    if interval_coverage is not None:
        possible += 1
        passed += interval_coverage >= 0.55
    if calibration_error is not None:
        possible += 1
        passed += calibration_error <= 0.10
    possible += 1
    passed += regime_supported
    possible += 1
    passed += health == "healthy"
    if (
        possible
        and passed == possible
        and (direction_accuracy or 0) >= 0.55
        and (interval_coverage or 0) >= 0.65
    ):
        return "high_reliability", evidence
    if possible and passed >= max(1, possible - 1):
        return "moderate_reliability", evidence
    return "low_reliability", evidence


__all__ = [
    "actual_direction",
    "apply_isotonic",
    "apply_platt",
    "apply_temperature",
    "calibration_analysis",
    "classification_metrics",
    "conditional_coverage",
    "degradation_analysis",
    "drift_analysis",
    "expected_calibration_error",
    "fit_isotonic",
    "fit_platt",
    "fit_temperature",
    "ks_2samp",
    "merge_metrics",
    "normalise_metrics",
    "predicted_direction",
    "probability_metrics",
    "psi",
    "quantile_loss",
    "regression_metrics",
    "reliability_bins",
    "reliability_label",
    "residual_series",
    "uncertainty_metrics",
]
