"""Chronological LSTM windows built from the existing PI-02/PI-04 matrix.

No future observation can enter a window: every row used at position ``j`` was
observed at or before the decision timestamp of the final row, and every window
is ordered by the source timestamps.  Missing values stay NaN until the
training-fitted transform, which writes the median and an explicit mask.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

import numpy as np

from novacontrol.novamarket.features.models import FeatureProfileName, FeatureRequest
from novacontrol.novamarket.features.models import FeatureSet
from novacontrol.novamarket.gradient.models import FeatureSchema
from novacontrol.novamarket.gradient.training import GradientMatrix, _feature_row
from novacontrol.novamarket.marketdata.models import DataInterval
from novacontrol.novamarket.prediction.models import ForecastHorizon

from .models import LSTMUnavailableError, LSTMValidationError, SequenceScaler

NDArray = np.ndarray[Any, Any]


@dataclass(slots=True)
class SequenceSet:
    raw: NDArray
    targets: NDArray
    labels: NDArray
    positions: tuple[int, ...]
    sequence_length: int
    start_at: datetime | None
    end_at: datetime | None

    @property
    def samples(self) -> int:
        return int(len(self.positions))

    def __len__(self) -> int:
        return self.samples

    def __post_init__(self) -> None:
        if self.raw.ndim != 3:
            raise LSTMValidationError("sequence tensor must be [samples, time, features]")
        if self.raw.shape[1] != self.sequence_length:
            raise LSTMValidationError("sequence length does not match the tensor")
        if not (len(self.raw) == len(self.targets) == len(self.labels) == len(self.positions)):
            raise LSTMValidationError("sequence targets or positions are misaligned")
        if np.isinf(self.raw).any():
            raise LSTMValidationError("sequence input contains an infinite value")
        if not np.isfinite(self.targets).all():
            raise LSTMValidationError("sequence target contains a missing value")


def _ordered_rows(matrix: GradientMatrix) -> tuple[Any, ...]:
    rows = tuple(matrix.rows)
    if not rows:
        raise LSTMValidationError("the matrix contains no rows")
    for index, row in enumerate(rows):
        if row.target_at <= row.decision_at:
            raise LSTMValidationError(
                f"row {index} has a target at or before its decision timestamp"
            )
        if index:
            previous = rows[index - 1]
            if row.decision_at <= previous.decision_at:
                raise LSTMValidationError(
                    "matrix rows are not strictly chronological; refusing to build windows"
                )
    return rows


def build_sequences(
    matrix: GradientMatrix,
    sequence_length: int,
    *,
    indices: Sequence[int] | None = None,
    require_complete: bool = False,
) -> SequenceSet:
    """Build windows ending at the requested matrix rows.

    Overlapping input windows are intentional: the model predicts only the final
    row's future target, and the overlap is historical input, not target data.
    """
    sequence_length = int(sequence_length)
    if not 1 <= sequence_length <= 120:
        raise LSTMValidationError("sequence length must be within [1, 120]")
    rows = _ordered_rows(matrix)
    selected = tuple(
        int(index)
        for index in (range(len(rows)) if indices is None else indices)
    )
    raw: list[NDArray] = []
    targets: list[float] = []
    labels: list[int] = []
    positions: list[int] = []
    for index in sorted(dict.fromkeys(selected)):
        if index < 0 or index >= len(rows):
            raise LSTMValidationError(f"sequence end index {index} is outside the matrix")
        start = index - sequence_length + 1
        if start < 0:
            continue
        window_positions = tuple(range(start, index + 1))
        window = matrix.x[list(window_positions)]
        if np.isinf(window).any():
            raise LSTMValidationError(
                f"sequence ending at row {index} contains an infinite feature"
            )
        missing_fraction = float(np.isnan(window).mean())
        if require_complete and missing_fraction > 0:
            continue
        end = rows[index]
        # Recheck the timestamp contract on the actual window.  This catches a
        # caller-supplied matrix whose rows were reordered after construction.
        for position in window_positions:
            row = rows[position]
            if row.decision_at > end.decision_at:
                raise LSTMValidationError(
                    "future observation found in an input window; sequence generation aborted"
                )
            if row.target_at <= row.decision_at:
                raise LSTMValidationError(
                    "a training row has a non-future target; sequence generation aborted"
                )
        raw.append(window)
        targets.append(float(end.target_return))
        labels.append(int(matrix.y_label[index]))
        positions.append(index)
    if not raw:
        raise LSTMUnavailableError(
            f"no complete {sequence_length}-observation sequences were produced"
        )
    tensor = np.stack(raw, axis=0).astype(np.float64, copy=True)
    output = SequenceSet(
        raw=tensor,
        targets=np.asarray(targets, dtype=np.float64),
        labels=np.asarray(labels, dtype=np.int64),
        positions=tuple(positions),
        sequence_length=sequence_length,
        start_at=rows[positions[0]].decision_at if positions else None,
        end_at=rows[positions[-1]].decision_at if positions else None,
    )
    return output


def fit_scaler(sequence: SequenceSet, schema: FeatureSchema) -> SequenceScaler:
    """Fit every preprocessing parameter on the training windows only."""
    if sequence.raw.shape[2] != len(schema.names):
        raise LSTMValidationError("training sequences do not match the feature schema")
    medians: dict[str, float] = {}
    scales: dict[str, float] = {}
    for column, name in enumerate(schema.names):
        values = sequence.raw[:, :, column]
        finite = values[np.isfinite(values)]
        if not len(finite):
            medians[name] = 0.0
            scales[name] = 1.0
            continue
        median = float(np.median(finite))
        q1, q3 = np.quantile(finite, [0.25, 0.75])
        iqr = float(q3 - q1)
        scale = iqr / 1.349 if iqr > 1e-12 else float(np.std(finite))
        medians[name] = median if math.isfinite(median) else 0.0
        scales[name] = scale if math.isfinite(scale) and scale > 1e-12 else 1.0
    target_mean = float(np.mean(sequence.targets))
    target_std = float(np.std(sequence.targets))
    if not math.isfinite(target_mean):
        target_mean = 0.0
    if not math.isfinite(target_std) or target_std <= 1e-12:
        target_std = max(1e-6, float(np.mean(np.abs(sequence.targets))) or 1.0)
    return SequenceScaler(
        schema=schema,
        medians=medians,
        scales=scales,
        target_mean=target_mean,
        target_std=target_std,
    )


def scaled_sequence(
    sequence: SequenceSet, scaler: SequenceScaler
) -> tuple[NDArray, NDArray, NDArray]:
    values, mask = scaler.transform(sequence.raw)
    targets = (sequence.targets - scaler.target_mean) / scaler.target_std
    if not np.isfinite(targets).all():
        raise LSTMValidationError("scaled targets contain a non-finite value")
    return values, mask, targets


def _inference_feature_row(
    *,
    engine: Any,
    instrument_id: str,
    resolution: DataInterval,
    profile: FeatureProfileName,
    as_of: datetime,
    bars: Sequence[Any],
    fundamentals: Sequence[Any],
    news_events: Sequence[Any],
    benchmark_bars: Mapping[str, Sequence[Any]] | None = None,
    sector_bars: Mapping[str, Sequence[Any]] | None = None,
    context_bars: Mapping[str, Mapping[str, Sequence[Any]]] | None = None,
) -> tuple[dict[str, float | None], FeatureSet]:
    prefix = [bar for bar in bars if bar.observed_at <= as_of]
    if not prefix:
        raise LSTMUnavailableError(
            f"no observation is available at or before {as_of.isoformat()}"
        )
    request = FeatureRequest(
        instrument_id=instrument_id,
        as_of=as_of,
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
    return _feature_row(result.feature_set), result.feature_set


def build_inference_sequence(
    *,
    engine: Any,
    instrument_id: str,
    resolution: DataInterval,
    profile: FeatureProfileName,
    as_of: datetime,
    bars: Sequence[Any],
    schema: FeatureSchema,
    sequence_length: int,
    fundamentals: Sequence[Any] = (),
    news_events: Sequence[Any] = (),
    benchmark_bars: Mapping[str, Sequence[Any]] | None = None,
    sector_bars: Mapping[str, Sequence[Any]] | None = None,
    context_bars: Mapping[str, Mapping[str, Sequence[Any]]] | None = None,
) -> tuple[NDArray, dict[str, float | None], FeatureSet, float]:
    """Create the latest inference window using the model's exact schema."""
    eligible = [bar for bar in bars if bar.observed_at <= as_of]
    if len(eligible) < sequence_length:
        raise LSTMUnavailableError(
            f"needs {sequence_length} observations but only {len(eligible)} are available"
        )
    selected = eligible[-sequence_length:]
    for previous, current in zip(selected, selected[1:]):
        if current.observed_at <= previous.observed_at:
            raise LSTMUnavailableError("observations are not strictly chronological")
    rows: list[list[float]] = []
    last_values: dict[str, float | None] = {}
    last_set: FeatureSet | None = None
    missing = 0
    for bar in selected:
        values, feature_set = _inference_feature_row(
            engine=engine,
            instrument_id=instrument_id,
            resolution=resolution,
            profile=profile,
            as_of=bar.observed_at,
            bars=eligible,
            fundamentals=fundamentals,
            news_events=news_events,
            benchmark_bars=benchmark_bars,
            sector_bars=sector_bars,
            context_bars=context_bars,
        )
        missing_names = [name for name in schema.names if name not in values]
        if missing_names:
            raise LSTMUnavailableError(
                "feature schema mismatch: missing columns "
                + ", ".join(missing_names[:8])
            )
        row = []
        clean_values: dict[str, float | None] = {}
        for name in schema.names:
            value = values.get(name)
            numeric: float | None = None
            if value is not None:
                try:
                    candidate = float(value)
                except (TypeError, ValueError):
                    candidate = np.nan
                if math.isfinite(candidate):
                    numeric = candidate
            if numeric is None:
                missing += 1
                row.append(np.nan)
                clean_values[name] = None
            else:
                row.append(numeric)
                clean_values[name] = numeric
        rows.append(row)
        last_values = clean_values
        last_set = feature_set
    if last_set is None:
        raise LSTMUnavailableError("the inference feature sequence is empty")
    missing_fraction = missing / max(1, len(rows) * len(schema.names))
    tensor = np.asarray(rows, dtype=np.float64)[None, :, :]
    return tensor, last_values, last_set, missing_fraction


__all__ = [
    "SequenceSet",
    "build_inference_sequence",
    "build_sequences",
    "fit_scaler",
    "scaled_sequence",
]
