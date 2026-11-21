"""Leakage-safe PI-05 evaluation and model comparison.

The split is chronological and the model is always fitted on the prefix before
each origin.  Validation data chooses the configuration/model; the test prefix is
scored only after that choice and never feeds back into it.  Every row records
the exact instrument, horizon, dataset version and date range it came from.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from novacontrol.novamarket.marketdata.models import DataInterval
from novacontrol.novamarket.prediction.models import ForecastHorizon, HORIZON_SPECS
from novacontrol.novamarket.statistical.estimators import (
    MODEL_IDS,
    forecast_model,
)
from novacontrol.novamarket.statistical.metrics import (
    PredictionRow,
    StatisticalMetrics,
    direction_label,
    evaluate_rows,
)
from novacontrol.novamarket.statistical.models import (
    MODEL_METADATA,
    ModelConfig,
    ModelForecast,
    StatisticalModelError,
    StatisticalValidationError,
    UnsupportedHorizonError,
    config_candidates,
    normalise_model_id,
)

_INTERVAL_SECONDS: dict[DataInterval, int] = {
    DataInterval.MINUTE_1: 60,
    DataInterval.MINUTE_5: 300,
    DataInterval.MINUTE_15: 900,
    DataInterval.MINUTE_30: 1800,
    DataInterval.HOUR_1: 3600,
    DataInterval.DAY_1: 86_400,
    DataInterval.WEEK_1: 7 * 86_400,
    DataInterval.MONTH_1: 30 * 86_400,
}

_DAILY_STEPS: dict[ForecastHorizon, int] = {
    ForecastHorizon.DAY_1: 1,
    ForecastHorizon.DAYS_3: 3,
    ForecastHorizon.DAYS_5: 5,
    ForecastHorizon.DAYS_30: 30,
    ForecastHorizon.DAYS_90: 90,
}

RegimeLookup = Callable[[datetime], str | None]


def horizon_steps(horizon: ForecastHorizon | str, resolution: DataInterval | str) -> int:
    """Number of model steps between the decision bar and the target bar."""
    try:
        chosen = ForecastHorizon(horizon)
        interval = DataInterval(resolution)
    except ValueError as exc:
        raise StatisticalValidationError(f"unknown horizon or resolution: {exc}") from exc
    if not HORIZON_SPECS[chosen].accepts_resolution(interval):
        raise UnsupportedHorizonError(
            f"a {chosen.value} forecast cannot use {interval.value} data; "
            f"the finest permitted input is "
            f"{HORIZON_SPECS[chosen].max_input_interval.value}"
        )
    if chosen in _DAILY_STEPS and interval is not DataInterval.DAY_1:
        raise StatisticalValidationError(
            f"{chosen.value} is a daily/session horizon and requires "
            "daily observations; intraday bars are not silently reinterpreted "
            "as trading days"
        )
    if chosen in _DAILY_STEPS:
        return _DAILY_STEPS[chosen]
    seconds = int(HORIZON_SPECS[chosen].duration.total_seconds())
    interval_seconds = _INTERVAL_SECONDS.get(interval)
    if not interval_seconds:
        raise UnsupportedHorizonError(
            f"no step conversion is defined for {interval.value}"
        )
    return max(1, int(round(seconds / interval_seconds)))


def default_resolution(horizon: ForecastHorizon | str) -> DataInterval:
    try:
        return HORIZON_SPECS[ForecastHorizon(horizon)].max_input_interval
    except (ValueError, KeyError) as exc:
        raise StatisticalValidationError(f"unknown horizon {horizon!r}") from exc


def walk_forward_origins(
    count: int,
    min_train: int,
    horizon: int,
    *,
    start: int | None = None,
    end: int | None = None,
) -> list[tuple[int, int]]:
    """Return ``(origin, target_index)`` pairs with a strictly future target.

    ``closes[:origin]`` is the training prefix; ``closes[origin - 1]`` is the
    decision close and ``closes[target]`` is the outcome.  Origins step by the
    horizon, so validation/test samples do not overlap.
    """
    if horizon < 1:
        raise StatisticalValidationError("a walk-forward horizon must be positive")
    first = max(int(min_train), int(start if start is not None else min_train))
    last = int(end if end is not None else count) - 1
    origins: list[tuple[int, int]] = []
    origin = first
    while origin + horizon - 1 <= last and origin < count:
        origins.append((origin, origin + horizon - 1))
        origin += horizon
    return origins


@dataclass(frozen=True, slots=True)
class DateRange:
    start: datetime | None
    end: datetime | None
    bars: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "bars": self.bars,
        }


@dataclass(frozen=True, slots=True)
class CandidateScore:
    model_id: str
    config: ModelConfig
    split: str
    metrics: StatisticalMetrics
    rows: int
    failures: int = 0
    failure_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "config": self.config.to_dict(),
            "config_digest": self.config.digest,
            "split": self.split,
            "metrics": self.metrics.to_dict(),
            "rows": self.rows,
            "failures": self.failures,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True, slots=True)
class ModelComparison:
    model_id: str
    config: ModelConfig
    validation: StatisticalMetrics
    test: StatisticalMetrics | None
    validation_rows: int
    test_rows: int
    failures: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "config": self.config.to_dict(),
            "config_digest": self.config.digest,
            "validation": self.validation.to_dict(),
            "test": self.test.to_dict() if self.test else None,
            "validation_rows": self.validation_rows,
            "test_rows": self.test_rows,
            "failures": list(self.failures),
        }


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    instrument_id: str
    horizon: ForecastHorizon
    resolution: DataInterval
    horizon_steps: int
    dataset_id: str
    dataset_version: str
    train_range: DateRange
    validation_range: DateRange
    test_range: DateRange
    models: Mapping[str, ModelComparison]
    selected_model: str | None
    selected_config: ModelConfig | None
    selection_metric: str
    baseline_model: str = "random_walk"
    validation_origins: int = 0
    test_origins: int = 0
    regime_samples: Mapping[str, int] = field(default_factory=dict)
    insufficient_regimes: tuple[str, ...] = ()
    failures: tuple[Mapping[str, Any], ...] = ()
    notes: tuple[str, ...] = ()
    report_id: str = ""

    @property
    def leakage_safe(self) -> bool:
        return True

    @property
    def test_used_for_selection(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "instrument_id": self.instrument_id,
            "horizon": self.horizon.value,
            "resolution": self.resolution.value,
            "horizon_steps": self.horizon_steps,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "splits": {
                "train": self.train_range.to_dict(),
                "validation": self.validation_range.to_dict(),
                "test": self.test_range.to_dict(),
            },
            "selection_metric": self.selection_metric,
            "selected_model": self.selected_model,
            "selected_config": (
                self.selected_config.to_dict() if self.selected_config else None
            ),
            "baseline_model": self.baseline_model,
            "validation_origins": self.validation_origins,
            "test_origins": self.test_origins,
            "models": {key: value.to_dict() for key, value in self.models.items()},
            "regime_samples": dict(self.regime_samples),
            "insufficient_regimes": list(self.insufficient_regimes),
            "failures": [dict(item) for item in self.failures],
            "notes": list(self.notes),
            "leakage_safe": True,
            "test_used_for_selection": False,
        }


def _predicted_label(forecast: ModelForecast) -> str:
    value = forecast.direction.value
    if value == "up":
        return "up"
    if value == "down":
        return "down"
    return "neutral"


def _rows_for(
    model_id: str,
    config: ModelConfig,
    closes: Sequence[float],
    bars: Sequence[Any],
    origins: Sequence[tuple[int, int]],
    steps: int,
    *,
    horizon: ForecastHorizon,
    instrument_id: str,
    neutral_band_pct: float,
    regime_lookup: RegimeLookup | None,
) -> tuple[list[PredictionRow], list[str]]:
    rows: list[PredictionRow] = []
    failures: list[str] = []
    regime_cache: dict[str, str] = {}
    for origin, target in origins:
        try:
            forecast = forecast_model(
                model_id,
                closes[:origin],
                steps,
                config,
                bars=bars[:origin],
            )
        except Exception as exc:  # noqa: BLE001 - one model must not crash comparison
            failures.append(f"{origin}: {type(exc).__name__}: {exc}")
            continue
        actual_price = closes[target]
        actual_return = actual_price / closes[origin - 1] - 1.0
        regime = "unclassified"
        if regime_lookup is not None:
            moment = bars[origin - 1].observed_at
            key = moment.isoformat()
            if key not in regime_cache:
                try:
                    value = regime_lookup(moment)
                    regime_cache[key] = str(value) if value else "unclassified"
                except Exception:  # noqa: BLE001 - regime evidence is contextual
                    regime_cache[key] = "unclassified"
            regime = regime_cache[key]
        rows.append(
            PredictionRow(
                actual_price=actual_price,
                predicted_price=forecast.predicted_price,
                actual_return=actual_return,
                predicted_return=forecast.predicted_return,
                lower_return=forecast.lower_return,
                upper_return=forecast.upper_return,
                actual_direction=direction_label(actual_return, neutral_band_pct),
                predicted_direction=_predicted_label(forecast),
                regime=regime,
                horizon=horizon.value,
                instrument_id=instrument_id,
                model_id=model_id,
                interval_supported=forecast.interval_supported,
            )
        )
    return rows, failures


def _regime_breakdown(
    rows: Sequence[PredictionRow],
) -> tuple[dict[str, int], tuple[str, ...]]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.regime] = counts.get(row.regime, 0) + 1
    insufficient = tuple(
        sorted(name for name, count in counts.items() if count < 5)
    )
    return dict(sorted(counts.items())), insufficient


def evaluate_models(
    closes: Sequence[float],
    bars: Sequence[Any],
    *,
    instrument_id: str,
    horizon: ForecastHorizon | str,
    resolution: DataInterval | str,
    dataset_id: str = "market-data",
    dataset_version: str = "unspecified",
    base_config: ModelConfig | Mapping[str, Any] | None = None,
    models: Sequence[str] | None = None,
    train_ratio: float = 0.6,
    validation_ratio: float = 0.2,
    min_train_bars: int | None = None,
    regime_lookup: RegimeLookup | None = None,
    minimum_validation_samples: int = 3,
) -> EvaluationReport:
    """Compare candidates chronologically, selecting on validation only."""
    try:
        chosen_horizon = ForecastHorizon(horizon)
        chosen_resolution = DataInterval(resolution)
    except ValueError as exc:
        raise StatisticalValidationError(f"unknown horizon or resolution: {exc}") from exc
    if len(closes) != len(bars):
        raise StatisticalValidationError("closes and bars must have the same length")
    try:
        train_ratio = float(train_ratio)
        validation_ratio = float(validation_ratio)
    except (TypeError, ValueError) as exc:
        raise StatisticalValidationError(
            "train_ratio and validation_ratio must be numeric"
        ) from exc
    if not 0.0 < train_ratio < 1.0 or not 0.0 < validation_ratio < 1.0:
        raise StatisticalValidationError("split ratios must be within (0, 1)")
    if train_ratio + validation_ratio >= 1.0:
        raise StatisticalValidationError("train + validation must leave a test portion")
    if base_config is None:
        base = ModelConfig()
    elif isinstance(base_config, ModelConfig):
        base = base_config
    elif isinstance(base_config, Mapping):
        base = ModelConfig.from_dict(base_config)
    else:
        raise StatisticalValidationError(
            "base_config must be a ModelConfig or a configuration object"
        )
    if (
        isinstance(minimum_validation_samples, bool)
        or not isinstance(minimum_validation_samples, int)
        or minimum_validation_samples < 1
    ):
        raise StatisticalValidationError(
            "minimum_validation_samples must be a positive integer"
        )
    steps = horizon_steps(chosen_horizon, chosen_resolution)
    minimum = max(
        int(min_train_bars or 0),
        HORIZON_SPECS[chosen_horizon].min_history_bars,
        base.min_observations,
        30,
        steps,
    )
    count = len(closes)
    required = minimum + steps * (minimum_validation_samples + 1)
    if count < required:
        raise StatisticalValidationError(
            f"{count} observations are insufficient for a chronological split; "
            f"at least {required} are required for {minimum_validation_samples} "
            f"validation origins and a test origin at a {steps}-bar target step"
        )
    train_end = max(minimum, int(count * train_ratio))
    # Keep at least the requested number of validation origins; a fixed 60/20
    # split can otherwise leave only one origin after a long minimum history.
    val_end = max(
        train_end + steps * minimum_validation_samples,
        int(count * (train_ratio + validation_ratio)),
    )
    train_end = min(
        train_end, count - steps * (minimum_validation_samples + 1)
    )
    val_end = min(max(val_end, train_end + steps), count - steps)
    if train_end <= 0 or val_end <= train_end:
        raise StatisticalValidationError("the chronological split has no usable validation range")
    validation_origins = walk_forward_origins(
        count, train_end, steps, start=train_end, end=val_end
    )
    test_origins = walk_forward_origins(count, val_end, steps, start=val_end)
    if len(validation_origins) < minimum_validation_samples:
        raise StatisticalValidationError(
            f"only {len(validation_origins)} validation origins are available; "
            f"{minimum_validation_samples} are required"
        )
    if not test_origins:
        raise StatisticalValidationError("the chronological split has no test origins")
    if isinstance(models, str):
        raise StatisticalValidationError(
            "models must be a list of model ids, not a single string"
        )
    selected_models = tuple(
        dict.fromkeys(normalise_model_id(item) for item in (models or MODEL_IDS))
    )
    neutral_band = base.neutral_band_pct
    comparisons: dict[str, ModelComparison] = {}
    failures: list[dict[str, Any]] = []
    all_regime_samples: dict[str, int] = {}
    all_insufficient: set[str] = set()
    for model_id in selected_models:
        candidates = config_candidates(model_id, base)
        best_validation: CandidateScore | None = None
        candidate_failures: list[str] = []
        for config in candidates:
            rows, errors = _rows_for(
                model_id,
                config,
                closes,
                bars,
                validation_origins,
                steps,
                horizon=chosen_horizon,
                instrument_id=instrument_id,
                neutral_band_pct=neutral_band,
                regime_lookup=regime_lookup,
            )
            candidate_failures.extend(errors[:2])
            if not rows:
                failures.append(
                    {
                        "model_id": model_id,
                        "config_digest": config.digest,
                        "split": "validation",
                        "reason": errors[0] if errors else "no validation rows",
                    }
                )
                continue
            metrics = evaluate_rows(
                rows,
                model_id=model_id,
                instrument_id=instrument_id,
                horizon=chosen_horizon.value,
            )
            score = CandidateScore(
                model_id=model_id,
                config=config,
                split="validation",
                metrics=metrics,
                rows=len(rows),
                failures=len(errors),
                failure_reason=errors[0] if errors else "",
            )
            if best_validation is None or _selection_key(score) < _selection_key(
                best_validation
            ):
                best_validation = score
        if best_validation is None:
            continue
        config = best_validation.config
        test_rows, test_errors = _rows_for(
            model_id,
            config,
            closes,
            bars,
            test_origins,
            steps,
            horizon=chosen_horizon,
            instrument_id=instrument_id,
            neutral_band_pct=neutral_band,
            regime_lookup=regime_lookup,
        )
        if not test_rows:
            failures.append(
                {
                    "model_id": model_id,
                    "config_digest": config.digest,
                    "split": "test",
                    "reason": test_errors[0] if test_errors else "no test rows",
                }
            )
            test_metrics = None
        else:
            test_metrics = evaluate_rows(
                test_rows,
                model_id=model_id,
                instrument_id=instrument_id,
                horizon=chosen_horizon.value,
            )
        comparisons[model_id] = ModelComparison(
            model_id=model_id,
            config=config,
            validation=best_validation.metrics,
            test=test_metrics,
            validation_rows=best_validation.rows,
            test_rows=len(test_rows),
            failures=tuple(candidate_failures + test_errors[:2]),
        )
        regime_samples, insufficient = _regime_breakdown(test_rows or [])
        for name, count_value in regime_samples.items():
            all_regime_samples[name] = all_regime_samples.get(name, 0) + count_value
        all_insufficient.update(insufficient)
    if not comparisons:
        raise StatisticalValidationError(
            "no statistical model could be evaluated on this history; "
            "the failure reasons are recorded in the evaluation report"
        )
    eligible = [
        item
        for item in comparisons.values()
        if item.validation.return_mae is not None
        and item.validation.samples >= minimum_validation_samples
    ]
    if not eligible:
        raise StatisticalValidationError(
            "no statistical model has enough finite validation observations "
            f"({minimum_validation_samples} required); no reliable baseline "
            "forecast can be selected"
        )
    selected = min(
        eligible,
        key=lambda item: (
            item.validation.return_mae
            if item.validation.return_mae is not None
            else float("inf"),
            -(
                item.validation.directional_accuracy
                if item.validation.directional_accuracy is not None
                else 0.0
            ),
            item.model_id,
        ),
    )
    baseline = comparisons.get("random_walk")
    notes: list[str] = [
        "validation selected the model/configuration; test data was not consulted",
        "origins step by the horizon so evaluation targets do not overlap",
    ]
    if baseline is None:
        notes.append("the random-walk baseline was not evaluable; no relative skill claim")
    else:
        notes.append(
            "random_walk validation return MAE="
            f"{baseline.validation.return_mae}; selected validation return MAE="
            f"{selected.validation.return_mae}"
        )
    report = EvaluationReport(
        instrument_id=instrument_id,
        horizon=chosen_horizon,
        resolution=chosen_resolution,
        horizon_steps=steps,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        train_range=DateRange(
            bars[0].observed_at if bars else None,
            bars[train_end - 1].observed_at if bars and train_end else None,
            train_end,
        ),
        validation_range=DateRange(
            bars[train_end].observed_at if bars and train_end < len(bars) else None,
            bars[val_end - 1].observed_at if bars and val_end else None,
            max(0, val_end - train_end),
        ),
        test_range=DateRange(
            bars[val_end].observed_at if bars and val_end < len(bars) else None,
            bars[-1].observed_at if bars else None,
            max(0, count - val_end),
        ),
        models=comparisons,
        selected_model=selected.model_id,
        selected_config=selected.config,
        selection_metric="validation_return_mae",
        validation_origins=len(validation_origins),
        test_origins=len(test_origins),
        regime_samples=dict(sorted(all_regime_samples.items())),
        insufficient_regimes=tuple(sorted(all_insufficient)),
        failures=tuple(failures),
        notes=tuple(notes),
    )
    return replace(
        report,
        report_id=_report_id(report),
    )


def _selection_key(score: CandidateScore) -> tuple[float, float, str]:
    metrics = score.metrics
    mae = metrics.return_mae if metrics.return_mae is not None else float("inf")
    direction = (
        metrics.directional_accuracy
        if metrics.directional_accuracy is not None
        else 0.0
    )
    return (mae, -direction, score.config.digest)


def _report_id(report: EvaluationReport) -> str:
    import hashlib
    import json

    payload = {
        "instrument_id": report.instrument_id,
        "horizon": report.horizon.value,
        "resolution": report.resolution.value,
        "dataset_id": report.dataset_id,
        "dataset_version": report.dataset_version,
        "selected_model": report.selected_model,
        "selected_config": (
            report.selected_config.digest if report.selected_config else None
        ),
        "models": {key: value.to_dict() for key, value in report.models.items()},
        "ranges": {
            "train": report.train_range.to_dict(),
            "validation": report.validation_range.to_dict(),
            "test": report.test_range.to_dict(),
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sfe_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


__all__ = [
    "CandidateScore",
    "DateRange",
    "EvaluationReport",
    "ModelComparison",
    "default_resolution",
    "evaluate_models",
    "horizon_steps",
    "walk_forward_origins",
]
