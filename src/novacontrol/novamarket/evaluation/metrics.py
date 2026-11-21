"""Deterministic PI-11 forecast metrics.

Every metric is computed from explicit evaluation records.  Price, return,
direction, probability and interval results are separate: a trading return is
never used as a forecast error and a forecast error is never presented as a
portfolio return.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any, Callable, Iterable, Mapping, Sequence

from novacontrol.novamarket.evaluation.models import EvaluationConfig, EvaluationRecord

DIRECTION_LABELS = ("up", "down", "neutral")
PREDICTED_LABELS = ("up", "down", "neutral", "uncertain", "abstained")


def _round(value: float | None, digits: int = 8) -> float | None:
    return None if value is None else round(float(value), digits)


def _mean(values: Iterable[float]) -> float | None:
    rows = list(values)
    return statistics.fmean(rows) if rows else None


def _median(values: Iterable[float]) -> float | None:
    rows = list(values)
    return statistics.median(rows) if rows else None


def _stdev(values: Iterable[float]) -> float | None:
    rows = list(values)
    return statistics.stdev(rows) if len(rows) > 1 else None


def _percentile(values: Iterable[float], fraction: float) -> float | None:
    rows = sorted(values)
    if not rows:
        return None
    index = min(len(rows) - 1, max(0, int(round((len(rows) - 1) * fraction))))
    return rows[index]


def actual_direction(actual_return: float, neutral_threshold: float) -> str:
    if actual_return > neutral_threshold:
        return "up"
    if actual_return < -neutral_threshold:
        return "down"
    return "neutral"


def predicted_label(record: EvaluationRecord) -> str:
    value = record.predicted_direction
    if value in PREDICTED_LABELS:
        return value
    return "uncertain"


def uncertainty_metrics(records: Sequence[EvaluationRecord]) -> dict[str, Any]:
    labels: dict[str, int] = {}
    for item in records:
        label = str(item.uncertainty or "unknown").strip().lower() or "unknown"
        labels[label] = labels.get(label, 0) + 1
    disagreements = [
        float(item.disagreement)
        for item in records
        if item.disagreement is not None
    ]
    return {
        "samples": len(records),
        "labels": dict(sorted(labels.items())),
        "disagreement": {
            "samples": len(disagreements),
            "mean": _round(_mean(disagreements)),
            "median": _round(_median(disagreements)),
            "p90": _round(_percentile(disagreements, 0.90)),
            "max": _round(max(disagreements) if disagreements else None),
        },
        "distinction": (
            "uncertainty labels are the stored forecast/ensemble state; "
            "disagreement is the stored constituent dispersion and is not "
            "reinterpreted as a model probability or residual variance"
        ),
    }


def observation_metrics(records: Sequence[EvaluationRecord]) -> dict[str, Any]:
    delays = [
        max(0.0, (item.actual_observed_at - item.target_at).total_seconds())
        for item in records
    ]
    delayed = [item for item, delay in zip(records, delays) if delay > 0]
    return {
        "samples": len(records),
        "delayed_observations": len(delayed),
        "mean_delay_seconds": _round(_mean(delays)),
        "max_delay_seconds": _round(max(delays) if delays else None),
        "delayed_sample": [
            {
                "forecast_id": item.forecast_id,
                "target_at": item.target_at.isoformat(),
                "actual_observed_at": item.actual_observed_at.isoformat(),
                "delay_seconds": round(delay, 3),
            }
            for item, delay in list(zip(records, delays))[:25]
            if delay > 0
        ],
        "definition": (
            "delay = actual observation timestamp - target timestamp; the real "
            "observation time is retained and no later price is substituted silently"
        ),
    }


def _sample_block(records: Sequence[EvaluationRecord], config: EvaluationConfig) -> dict[str, Any]:
    return {
        "samples": len(records),
        "sufficient": len(records) >= config.min_samples,
        "reliable": len(records) >= max(config.min_samples, config.small_sample_size),
        "small_sample": len(records) < config.small_sample_size,
    }


def price_metrics(records: Sequence[EvaluationRecord], config: EvaluationConfig) -> dict[str, Any]:
    errors = [item.predicted_price - item.actual_price for item in records]
    absolute = [abs(value) for value in errors]
    percentage = [
        abs(item.predicted_price - item.actual_price) / item.actual_price
        for item in records
        if item.actual_price
    ]
    symmetric = [
        2.0
        * abs(item.predicted_price - item.actual_price)
        / (abs(item.predicted_price) + abs(item.actual_price))
        for item in records
        if abs(item.predicted_price) + abs(item.actual_price) > 0
    ]
    return {
        **_sample_block(records, config),
        "mae": _round(_mean(absolute)),
        "rmse": _round(math.sqrt(statistics.fmean(value**2 for value in errors)) if errors else None),
        "mape": _round(_mean(percentage)),
        "smape": _round(_mean(symmetric)),
        "mean_error": _round(_mean(errors)),
        "bias": _round(_mean(errors)),
        "median_absolute_error": _round(_median(absolute)),
        "error_std": _round(_stdev(errors)),
        "error_p90": _round(_percentile(absolute, 0.90)),
        "definition": "error = predicted price - actual target price",
    }


def return_metrics(records: Sequence[EvaluationRecord], config: EvaluationConfig) -> dict[str, Any]:
    errors = [item.predicted_return - item.actual_return for item in records]
    absolute = [abs(value) for value in errors]
    downside = [
        item.predicted_return - item.actual_return
        for item in records
        if item.actual_return < 0
    ]
    upside = [
        item.predicted_return - item.actual_return
        for item in records
        if item.actual_return > 0
    ]
    over = sum(1 for value in errors if value > 0)
    under = sum(1 for value in errors if value < 0)
    return {
        **_sample_block(records, config),
        "mean_absolute_error": _round(_mean(absolute)),
        "mean_error": _round(_mean(errors)),
        "bias": _round(_mean(errors)),
        "rmse": _round(
            math.sqrt(statistics.fmean(value**2 for value in errors)) if errors else None
        ),
        "median_absolute_error": _round(_median(absolute)),
        "error_std": _round(_stdev(errors)),
        "overestimate_rate": _round(over / len(errors) if errors else None),
        "underestimate_rate": _round(under / len(errors) if errors else None),
        "downside_mean_error": _round(_mean(downside)),
        "upside_mean_error": _round(_mean(upside)),
        "downside_samples": len(downside),
        "upside_samples": len(upside),
        "definition": (
            "return error = predicted return - realised return; positive means "
            "the forecast overestimated the return"
        ),
    }


def _confusion(rows: Sequence[tuple[str, str]]) -> dict[str, dict[str, int]]:
    matrix = {
        actual: {predicted: 0 for predicted in PREDICTED_LABELS}
        for actual in DIRECTION_LABELS
    }
    for actual, predicted in rows:
        matrix.setdefault(actual, {label: 0 for label in PREDICTED_LABELS})
        matrix[actual][predicted] = matrix[actual].get(predicted, 0) + 1
    return matrix


def direction_metrics(records: Sequence[EvaluationRecord], config: EvaluationConfig) -> dict[str, Any]:
    rows: list[tuple[str, str]] = []
    for item in records:
        rows.append(
            (
                actual_direction(item.actual_return, config.neutral_threshold),
                predicted_label(item),
            )
        )
    uncertain = sum(1 for _, predicted in rows if predicted == "uncertain")
    abstained = sum(1 for _, predicted in rows if predicted == "abstained")
    eligible = (
        rows
        if config.neutral_policy == "exact"
        else [(actual, predicted) for actual, predicted in rows if actual != "neutral"]
    )
    resolved = [
        (actual, predicted)
        for actual, predicted in eligible
        if predicted in DIRECTION_LABELS
    ]
    correct = sum(1 for actual, predicted in resolved if actual == predicted)
    if config.direction_policy == "count_uncertain_incorrect":
        # Non-directional predictions count as incorrect, but neutral actuals
        # excluded by neutral_policy are removed from the denominator too.
        denominator = len(eligible)
    else:
        denominator = len(resolved)
    per_class: dict[str, Any] = {}
    for label in DIRECTION_LABELS:
        true_positive = sum(1 for actual, predicted in resolved if actual == label and predicted == label)
        predicted_total = sum(1 for _, predicted in resolved if predicted == label)
        actual_total = sum(1 for actual, _ in resolved if actual == label)
        precision = true_positive / predicted_total if predicted_total else None
        recall = true_positive / actual_total if actual_total else None
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and precision + recall
            else None
        )
        per_class[label] = {
            "support": actual_total,
            "predicted": predicted_total,
            "true_positive": true_positive,
            "precision": _round(precision),
            "recall": _round(recall),
            "f1": _round(f1),
        }
    predicted_up = sum(1 for _, predicted in resolved if predicted == "up")
    predicted_down = sum(1 for _, predicted in resolved if predicted == "down")
    false_bullish = sum(1 for actual, predicted in resolved if predicted == "up" and actual == "down")
    false_bearish = sum(1 for actual, predicted in resolved if predicted == "down" and actual == "up")
    return {
        **_sample_block(records, config),
        "policy": {
            "direction_policy": config.direction_policy,
            "neutral_policy": config.neutral_policy,
            "neutral_threshold": config.neutral_threshold,
            "uncertain_treatment": (
                "uncertain and abstained predictions are excluded from the "
                "accuracy denominator"
                if config.direction_policy == "exclude_uncertain"
                else "uncertain and abstained predictions are counted as incorrect "
                "and included in the denominator"
            ),
            "neutral_treatment": (
                "neutral is correct only when the actual movement is also neutral"
                if config.neutral_policy == "exact"
                else "neutral observations are excluded from resolved direction accuracy"
            ),
        },
        "accuracy": _round(correct / denominator if denominator else None),
        "resolved": len(resolved),
        "uncertain": uncertain,
        "uncertain_rate": _round(uncertain / len(rows) if rows else None),
        "abstained": abstained,
        "abstention_rate": _round(abstained / len(rows) if rows else None),
        "non_directional": uncertain + abstained,
        "non_directional_rate": _round(
            (uncertain + abstained) / len(rows) if rows else None
        ),
        "confusion": _confusion(rows),
        "per_class": per_class,
        "bullish_accuracy": per_class["up"]["recall"],
        "bearish_accuracy": per_class["down"]["recall"],
        "neutral_accuracy": per_class["neutral"]["recall"],
        "false_bullish_rate": _round(false_bullish / predicted_up if predicted_up else None),
        "false_bearish_rate": _round(false_bearish / predicted_down if predicted_down else None),
        "buy_signals": predicted_up,
        "sell_signals": predicted_down,
    }


def probability_metrics(records: Sequence[EvaluationRecord], config: EvaluationConfig) -> dict[str, Any]:
    eligible = [
        item
        for item in records
        if item.p_up is not None
        and item.p_down is not None
        and item.p_neutral is not None
        and str(item.probability_status).lower() == "calibrated"
    ]
    if not eligible:
        return {
            "samples": 0,
            "sufficient": False,
            "status": "unavailable",
            "reason": (
                "no calibrated directional probabilities were present; arbitrary "
                "NM-06 model scores and uncalibrated probabilities are not treated "
                "as probabilities"
            ),
        }
    brier = 0.0
    log_loss = 0.0
    confidence_rows: list[tuple[float, bool]] = []
    class_bins: dict[str, list[tuple[float, bool]]] = {label: [] for label in DIRECTION_LABELS}
    for item in eligible:
        actual = actual_direction(item.actual_return, config.neutral_threshold)
        probabilities = {
            "up": float(item.p_up or 0.0),
            "down": float(item.p_down or 0.0),
            "neutral": float(item.p_neutral or 0.0),
        }
        brier += sum(
            (probabilities[label] - (1.0 if label == actual else 0.0)) ** 2
            for label in DIRECTION_LABELS
        )
        log_loss -= math.log(max(probabilities[actual], 1e-12))
        confidence_rows.append(
            (
                max(probabilities.values()),
                max(probabilities, key=lambda label: probabilities[label]) == actual,
            )
        )
        for label in DIRECTION_LABELS:
            class_bins[label].append((probabilities[label], actual == label))
    brier /= len(eligible)
    log_loss /= len(eligible)
    bins = _calibration_bins(confidence_rows, bins=10)
    ece = _ece(confidence_rows, bins=10)
    class_ece = {
        label: _ece(values, bins=10)
        for label, values in class_bins.items()
        if values
    }
    return {
        **_sample_block(eligible, config),
        "status": "evaluated",
        "brier": _round(brier),
        "log_loss": _round(log_loss),
        "ece": _round(ece),
        "max_class_ece": _round(
            max((value for value in class_ece.values() if value is not None), default=None)
        ),
        "class_ece": {key: _round(value) for key, value in class_ece.items()},
        "bins": bins,
        "method": (
            "multi-class Brier and log loss on the calibrated up/down/neutral "
            "probabilities; ECE compares the maximum class confidence with the "
            "observed correct-class frequency, and class_ece repeats that check "
            "for each class probability"
        ),
    }


def _calibration_bins(samples: Sequence[tuple[float, bool]], *, bins: int) -> list[dict[str, Any]]:
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for probability, outcome in samples:
        index = min(bins - 1, max(0, int(probability * bins)))
        buckets[index].append((probability, outcome))
    rows: list[dict[str, Any]] = []
    for index, bucket in enumerate(buckets):
        rows.append(
            {
                "bin": index,
                "lower": index / bins,
                "upper": (index + 1) / bins,
                "samples": len(bucket),
                "mean_probability": _round(_mean(value for value, _ in bucket)),
                "observed_frequency": _round(
                    sum(1 for _, outcome in bucket if outcome) / len(bucket)
                    if bucket
                    else None
                ),
            }
        )
    return rows


def _ece(samples: Sequence[tuple[float, bool]], *, bins: int) -> float | None:
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
        confidence = statistics.fmean(value for value, _ in bucket)
        frequency = sum(1 for _, outcome in bucket if outcome) / len(bucket)
        total += (len(bucket) / len(samples)) * abs(frequency - confidence)
    return total


def interval_metrics(records: Sequence[EvaluationRecord], config: EvaluationConfig) -> dict[str, Any]:
    eligible = [
        item
        for item in records
        if item.interval_lower is not None and item.interval_upper is not None
    ]
    if not eligible:
        return {
            "samples": 0,
            "sufficient": False,
            "status": "unavailable",
            "expected_coverage": config.interval_confidence,
            "reason": "no prediction interval was recorded",
        }
    inside: list[bool] = []
    widths: list[float] = []
    below = 0
    above = 0
    for item in eligible:
        assert item.interval_lower is not None and item.interval_upper is not None
        lower = float(item.interval_lower)
        upper = float(item.interval_upper)
        observed = lower <= item.actual_return <= upper
        inside.append(observed)
        widths.append(upper - lower)
        below += 1 if item.actual_return < lower else 0
        above += 1 if item.actual_return > upper else 0
    coverage = sum(inside) / len(inside)
    return {
        **_sample_block(eligible, config),
        "status": "evaluated",
        "expected_coverage": config.interval_confidence,
        "actual_coverage": _round(coverage),
        "coverage_gap": _round(coverage - config.interval_confidence),
        "mean_width": _round(_mean(widths)),
        "median_width": _round(_median(widths)),
        "misses_below": below,
        "misses_above": above,
        "nominal_confidence_is_not_assumed": True,
        "definition": "return-interval coverage over the realised target return",
    }


def _group(
    records: Sequence[EvaluationRecord], key: Callable[[EvaluationRecord], Any]
) -> dict[Any, list[EvaluationRecord]]:
    grouped: dict[Any, list[EvaluationRecord]] = defaultdict(list)
    for item in records:
        grouped[key(item)].append(item)
    return grouped


def stability_metrics(records: Sequence[EvaluationRecord], config: EvaluationConfig) -> dict[str, Any]:
    ordered = sorted(records, key=lambda item: (item.target_at, item.forecast_id))
    if len(ordered) < 10:
        return {
            "samples": len(ordered),
            "status": "insufficient_data",
            "reason": "at least 10 ordered forecasts are required for a stability profile",
        }
    window = max(5, min(20, len(ordered) // 5))
    windows: list[dict[str, Any]] = []
    for start in range(0, max(1, len(ordered) - window + 1), window):
        chunk = ordered[start : start + window]
        if len(chunk) < 3:
            continue
        price = price_metrics(chunk, config)
        direction = direction_metrics(chunk, config)
        windows.append(
            {
                "start": start,
                "end": start + len(chunk) - 1,
                "samples": len(chunk),
                "mae": price["mae"],
                "direction_accuracy": direction["accuracy"],
            }
        )
    maes = [row["mae"] for row in windows if row["mae"] is not None]
    accuracies = [row["direction_accuracy"] for row in windows if row["direction_accuracy"] is not None]
    split = max(1, len(ordered) // 2)
    first, second = ordered[:split], ordered[split:]
    first_price = price_metrics(first, config)
    second_price = price_metrics(second, config)
    first_direction = direction_metrics(first, config)
    second_direction = direction_metrics(second, config)
    mae_degradation = (
        second_price["mae"] - first_price["mae"]
        if second_price["mae"] is not None and first_price["mae"] is not None
        else None
    )
    accuracy_degradation = (
        second_direction["accuracy"] - first_direction["accuracy"]
        if second_direction["accuracy"] is not None and first_direction["accuracy"] is not None
        else None
    )
    return {
        "samples": len(ordered),
        "windows": windows,
        "window_size": window,
        "mae_std_across_windows": _round(_stdev(maes)),
        "direction_accuracy_std_across_windows": _round(_stdev(accuracies)),
        "first_half_mae": first_price["mae"],
        "second_half_mae": second_price["mae"],
        "mae_degradation": _round(mae_degradation),
        "first_half_direction_accuracy": first_direction["accuracy"],
        "second_half_direction_accuracy": second_direction["accuracy"],
        "direction_accuracy_degradation": _round(accuracy_degradation),
        "interpretation": (
            "positive MAE degradation means later forecasts were less accurate; "
            "positive direction degradation means later forecasts were more accurate"
        ),
    }


def lifecycle_metrics(records: Sequence[EvaluationRecord], config: EvaluationConfig) -> dict[str, Any]:
    stage_order = ("t_minus_20", "t_minus_5", "t_minus_1")
    grouped = _group(records, lambda item: item.stage or "unknown")
    stages: list[dict[str, Any]] = []
    for stage in stage_order + tuple(sorted(set(grouped) - set(stage_order))):
        rows = grouped.get(stage)
        if not rows:
            continue
        stages.append(
            {
                "stage": stage,
                "samples": len(rows),
                "generated_at_range": [
                    min(item.generated_at for item in rows).isoformat(),
                    max(item.generated_at for item in rows).isoformat(),
                ],
                "target_at_range": [
                    min(item.target_at for item in rows).isoformat(),
                    max(item.target_at for item in rows).isoformat(),
                ],
                "price": price_metrics(rows, config),
                "returns": return_metrics(rows, config),
                "direction": direction_metrics(rows, config),
                "intervals": interval_metrics(rows, config),
            }
        )
    freshness: list[dict[str, Any]] = []
    for label, rows in sorted(_group(records, lambda item: item.data_freshness or "unknown").items()):
        freshness.append(
            {
                "data_freshness": label,
                "samples": len(rows),
                "price": price_metrics(rows, config),
                "direction": direction_metrics(rows, config),
                "intervals": interval_metrics(rows, config),
            }
        )
    by_cycle: dict[str, dict[str, EvaluationRecord]] = defaultdict(dict)
    for item in records:
        if item.cycle_id and item.stage in stage_order:
            by_cycle[item.cycle_id][item.stage] = item
    comparisons: dict[str, Any] = {}
    for left, right, label in (
        ("t_minus_20", "t_minus_5", "initial_vs_first_confirmation"),
        ("t_minus_20", "t_minus_1", "initial_vs_final_confirmation"),
        ("t_minus_5", "t_minus_1", "first_vs_final_confirmation"),
    ):
        paired = [
            (stages_map[left], stages_map[right])
            for stages_map in by_cycle.values()
            if left in stages_map and right in stages_map
        ]
        left_errors = [abs(a.predicted_price - a.actual_price) for a, _ in paired]
        right_errors = [abs(b.predicted_price - b.actual_price) for _, b in paired]
        left_correct = [
            predicted_label(a) == actual_direction(a.actual_return, config.neutral_threshold)
            for a, _ in paired
            if predicted_label(a) in DIRECTION_LABELS
        ]
        right_correct = [
            predicted_label(b) == actual_direction(b.actual_return, config.neutral_threshold)
            for _, b in paired
            if predicted_label(b) in DIRECTION_LABELS
        ]
        revisions = [
            a.predicted_direction != b.predicted_direction for a, b in paired
        ]
        left_mean = _mean(left_errors)
        right_mean = _mean(right_errors)
        comparisons[label] = {
            "samples": len(paired),
            "left_stage": left,
            "right_stage": right,
            "left_mae": _round(left_mean),
            "right_mae": _round(right_mean),
            "mean_abs_error_change": _round(
                right_mean - left_mean
                if left_mean is not None and right_mean is not None
                else None
            ),
            "right_better": sum(
                1 for left_value, right_value in zip(left_errors, right_errors) if right_value < left_value
            ),
            "right_worse": sum(
                1 for left_value, right_value in zip(left_errors, right_errors) if right_value > left_value
            ),
            "tied": sum(
                1 for left_value, right_value in zip(left_errors, right_errors) if right_value == left_value
            ),
            "left_direction_accuracy": _round(
                sum(left_correct) / len(left_correct) if left_correct else None
            ),
            "right_direction_accuracy": _round(
                sum(right_correct) / len(right_correct) if right_correct else None
            ),
            "direction_changed": sum(revisions),
            "direction_change_rate": _round(sum(revisions) / len(revisions) if revisions else None),
            "confirmation_unavailable": max(0, len(by_cycle) - len(paired)),
            "caveat": (
                "paired checkpoint records use the same cycle target; later "
                "confirmation is not assumed to be more accurate"
            ),
        }
    return {
        "checkpoint_order": list(stage_order),
        "by_stage": stages,
        "by_data_freshness": freshness,
        "paired_comparisons": comparisons,
        "cycles_observed": len(by_cycle),
        "confirmation_unavailable": sum(
            1 for stages_map in by_cycle.values() if "t_minus_1" not in stages_map
        ),
        "note": (
            "A checkpoint label alone is not evidence of improvement. The paired "
            "comparison counts better and worse absolute errors on identical targets."
        ),
    }


def model_comparison(records: Sequence[EvaluationRecord], config: EvaluationConfig) -> dict[str, Any]:
    grouped = _group(
        records,
        lambda item: (
            item.horizon,
            item.model_id or item.ensemble_id or "unspecified",
            item.model_version,
            item.ensemble_id,
        ),
    )
    models: list[dict[str, Any]] = []
    for (horizon, model_id, model_version, ensemble_id), rows in sorted(grouped.items()):
        price = price_metrics(rows, config)
        direction = direction_metrics(rows, config)
        models.append(
            {
                "horizon": horizon,
                "model_id": model_id,
                "model_version": model_version,
                "ensemble_id": ensemble_id,
                "samples": len(rows),
                "sufficient": len(rows) >= config.min_samples,
                "mae": price["mae"],
                "rmse": price["rmse"],
                "direction_accuracy": direction["accuracy"],
                "interval_coverage": interval_metrics(rows, config).get("actual_coverage"),
                "brier": probability_metrics(rows, config).get("brier"),
            }
        )
    ranking: list[dict[str, Any]] = []
    for horizon in sorted({row["horizon"] for row in models}):
        eligible = [
            row
            for row in models
            if row["horizon"] == horizon
            and row["sufficient"]
            and row["mae"] is not None
        ]
        eligible.sort(key=lambda row: (row["mae"], row["rmse"] if row["rmse"] is not None else float("inf")))
        for position, row in enumerate(eligible, start=1):
            ranking.append(
                {
                    "horizon": horizon,
                    "rank": position,
                    "model_id": row["model_id"],
                    "model_version": row["model_version"],
                    "mae": row["mae"],
                    "rmse": row["rmse"],
                }
            )
    baselines: dict[str, dict[str, Any]] = {}
    for horizon in sorted({row["horizon"] for row in models}):
        candidates = [
            row
            for row in models
            if row["horizon"] == horizon
            and row["samples"] >= config.min_samples
            and any(
                token in str(row["model_id"]).lower()
                for token in ("naive", "baseline", "statistical", "sma", "arima")
            )
        ]
        if candidates:
            baseline = sorted(
                candidates,
                key=lambda row: row["mae"] if row["mae"] is not None else float("inf"),
            )[0]
            baselines[horizon] = baseline
    for row in models:
        baseline_row = baselines.get(row["horizon"])
        row["baseline_model"] = baseline_row["model_id"] if baseline_row else None
        row["beats_baseline"] = (
            None
            if baseline_row is None
            or row["mae"] is None
            or baseline_row["mae"] is None
            else row["mae"] < baseline_row["mae"]
        )
    return {
        "models": models,
        "ranking": ranking,
        "ranking_rule": (
            "ranked only within the same horizon by lower price MAE, then lower "
            "RMSE; equal evidence is not reordered by model complexity"
        ),
        "baselines": baselines,
        "automatic_promotion": False,
        "note": (
            "A more complex model is not ranked above a simpler one without "
            "lower out-of-sample error on the same records."
        ),
    }


def regime_metrics(records: Sequence[EvaluationRecord], config: EvaluationConfig) -> list[dict[str, Any]]:
    grouped = _group(records, lambda item: item.regime or "unknown")
    result: list[dict[str, Any]] = []
    for regime, rows in sorted(grouped.items()):
        price = price_metrics(rows, config)
        direction = direction_metrics(rows, config)
        intervals = interval_metrics(rows, config)
        result.append(
            {
                "regime": regime,
                "samples": len(rows),
                "sufficient": len(rows) >= config.min_samples,
                "reliable": len(rows) >= max(config.min_samples, config.small_sample_size),
                "mae": price["mae"],
                "rmse": price["rmse"],
                "direction_accuracy": direction["accuracy"],
                "return_bias": return_metrics(rows, config)["bias"],
                "interval_coverage": intervals.get("actual_coverage"),
                "abstention_rate": direction["abstention_rate"],
                "caveat": (
                    "insufficient samples; no regime conclusion is drawn"
                    if len(rows) < config.min_samples
                    else "historical association only; no model weight change"
                ),
            }
        )
    return result


def horizon_metrics(records: Sequence[EvaluationRecord], config: EvaluationConfig) -> list[dict[str, Any]]:
    grouped = _group(records, lambda item: item.horizon)
    result: list[dict[str, Any]] = []
    for horizon, rows in sorted(grouped.items()):
        price = price_metrics(rows, config)
        direction = direction_metrics(rows, config)
        returns = return_metrics(rows, config)
        intervals = interval_metrics(rows, config)
        probabilities = probability_metrics(rows, config)
        result.append(
            {
                "horizon": horizon,
                "samples": len(rows),
                "sufficient": len(rows) >= config.min_samples,
                "price": price,
                "returns": returns,
                "direction": direction,
                "intervals": intervals,
                "probabilities": probabilities,
            }
        )
    return result


def walk_forward_metrics(
    records: Sequence[EvaluationRecord], config: EvaluationConfig
) -> list[dict[str, Any]]:
    if config.walk_forward_folds < 2:
        return []
    ordered = sorted(records, key=lambda item: (item.target_at, item.forecast_id))
    if len(ordered) < config.walk_forward_folds * 2:
        return []
    folded: list[dict[str, Any]] = []
    size = len(ordered) // config.walk_forward_folds
    for index in range(config.walk_forward_folds):
        start = index * size
        end = len(ordered) if index == config.walk_forward_folds - 1 else (index + 1) * size
        test = ordered[start:end]
        candidate_train = ordered[:start]
        # A training record is usable only when its realised target observation
        # had already ended when this test window's first target began.  Using
        # the forecast's generation time here would retain records whose actual
        # observation arrived during the test window.
        test_start_at = test[0].target_at if test else None
        preceding = [
            item
            for item in candidate_train
            if test_start_at is None or item.actual_observed_at <= test_start_at
        ]
        purged = len(candidate_train) - len(preceding)
        folded.append(
            {
                "fold": index + 1,
                "preceding_records": len(preceding),
                "purged_overlapping_targets": purged,
                "purge_rule": (
                    "a preceding record is retained only when its target "
                    "observation ended at or before the test window began"
                ),
                "test_start": test[0].target_at.isoformat() if test else None,
                "test_end": test[-1].target_at.isoformat() if test else None,
                "train_end": preceding[-1].target_at.isoformat() if preceding else None,
                "price": price_metrics(test, config),
                "direction": direction_metrics(test, config),
                "returns": return_metrics(test, config),
                "intervals": interval_metrics(test, config),
                "probabilities": probability_metrics(test, config),
                "selection": "fixed configuration; no fold is used to tune it",
            }
        )
    return folded


def leakage_audit(records: Sequence[EvaluationRecord], rejected: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    lookahead = [
        item.forecast_id
        for item in records
        if item.information_available_at > item.generated_at
    ]
    delayed = [
        item.forecast_id
        for item in records
        if item.actual_observed_at > item.target_at
    ]
    missing_versions = [
        item.forecast_id
        for item in records
        if not item.dataset_version and not item.feature_set_version
    ]
    return {
        "records_checked": len(records),
        "rejected_records": len(rejected),
        "rejected": [dict(item) for item in rejected[:50]],
        "lookahead_violations": lookahead[:50],
        "lookahead_count": len(lookahead),
        "delayed_observations": delayed[:50],
        "delayed_observation_count": len(delayed),
        "missing_version_records": missing_versions[:50],
        "missing_version_count": len(missing_versions),
        "safeguards": [
            "a target must follow forecast generation",
            "actual observations may not precede the target timestamp",
            "reconstructed records must state model-training and information cutoffs",
            "records generated after the evaluation as_of cutoff are excluded",
            "stored forecast contracts are read, never regenerated",
        ],
    }


def evaluate_metrics(
    records: Sequence[EvaluationRecord], config: EvaluationConfig
) -> dict[str, Any]:
    price = price_metrics(records, config)
    returns = return_metrics(records, config)
    direction = direction_metrics(records, config)
    intervals = interval_metrics(records, config)
    probabilities = probability_metrics(records, config)
    sources: list[dict[str, Any]] = []
    for source, rows in sorted(_group(records, lambda item: item.source).items()):
        sources.append(
            {
                "source": source,
                "samples": len(rows),
                "price": price_metrics(rows, config),
                "direction": direction_metrics(rows, config),
                "origin": sorted({item.origin for item in rows}),
            }
        )
    return {
        "price": price,
        "returns": returns,
        "direction": direction,
        "probabilities": probabilities,
                    "intervals": intervals,
            "uncertainty": uncertainty_metrics(records),
            "observations": observation_metrics(records),
        "stability": stability_metrics(records, config),
        "lifecycle": lifecycle_metrics(records, config),
        "models": model_comparison(records, config),
        "regimes": regime_metrics(records, config),
        "horizons": horizon_metrics(records, config),
        "sources": sources,
        "walk_forward": walk_forward_metrics(records, config),
        "sample": _sample_block(records, config),
    }


__all__ = [
    "actual_direction",
    "direction_metrics",
    "evaluate_metrics",
    "horizon_metrics",
    "interval_metrics",
    "leakage_audit",
    "lifecycle_metrics",
    "model_comparison",
    "observation_metrics",
    "predicted_label",
    "uncertainty_metrics",
    "price_metrics",
    "probability_metrics",
    "regime_metrics",
    "return_metrics",
    "stability_metrics",
    "walk_forward_metrics",
]
