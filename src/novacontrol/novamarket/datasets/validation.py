"""PI-02 validation, provider selection, coverage and gap classification.

Everything here operates on bars that are already normalized and stored by the
existing NM-02 pipeline.  This module adds the dataset-level judgements that a
single bar cannot make:

* deterministic provider selection when two adapters observed the same instant;
* candle-boundary, duplicate, ordering and non-trading-day checks;
* coverage measured against a verified exchange calendar;
* explicit classification of every absent expected observation.

Two refusals are deliberate:

* a missing candle is never forward-filled or imputed into a trusted dataset;
* a weekday is never treated as a trading day unless a verified calendar says
  so.  Outside calendar coverage the answer is ``unverified``, not "holiday".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from novacontrol.novamarket.datasets.models import (
    CoverageSummary,
    DatasetQuality,
    DatasetValidationError,
    GapKind,
    GapRecord,
    MarketAnnotation,
    MissingSummary,
    SelectionRecord,
    ValidationSummary,
    identity_for,
    interval_duration,
    membership_digest,
    worst_dataset_quality,
)
from novacontrol.novamarket.marketdata.calendar import ExchangeCalendar, calendar_for
from novacontrol.novamarket.marketdata.models import (
    DataInterval,
    DataQuality,
    Instrument,
    OhlcvBar,
)

#: Historical bars are old by definition, so STALE is accepted here.  It still
#: ranks below VALID/WARNING when two providers observed the same instant, and
#: its presence makes the segment ``valid_with_warnings``.  PI-01's live
#: snapshots deliberately keep STALE out; a dataset is a different question.
_ACCEPTED_BAR_QUALITIES = frozenset(
    {DataQuality.VALID, DataQuality.WARNING, DataQuality.STALE}
)
_QUALITY_RANK = {DataQuality.VALID: 0, DataQuality.WARNING: 1, DataQuality.STALE: 2}


@dataclass(frozen=True, slots=True)
class SegmentAssessment:
    """The complete validated view of one instrument in a dataset build."""

    instrument: Instrument
    selected: tuple[OhlcvBar, ...]
    validation: ValidationSummary
    coverage: CoverageSummary
    missing: MissingSummary
    quality: DatasetQuality
    selections: tuple[SelectionRecord, ...]
    membership_digest: str
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": identity_for(self.instrument),
            "bar_count": len(self.selected),
            "validation": self.validation.to_dict(),
            "coverage": self.coverage.to_dict(),
            "missing": self.missing.to_dict(),
            "quality": self.quality.value,
            "selections": [item.to_dict() for item in self.selections],
            "membership_digest": self.membership_digest,
            "limitations": list(self.limitations),
        }


def _provider_rank(
    bar: OhlcvBar, provider_priority: Mapping[str, int]
) -> tuple[int, int, int, datetime, str, str]:
    """Deterministic ordering: quality, declared priority, completeness, age."""
    return (
        _QUALITY_RANK.get(bar.quality, 9),
        provider_priority.get(bar.provider, len(provider_priority)),
        0 if bar.volume is not None else 1,
        bar.retrieved_at,
        bar.provider,
        bar.bar_id,
    )


def select_bars(
    bars: Sequence[OhlcvBar],
    *,
    provider_priority: Sequence[str] = (),
    allow_partial: bool = False,
) -> tuple[tuple[OhlcvBar, ...], ValidationSummary, tuple[SelectionRecord, ...]]:
    """Choose one observation per timestamp and record every alternative.

    The order is fixed and does not depend on dictionary iteration:
    accepted quality, declared provider priority, non-null volume, earliest
    retrieval, provider name, bar id.  A lower-quality provider can therefore
    never displace a validated observation.
    """
    priority = {name: index for index, name in enumerate(provider_priority)}
    considered = len(bars)
    excluded_invalid = sum(1 for bar in bars if bar.quality is DataQuality.INVALID)
    excluded_suspect = sum(1 for bar in bars if bar.quality is DataQuality.SUSPECT)
    stale = sum(1 for bar in bars if bar.quality is DataQuality.STALE)
    excluded_partial = (
        0
        if allow_partial
        else sum(
            1
            for bar in bars
            if bar.partial
            and bar.quality not in (DataQuality.INVALID, DataQuality.SUSPECT)
        )
    )
    missing_volume = sum(1 for bar in bars if bar.volume is None)
    out_of_order = sum(
        1
        for previous, current in zip(bars, bars[1:])
        if current.observed_at < previous.observed_at
    )
    grouped: dict[datetime, list[OhlcvBar]] = {}
    for bar in bars:
        grouped.setdefault(bar.observed_at, []).append(bar)
    duplicate_timestamps = sum(max(len(group) - 1, 0) for group in grouped.values())

    selected: list[OhlcvBar] = []
    selections: list[SelectionRecord] = []
    conflicts = 0
    for observed_at in sorted(grouped):
        candidates = [
            bar
            for bar in grouped[observed_at]
            if bar.quality in _ACCEPTED_BAR_QUALITIES
            and (allow_partial or not bar.partial)
        ]
        if not candidates:
            continue
        candidates.sort(key=lambda bar: _provider_rank(bar, priority))
        winner = candidates[0]
        selected.append(winner)
        if len(candidates) > 1:
            conflicts += 1
            alternatives = tuple(
                f"{bar.provider}:{bar.bar_id}"
                for bar in candidates[1:]
            )
            selections.append(
                SelectionRecord(
                    instrument_id=winner.instrument_id,
                    observed_at=observed_at,
                    selected_bar_id=winner.bar_id,
                    selected_provider=winner.provider,
                    alternatives=alternatives,
                    reason=(
                        "selected by accepted quality, declared provider priority, "
                        "volume completeness, earliest retrieval and provider name"
                    ),
                )
            )

    reasons: list[str] = []
    if excluded_invalid:
        reasons.append(f"{excluded_invalid} structurally invalid observation(s) excluded")
    if excluded_suspect:
        reasons.append(f"{excluded_suspect} suspect observation(s) excluded")
    if excluded_partial:
        reasons.append(f"{excluded_partial} partial observation(s) excluded")
    if missing_volume:
        reasons.append(
            f"{missing_volume} observation(s) have no published volume; "
            "no zero or estimated volume was substituted"
        )
    if stale:
        reasons.append(
            f"{stale} observation(s) are stale relative to the live clock; "
            "age is expected for historical data and is recorded, not treated as a defect"
        )
    if duplicate_timestamps:
        reasons.append(
            f"{duplicate_timestamps} duplicate timestamp(s) across providers resolved"
        )
    if out_of_order:
        reasons.append(f"{out_of_order} out-of-order observation(s) reordered")
    validation = ValidationSummary(
        bars_considered=considered,
        bars_selected=len(selected),
        bars_excluded_invalid=excluded_invalid,
        bars_excluded_suspect=excluded_suspect,
        bars_excluded_partial=excluded_partial,
        bars_selected_partial=sum(1 for bar in selected if bar.partial),
        bars_missing_volume=missing_volume,
        duplicate_timestamps=duplicate_timestamps,
        out_of_order=out_of_order,
        provider_conflicts=conflicts,
        reasons=tuple(reasons),
    )
    return tuple(selected), validation, tuple(selections)


def _local_date(value: datetime, calendar: ExchangeCalendar) -> date:
    return calendar.local(value).date()


def _days(start: datetime, end: datetime, calendar: ExchangeCalendar) -> list[date]:
    """Calendar days touched by the instant range, not by a UTC date guess.

    A range ending at ``23:59 UTC`` is already the next local date in IST.  A
    trading day whose session opens after ``end`` is therefore not expected, or
    a 27-Jan request would demand a 28-Jan bar.  Non-trading dates are kept so
    coverage can count weekends and holidays as closures (never as gaps).
    """
    first = _local_date(start, calendar)
    last = _local_date(end, calendar)
    if (last - first).days > 3_700:
        raise DatasetValidationError("a dataset build is limited to ten years")
    result: list[date] = []
    for offset in range((last - first).days + 1):
        day = first + timedelta(days=offset)
        bounds = calendar.session_bounds(day)
        if bounds is None or (bounds[1] >= start and bounds[0] <= end):
            result.append(day)
    return result


def _session_complete(
    calendar: ExchangeCalendar, day: date, start: datetime, end: datetime
) -> bool:
    """Whether a session's full observation window is inside the request.

    A daily bar is only available after its session closes, and an intraday
    slot is only available after its slot ends.  A request that ends mid-session
    must not report the unfinished session as a provider gap.
    """
    bounds = calendar.session_bounds(day)
    return bounds is not None and bounds[0] >= start and bounds[1] <= end


def _verified_end_date(
    calendar: ExchangeCalendar, end: datetime
) -> date:
    """The last local date whose session could be observed before ``end``."""
    day = _local_date(end, calendar)
    bounds = calendar.session_bounds(day)
    if bounds is not None and bounds[0] > end:
        day -= timedelta(days=1)
    return day


def _calendar_verified(calendar: ExchangeCalendar | None, start: datetime, end: datetime) -> bool:
    if calendar is None:
        return False
    if not calendar.coverage_years:
        return False
    years = range(
        _local_date(start, calendar).year,
        _verified_end_date(calendar, end).year + 1,
    )
    return all(year in calendar.coverage_years for year in years)


def _session_slots(
    calendar: ExchangeCalendar,
    resolution: DataInterval,
    day: date,
) -> list[tuple[datetime, datetime]]:
    """Expected bar start/end instants for one covered trading day."""
    bounds = calendar.session_bounds(day)
    if bounds is None:
        return []
    open_at, close_at = bounds
    step = interval_duration(resolution)
    slots: list[tuple[datetime, datetime]] = []
    cursor = open_at
    while cursor < close_at:
        end = min(cursor + step, close_at)
        slots.append((cursor, end))
        cursor += step
    return slots


def _slot_kind(
    instrument_id: str,
    start: datetime,
    end: datetime,
    annotations: Sequence[MarketAnnotation],
) -> tuple[GapKind, str, bool]:
    for annotation in annotations:
        if annotation.covers(start, end):
            return annotation.gap_kind, f"annotation:{annotation.annotation_id}", annotation.verified
    return GapKind.MISSING_PROVIDER_OBSERVATION, "", True


def _collapse_daily(
    instrument_id: str,
    missing: Sequence[tuple[date, GapKind, str, bool]],
    calendar: ExchangeCalendar,
    resolution: DataInterval,
) -> list[GapRecord]:
    pending: list[tuple[date, GapKind, str, bool]] = []
    records: list[GapRecord] = []

    def flush() -> None:
        if not pending:
            return
        first = pending[0][0]
        last = pending[-1][0]
        kind = pending[0][1]
        verified = pending[0][3]
        detail = pending[0][2]
        start_at, _ = calendar.session_bounds(first) or (
            datetime.combine(first, time(0, 0), tzinfo=ZoneInfo(calendar.timezone)),
            datetime.combine(first, time(0, 0), tzinfo=ZoneInfo(calendar.timezone)),
        )
        end_bounds = calendar.session_bounds(last)
        end_at = end_bounds[1] if end_bounds else datetime.combine(
            last, time(0, 0), tzinfo=ZoneInfo(calendar.timezone)
        )
        records.append(
            GapRecord(
                instrument_id=instrument_id,
                kind=kind,
                start_at=start_at,
                end_at=end_at,
                count=len(pending),
                detail=detail or f"{len(pending)} expected {resolution.value} observation(s) absent",
                verified=verified,
            )
        )

    for row in missing:
        if pending and (
            row[0] - pending[-1][0] != timedelta(days=1) or row[1] is not pending[-1][1]
        ):
            flush()
            pending = []
        pending.append(row)
    flush()
    return records


def _intraday_gaps(
    instrument: Instrument,
    resolution: DataInterval,
    start: datetime,
    end: datetime,
    selected_dates: Mapping[date, set[datetime]],
    calendar: ExchangeCalendar,
    annotations: Sequence[MarketAnnotation],
    calendar_verified: bool,
    first_observed_at: datetime | None = None,
) -> list[GapRecord]:
    records: list[GapRecord] = []
    unverified_days: list[date] = []
    for day in _days(start, end, calendar):
        if not calendar.is_trading_day(day):
            continue
        # Intraday requests may begin or end inside a session, so the session
        # is not required to be wholly contained; the slots are clipped below.
        slots = [
            (slot_start, slot_end)
            for slot_start, slot_end in _session_slots(calendar, resolution, day)
            if slot_start >= start and slot_end <= end
        ]
        if not slots:
            continue
        if day.year not in calendar.coverage_years:
            if not selected_dates.get(day):
                unverified_days.append(day)
            continue
        observed = selected_dates.get(day, set())
        missing: list[tuple[datetime, datetime, GapKind, str, bool]] = []
        for slot_start, slot_end in slots:
            if slot_start in observed:
                continue
            kind, detail, verified = _slot_kind(
                instrument.instrument_id, slot_start, slot_end, annotations
            )
            if (
                kind is GapKind.MISSING_PROVIDER_OBSERVATION
                and first_observed_at is not None
                and slot_start < first_observed_at
            ):
                kind = GapKind.INCOMPLETE_COVERAGE
                detail = (
                    "requested range begins before the first stored observation; "
                    "the provider's historical depth is incomplete"
                )
                verified = True
            missing.append((slot_start, slot_end, kind, detail, verified))
        # Collapse adjacent missing slots of the same kind.
        pending: list[tuple[datetime, datetime, GapKind, str, bool]] = []
        step = interval_duration(resolution)

        def flush() -> None:
            if not pending:
                return
            records.append(
                GapRecord(
                    instrument_id=instrument.instrument_id,
                    kind=pending[0][2],
                    start_at=pending[0][0],
                    end_at=pending[-1][1],
                    count=len(pending),
                    detail=pending[0][3]
                    or f"{len(pending)} expected {resolution.value} observation(s) absent",
                    verified=pending[0][4],
                )
            )

        for row in missing:
            if pending and (
                row[0] != pending[-1][0] + step or row[2] is not pending[-1][2]
            ):
                flush()
                pending = []
            pending.append(row)
        flush()
    if unverified_days:
        zone = ZoneInfo(calendar.timezone)
        records.append(
            GapRecord(
                instrument_id=instrument.instrument_id,
                kind=GapKind.OUTSIDE_CALENDAR_COVERAGE,
                start_at=datetime.combine(unverified_days[0], time(0, 0), tzinfo=zone),
                end_at=datetime.combine(
                    unverified_days[-1], time(23, 59, 59), tzinfo=zone
                ),
                count=len(unverified_days),
                detail=(
                    f"{len(unverified_days)} day(s) fall outside verified calendar "
                    "coverage; absence cannot be called a holiday or a defect"
                ),
                verified=False,
            )
        )
    return records


def _bucket_within(
    calendar: ExchangeCalendar,
    day: date,
    resolution: DataInterval,
    start: datetime,
    end: datetime,
) -> bool:
    bounds = _bucket_bounds(calendar, day, resolution)
    return bounds is not None and bounds[0] >= start and bounds[1] <= end


def _bucket_bounds(
    calendar: ExchangeCalendar, day: date, resolution: DataInterval
) -> tuple[datetime, datetime] | None:
    """The first open and last close of a week/month bucket."""
    if resolution is DataInterval.WEEK_1:
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=6)
    elif resolution is DataInterval.MONTH_1:
        start = day.replace(day=1)
        end = (start.replace(year=start.year + 1, month=1) - timedelta(days=1)) if start.month == 12 else start.replace(month=start.month + 1) - timedelta(days=1)
    else:
        return None
    first: datetime | None = None
    last: datetime | None = None
    cursor = start
    while cursor <= end:
        bounds = calendar.session_bounds(cursor)
        if bounds is not None:
            first = first or bounds[0]
            last = bounds[1]
        cursor += timedelta(days=1)
    return (first, last) if first is not None and last is not None else None


def _bucket_gaps(
    instrument: Instrument,
    resolution: DataInterval,
    start: datetime,
    end: datetime,
    selected_dates: set[date],
    calendar: ExchangeCalendar,
    annotations: Sequence[MarketAnnotation],
    first_observed_date: date | None = None,
) -> list[GapRecord]:
    """Weekly/monthly absence classified per calendar bucket."""
    buckets: dict[tuple[int, int], list[date]] = {}
    for day in _days(start, end, calendar):
        if not _session_complete(calendar, day, start, end):
            continue
        if not calendar.is_trading_day(day):
            continue
        key = (
            (day.isocalendar().year, day.isocalendar().week)
            if resolution is DataInterval.WEEK_1
            else (day.year, day.month)
        )
        buckets.setdefault(key, []).append(day)
    records: list[GapRecord] = []
    zone = ZoneInfo(calendar.timezone)
    for key in sorted(buckets):
        days = buckets[key]
        bucket_bounds = _bucket_bounds(calendar, days[0], resolution)
        if bucket_bounds is None:
            continue
        if bucket_bounds[0] < start or bucket_bounds[1] > end:
            # A partial week/month at a request boundary is not an expected bar.
            continue
        if any(day in selected_dates for day in days):
            continue
        covered = [day for day in days if day.year in calendar.coverage_years]
        if not covered:
            kind = GapKind.OUTSIDE_CALENDAR_COVERAGE
            verified = False
            detail = "bucket falls outside verified calendar coverage"
        else:
            covered.sort()
            kind, detail, verified = _slot_kind(
                instrument.instrument_id,
                datetime.combine(covered[0], time(0, 0), tzinfo=zone),
                datetime.combine(covered[-1], time(23, 59, 59), tzinfo=zone),
                annotations,
            )
            if (
                kind is GapKind.MISSING_PROVIDER_OBSERVATION
                and first_observed_date is not None
                and days[-1] < first_observed_date
            ):
                kind = GapKind.INCOMPLETE_COVERAGE
                verified = True
                detail = (
                    "requested range begins before the first stored observation; "
                    "the provider's historical depth is incomplete"
                )
        records.append(
            GapRecord(
                instrument_id=instrument.instrument_id,
                kind=kind,
                start_at=datetime.combine(days[0], time(0, 0), tzinfo=zone),
                end_at=datetime.combine(days[-1], time(23, 59, 59), tzinfo=zone),
                count=1,
                detail=detail or f"no {resolution.value} bar covers this calendar bucket",
                verified=verified,
            )
        )
    return records


def classify_gaps(
    instrument: Instrument,
    resolution: DataInterval,
    start: datetime,
    end: datetime,
    selected: Sequence[OhlcvBar],
    *,
    annotations: Sequence[MarketAnnotation] = (),
) -> tuple[GapRecord, ...]:
    """Every absent expected observation, classified with measured evidence.

    Weekends and holidays are NOT emitted as gaps: nothing was expected, so
    nothing is missing.  They are still distinguished from a real absence by the
    coverage summary, which counts them as closures rather than defects.
    """
    calendar = calendar_for(instrument)
    if calendar is None:
        return ()
    verified = _calendar_verified(calendar, start, end)
    selected_dates: dict[date, set[datetime]] = {}
    for bar in selected:
        day = _local_date(bar.observed_at, calendar)
        selected_dates.setdefault(day, set()).add(bar.observed_at)
    first_observed_at = min((bar.observed_at for bar in selected), default=None)
    first_observed_date = (
        _local_date(first_observed_at, calendar)
        if first_observed_at is not None
        else None
    )

    if resolution is DataInterval.DAY_1:
        missing: list[tuple[date, GapKind, str, bool]] = []
        for day in _days(start, end, calendar):
            if not _session_complete(calendar, day, start, end):
                continue
            if day in selected_dates:
                continue
            if not calendar.is_trading_day(day):
                continue
            if day.year not in calendar.coverage_years:
                zone = ZoneInfo(calendar.timezone)
                bounds = calendar.session_bounds(day)
                slot_start, slot_end = bounds or (
                    datetime.combine(day, time(0, 0), tzinfo=zone),
                    datetime.combine(day, time(23, 59, 59), tzinfo=zone),
                )
                missing.append(
                    (
                        day,
                        GapKind.OUTSIDE_CALENDAR_COVERAGE,
                        "day falls outside verified calendar coverage",
                        False,
                    )
                )
                continue
            bounds = calendar.session_bounds(day)
            slot_start, slot_end = bounds or (start, end)
            kind, detail, kind_verified = _slot_kind(
                instrument.instrument_id, slot_start, slot_end, annotations
            )
            if (
                kind is GapKind.MISSING_PROVIDER_OBSERVATION
                and first_observed_date is not None
                and day < first_observed_date
            ):
                kind = GapKind.INCOMPLETE_COVERAGE
                detail = (
                    "requested range begins before the first stored observation; "
                    "the provider's historical depth is incomplete"
                )
                kind_verified = True
            missing.append((day, kind, detail, kind_verified))
        return tuple(
            _collapse_daily(instrument.instrument_id, missing, calendar, resolution)
        )

    if resolution in (DataInterval.WEEK_1, DataInterval.MONTH_1):
        return tuple(
            _bucket_gaps(
                instrument,
                resolution,
                start,
                end,
                set(selected_dates),
                calendar,
                annotations,
                first_observed_date=first_observed_date,
            )
        )

    return tuple(
        _intraday_gaps(
            instrument,
            resolution,
            start,
            end,
            selected_dates,
            calendar,
            annotations,
            verified,
            first_observed_at=first_observed_at,
        )
    )


def measure_coverage(
    instrument: Instrument,
    resolution: DataInterval,
    start: datetime,
    end: datetime,
    selected: Sequence[OhlcvBar],
) -> CoverageSummary:
    calendar = calendar_for(instrument)
    verified = _calendar_verified(calendar, start, end) if calendar else False
    covered_sessions = 0
    expected_sessions: int | None = None
    expected_bars: int | None = None
    closures = {"weekend": 0, "holiday": 0, "outside_coverage": 0}
    if calendar is not None:
        days = _days(start, end, calendar)
        if resolution.is_intraday:
            expected_days = [
                day
                for day in days
                if any(
                    slot_start >= start and slot_end <= end
                    for slot_start, slot_end in _session_slots(
                        calendar, resolution, day
                    )
                )
            ]
        else:
            expected_days = [
                day
                for day in days
                if _session_complete(calendar, day, start, end)
            ]
        expected_set = set(expected_days)
        observed_dates = {_local_date(bar.observed_at, calendar) for bar in selected}
        trading_days = 0
        for day in days:
            in_coverage = day.year in calendar.coverage_years
            if calendar.continuous:
                if day in expected_set:
                    trading_days += 1
                    if day in observed_dates:
                        covered_sessions += 1
                continue
            if day.weekday() >= 5:
                closures["weekend"] += 1
                continue
            if not in_coverage:
                closures["outside_coverage"] += 1
                if day in expected_set and day in observed_dates:
                    covered_sessions += 1
                continue
            if day in calendar.holidays:
                closures["holiday"] += 1
                continue
            if day in expected_set:
                trading_days += 1
                if day in observed_dates:
                    covered_sessions += 1
        expected_sessions = trading_days
        if resolution is DataInterval.DAY_1:
            expected_bars = trading_days
        elif resolution is DataInterval.WEEK_1:
            expected_bars = len(
                {
                    (day.isocalendar().year, day.isocalendar().week)
                    for day in expected_days
                    if day.year in calendar.coverage_years
                    and calendar.is_trading_day(day)
                    and _bucket_within(
                        calendar, day, resolution, start, end
                    )
                }
            )
        elif resolution is DataInterval.MONTH_1:
            expected_bars = len(
                {
                    (day.year, day.month)
                    for day in expected_days
                    if day.year in calendar.coverage_years
                    and calendar.is_trading_day(day)
                    and _bucket_within(
                        calendar, day, resolution, start, end
                    )
                }
            )
        else:
            expected_bars = 0
            for day in expected_days:
                if day.year not in calendar.coverage_years:
                    continue
                if not calendar.is_trading_day(day):
                    continue
                expected_bars += sum(
                    1
                    for slot_start, slot_end in _session_slots(
                        calendar, resolution, day
                    )
                    if slot_start >= start and slot_end <= end
                )
    ratio: float | None = None
    if expected_bars:
        ratio = min(len(selected) / expected_bars, 1.0)
    first = selected[0].observed_at if selected else None
    last = selected[-1].observed_at if selected else None
    limited = bool(
        selected
        and (
            first is not None
            and first > start + interval_duration(resolution)
            or last is not None
            and last < end - interval_duration(resolution)
        )
    )
    detail = (
        f"{closures['weekend']} weekend day(s), {closures['holiday']} exchange "
        f"holiday(s), {closures['outside_coverage']} unverified day(s); "
        f"{covered_sessions} observed session(s) of {expected_sessions}"
        if calendar is not None and expected_sessions is not None
        else "no verified exchange calendar; coverage cannot be measured"
    )
    return CoverageSummary(
        requested_start=start,
        requested_end=end,
        first_observed_at=first,
        last_observed_at=last,
        bar_count=len(selected),
        expected_bars=expected_bars,
        covered_sessions=covered_sessions,
        expected_sessions=expected_sessions,
        coverage_ratio=ratio,
        calendar_verified=verified,
        coverage_limited=limited,
        detail=detail,
    )


def _boundary_issues(
    instrument: Instrument,
    resolution: DataInterval,
    bars: Sequence[OhlcvBar],
) -> tuple[int, int]:
    """(misaligned candle boundaries, observations on non-trading days)."""
    calendar = calendar_for(instrument)
    if calendar is None:
        return 0, 0
    misaligned = 0
    non_trading = 0
    step = interval_duration(resolution)
    for bar in bars:
        local = calendar.local(bar.observed_at)
        day = local.date()
        verified_day = day.year in calendar.coverage_years
        if verified_day and not calendar.is_trading_day(day):
            non_trading += 1
        if resolution.is_intraday:
            if not verified_day:
                continue
            bounds = calendar.session_bounds(day)
            if bounds is None:
                continue
            open_at, close_at = bounds
            if not (open_at <= bar.observed_at < close_at):
                misaligned += 1
                continue
            offset = bar.observed_at - open_at
            if offset % step != timedelta(0):
                misaligned += 1
        else:
            bounds = calendar.session_bounds(day)
            if bounds is None:
                continue
            local_time = local.timetz().replace(tzinfo=None)
            if local_time not in (
                bounds[0].timetz().replace(tzinfo=None),
                bounds[1].timetz().replace(tzinfo=None),
            ):
                misaligned += 1
    return misaligned, non_trading


def assess_segment(
    instrument: Instrument,
    resolution: DataInterval,
    start: datetime,
    end: datetime,
    bars: Sequence[OhlcvBar],
    *,
    provider_priority: Sequence[str] = (),
    annotations: Sequence[MarketAnnotation] = (),
    min_coverage_ratio: float = 0.9,
    allow_partial: bool = False,
    extra_limitations: Sequence[str] = (),
) -> SegmentAssessment:
    """Validate, select, classify and grade one instrument's stored bars."""
    for bar in bars:
        if bar.instrument_id != instrument.instrument_id:
            raise DatasetValidationError(
                "a segment may contain only one instrument's observations"
            )
        if bar.interval is not resolution:
            raise DatasetValidationError(
                "a segment may contain only one resolution"
            )
        if bar.currency != instrument.currency:
            raise DatasetValidationError(
                f"bar currency {bar.currency} does not match instrument currency "
                f"{instrument.currency}; refusing to mix currencies"
            )
    # select_bars groups and sorts timestamps itself, so passing the original
    # order preserves the out-of-order evidence instead of erasing it first.
    selected, validation, selections = select_bars(
        bars,
        provider_priority=provider_priority,
        allow_partial=allow_partial,
    )
    misaligned, non_trading = _boundary_issues(instrument, resolution, selected)
    validation = ValidationSummary(
        bars_considered=validation.bars_considered,
        bars_selected=validation.bars_selected,
        bars_excluded_invalid=validation.bars_excluded_invalid,
        bars_excluded_suspect=validation.bars_excluded_suspect,
        bars_excluded_partial=validation.bars_excluded_partial,
        bars_selected_partial=validation.bars_selected_partial,
        bars_missing_volume=validation.bars_missing_volume,
        duplicate_timestamps=validation.duplicate_timestamps,
        out_of_order=validation.out_of_order,
        boundary_misaligned=misaligned,
        non_trading_day_observations=non_trading,
        provider_conflicts=validation.provider_conflicts,
        reasons=validation.reasons
        + tuple(
            reason
            for reason in (
                f"{misaligned} candle(s) outside a verified session boundary"
                if misaligned
                else "",
                f"{non_trading} observation(s) on a non-trading day"
                if non_trading
                else "",
            )
            if reason
        ),
    )
    coverage = measure_coverage(instrument, resolution, start, end, selected)
    gaps = classify_gaps(
        instrument, resolution, start, end, selected, annotations=annotations
    )
    by_kind: dict[str, int] = {}
    for gap in gaps:
        by_kind[gap.kind.value] = by_kind.get(gap.kind.value, 0) + gap.count
    missing = MissingSummary(
        total_missing=sum(by_kind.values()),
        by_kind=by_kind,
        gaps=gaps,
    )

    quality = DatasetQuality.VALID
    if validation.bars_considered and not selected:
        quality = DatasetQuality.INVALID
    elif not selected:
        quality = DatasetQuality.INCOMPLETE
    elif (
        coverage.coverage_ratio is not None
        and coverage.coverage_ratio < min_coverage_ratio
    ):
        quality = DatasetQuality.INCOMPLETE
    elif validation.provider_conflicts:
        quality = DatasetQuality.PROVIDER_CONFLICTED
    elif not coverage.calendar_verified:
        quality = DatasetQuality.UNVERIFIED
    elif (
        validation.bars_excluded_invalid
        or validation.bars_excluded_suspect
        or validation.bars_excluded_partial
        or validation.bars_missing_volume
        or validation.duplicate_timestamps
        or validation.boundary_misaligned
        or validation.non_trading_day_observations
        or validation.out_of_order
        or missing.total_missing
        or coverage.coverage_limited
        or any(bar.quality is DataQuality.STALE for bar in selected)
        or any(bar.partial for bar in selected)
    ):
        quality = DatasetQuality.VALID_WITH_WARNINGS

    limitations: list[str] = list(extra_limitations)
    if coverage.calendar_verified is False:
        limitations.append(
            "exchange calendar coverage does not verify every requested year; "
            "holiday/closure classification is reported as unverified"
        )
    if coverage.coverage_limited:
        limitations.append(
            "stored observations do not span the full requested range"
        )
    if validation.provider_conflicts:
        limitations.append(
            f"{validation.provider_conflicts} timestamp(s) had observations from "
            "more than one provider; the selected winner and the alternatives are recorded"
        )
    if any(not gap.verified for gap in gaps):
        limitations.append(
            "some gaps are unverified; they are presented as unverified rather than "
            "as exchange holidays or trading halts"
        )
    if not calendar_for(instrument):
        limitations.append(
            "no verified exchange calendar exists for this instrument; the dataset "
            "does not claim holiday or session facts it cannot verify"
        )
    return SegmentAssessment(
        instrument=instrument,
        selected=selected,
        validation=validation,
        coverage=coverage,
        missing=missing,
        quality=quality,
        selections=selections,
        membership_digest=membership_digest(selected),
        limitations=tuple(dict.fromkeys(limitations)),
    )


def combine_coverage(assessments: Sequence[SegmentAssessment]) -> CoverageSummary:
    """Aggregate per-instrument coverage without inventing an average ratio."""
    if not assessments:
        raise DatasetValidationError("at least one segment is required")
    start = min(item.coverage.requested_start for item in assessments)
    end = max(item.coverage.requested_end for item in assessments)
    counts = [item.coverage for item in assessments]
    expected = [item.expected_bars for item in counts if item.expected_bars is not None]
    ratio = None
    if expected and len(expected) == len(counts) and sum(expected) > 0:
        ratio = min(
            sum(item.coverage.bar_count for item in assessments) / sum(expected), 1.0
        )
    firsts = [
        item.coverage.first_observed_at
        for item in assessments
        if item.coverage.first_observed_at is not None
    ]
    lasts = [
        item.coverage.last_observed_at
        for item in assessments
        if item.coverage.last_observed_at is not None
    ]
    return CoverageSummary(
        requested_start=start,
        requested_end=end,
        first_observed_at=min(firsts) if firsts else None,
        last_observed_at=max(lasts) if lasts else None,
        bar_count=sum(item.coverage.bar_count for item in assessments),
        expected_bars=sum(expected) if expected and len(expected) == len(counts) else None,
        covered_sessions=sum(item.coverage.covered_sessions for item in assessments),
        expected_sessions=(
            sum(
                value
                for value in (
                    item.coverage.expected_sessions for item in assessments
                )
                if value is not None
            )
            if all(
                item.coverage.expected_sessions is not None
                for item in assessments
            )
            else None
        ),
        coverage_ratio=ratio,
        calendar_verified=all(item.coverage.calendar_verified for item in assessments),
        coverage_limited=any(item.coverage.coverage_limited for item in assessments),
        detail=(
            f"{len(assessments)} instrument segment(s); coverage is reported per "
            "segment and aggregated only where every segment can be measured"
        ),
    )


def combine_validation(
    assessments: Sequence[SegmentAssessment],
) -> ValidationSummary:
    return ValidationSummary(
        bars_considered=sum(item.validation.bars_considered for item in assessments),
        bars_selected=sum(item.validation.bars_selected for item in assessments),
        bars_excluded_invalid=sum(
            item.validation.bars_excluded_invalid for item in assessments
        ),
        bars_excluded_suspect=sum(
            item.validation.bars_excluded_suspect for item in assessments
        ),
        bars_excluded_partial=sum(
            item.validation.bars_excluded_partial for item in assessments
        ),
        bars_selected_partial=sum(
            item.validation.bars_selected_partial for item in assessments
        ),
        bars_missing_volume=sum(
            item.validation.bars_missing_volume for item in assessments
        ),
        duplicate_timestamps=sum(
            item.validation.duplicate_timestamps for item in assessments
        ),
        out_of_order=sum(item.validation.out_of_order for item in assessments),
        boundary_misaligned=sum(
            item.validation.boundary_misaligned for item in assessments
        ),
        non_trading_day_observations=sum(
            item.validation.non_trading_day_observations for item in assessments
        ),
        provider_conflicts=sum(
            item.validation.provider_conflicts for item in assessments
        ),
        reasons=tuple(
            dict.fromkeys(
                reason
                for item in assessments
                for reason in item.validation.reasons
            )
        ),
    )


def combine_missing(assessments: Sequence[SegmentAssessment]) -> MissingSummary:
    gaps = tuple(gap for item in assessments for gap in item.missing.gaps)
    by_kind: dict[str, int] = {}
    for gap in gaps:
        by_kind[gap.kind.value] = by_kind.get(gap.kind.value, 0) + gap.count
    return MissingSummary(
        total_missing=sum(by_kind.values()),
        by_kind=by_kind,
        gaps=gaps,
    )


def aggregate_quality(
    assessments: Sequence[SegmentAssessment], *, complete: bool
) -> DatasetQuality:
    quality = DatasetQuality.VALID
    for item in assessments:
        quality = worst_dataset_quality(quality, item.quality)
    if not complete:
        quality = worst_dataset_quality(quality, DatasetQuality.INCOMPLETE)
    return quality


def apply_provider_failure_annotations(
    instrument_id: str,
    start: datetime,
    end: datetime,
    failures: Iterable[str],
) -> tuple[MarketAnnotation, ...]:
    """Turn acquisition failures into unverified PROVIDER_OUTAGE hints.

    They are hints, not facts: the annotation is unverified, so a later build
    can distinguish "the provider failed during this run" from an exchange
    closure.
    """
    errors = tuple(str(item) for item in failures if str(item).strip())
    if not errors:
        return ()
    return (
        MarketAnnotation(
            instrument_id=instrument_id,
            kind="provider_outage",
            start_at=start,
            end_at=end,
            source="dataset-acquisition",
            verified=False,
            detail="; ".join(errors)[:1_000],
        ),
    )


__all__ = [
    "SegmentAssessment",
    "aggregate_quality",
    "apply_provider_failure_annotations",
    "assess_segment",
    "classify_gaps",
    "combine_coverage",
    "combine_missing",
    "combine_validation",
    "measure_coverage",
    "select_bars",
]
