"""Leakage-safe training and evaluation for PI-06 gradient boosting.

The trainer consumes PI-02's chronological, purged split builder and PI-04's
feature engine.  It never fetches a provider, writes market data, or chooses a
model from test data.  A model is fitted on the training prefix, validated on
the validation prefix, selected only on validation, and then refitted on
train+validation for the single held-out test report.

The implementation uses the native XGBoost and LightGBM boosters rather than a
third estimator framework.  Missing values remain NaN and are handled by the
tree learners' native missing-value path; actual zeros stay zero.
"""

from __future__ import annotations

import importlib
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence, cast

import numpy as np

from novacontrol.novamarket.datasets.models import TrainingSample
from novacontrol.novamarket.datasets.training import build_training_dataset
from novacontrol.novamarket.features.models import FeatureProfileName, FeatureRequest
from novacontrol.novamarket.features.models import FeatureSet
from novacontrol.novamarket.forecasting.features import PriceBar
from novacontrol.novamarket.marketdata.models import DataInterval, OhlcvBar
from novacontrol.novamarket.prediction.models import ForecastHorizon, HORIZON_SPECS
from novacontrol.novamarket.statistical.metrics import (
    PredictionRow,
    StatisticalMetrics,
    direction_label,
    evaluate_rows,
)
from novacontrol.novamarket.statistical.models import (
    ModelConfig,
    classify_direction,
    decimal_price,
)
from novacontrol.novamarket.statistical.estimators import forecast_model
from novacontrol.novamarket.statistical.validation import horizon_steps

from novacontrol.novamarket.gradient.models import (
    ClassifierReport,
    FamilyResult,
    FeatureSchema,
    FeatureStats,
    GradientConfig,
    GradientFamily,
    GradientStatus,
    GradientTrainingError,
    GradientValidationError,
    IntervalCalibration,
    digest_payload,
)

_LABELS = ("down", "neutral", "up")
_LABEL_TO_INDEX = {name: index for index, name in enumerate(_LABELS)}
_INDEX_TO_LABEL = {index: name for name, index in _LABEL_TO_INDEX.items()}

NDArray = np.ndarray[Any, Any]


def backend_version(family: GradientFamily) -> str:
    module_name = "xgboost" if family is GradientFamily.XGBOOST else "lightgbm"
    try:
        module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 - absence is a reported capability state
        return ""
    return str(getattr(module, "__version__", "") or "unknown")


def backend_available(family: GradientFamily) -> bool:
    return bool(backend_version(family))


def _module(family: GradientFamily) -> Any:
    try:
        return importlib.import_module(
            "xgboost" if family is GradientFamily.XGBOOST else "lightgbm"
        )
    except Exception as exc:  # noqa: BLE001
        raise GradientTrainingError(
            f"{family.value} backend is not installed; install the prediction "
            "extra before training this family"
        ) from exc


@dataclass(frozen=True, slots=True)
class GradientRow:
    decision_at: datetime
    target_at: datetime
    decision_close: float
    target_close: float
    target_return: float
    label: str
    features: Mapping[str, float | None]
    regime: str
    sample: TrainingSample


@dataclass(slots=True)
class GradientMatrix:
    rows: tuple[GradientRow, ...]
    schema: FeatureSchema
    x: NDArray
    y_return: NDArray
    y_label: NDArray
    train_indices: tuple[int, ...]
    validation_indices: tuple[int, ...]
    test_indices: tuple[int, ...]
    feature_stats: FeatureStats
    validation_start: datetime | None
    test_start: datetime | None
    validation_end: datetime | None
    test_end: datetime | None
    failures: tuple[str, ...] = ()
    regime_samples: Mapping[str, int] = field(default_factory=dict)

    def split(self, name: str) -> tuple[NDArray, NDArray, NDArray, NDArray]:
        if name == "train":
            indices = self.train_indices
        elif name == "validation":
            indices = self.validation_indices
        elif name == "test":
            indices = self.test_indices
        else:
            raise GradientValidationError(f"unknown split {name!r}")
        selected = np.asarray(indices, dtype=np.int64)
        return (
            self.x[selected],
            self.y_return[selected],
            self.y_label[selected],
            selected,
        )


@dataclass(slots=True)
class FittedGradient:
    family: GradientFamily
    config: GradientConfig
    schema: FeatureSchema
    regressor: Any
    classifier: Any
    regressor_iterations: int
    classifier_iterations: int
    interval: IntervalCalibration
    feature_importance: tuple[Mapping[str, Any], ...]
    feature_stats: FeatureStats
    validation: StatisticalMetrics
    test: StatisticalMetrics | None
    classifier_validation: ClassifierReport | None
    classifier_test: ClassifierReport | None
    train_samples: int
    validation_samples: int
    test_samples: int
    training_start: datetime | None
    training_end: datetime | None
    regime_metrics: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    regime_samples: Mapping[str, int] = field(default_factory=dict)
    walk_forward: tuple[Mapping[str, Any], ...] = ()
    failures: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def _finite_array(values: Iterable[float | None], name: str) -> NDArray:
    output = np.asarray(
        [np.nan if value is None else float(value) for value in values],
        dtype=np.float64,
    )
    if np.isinf(output).any():
        raise GradientValidationError(f"{name} contains an infinite value")
    if np.isnan(output).any():
        raise GradientValidationError(f"{name} contains a missing target value")
    return output


def _sample_profile(horizon: ForecastHorizon, resolution: DataInterval) -> FeatureProfileName:
    if HORIZON_SPECS[horizon].family.value == "intraday" or resolution.is_intraday:
        return FeatureProfileName.INTRADAY
    if horizon in (
        ForecastHorizon.DAY_1,
        ForecastHorizon.DAYS_3,
        ForecastHorizon.DAYS_5,
    ):
        return FeatureProfileName.SWING
    return FeatureProfileName.LONG_TERM


def _split_value(sample: TrainingSample) -> str:
    """Read a split whether the training model stores the enum or its value."""
    split = sample.split
    return str(getattr(split, "value", split))


def _feature_row(
    feature_set: FeatureSet,
    *,
    allow_imputed: bool = False,
) -> dict[str, float | None]:
    """Preserve zeros and distinguish every non-value state as missing."""
    allowed = {"value", "zero"}
    if allow_imputed:
        allowed.add("imputed")
    row: dict[str, float | None] = {}
    for name in feature_set.feature_names:
        status = str(feature_set.statuses.get(name, "missing"))
        value = feature_set.values.get(name)
        if status in allowed and value is not None:
            number = float(value)
            row[name] = number if math.isfinite(number) else None
        else:
            row[name] = None
    return row


def _sample_features(
    *,
    engine: Any,
    instrument_id: str,
    resolution: DataInterval,
    profile: FeatureProfileName,
    sample: TrainingSample,
    bars: Sequence[OhlcvBar],
    fundamentals: Sequence[Any],
    news_events: Sequence[Any],
    benchmark_bars: Mapping[str, Sequence[OhlcvBar]] | None = None,
    sector_bars: Mapping[str, Sequence[OhlcvBar]] | None = None,
    context_bars: Mapping[str, Mapping[str, Sequence[OhlcvBar]]] | None = None,
) -> tuple[dict[str, float | None], str, str]:
    prefix = [bar for bar in bars if bar.observed_at <= sample.decision_at]
    if not prefix:
        raise GradientValidationError(
            f"no observation is available at or before {sample.decision_at.isoformat()}"
        )
    request = FeatureRequest(
        instrument_id=instrument_id,
        as_of=sample.decision_at,
        resolution=resolution.value,
        profile=profile,
        timeframes=(),
        include_context=bool(benchmark_bars or sector_bars or context_bars),
        include_fundamentals=bool(fundamentals),
        include_news=bool(news_events),
        max_bars=min(len(prefix), 500),
    )
    engine_bars: dict[str, Any] = {resolution.value: prefix}
    engine_bars.update(benchmark_bars or {})
    engine_bars.update(sector_bars or {})
    for role_bars in (context_bars or {}).values():
        engine_bars.update(role_bars)
    result = engine.generate(
        request,
        bars=engine_bars,
        fundamentals=fundamentals,
        news_events=news_events,
    )
    return (
        _feature_row(result.feature_set),
        str(result.regime.primary),
        result.feature_set.version,
    )


def _select_schema(
    feature_rows: Sequence[Mapping[str, float | None]],
    *,
    feature_set_version: str,
    profile: str,
    train_indices: Sequence[int],
    max_features: int,
    max_missing_fraction: float,
) -> tuple[FeatureSchema, tuple[int, ...]]:
    if not feature_rows:
        raise GradientTrainingError("no feature rows were produced")
    names = tuple(sorted(feature_rows[0]))
    if not names:
        raise GradientTrainingError("the feature engine produced no named features")
    for index, row in enumerate(feature_rows):
        if tuple(sorted(row)) != names:
            raise GradientValidationError(
                f"feature schema changed at row {index}; refusing to reorder columns"
            )
    train = list(train_indices)
    selected: list[str] = []
    dropped: dict[str, str] = {}
    for name in names:
        present = sum(
            1
            for index in train
            if feature_rows[index].get(name) is not None
        )
        if not train:
            raise GradientTrainingError("training split is empty")
        fraction = present / len(train)
        if fraction < 1.0 - max_missing_fraction:
            dropped[name] = (
                f"missing in {1.0 - fraction:.1%} of training rows "
                f"(limit {max_missing_fraction:.0%})"
            )
            continue
        values: list[float] = []
        for index in train:
            value = feature_rows[index].get(name)
            if value is not None:
                values.append(float(value))
        if len(values) < 2:
            dropped[name] = "fewer than two finite training values"
            continue
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        if variance <= 1e-16:
            dropped[name] = "zero training variance"
            continue
        selected.append(name)
        if len(selected) >= max_features:
            break
    if not selected:
        raise GradientTrainingError(
            "no feature survived training-only missing/variance selection"
        )
    schema = FeatureSchema(
        names=tuple(selected),
        feature_set_version=feature_set_version,
        profile=profile,
        source_names=names,
        dropped=dropped,
    )
    selected_indices = tuple(
        index
        for index, row in enumerate(feature_rows)
        if any(row.get(name) is not None for name in schema.names)
    )
    return schema, selected_indices


def _matrix_from_rows(
    rows: Sequence[GradientRow],
    schema: FeatureSchema,
    *,
    feature_set_version: str,
    profile: str,
    config: GradientConfig,
    failures: Sequence[str] = (),
) -> GradientMatrix:
    x = np.asarray(
        [
            [
                np.nan
                if row.features.get(name) is None
                else float(row.features[name] or 0.0)
                for name in schema.names
            ]
            for row in rows
        ],
        dtype=np.float64,
    )
    if np.isinf(x).any():
        raise GradientValidationError("feature matrix contains an infinite value")
    y_return = _finite_array((row.target_return for row in rows), "target_return")
    y_label = np.asarray(
        [_LABEL_TO_INDEX.get(row.label, 1) for row in rows], dtype=np.int64
    )
    train = tuple(
        index for index, row in enumerate(rows) if _split_value(row.sample) == "train"
    )
    validation = tuple(
        index
        for index, row in enumerate(rows)
        if _split_value(row.sample) == "validation"
    )
    test = tuple(
        index for index, row in enumerate(rows) if _split_value(row.sample) == "test"
    )
    train_x = x[list(train)]
    stats = _feature_stats(train_x, schema.names)
    regime_counts: dict[str, int] = {}
    for row in rows:
        regime_counts[row.regime] = regime_counts.get(row.regime, 0) + 1
    return GradientMatrix(
        rows=tuple(rows),
        schema=schema,
        x=x,
        y_return=y_return,
        y_label=y_label,
        train_indices=train,
        validation_indices=validation,
        test_indices=test,
        feature_stats=stats,
        validation_start=(
            min((rows[index].decision_at for index in validation), default=None)
        ),
        validation_end=(
            max((rows[index].decision_at for index in validation), default=None)
        ),
        test_start=min((rows[index].decision_at for index in test), default=None),
        test_end=max((rows[index].decision_at for index in test), default=None),
        failures=tuple(failures),
        regime_samples=dict(sorted(regime_counts.items())),
    )


def _feature_stats(x: NDArray, names: Sequence[str]) -> FeatureStats:
    medians: dict[str, float] = {}
    scales: dict[str, float] = {}
    minimums: dict[str, float] = {}
    maximums: dict[str, float] = {}
    for index, name in enumerate(names):
        column = x[:, index]
        finite = column[np.isfinite(column)]
        if not len(finite):
            medians[name] = 0.0
            scales[name] = 0.0
            minimums[name] = 0.0
            maximums[name] = 0.0
            continue
        median = float(np.median(finite))
        q1, q3 = np.quantile(finite, [0.25, 0.75])
        iqr = float(q3 - q1)
        scale = iqr / 1.349 if iqr > 1e-12 else float(np.std(finite))
        medians[name] = median
        scales[name] = scale if math.isfinite(scale) else 0.0
        minimums[name] = float(np.min(finite))
        maximums[name] = float(np.max(finite))
    return FeatureStats(
        medians=medians,
        scales=scales,
        minimums=minimums,
        maximums=maximums,
    )


def build_training_matrix(
    *,
    feature_engine: Any,
    instrument_id: str,
    horizon: ForecastHorizon,
    resolution: DataInterval,
    as_of: datetime,
    bars: Sequence[OhlcvBar],
    dataset_id: str,
    dataset_version: str,
    config: GradientConfig,
    calendars: Mapping[str, Any] | None = None,
    fundamentals: Sequence[Any] = (),
    news_events: Sequence[Any] = (),
    benchmark_bars: Mapping[str, Sequence[OhlcvBar]] | None = None,
    sector_bars: Mapping[str, Sequence[OhlcvBar]] | None = None,
    context_bars: Mapping[str, Mapping[str, Sequence[OhlcvBar]]] | None = None,
) -> GradientMatrix:
    """Build PI-02-split, PI-04-feature rows for one instrument/horizon."""
    if len(bars) < max(30, config.window_bars + 1):
        raise GradientTrainingError(
            f"{len(bars)} observations are insufficient; at least "
            f"{max(30, config.window_bars + 1)} are required for the feature window"
        )
    cutoff_bars = [bar for bar in bars if bar.observed_at <= as_of]
    if not cutoff_bars:
        raise GradientTrainingError("no bars are available at or before as_of")
    dataset, samples = build_training_dataset(
        dataset_id=dataset_id or "market-data",
        dataset_version=dataset_version or "market-data",
        resolution=resolution,
        horizon=horizon,
        created_at=as_of,
        bars_by_instrument={instrument_id: cutoff_bars},
        calendars=dict(calendars or {}),
        window_bars=config.window_bars,
        train_ratio=config.train_ratio,
        validation_ratio=config.validation_ratio,
        max_samples=config.max_samples,
    )
    if not samples:
        raise GradientTrainingError(
            "PI-02 produced no chronological samples; there is no target after "
            "the feature window"
        )
    profile = _sample_profile(horizon, resolution)
    feature_rows: list[GradientRow] = []
    failures: list[str] = []
    feature_versions: set[str] = set()
    for sample in samples:
        try:
            values, regime, version = _sample_features(
                engine=feature_engine,
                instrument_id=instrument_id,
                resolution=resolution,
                profile=profile,
                sample=sample,
                bars=cutoff_bars,
                fundamentals=fundamentals,
                news_events=news_events,
                benchmark_bars=benchmark_bars,
                sector_bars=sector_bars,
                context_bars=context_bars,
            )
        except Exception as exc:  # noqa: BLE001 - one row must not abort the build
            failures.append(
                f"{sample.decision_at.isoformat()}: {type(exc).__name__}: {exc}"
            )
            continue
        if (
            not math.isfinite(float(sample.decision_close))
            or not math.isfinite(float(sample.target_close))
            or float(sample.decision_close) <= 0.0
            or float(sample.target_close) <= 0.0
        ):
            failures.append(
                f"{sample.decision_at.isoformat()}: non-positive or non-finite "
                "decision/target close"
            )
            continue
        target_return = float(
            (sample.target_close / sample.decision_close) - 1
        )
        feature_rows.append(
            GradientRow(
                decision_at=sample.decision_at,
                target_at=sample.target_at,
                decision_close=float(sample.decision_close),
                target_close=float(sample.target_close),
                target_return=target_return,
                label=direction_label(target_return, config.effective_neutral_band_pct),
                features=values,
                regime=regime,
                sample=sample,
            )
        )
        feature_versions.add(version)
    if not feature_rows:
        raise GradientTrainingError(
            "no feature row could be generated for any chronological sample"
        )
    if len(feature_versions) != 1:
        raise GradientValidationError(
            "feature rows mix feature-set versions; refusing to train"
        )
    # build_training_dataset marks split on the sample, but a failed feature row
    # must not silently move later examples across the chronological boundary.
    train_indices = tuple(
        index
        for index, row in enumerate(feature_rows)
        if _split_value(row.sample) == "train"
    )
    validation_indices = tuple(
        index
        for index, row in enumerate(feature_rows)
        if _split_value(row.sample) == "validation"
    )
    test_indices = tuple(
        index
        for index, row in enumerate(feature_rows)
        if _split_value(row.sample) == "test"
    )
    schema, _ = _select_schema(
        [row.features for row in feature_rows],
        feature_set_version=next(iter(feature_versions)),
        profile=profile.value,
        train_indices=train_indices,
        max_features=config.max_features,
        max_missing_fraction=config.max_missing_fraction,
    )
    matrix = _matrix_from_rows(
        feature_rows,
        schema,
        feature_set_version=next(iter(feature_versions)),
        profile=profile.value,
        config=config,
        failures=failures,
    )
    if len(matrix.train_indices) < config.min_train_samples:
        raise GradientTrainingError(
            f"only {len(matrix.train_indices)} usable training rows; "
            f"{config.min_train_samples} are required"
        )
    if len(matrix.validation_indices) < config.min_validation_samples:
        raise GradientTrainingError(
            f"only {len(matrix.validation_indices)} usable validation rows; "
            f"{config.min_validation_samples} are required"
        )
    if len(matrix.test_indices) < config.min_test_samples:
        raise GradientTrainingError(
            f"only {len(matrix.test_indices)} usable test rows; "
            f"{config.min_test_samples} are required"
        )
    if matrix.validation_start is None or matrix.test_start is None:
        raise GradientValidationError("the chronological split has no validation/test range")
    return matrix


def _rows_for_predictions(
    rows: Sequence[GradientRow],
    indices: Sequence[int],
    predicted_returns: Sequence[float],
    *,
    predicted_labels: Sequence[str],
    lower_bounds: Sequence[float],
    upper_bounds: Sequence[float],
    model_id: str,
    horizon: str,
    instrument_id: str,
    intervals_supported: bool,
    neutral_band_pct: float,
) -> list[PredictionRow]:
    result: list[PredictionRow] = []
    for position, index in enumerate(indices):
        row = rows[index]
        result.append(
            PredictionRow(
                actual_price=row.target_close,
                predicted_price=row.decision_close * (1.0 + float(predicted_returns[position])),
                actual_return=row.target_return,
                predicted_return=float(predicted_returns[position]),
                lower_return=float(lower_bounds[position]) if intervals_supported else None,
                upper_return=float(upper_bounds[position]) if intervals_supported else None,
                actual_direction=direction_label(
                    row.target_return, neutral_band_pct
                ),
                predicted_direction=str(predicted_labels[position]),
                regime=row.regime,
                horizon=horizon,
                instrument_id=instrument_id,
                model_id=model_id,
                interval_supported=intervals_supported,
            )
        )
    return result


def _classifier_metrics(
    actual_labels: Sequence[str],
    predicted_labels: Sequence[str],
) -> ClassifierReport:
    labels = list(_LABELS)
    confusion = {actual: {predicted: 0 for predicted in labels} for actual in labels}
    for actual, predicted in zip(actual_labels, predicted_labels):
        a = actual if actual in labels else "neutral"
        p = predicted if predicted in labels else "neutral"
        confusion[a][p] += 1
    samples = len(actual_labels)
    accuracy = (
        sum(1 for actual, predicted in zip(actual_labels, predicted_labels) if actual == predicted)
        / samples
        if samples
        else None
    )
    f1_scores: list[float] = []
    for label in labels:
        tp = confusion[label][label]
        predicted_total = sum(confusion[actual][label] for actual in labels)
        actual_total = sum(confusion[label][predicted] for predicted in labels)
        precision = tp / predicted_total if predicted_total else 0.0
        recall = tp / actual_total if actual_total else 0.0
        f1_scores.append(
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
    return ClassifierReport(
        classes=tuple(labels),
        accuracy=accuracy,
        macro_f1=sum(f1_scores) / len(f1_scores) if f1_scores else None,
        confusion=confusion,
        samples=samples,
        status="trained" if samples else "unavailable",
        probabilities_calibrated=False,
    )


def _xgb_train(
    config: GradientConfig,
    x_train: NDArray,
    y_train: NDArray,
    x_validation: NDArray,
    y_validation: NDArray,
    *,
    objective: str,
    num_class: int = 0,
) -> tuple[Any, int]:
    xgb = _module(GradientFamily.XGBOOST)
    dtrain = xgb.DMatrix(x_train, label=y_train, missing=np.nan)
    dvalid = xgb.DMatrix(x_validation, label=y_validation, missing=np.nan)
    params: dict[str, Any] = {
        "objective": objective,
        "eta": config.learning_rate,
        "max_depth": config.max_depth,
        "subsample": config.subsample,
        "colsample_bytree": config.colsample_bytree,
        "min_child_weight": config.min_child_weight,
        "lambda": config.reg_lambda,
        "alpha": config.reg_alpha,
        "tree_method": "hist",
        "device": "cpu",
        "nthread": config.n_jobs,
        "seed": config.random_seed,
        "verbosity": 0,
    }
    if num_class:
        params["num_class"] = num_class
        params["eval_metric"] = "mlogloss"
    else:
        params["eval_metric"] = "rmse"
    booster = xgb.train(
        params,
        dtrain,
        num_boost_round=config.n_estimators,
        evals=[(dvalid, "validation")],
        early_stopping_rounds=(
            config.early_stopping_rounds if config.early_stopping_rounds else None
        ),
        verbose_eval=False,
    )
    best = int(getattr(booster, "best_iteration", config.n_estimators - 1)) + 1
    if not config.early_stopping_rounds:
        best = config.n_estimators
    return booster, max(1, best)


def _lgb_train(
    config: GradientConfig,
    x_train: NDArray,
    y_train: NDArray,
    x_validation: NDArray,
    y_validation: NDArray,
    *,
    objective: str,
    num_class: int = 0,
) -> tuple[Any, int]:
    lgb = _module(GradientFamily.LIGHTGBM)
    # LightGBM names the same objectives differently from XGBoost.
    objective = {
        "reg:squarederror": "regression",
        "multi:softprob": "multiclass",
    }.get(objective, objective)
    dtrain = lgb.Dataset(x_train, label=y_train, free_raw_data=False)
    dvalid = lgb.Dataset(
        x_validation,
        label=y_validation,
        reference=dtrain,
        free_raw_data=False,
    )
    params: dict[str, Any] = {
        "objective": objective,
        "learning_rate": config.learning_rate,
        "num_leaves": config.num_leaves,
        "max_depth": config.max_depth,
        "feature_fraction": config.colsample_bytree,
        "bagging_fraction": config.subsample,
        "bagging_freq": 1,
        "min_data_in_leaf": config.min_child_samples,
        "min_sum_hessian_in_leaf": config.min_child_weight,
        "lambda_l1": config.reg_alpha,
        "lambda_l2": config.reg_lambda,
        "verbosity": -1,
        "num_threads": config.n_jobs,
        "seed": config.random_seed,
        "device_type": "cpu",
        "force_col_wise": True,
    }
    if num_class:
        params["num_class"] = num_class
        params["metric"] = "multi_logloss"
    else:
        params["metric"] = "rmse"
    callbacks: list[Any] = []
    if config.early_stopping_rounds:
        callbacks.append(
            lgb.early_stopping(
                config.early_stopping_rounds, verbose=False, min_delta=0.0
            )
        )
    booster = lgb.train(
        params,
        dtrain,
        num_boost_round=config.n_estimators,
        valid_sets=[dvalid],
        callbacks=callbacks,
    )
    best = int(getattr(booster, "best_iteration", 0) or 0)
    if not config.early_stopping_rounds or best <= 0:
        best = config.n_estimators
    return booster, max(1, best)


def _fit_booster(
    family: GradientFamily,
    config: GradientConfig,
    x_train: NDArray,
    y_train: NDArray,
    x_validation: NDArray,
    y_validation: NDArray,
    *,
    objective: str,
    num_class: int = 0,
) -> tuple[Any, int]:
    if family is GradientFamily.XGBOOST:
        return _xgb_train(
            config,
            x_train,
            y_train,
            x_validation,
            y_validation,
            objective=objective,
            num_class=num_class,
        )
    return _lgb_train(
        config,
        x_train,
        y_train,
        x_validation,
        y_validation,
        objective=objective,
        num_class=num_class,
    )


def _predict(
    family: GradientFamily,
    booster: Any,
    x: NDArray,
    iterations: int,
) -> NDArray:
    if family is GradientFamily.XGBOOST:
        xgb = _module(family)
        dmatrix = xgb.DMatrix(x, missing=np.nan)
        return cast(
            NDArray,
            booster.predict(dmatrix, iteration_range=(0, max(1, iterations))),
        )
    return cast(
        NDArray,
        booster.predict(x, num_iteration=max(1, iterations)),
    )


def _interval_from_residuals(
    residuals: NDArray, level: float
) -> IntervalCalibration:
    clean = residuals[np.isfinite(residuals)]
    if len(clean) < 3:
        return IntervalCalibration(
            level=level,
            lower_offset=0.0,
            upper_offset=0.0,
            samples=int(len(clean)),
            supported=False,
            method="unavailable: fewer than three validation residuals",
        )
    alpha = (1.0 - level) / 2.0
    low, high = np.quantile(clean, [alpha, 1.0 - alpha])
    return IntervalCalibration(
        level=level,
        lower_offset=min(float(low), 0.0),
        upper_offset=max(float(high), 0.0),
        samples=int(len(clean)),
    )


def _importance(
    family: GradientFamily,
    regressor: Any,
    names: Sequence[str],
) -> tuple[Mapping[str, Any], ...]:
    values: list[tuple[str, float]] = []
    if family is GradientFamily.XGBOOST:
        try:
            scores = regressor.get_score(importance_type="gain")
        except Exception:  # noqa: BLE001
            scores = {}
        for index, name in enumerate(names):
            key = name
            if key not in scores and f"f{index}" in scores:
                key = f"f{index}"
            values.append((name, float(scores.get(key, 0.0))))
    else:
        try:
            scores = regressor.feature_importance(importance_type="gain")
        except Exception:  # noqa: BLE001
            scores = np.zeros(len(names))
        values = [
            (name, float(scores[index]) if index < len(scores) else 0.0)
            for index, name in enumerate(names)
        ]
    total = sum(value for _, value in values) or 1.0
    ranked = sorted(values, key=lambda item: (-item[1], item[0]))
    return tuple(
        {
            "feature": name,
            "gain": round(value, 8),
            "gain_normalized": round(value / total, 8),
            "method": "gain",
            "rank": rank,
        }
        for rank, (name, value) in enumerate(ranked[: min(50, len(ranked))], 1)
    )


def _regression_metrics(
    rows: Sequence[GradientRow],
    indices: Sequence[int],
    predictions: NDArray,
    lower: NDArray,
    upper: NDArray,
    predicted_labels: Sequence[str],
    *,
    label: str,
    instrument_id: str,
    horizon: str,
    neutral_band_pct: float,
    intervals_supported: bool,
) -> StatisticalMetrics:
    prediction_rows = _rows_for_predictions(
        rows,
        indices,
        predictions.tolist(),
        predicted_labels=predicted_labels,
        lower_bounds=lower.tolist(),
        upper_bounds=upper.tolist(),
        model_id=label,
        horizon=horizon,
        instrument_id=instrument_id,
        intervals_supported=intervals_supported,
        neutral_band_pct=neutral_band_pct,
    )
    return evaluate_rows(
        prediction_rows,
        model_id=label,
        instrument_id=instrument_id,
        horizon=horizon,
    )


def _walk_forward_folds(
    *,
    family: GradientFamily,
    config: GradientConfig,
    matrix: GradientMatrix,
    instrument_id: str,
    horizon: ForecastHorizon,
    fold_count: int = 3,
) -> tuple[Mapping[str, Any], ...]:
    """Refit the validation-selected configuration on expanding windows.

    The held-out tail is cut into contiguous blocks.  For each block the model
    is fitted only on observations before it, with the final part of that past
    used for early stopping and interval calibration.  Earlier test blocks are
    legitimately available to later folds; this is walk-forward evaluation, not
    a random split.  The configuration itself was already selected on the
    initial validation window and is never reselected from a fold's test block.
    """
    ordered = sorted(int(index) for index in matrix.test_indices)
    if len(ordered) < 2:
        return ()
    fold_count = max(1, min(fold_count, len(ordered)))
    base_size, remainder = divmod(len(ordered), fold_count)
    blocks: list[list[int]] = []
    cursor = 0
    for fold in range(fold_count):
        size = base_size + (1 if fold < remainder else 0)
        blocks.append(ordered[cursor : cursor + size])
        cursor += size

    output: list[Mapping[str, Any]] = []
    for number, block in enumerate(blocks, 1):
        if not block:
            continue
        test_start = matrix.rows[block[0]].decision_at
        # Purge every training/validation row whose target extends into the
        # fold's test block. A chronological index split alone is not enough:
        # the label horizon spans future bars and would leak across the boundary.
        past_all = [
            index for index in range(block[0]) if index < len(matrix.rows)
        ]
        past = [
            index for index in past_all if matrix.rows[index].target_at < test_start
        ]
        purged = len(past_all) - len(past)
        validation_size = max(
            config.min_validation_samples, max(1, len(past) // 5)
        )
        if len(past) - validation_size < config.min_train_samples:
            output.append(
                {
                    "fold": number,
                    "status": "insufficient_data",
                    "train_samples": max(0, len(past) - validation_size),
                    "validation_samples": min(validation_size, len(past)),
                    "test_samples": len(block),
                    "purged_samples": purged,
                    "reason": "an expanding training prefix is below the minimum",
                }
            )
            continue
        train = past[:-validation_size]
        validation = past[-validation_size:]
        try:
            regressor, iterations = _fit_booster(
                family,
                config,
                matrix.x[train],
                matrix.y_return[train],
                matrix.x[validation],
                matrix.y_return[validation],
                objective="reg:squarederror",
            )
            validation_return = _predict(
                family, regressor, matrix.x[validation], iterations
            )
            interval = _interval_from_residuals(
                matrix.y_return[validation] - validation_return,
                config.interval_level,
            )
            test_return = _predict(family, regressor, matrix.x[block], iterations)
            lower = np.asarray(
                [interval.apply(float(value))[0] for value in test_return]
            )
            upper = np.asarray(
                [interval.apply(float(value))[1] for value in test_return]
            )
            labels: list[str] = []
            for value, lo, hi in zip(test_return, lower, upper, strict=False):
                direction, _ = classify_direction(
                    float(value),
                    float(lo),
                    float(hi),
                    neutral_band_pct=config.effective_neutral_band_pct,
                    interval_supported=interval.supported,
                )
                labels.append(direction.value)
            metrics = _regression_metrics(
                matrix.rows,
                block,
                test_return,
                lower,
                upper,
                labels,
                label=family.value,
                instrument_id=instrument_id,
                horizon=horizon.value,
                neutral_band_pct=config.effective_neutral_band_pct,
                intervals_supported=interval.supported,
            )
            output.append(
                {
                    "fold": number,
                    "status": "evaluated",
                    "train_samples": len(train),
                    "validation_samples": len(validation),
                    "test_samples": len(block),
                    "train_start": matrix.rows[train[0]].decision_at.isoformat(),
                    "train_end": matrix.rows[train[-1]].decision_at.isoformat(),
                    "train_target_end": matrix.rows[train[-1]].target_at.isoformat(),
                    "validation_target_end": matrix.rows[
                        validation[-1]
                    ].target_at.isoformat(),
                    "test_start": matrix.rows[block[0]].decision_at.isoformat(),
                    "test_end": matrix.rows[block[-1]].decision_at.isoformat(),
                    "purged_samples": purged,
                    "metrics": metrics.to_dict(),
                }
            )
        except Exception as exc:  # noqa: BLE001 - one fold cannot abort the report
            output.append(
                {
                    "fold": number,
                    "status": "failed",
                    "train_samples": len(train),
                    "validation_samples": len(validation),
                    "test_samples": len(block),
                    "purged_samples": purged,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
    return tuple(output)


def train_family(
    *,
    family: GradientFamily,
    config: GradientConfig,
    matrix: GradientMatrix,
    instrument_id: str,
    horizon: ForecastHorizon,
    as_of: datetime,
) -> FittedGradient:
    """Tune on validation, refit on train+validation and report test once."""
    if not backend_available(family):
        raise GradientTrainingError(
            f"{family.value} is not installed; install the optional prediction extra"
        )
    x_train, y_train, label_train, _ = matrix.split("train")
    x_validation, y_validation, label_validation, validation_indices = matrix.split(
        "validation"
    )
    x_test, _y_test, label_test, test_indices = matrix.split("test")
    if len(x_train) < config.min_train_samples:
        raise GradientTrainingError("training split is below the minimum")
    if len(x_validation) < config.min_validation_samples:
        raise GradientTrainingError("validation split is below the minimum")

    best: tuple[float, float, GradientConfig, Any, Any, int, int, NDArray] | None = None
    failures: list[str] = []
    for candidate in config.candidates()[: config.min_candidates]:
        try:
            regressor, reg_iterations = _fit_booster(
                family,
                candidate,
                x_train,
                y_train,
                x_validation,
                y_validation,
                objective="reg:squarederror",
            )
            validation_return = _predict(
                family, regressor, x_validation, reg_iterations
            )
            classifier = None
            classifier_iterations = 0
            try:
                classifier, classifier_iterations = _fit_booster(
                    family,
                    candidate,
                    x_train,
                    label_train.astype(np.float64),
                    x_validation,
                    label_validation.astype(np.float64),
                    objective="multi:softprob",
                    num_class=3,
                )
            except Exception as exc:  # noqa: BLE001 - classifier is additive
                failures.append(
                    f"{candidate.digest}: classifier: {type(exc).__name__}: {exc}"
                )
            residuals = y_validation - validation_return
            interval = _interval_from_residuals(residuals, candidate.interval_level)
            lower, upper = (
                np.asarray(
                    [
                        interval.apply(float(value))[0]
                        for value in validation_return
                    ]
                ),
                np.asarray(
                    [
                        interval.apply(float(value))[1]
                        for value in validation_return
                    ]
                ),
            )
            predicted_labels: list[str] = []
            for value, lo, hi in zip(
                validation_return, lower, upper, strict=False
            ):
                direction, _ = classify_direction(
                    float(value),
                    float(lo),
                    float(hi),
                    neutral_band_pct=candidate.effective_neutral_band_pct,
                    interval_supported=interval.supported,
                )
                predicted_labels.append(direction.value)
            metric = _regression_metrics(
                matrix.rows,
                validation_indices.tolist(),
                validation_return,
                lower,
                upper,
                predicted_labels,
                label=family.value,
                instrument_id=instrument_id,
                horizon=horizon.value,
                neutral_band_pct=candidate.effective_neutral_band_pct,
                intervals_supported=interval.supported,
            )
            score = (
                metric.price_mae
                if metric.price_mae is not None
                else float("inf")
            )
            directional_score = (
                metric.directional_accuracy
                if metric.directional_accuracy is not None
                else 0.0
            )
            if best is None or (score, -directional_score, candidate.digest) < (
                best[0],
                best[1],
                best[2].digest,
            ):
                best = (
                    score,
                    -directional_score,
                    candidate,
                    regressor,
                    classifier,
                    reg_iterations,
                    classifier_iterations,
                    validation_return,
                )
        except Exception as exc:  # noqa: BLE001 - try the next bounded candidate
            failures.append(
                f"{candidate.digest}: {type(exc).__name__}: {exc}"
            )
    if best is None:
        raise GradientTrainingError(
            f"{family.value} could not fit any candidate configuration: "
            + "; ".join(failures[:3])
        )
    (
        _,
        _,
        chosen,
        candidate_regressor,
        candidate_classifier,
        candidate_iterations,
        candidate_classifier_iterations,
        candidate_validation_return,
    ) = best
    residuals = y_validation - candidate_validation_return
    interval = _interval_from_residuals(residuals, chosen.interval_level)

    x_fit = np.vstack([x_train, x_validation])
    y_fit = np.concatenate([y_train, y_validation])
    label_fit = np.concatenate([label_train, label_validation])
    final_regressor, final_iterations = _fit_booster(
        family,
        chosen,
        x_fit,
        y_fit,
        x_validation,
        y_validation,
        objective="reg:squarederror",
    )
    final_classifier = None
    final_classifier_iterations = 0
    try:
        final_classifier, final_classifier_iterations = _fit_booster(
            family,
            chosen,
            x_fit,
            label_fit.astype(np.float64),
            x_validation,
            label_validation.astype(np.float64),
            objective="multi:softprob",
            num_class=3,
        )
    except Exception as exc:  # noqa: BLE001
        failures.append(f"final classifier: {type(exc).__name__}: {exc}")

    test_return = _predict(family, final_regressor, x_test, final_iterations)
    test_lower, test_upper = (
        np.asarray([interval.apply(float(value))[0] for value in test_return]),
        np.asarray([interval.apply(float(value))[1] for value in test_return]),
    )
    test_predicted_labels: list[str] = []
    for value, lo, hi in zip(test_return, test_lower, test_upper, strict=False):
        direction, _ = classify_direction(
            float(value),
            float(lo),
            float(hi),
            neutral_band_pct=chosen.effective_neutral_band_pct,
            interval_supported=interval.supported,
        )
        test_predicted_labels.append(direction.value)
    test_metrics = _regression_metrics(
        matrix.rows,
        test_indices.tolist(),
        test_return,
        test_lower,
        test_upper,
        test_predicted_labels,
        label=family.value,
        instrument_id=instrument_id,
        horizon=horizon.value,
        neutral_band_pct=chosen.effective_neutral_band_pct,
        intervals_supported=interval.supported,
    )
    validation_lower, validation_upper = (
        np.asarray(
            [interval.apply(float(value))[0] for value in candidate_validation_return]
        ),
        np.asarray(
            [interval.apply(float(value))[1] for value in candidate_validation_return]
        ),
    )
    validation_predicted_labels: list[str] = []
    for value, lo, hi in zip(
        candidate_validation_return, validation_lower, validation_upper, strict=False
    ):
        direction, _ = classify_direction(
            float(value),
            float(lo),
            float(hi),
            neutral_band_pct=chosen.effective_neutral_band_pct,
            interval_supported=interval.supported,
        )
        validation_predicted_labels.append(direction.value)
    validation_metrics = _regression_metrics(
        matrix.rows,
        validation_indices.tolist(),
        candidate_validation_return,
        validation_lower,
        validation_upper,
        validation_predicted_labels,
        label=family.value,
        instrument_id=instrument_id,
        horizon=horizon.value,
        neutral_band_pct=chosen.effective_neutral_band_pct,
        intervals_supported=interval.supported,
    )

    classifier_validation = None
    classifier_test = None
    # Validation classification evidence comes from the candidate classifier
    # fitted on the training prefix only.  The final classifier saw validation
    # labels during its refit, so reporting it as validation evidence would leak.
    if candidate_classifier is not None:
        try:
            proba_validation = _predict(
                family,
                candidate_classifier,
                x_validation,
                candidate_classifier_iterations,
            )
            classifier_validation = _classifier_metrics(
                [str(matrix.rows[index].label) for index in validation_indices],
                [
                    _INDEX_TO_LABEL.get(int(np.argmax(row)), "neutral")
                    for row in np.asarray(proba_validation).reshape(
                        len(validation_indices), -1
                    )
                ],
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"validation classifier metrics: {type(exc).__name__}: {exc}")
    if final_classifier is not None:
        try:
            proba_test = _predict(
                family, final_classifier, x_test, final_classifier_iterations
            )
            classifier_test = _classifier_metrics(
                [str(matrix.rows[index].label) for index in test_indices],
                [
                    _INDEX_TO_LABEL.get(int(np.argmax(row)), "neutral")
                    for row in np.asarray(proba_test).reshape(len(test_indices), -1)
                ],
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"test classifier metrics: {type(exc).__name__}: {exc}")

    regime_metrics: dict[str, dict[str, Any]] = {}
    test_positions = {
        int(index): position for position, index in enumerate(test_indices)
    }
    test_set = set(test_positions)
    for regime in sorted({matrix.rows[index].regime for index in test_indices}):
        selected = [
            index
            for index, row in enumerate(matrix.rows)
            if index in test_set and row.regime == regime
        ]
        if not selected:
            continue
        positions = [test_positions[index] for index in selected]
        sub = evaluate_rows(
            _rows_for_predictions(
                matrix.rows,
                selected,
                test_return[positions].tolist(),
                predicted_labels=[test_predicted_labels[pos] for pos in positions],
                lower_bounds=test_lower[positions].tolist(),
                upper_bounds=test_upper[positions].tolist(),
                model_id=family.value,
                horizon=horizon.value,
                instrument_id=instrument_id,
                intervals_supported=interval.supported,
                neutral_band_pct=chosen.effective_neutral_band_pct,
            ),
            model_id=family.value,
            instrument_id=instrument_id,
            horizon=horizon.value,
            regime=regime,
        )
        regime_metrics[regime] = sub.to_dict()

    notes = (
        "validation selected the candidate configuration; test was reported once",
        "walk-forward folds refit the selected configuration on expanding windows",
        "the interval is validation-residual based, not a calibrated probability",
        "classifier probabilities are withheld from the forecast contract",
        "no automatic promotion or execution authority",
    )
    walk_forward = _walk_forward_folds(
        family=family,
        config=chosen,
        matrix=matrix,
        instrument_id=instrument_id,
        horizon=horizon,
    )
    return FittedGradient(
        family=family,
        config=chosen,
        schema=matrix.schema,
        regressor=final_regressor,
        classifier=final_classifier,
        regressor_iterations=final_iterations,
        classifier_iterations=final_classifier_iterations,
        interval=interval,
        feature_importance=_importance(family, final_regressor, matrix.schema.names),
        feature_stats=matrix.feature_stats,
        validation=validation_metrics,
        test=test_metrics,
        classifier_validation=classifier_validation,
        classifier_test=classifier_test,
        train_samples=len(matrix.train_indices),
        validation_samples=len(matrix.validation_indices),
        test_samples=len(matrix.test_indices),
        training_start=min(
            (matrix.rows[index].decision_at for index in matrix.train_indices),
            default=None,
        ),
        training_end=max(
            (matrix.rows[index].decision_at for index in matrix.train_indices),
            default=None,
        ),
        regime_metrics=regime_metrics,
        regime_samples={
            regime: sum(1 for index in matrix.test_indices if matrix.rows[index].regime == regime)
            for regime in sorted({matrix.rows[index].regime for index in matrix.test_indices})
        },
        walk_forward=walk_forward,
        failures=tuple(failures),
        notes=notes,
    )


def baseline_metrics(
    *,
    rows: Sequence[GradientRow],
    test_indices: Sequence[int],
    closes: Sequence[float],
    price_bars: Sequence[Any],
    horizon: ForecastHorizon,
    resolution: DataInterval,
    instrument_id: str,
    neutral_band_pct: float,
    bar_id_to_index: Mapping[str, int] | None = None,
) -> dict[str, StatisticalMetrics]:
    """Score PI-05 baselines at the exact gradient test origins."""
    steps = horizon_steps(horizon, resolution)
    base_config = ModelConfig(
        neutral_band_pct=neutral_band_pct,
        min_observations=20,
    )
    output: dict[str, StatisticalMetrics] = {}
    origin_by_bar_id = dict(bar_id_to_index or {})
    if not origin_by_bar_id:
        origin_by_bar_id = {
            str(getattr(bar, "bar_id", "")): index
            for index, bar in enumerate(price_bars)
            if getattr(bar, "bar_id", "")
        }
    close_to_index = {
        (bar.observed_at, float(bar.close)): index for index, bar in enumerate(price_bars)
    }
    for model_id in ("naive_last", "random_walk", "sma", "ses", "arima"):
        predictions: list[float] = []
        actuals: list[GradientRow] = []
        lower: list[float] = []
        upper: list[float] = []
        for index in test_indices:
            row = rows[index]
            origin = origin_by_bar_id.get(str(row.sample.decision_bar_id))
            if origin is None:
                origin = close_to_index.get((row.sample.decision_at, row.decision_close))
            if origin is None:
                # The sample's decision bar may carry a session-close timestamp.
                origin = next(
                    (
                        probe
                        for probe in range(len(price_bars))
                        if float(price_bars[probe].close) == row.decision_close
                    ),
                    None,
                )
            if origin is None or origin < 1:
                continue
            try:
                forecast = forecast_model(
                    model_id,
                    [float(bar.close) for bar in price_bars[: origin + 1]],
                    steps,
                    base_config,
                    bars=price_bars[: origin + 1],
                )
            except Exception:  # noqa: BLE001 - baseline failure is not a gradient failure
                continue
            predicted = float(forecast.predicted_return)
            actuals.append(row)
            predictions.append(predicted)
            lower.append(float(forecast.lower_return if forecast.lower_return is not None else predicted))
            upper.append(float(forecast.upper_return if forecast.upper_return is not None else predicted))
        if not actuals:
            continue
        prediction_rows: list[PredictionRow] = []
        for position, row in enumerate(actuals):
            lo = min(lower[position], predictions[position])
            hi = max(upper[position], predictions[position])
            direction, _ = classify_direction(
                predictions[position],
                lo,
                hi,
                neutral_band_pct=neutral_band_pct,
                interval_supported=lo != hi,
            )
            prediction_rows.append(
                PredictionRow(
                    actual_price=row.target_close,
                    predicted_price=row.decision_close * (1.0 + predictions[position]),
                    actual_return=row.target_return,
                    predicted_return=predictions[position],
                    lower_return=lo,
                    upper_return=hi,
                    actual_direction=direction_label(row.target_return, neutral_band_pct),
                    predicted_direction=direction.value,
                    regime=row.regime,
                    horizon=horizon.value,
                    instrument_id=instrument_id,
                    model_id=model_id,
                    interval_supported=lo != hi,
                )
            )
        output[model_id] = evaluate_rows(
            prediction_rows,
            model_id=model_id,
            instrument_id=instrument_id,
            horizon=horizon.value,
        )
    return output


def predict_return(
    fitted: FittedGradient | Any,
    x: NDArray,
) -> tuple[float, dict[str, float] | None]:
    prediction = _predict(
        fitted.family,
        fitted.regressor,
        np.asarray(x, dtype=np.float64).reshape(1, -1),
        fitted.regressor_iterations,
    )
    value = float(np.asarray(prediction).reshape(-1)[0])
    probabilities: dict[str, float] | None = None
    if fitted.classifier is not None:
        try:
            raw = _predict(
                fitted.family,
                fitted.classifier,
                np.asarray(x, dtype=np.float64).reshape(1, -1),
                fitted.classifier_iterations,
            )
            row = np.asarray(raw).reshape(-1)
            if len(row) == 3:
                probabilities = {
                    label: float(row[index])
                    for index, label in enumerate(_LABELS)
                }
        except Exception:  # noqa: BLE001 - classifier is diagnostic
            probabilities = None
    # TreeSHAP contributions are intentionally NOT computed here: they are an
    # on-demand explanation, not part of ordinary inference latency.
    return value, probabilities


def local_contributions(fitted: FittedGradient | Any, x: NDArray) -> NDArray:
    """Exact tree SHAP contributions from the native boosters."""
    row = np.asarray(x, dtype=np.float64).reshape(1, -1)
    if fitted.family is GradientFamily.XGBOOST:
        xgb = _module(fitted.family)
        matrix = xgb.DMatrix(row, missing=np.nan)
        values = fitted.regressor.predict(
            matrix,
            pred_contribs=True,
            iteration_range=(0, max(1, fitted.regressor_iterations)),
        )
        return np.asarray(values, dtype=np.float64).reshape(-1)
    values = fitted.regressor.predict(
        row, num_iteration=max(1, fitted.regressor_iterations), pred_contrib=True
    )
    return np.asarray(values, dtype=np.float64).reshape(-1)


def feature_vector(
    feature_set: FeatureSet,
    schema: FeatureSchema,
    *,
    allow_imputed: bool = False,
) -> tuple[NDArray, dict[str, float | None], float]:
    """Map one PI-04 row into the persisted column order; refuse missing names."""
    missing = [name for name in schema.names if name not in feature_set.statuses]
    if missing:
        raise GradientValidationError(
            "feature schema mismatch: missing columns " + ", ".join(missing[:8])
        )
    values = _feature_row(feature_set, allow_imputed=allow_imputed)
    row: list[float] = []
    unavailable = 0
    for name in schema.names:
        value = values.get(name)
        if value is None:
            unavailable += 1
            row.append(np.nan)
        else:
            row.append(float(value))
    if len(row) != len(schema.names):
        raise GradientValidationError("feature row length does not match the schema")
    return (
        np.asarray(row, dtype=np.float64),
        {name: values.get(name) for name in schema.names},
        unavailable / max(1, len(schema.names)),
    )


def out_of_distribution(
    x: NDArray,
    stats: FeatureStats,
    names: Sequence[str],
    *,
    z_limit: float,
    max_fraction: float = 0.20,
) -> bool:
    exceed = 0
    usable = 0
    for index, name in enumerate(names):
        value = float(x[index])
        if not math.isfinite(value):
            continue
        scale = float(stats.scales.get(name, 0.0))
        if scale <= 1e-12:
            continue
        usable += 1
        if abs(value - float(stats.medians.get(name, 0.0))) / scale > z_limit:
            exceed += 1
    return usable > 0 and exceed / usable > max_fraction


def family_result(
    fitted: FittedGradient,
    *,
    version: str,
    artifact: Any,
    status: GradientStatus,
    selected: bool,
    reason: str = "",
) -> FamilyResult:
    return FamilyResult(
        family=fitted.family,
        version=version,
        status=status,
        config=fitted.config,
        schema=fitted.schema,
        validation=fitted.validation,
        test=fitted.test,
        classifier_validation=fitted.classifier_validation,
        classifier_test=fitted.classifier_test,
        interval=fitted.interval,
        feature_importance=fitted.feature_importance,
        artifact=artifact,
        train_samples=fitted.train_samples,
        validation_samples=fitted.validation_samples,
        test_samples=fitted.test_samples,
        training_start=fitted.training_start,
        training_end=fitted.training_end,
        dependency_version=backend_version(fitted.family),
        reason=reason,
        notes=fitted.notes,
        failures=fitted.failures,
        selected=selected,
        regime_metrics=fitted.regime_metrics,
        regime_samples=fitted.regime_samples,
        walk_forward=fitted.walk_forward,
    )


def selection_suitability(
    family: FamilyResult,
    baselines: Mapping[str, StatisticalMetrics],
    *,
    minimum_test_samples: int,
) -> dict[str, Any]:
    test = family.test
    if test is None or test.price_mae is None:
        return {
            "suitable": False,
            "status": "insufficient_data",
            "reason": "no finite test metrics",
        }
    baseline_scores = {
        name: metrics.price_mae
        for name, metrics in baselines.items()
        if metrics.price_mae is not None
    }
    best_baseline_name = (
        min(baseline_scores, key=baseline_scores.__getitem__)
        if baseline_scores
        else None
    )
    best_baseline = (
        baseline_scores[best_baseline_name]
        if best_baseline_name is not None
        else None
    )
    if best_baseline is None or best_baseline_name is None:
        return {
            "suitable": False,
            "status": "provisional",
            "reason": "no baseline comparison could be evaluated",
            "test_samples": test.samples,
        }
    improvement = (
        (best_baseline - test.price_mae) / best_baseline
        if best_baseline > 0
        else 0.0
    )
    # Compare direction with the *same* baseline that wins on price error;
    # taking the best direction from a different baseline would make the
    # comparison inconsistent with the selection metric.
    baseline_direction = baselines[best_baseline_name].directional_accuracy
    # Direction is compared with the baseline measured on the same origins;
    # there is deliberately no universal accuracy cut-off.
    direction_ok = test.directional_accuracy is not None and (
        baseline_direction is None
        or test.directional_accuracy >= baseline_direction
    )
    coverage_floor: float | None = None
    uncertainty_ok: bool | None = None
    interval = getattr(family, "interval", None)
    interval_level = float(getattr(interval, "level", 0.8) or 0.8)
    coverage = getattr(test, "interval_coverage", None)
    if coverage is not None and test.samples > 0:
        # A binomial lower bound at ~95% confidence turns "coverage looks low"
        # into a defensible check against the interval's nominal level.
        standard_error = math.sqrt(
            interval_level * (1.0 - interval_level) / test.samples
        )
        coverage_floor = max(0.0, interval_level - 1.96 * standard_error)
        uncertainty_ok = coverage >= coverage_floor
    if test.samples < minimum_test_samples:
        status = "provisional"
        suitable = False
        reason = "test sample is below the configured minimum"
    elif uncertainty_ok is False:
        status = "provisional"
        suitable = False
        reason = (
            f"test interval coverage {coverage:.1%} is below the "
            f"nominal {interval_level:.0%} interval's binomial lower bound"
        )
    elif improvement > 0.0 and direction_ok:
        status = "gradient_suitable"
        suitable = True
        reason = (
            f"test price MAE improves on the best baseline by {improvement:.1%} "
            "and directional accuracy is not below the best baseline"
        )
    else:
        status = "baselines_preferred"
        suitable = False
        reason = (
            f"test price MAE improvement over the best baseline is {improvement:.1%}; "
            "the baseline remains preferable"
        )
    return {
        "suitable": suitable,
        "status": status,
        "reason": reason,
        "test_samples": test.samples,
        "best_baseline_model": best_baseline_name,
        "best_baseline_price_mae": best_baseline,
        "price_mae": test.price_mae,
        "price_mae_improvement": improvement,
        "directional_accuracy": test.directional_accuracy,
        "best_baseline_directional_accuracy": baseline_direction,
        "direction_comparison": "at least the best baseline on the same test origins",
        "interval_coverage": coverage,
        "interval_level": interval_level,
        "coverage_floor": coverage_floor,
        "uncertainty_ok": uncertainty_ok,
        "train_samples": getattr(family, "train_samples", None),
        "validation_samples": getattr(family, "validation_samples", None),
        "test_samples": test.samples,
        "regime_samples": dict(getattr(family, "regime_samples", {}) or {}),
        "regime_metrics": {
            key: dict(value)
            for key, value in (getattr(family, "regime_metrics", {}) or {}).items()
        },
        "selection_basis": "out-of-sample test is reported, never used to select",
    }


__all__ = [
    "FittedGradient",
    "GradientMatrix",
    "GradientRow",
    "backend_available",
    "backend_version",
    "baseline_metrics",
    "build_training_matrix",
    "family_result",
    "feature_vector",
    "local_contributions",
    "out_of_distribution",
    "predict_return",
    "selection_suitability",
    "train_family",
]
