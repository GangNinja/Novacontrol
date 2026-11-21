"""Leakage-safe chronological training-dataset construction (PI-02).

A sample is built from one dataset version only.  Features are the trailing
window whose observations are at or before the decision timestamp; the target is
the first observed bar whose *close* is at or after ``decision + horizon``.  A
target is emitted only when that bar is present in the same dataset version, so
a sample can never be fabricated from a future download.

Splitting is chronological and grouped by decision timestamp, then **purged**:
a training sample whose target reaches into the validation period is removed,
and likewise for validation targets reaching into the test period.  That makes
the stronger promise the phase requires: not just "training rows have earlier
timestamps", but "no training target overlaps a later evaluation window".
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from novacontrol.novamarket.datasets.models import (
    DatasetLeakageError,
    DatasetValidationError,
    SplitName,
    SplitSummary,
    TrainingDataset,
    TrainingSample,
    interval_duration,
)
from novacontrol.novamarket.datasets.validation import _ACCEPTED_BAR_QUALITIES
from novacontrol.novamarket.marketdata.models import DataInterval, OhlcvBar
from novacontrol.novamarket.prediction.models import ForecastHorizon, HORIZON_SPECS, horizon_spec


def _digest(payload: Mapping[str, Any] | Sequence[Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _close_instant(
    bar: OhlcvBar, resolution: DataInterval, calendar: Any | None
) -> datetime:
    """The instant the bar's information is fully available."""
    step = interval_duration(resolution)
    if calendar is not None and resolution in (
        DataInterval.DAY_1,
        DataInterval.WEEK_1,
        DataInterval.MONTH_1,
    ):
        bounds = calendar.session_bounds(calendar.local(bar.observed_at).date())
        if bounds is not None and bounds[1] >= bar.observed_at:
            close = bounds[1]
            if isinstance(close, datetime):
                return close
    return bar.observed_at + step


def _eligible(bar: OhlcvBar) -> bool:
    return (
        not bar.partial
        and bar.quality in _ACCEPTED_BAR_QUALITIES
        and bar.open > 0
        and bar.high >= bar.low
        and bar.low <= bar.close <= bar.high
    )


def _samples_for_instrument(
    instrument_id: str,
    resolution: DataInterval,
    horizon: ForecastHorizon,
    bars: Sequence[OhlcvBar],
    *,
    window_bars: int,
    calendar: Any | None,
) -> tuple[list[tuple[datetime, datetime, TrainingSample]], int]:
    """(decision, target, sample) triples plus how many lacked a target."""
    eligible = [bar for bar in bars if _eligible(bar)]
    if len(eligible) < window_bars + 1:
        return [], 0
    closes = [_close_instant(bar, resolution, calendar) for bar in eligible]
    spec = HORIZON_SPECS[horizon]
    samples: list[tuple[datetime, datetime, TrainingSample]] = []
    missing_targets = 0
    window_start_index = 0
    for index in range(window_bars - 1, len(eligible)):
        decision_bar = eligible[index]
        decision_at = closes[index]
        target_index = None
        swing_sessions = {
            ForecastHorizon.DAY_1: 1,
            ForecastHorizon.DAYS_3: 3,
            ForecastHorizon.DAYS_5: 5,
        }
        long_calendar_days = {
            ForecastHorizon.DAYS_30: 30,
            ForecastHorizon.DAYS_90: 90,
        }
        if resolution is DataInterval.DAY_1 and calendar is not None and (
            horizon in swing_sessions or horizon in long_calendar_days
        ):
            # Swing labels count verified sessions; long labels take the first
            # verified session at/after their calendar interval.  A gap in the
            # dataset must make the target unavailable rather than silently
            # lengthen the horizon by using the next observed row.
            decision_day = calendar.local(decision_bar.observed_at).date()
            if horizon in swing_sessions:
                target_day = _swing_target_day(
                    calendar, decision_day, swing_sessions[horizon]
                )
            else:
                target_day = _first_verified_day(
                    calendar,
                    decision_day + timedelta(days=long_calendar_days[horizon]),
                )
            target_index = _bar_index_for_day(
                calendar, eligible, index + 1, target_day
            ) if target_day is not None else None
        elif resolution is DataInterval.DAY_1 and horizon in swing_sessions:
            # Callers with no verified calendar keep the historical row-count
            # fallback; production dataset/training paths supply the calendar.
            candidate = index + swing_sessions[horizon]
            if candidate < len(closes):
                target_index = candidate
        else:
            required_at = decision_at + spec.duration
            for probe in range(index + 1, len(closes)):
                if closes[probe] >= required_at:
                    target_index = probe
                    break
        if target_index is None:
            missing_targets += 1
            continue
        target_bar = eligible[target_index]
        if target_bar.observed_at <= decision_bar.observed_at:
            missing_targets += 1
            continue
        window_start_index = max(index - window_bars + 1, 0)
        window = eligible[window_start_index : index + 1]
        samples.append(
            (
                decision_at,
                closes[target_index],
                TrainingSample(
                    instrument_id=instrument_id,
                    split=SplitName.TRAIN,
                    horizon=horizon,
                    decision_at=decision_at,
                    target_at=closes[target_index],
                    decision_bar_id=decision_bar.bar_id,
                    target_bar_id=target_bar.bar_id,
                    window_start_at=window[0].observed_at,
                    window_end_at=decision_bar.observed_at,
                    decision_close=decision_bar.close,
                    target_close=target_bar.close,
                    window_bar_ids=tuple(bar.bar_id for bar in window),
                ),
            )
        )
    return samples, missing_targets


def _swing_target_day(calendar: Any, start: date, sessions: int) -> date | None:
    """The ``sessions``-th verified trading day strictly after ``start``."""
    counted = 0
    for offset in range(1, 400):
        day = start + timedelta(days=offset)
        if day.year not in calendar.coverage_years:
            return None
        if not calendar.is_trading_day(day):
            continue
        counted += 1
        if counted == sessions:
            return day
    return None


def _first_verified_day(calendar: Any, start: date) -> date | None:
    """The first verified trading day at or after ``start``."""
    for offset in range(0, 20):
        day = start + timedelta(days=offset)
        if day.year not in calendar.coverage_years:
            return None
        if calendar.is_trading_day(day):
            return day
    return None


def _bar_index_for_day(
    calendar: Any,
    eligible: Sequence[OhlcvBar],
    start_index: int,
    target_day: date,
) -> int | None:
    """The exact bar observed on ``target_day``; a later bar is not a target."""
    for probe in range(start_index, len(eligible)):
        probe_day = calendar.local(eligible[probe].observed_at).date()
        if probe_day < target_day:
            continue
        return probe if probe_day == target_day else None
    return None


def _cut_index(samples: Sequence[tuple[datetime, datetime, TrainingSample]], index: int) -> int:
    """Move a cut point backwards so one decision timestamp never straddles."""
    if index <= 0:
        return 0
    if index >= len(samples):
        return len(samples)
    while index > 0 and samples[index - 1][0] == samples[index][0]:
        index -= 1
    return index


def _assign(
    samples: Sequence[tuple[datetime, datetime, TrainingSample]],
    cut: int,
    split: SplitName,
) -> list[TrainingSample]:
    return [
        TrainingSample(
            instrument_id=item.instrument_id,
            split=split,
            horizon=item.horizon,
            decision_at=item.decision_at,
            target_at=item.target_at,
            decision_bar_id=item.decision_bar_id,
            target_bar_id=item.target_bar_id,
            window_start_at=item.window_start_at,
            window_end_at=item.window_end_at,
            decision_close=item.decision_close,
            target_close=item.target_close,
            window_bar_ids=item.window_bar_ids,
        )
        for _, _, item in samples[:cut]
    ]


def _purge(
    samples: Sequence[TrainingSample], later_start: datetime | None
) -> tuple[list[TrainingSample], int]:
    if later_start is None:
        return list(samples), 0
    kept = [sample for sample in samples if sample.target_at < later_start]
    return kept, len(samples) - len(kept)


def _summary(
    split: SplitName,
    samples: Sequence[TrainingSample],
    purged: int,
) -> SplitSummary:
    return SplitSummary(
        split=split,
        start_at=min((item.decision_at for item in samples), default=None),
        end_at=max((item.decision_at for item in samples), default=None),
        sample_count=len(samples),
        instruments=tuple(sorted({item.instrument_id for item in samples})),
        purged_target_overlaps=purged,
        digest_value=_digest([item.digest_value for item in samples]),
    )


def _assert_chronology(
    train: Sequence[TrainingSample],
    validation: Sequence[TrainingSample],
    test: Sequence[TrainingSample],
) -> tuple[str, ...]:
    checks: list[str] = []
    for name, group in (("train", train), ("validation", validation), ("test", test)):
        if any(item.decision_at >= item.target_at for item in group):
            raise DatasetLeakageError(f"{name}: a target does not follow its decision")
        if any(item.window_end_at > item.decision_at for item in group):
            raise DatasetLeakageError(
                f"{name}: a feature observation is later than the decision timestamp"
            )
        checks.append(f"{name}: targets strictly after decisions")
        checks.append(f"{name}: features at or before decision")
    if train and validation and max(item.decision_at for item in train) >= min(
        item.decision_at for item in validation
    ):
        raise DatasetLeakageError("training decisions overlap the validation period")
    if validation and test and max(item.decision_at for item in validation) >= min(
        item.decision_at for item in test
    ):
        raise DatasetLeakageError("validation decisions overlap the test period")
    checks.append("chronological train < validation < test")
    validation_start = min((item.decision_at for item in validation), default=None)
    test_start = min((item.decision_at for item in test), default=None)
    if validation_start is not None and any(
        item.target_at >= validation_start for item in train
    ):
        raise DatasetLeakageError("a training target reaches into the validation period")
    if test_start is not None and any(
        item.target_at >= test_start for item in validation
    ):
        raise DatasetLeakageError("a validation target reaches into the test period")
    if test_start is not None and any(
        item.target_at >= test_start for item in train
    ):
        raise DatasetLeakageError("a training target reaches into the test period")
    checks.append("purged: no training target overlaps validation")
    checks.append("purged: no validation target overlaps test")
    checks.append("purged: no training target overlaps test")
    checks.append("test never used for training")
    return tuple(checks)


def build_training_dataset(
    *,
    dataset_id: str,
    dataset_version: str,
    resolution: DataInterval,
    horizon: ForecastHorizon | str,
    created_at: datetime,
    bars_by_instrument: Mapping[str, Sequence[OhlcvBar]],
    calendars: Mapping[str, Any] | None = None,
    window_bars: int = 60,
    train_ratio: float = 0.7,
    validation_ratio: float = 0.15,
    max_samples: int = 200_000,
    limitations: Sequence[str] = (),
) -> tuple[TrainingDataset, tuple[TrainingSample, ...]]:
    """Build a chronological, purged training set over one dataset version."""
    try:
        forecast_horizon = ForecastHorizon(horizon)
    except ValueError as exc:
        raise DatasetValidationError(f"unknown horizon: {horizon!r}") from exc
    spec = horizon_spec(forecast_horizon)
    if not spec.accepts_resolution(resolution):
        raise DatasetValidationError(
            f"{resolution.value} bars cannot support a {forecast_horizon.value} "
            f"target; the horizon needs {spec.max_input_interval.value} or finer data"
        )
    if window_bars < 2:
        raise DatasetValidationError("window_bars must be at least 2")
    if resolution is DataInterval.SPOT:
        raise DatasetValidationError("SPOT observations cannot form a training dataset")
    if not 0.0 < train_ratio < 1.0:
        raise DatasetValidationError("train_ratio must be between 0 and 1")
    if not 0.0 <= validation_ratio < 1.0:
        raise DatasetValidationError("validation_ratio must be between 0 and 1")
    if train_ratio + validation_ratio >= 1.0:
        raise DatasetValidationError(
            "train_ratio + validation_ratio must leave a non-empty test period"
        )
    rows: list[tuple[datetime, datetime, TrainingSample]] = []
    missing_targets = 0
    calendars = calendars or {}
    for instrument_id in sorted(bars_by_instrument):
        produced, missing = _samples_for_instrument(
            instrument_id,
            resolution,
            forecast_horizon,
            bars_by_instrument[instrument_id],
            window_bars=window_bars,
            calendar=calendars.get(instrument_id),
        )
        rows.extend(produced)
        missing_targets += missing
    if not rows:
        raise DatasetValidationError(
            "no leakage-safe samples could be constructed; the dataset has too "
            "little valid history for this window and horizon"
        )
    rows.sort(key=lambda item: (item[0], item[2].instrument_id, item[2].decision_bar_id))
    if len(rows) > max_samples:
        raise DatasetValidationError(
            f"the training set would exceed the {max_samples}-sample bound; narrow "
            "the instrument range or increase the minimum window"
        )
    total = len(rows)
    train_cut = _cut_index(rows, int(total * train_ratio))
    validation_cut = _cut_index(
        rows, int(total * (train_ratio + validation_ratio))
    )
    raw_train = _assign(rows[:train_cut], train_cut, SplitName.TRAIN)
    raw_validation = _assign(
        rows[train_cut:validation_cut], validation_cut - train_cut, SplitName.VALIDATION
    )
    raw_test = _assign(rows[validation_cut:], total - validation_cut, SplitName.TEST)

    validation_start = (
        min(item.decision_at for item in raw_validation) if raw_validation else None
    )
    test_start = min(item.decision_at for item in raw_test) if raw_test else None
    # If validation is empty, the next evaluation period is test.  Training
    # targets must still be purged against it; otherwise a zero validation
    # ratio would silently permit look-ahead into the test split.
    train_boundary = validation_start if validation_start is not None else test_start
    train, train_purged = _purge(raw_train, train_boundary)
    validation, validation_purged = _purge(raw_validation, test_start)
    checks = _assert_chronology(train, validation, raw_test)

    all_samples = tuple(train) + tuple(validation) + tuple(raw_test)
    if len(all_samples) != (
        len(train) + len(validation) + len(raw_test)
    ):  # pragma: no cover - structural guard
        raise DatasetLeakageError("a sample was assigned to more than one split")
    splits = {
        SplitName.TRAIN: _summary(SplitName.TRAIN, train, train_purged),
        SplitName.VALIDATION: _summary(
            SplitName.VALIDATION, validation, validation_purged
        ),
        SplitName.TEST: _summary(SplitName.TEST, raw_test, 0),
    }
    digest_payload = {
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "horizon": forecast_horizon.value,
        "resolution": resolution.value,
        "window_bars": window_bars,
        "train_ratio": train_ratio,
        "validation_ratio": validation_ratio,
        "samples": [item.digest_value for item in all_samples],
    }
    training_id = "tr_" + _digest(digest_payload)
    dataset_limitations = list(limitations)
    if missing_targets:
        dataset_limitations.append(
            f"{missing_targets} potential sample(s) omitted because no observed "
            "target bar existed after the decision"
        )
    if train_purged or validation_purged:
        dataset_limitations.append(
            f"{train_purged + validation_purged} sample(s) purged where a target "
            "would have overlapped a later split"
        )
    training = TrainingDataset(
        training_id=training_id,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        horizon=forecast_horizon,
        resolution=resolution,
        created_at=created_at,
        window_bars=window_bars,
        splits=splits,
        sample_count=len(all_samples),
        leakage_checks=checks,
        limitations=tuple(dict.fromkeys(dataset_limitations)),
    )
    return training, all_samples


def verify_stored_training(
    training: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
) -> tuple[bool, tuple[str, ...]]:
    """Re-verify a persisted training set without trusting its metadata."""
    problems: list[str] = []
    by_split: dict[str, list[Mapping[str, Any]]] = {
        "train": [],
        "validation": [],
        "test": [],
    }
    for sample in samples:
        split = str(sample.get("split", ""))
        if split not in by_split:
            problems.append(f"unknown split {split!r}")
            continue
        by_split[split].append(sample)
        try:
            decision = datetime.fromisoformat(str(sample["decision_at"]))
            target = datetime.fromisoformat(str(sample["target_at"]))
            window_start = datetime.fromisoformat(str(sample["window_start_at"]))
            window_end = datetime.fromisoformat(str(sample["window_end_at"]))
            if decision >= target:
                problems.append("a target does not follow its decision")
            if window_start > window_end:
                problems.append("a feature window starts after it ends")
            if window_end > decision:
                problems.append("a feature observation is later than its decision")
        except (KeyError, ValueError):
            problems.append("a sample is missing its timestamps")
    def _bounds(rows: Sequence[Mapping[str, Any]]) -> tuple[datetime | None, datetime | None]:
        decisions: list[datetime] = []
        for row in rows:
            try:
                decisions.append(datetime.fromisoformat(str(row["decision_at"])))
            except (KeyError, ValueError):
                continue
        return (min(decisions), max(decisions)) if decisions else (None, None)

    train_min, train_max = _bounds(by_split["train"])
    val_min, val_max = _bounds(by_split["validation"])
    test_min, _ = _bounds(by_split["test"])
    if train_max is not None and val_min is not None and train_max >= val_min:
        problems.append("training decisions overlap the validation period")
    if val_max is not None and test_min is not None and val_max >= test_min:
        problems.append("validation decisions overlap the test period")
    def _targets(rows: Sequence[Mapping[str, Any]]) -> list[datetime]:
        values: list[datetime] = []
        for row in rows:
            try:
                values.append(datetime.fromisoformat(str(row["target_at"])))
            except (KeyError, ValueError):
                continue
        return values

    if val_min is not None and any(
        target >= val_min for target in _targets(by_split["train"])
    ):
        problems.append("a training target reaches into the validation period")
    if test_min is not None and any(
        target >= test_min for target in _targets(by_split["validation"])
    ):
        problems.append("a validation target reaches into the test period")
    if test_min is not None and any(
        target >= test_min for target in _targets(by_split["train"])
    ):
        problems.append("a training target reaches into the test period")
    split_metadata = training.get("splits", {})
    if not isinstance(split_metadata, Mapping):
        problems.append("training split metadata is not an object")
        split_metadata = {}
    for split_name, rows in by_split.items():
        summary = split_metadata.get(split_name)
        if not isinstance(summary, Mapping):
            problems.append(f"training split metadata is missing {split_name!r}")
            continue
        try:
            if int(summary.get("sample_count", -1)) != len(rows):
                problems.append(
                    f"training split {split_name!r} count does not match its rows"
                )
        except (TypeError, ValueError):
            problems.append(f"training split {split_name!r} has an invalid count")
    try:
        expected = int(training.get("sample_count", len(samples)))
    except (TypeError, ValueError):
        problems.append("training sample_count is not an integer")
        expected = len(samples)
    if expected != len(samples):
        problems.append(
            f"stored sample count {expected} does not match {len(samples)} rows"
        )
    return not problems, tuple(problems)


__all__ = [
    "build_training_dataset",
    "verify_stored_training",
]
