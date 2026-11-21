"""Deterministic, point-in-time feature generation.

The engine composes the existing market-data read door (PI-03), the dataset
version metadata (PI-02), the prediction contracts (PI-01) and the one
technical-indicator module.  It does not acquire prices, write to the market
data store, or make any trading decision.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from novacontrol.novamarket.datasets.models import interval_duration
from novacontrol.novamarket.features.definitions import (
    FEATURE_SPECS,
    feature_spec,
    profile_features,
)
from novacontrol.novamarket.features.models import (
    FEATURE_CONTRACT_VERSION,
    FEATURE_SET_VERSION,
    FeatureFamily,
    FeatureGenerationResult,
    FeatureProfileName,
    FeatureQuality,
    FeatureRequest,
    FeatureSet,
    FeatureSpec,
    FeatureStatus,
    FeatureValidationError,
    FundamentalFeatureResult,
    NewsFeatureResult,
)
from novacontrol.novamarket.features.regime import RegimeDetector
from novacontrol.novamarket.marketdata.calendar import calendar_by_mic
from novacontrol.novamarket.marketdata.models import DataInterval, DataQuality, Instrument, OhlcvBar
from novacontrol.novamarket.prediction.models import FundamentalSnapshot, NewsEventEvidence

_ACCEPTED_QUALITY = frozenset(
    {DataQuality.VALID, DataQuality.WARNING, DataQuality.STALE}
)
_INTERVAL_RANK = {
    "1m": 0,
    "5m": 1,
    "15m": 2,
    "30m": 3,
    "1h": 4,
    "1d": 5,
    "1wk": 6,
    "1mo": 7,
}
_DAY = timedelta(days=1)


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(slots=True)
class _BarSeries:
    resolution: DataInterval
    bars: tuple[OhlcvBar, ...]
    invalid: int = 0
    partial: int = 0
    future: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    gaps: int = 0
    unavailable_reason: str = ""

    @property
    def available(self) -> bool:
        return bool(self.bars)

    @property
    def source_first_at(self) -> datetime | None:
        return self.bars[0].observed_at if self.bars else None

    @property
    def source_last_at(self) -> datetime | None:
        return self.bars[-1].observed_at if self.bars else None

    @property
    def retrieved_at(self) -> datetime | None:
        return max((bar.retrieved_at for bar in self.bars), default=None)


@dataclass(slots=True)
class _Values:
    values: dict[str, float] = field(default_factory=dict)
    statuses: dict[str, FeatureStatus] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)

    def mark(self, name: str, status: FeatureStatus, reason: str = "") -> None:
        self.statuses[name] = status
        if reason:
            self.reasons[name] = reason

    def put(
        self,
        name: str,
        value: Any,
        *,
        zero_status: bool = True,
        status: FeatureStatus | None = None,
        reason: str = "",
    ) -> None:
        number = _float(value)
        if status is not None and number is None:
            self.mark(name, status, reason)
            return
        if number is None:
            if value is None:
                self.mark(name, FeatureStatus.MISSING, reason or "the required input is unavailable")
            else:
                self.mark(name, FeatureStatus.INVALID, reason or "value is not finite")
            return
        self.values[name] = number
        if status is not None:
            self.statuses[name] = status
            if reason:
                self.reasons[name] = reason
        elif zero_status and number == 0:
            self.statuses[name] = FeatureStatus.ZERO
        else:
            self.statuses[name] = FeatureStatus.VALUE

    def merge_tf(self, prefix: str, other: "_Values") -> None:
        for name, value in other.values.items():
            self.values[f"{prefix}.{name}"] = value
        for name, status in other.statuses.items():
            self.statuses[f"{prefix}.{name}"] = status
        for name, reason in other.reasons.items():
            self.reasons[f"{prefix}.{name}"] = reason


class FeatureEngine:
    """Build feature rows without writing or authorising anything."""

    def __init__(
        self,
        *,
        market_data: Any = None,
        dataset_store: Any = None,
        clock: Any = None,
        cache_size: int = 256,
        regime: Any = None,
    ) -> None:
        self._market_data = market_data
        self._dataset_store = dataset_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._regime = regime or RegimeDetector()
        self._cache: dict[str, FeatureSet] = {}
        self._cache_order: deque[str] = deque()
        self._cache_size = max(0, int(cache_size))
        self._last_error = ""

    # -- public API -----------------------------------------------------------

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def cache_entries(self) -> int:
        return len(self._cache)

    def clear_cache(self) -> None:
        self._cache.clear()
        self._cache_order.clear()

    def definitions(self, profile: FeatureProfileName | str | None = None) -> tuple[FeatureSpec, ...]:
        if profile is None:
            return tuple(FEATURE_SPECS.values())
        return profile_features(profile)

    def definition_payload(self) -> dict[str, Any]:
        definitions = {
            name: spec.to_dict() for name, spec in sorted(FEATURE_SPECS.items())
        }
        return {
            "feature_set_version": FEATURE_SET_VERSION,
            "contract_version": FEATURE_CONTRACT_VERSION,
            "definition_digest": _digest(definitions),
            "feature_count": len(definitions),
            "definitions": definitions,
        }

    def generate(
        self,
        request: FeatureRequest,
        *,
        bars: Mapping[str, Sequence[OhlcvBar]] | None = None,
        benchmark_bars: Mapping[str, Sequence[OhlcvBar]] | None = None,
        sector_bars: Mapping[str, Sequence[OhlcvBar]] | None = None,
        context_bars: Mapping[str, Mapping[str, Sequence[OhlcvBar]]] | None = None,
        fundamentals: Sequence[FundamentalSnapshot] = (),
        news_events: Sequence[Any] = (),
        previous_regime: Any = None,
        instrument: Instrument | None = None,
    ) -> FeatureGenerationResult:
        """Generate one immutable row, or return the cached identical row.

        ``bars`` and context are optional direct inputs for dataset/tests; when
        absent the existing market-data read door is used.  Nothing is fetched
        from a provider here.
        """
        self._last_error = ""
        source = self._instrument(request, instrument)
        calendar = calendar_by_mic(source.mic) if source is not None else None
        requested = request.requested_timeframes
        series: dict[str, _BarSeries] = {}
        derived_timeframes: dict[str, str] = {}
        for resolution in requested:
            supplied = _lookup_bars(bars, resolution)
            if supplied is None:
                supplied = self._read_bars(request.instrument_id, resolution, request)
            prepared = self._prepare_series(
                supplied or (),
                resolution,
                request.as_of,
                calendar,
                max_bars=request.max_bars,
            )
            if not prepared.available:
                for finer in _finer_resolutions(resolution):
                    fine_bars = _lookup_bars(bars, finer)
                    if fine_bars is None:
                        fine_bars = self._read_bars(request.instrument_id, finer, request)
                    if not fine_bars:
                        continue
                    fine_series = self._prepare_series(
                        fine_bars,
                        finer,
                        request.as_of,
                        calendar,
                        max_bars=request.max_bars,
                    )
                    if not fine_series.available:
                        continue
                    aggregated = _aggregate_bars(
                        fine_series.bars,
                        resolution,
                        request.as_of,
                        calendar,
                    )
                    if not aggregated:
                        continue
                    prepared = self._prepare_series(
                        aggregated,
                        resolution,
                        request.as_of,
                        calendar,
                        max_bars=request.max_bars,
                    )
                    if prepared.available:
                        derived_timeframes[resolution] = finer
                        break
            series[resolution] = prepared

        # The primary row must stay on the requested resolution.  Falling back
        # to another timeframe would label daily values as an intraday row.
        primary = series.get(request.resolution)
        if primary is None:
            primary = _BarSeries(DataInterval(request.resolution), ())

        profile = request.profile
        expected = profile_features(profile)
        computed = _Values()
        for spec in expected:
            computed.mark(spec.name, FeatureStatus.UNAVAILABLE, "not generated yet")
        self._calculate(primary, computed, expected, calendar)

        timeframes: dict[str, dict[str, float]] = {}
        timeframe_statuses: dict[str, dict[str, FeatureStatus]] = {}
        timeframe_reasons: dict[str, str] = {}
        for resolution, item in series.items():
            row = _Values()
            core = _core_specs()
            for spec in core:
                row.mark(spec.name, FeatureStatus.UNAVAILABLE, "not generated yet")
            if item.available:
                self._calculate_core(item, row, calendar)
                timeframes[resolution] = dict(row.values)
                timeframe_statuses[resolution] = dict(row.statuses)
                timeframe_reasons[resolution] = _digest(
                    {"reasons": row.reasons, "bars": len(item.bars)}
                )
            else:
                timeframe_statuses[resolution] = {
                    spec.name: FeatureStatus.UNAVAILABLE for spec in core
                }
                timeframes[resolution] = {}
                timeframe_reasons[resolution] = item.unavailable_reason or "no validated bars"

        context = self._context_features(
            request,
            primary,
            benchmark_bars=benchmark_bars,
            sector_bars=sector_bars,
            context_bars=context_bars,
            calendar=calendar,
        )
        for name, value in context.values.items():
            if name in computed.statuses:
                computed.put(name, value, zero_status=False, reason=context.reasons.get(name, ""))
        for name, status in context.statuses.items():
            if name in computed.statuses and name not in context.values:
                computed.mark(name, status, context.reasons.get(name, ""))

        fundamental_result = self._fundamental_features(request, fundamentals, primary)
        for name, value in fundamental_result.values.items():
            if name in computed.statuses:
                computed.put(name, value, reason=fundamental_result.reasons.get(name, ""))
        for name, status in fundamental_result.statuses.items():
            if name in computed.statuses and name not in fundamental_result.values:
                computed.mark(name, status, fundamental_result.reasons.get(name, ""))

        news_result = self._news_features(request, news_events, primary)
        for name, value in news_result.values.items():
            if name in computed.statuses:
                computed.put(name, value, reason=news_result.reasons.get(name, ""))
        for name, status in news_result.statuses.items():
            if name in computed.statuses and name not in news_result.values:
                computed.mark(name, status, news_result.reasons.get(name, ""))

        self._multi_timeframe_features(
            computed,
            primary,
            series,
            timeframes,
            request,
        )

        profile_names = tuple(spec.name for spec in expected)
        profile_name_set = set(profile_names)
        values = {
            name: value for name, value in computed.values.items() if name in profile_name_set
        }
        available = sum(
            1
            for name in profile_names
            if computed.statuses.get(name) in (FeatureStatus.VALUE, FeatureStatus.ZERO)
        )
        expected_count = len(profile_names)
        completeness = (available / expected_count) if expected_count else 0.0
        quality = self._quality(primary, completeness, available, expected_count)
        source_last = max(
            (item.source_last_at for item in series.values() if item.source_last_at),
            default=None,
        )
        source_first = min(
            (item.source_first_at for item in series.values() if item.source_first_at),
            default=None,
        )
        retrieved = max(
            (item.retrieved_at for item in series.values() if item.retrieved_at),
            default=None,
        )
        now = self._clock()
        generated_at = now if now >= request.as_of else request.as_of
        definition_payload = self.definition_payload()
        input_digest = _digest(
            {
                "request": request.to_dict(),
                "bars": {
                    key: [bar.bar_id for bar in item.bars] for key, item in series.items()
                },
                "context_bars": {
                    "benchmark": [
                        bar.bar_id for bar in (benchmark_bars or {}).get(request.resolution, ())
                    ],
                    "sector": [
                        bar.bar_id for bar in (sector_bars or {}).get(request.resolution, ())
                    ],
                    "breadth": {
                        key: [
                            bar.bar_id
                            for bar in (mapping or {}).get(request.resolution, ())
                        ]
                        for key, mapping in sorted((context_bars or {}).items())
                    },
                },
                "fundamentals": [
                    {
                        "snapshot_id": item.snapshot_id,
                        "received_at": item.received_at.isoformat(),
                        "fiscal_period": item.fiscal_period,
                        "metrics": dict(item.metrics),
                    }
                    for item in fundamentals
                ],
                "news": [_news_descriptor(item) for item in news_events],
                "definition": definition_payload["definition_digest"],
            }
        )
        feature_set_id = f"fs_{input_digest[:24]}"
        cached = self._cache.get(feature_set_id)
        if cached is not None:
            regime = self._regime.detect(
                cached,
                previous=previous_regime,
                exchange=request.exchange or (source.exchange if source else ""),
                generated_at=generated_at,
            )
            return FeatureGenerationResult(feature_set=cached, regime=regime)

        statuses = {name: computed.statuses.get(name, FeatureStatus.MISSING) for name in profile_names}
        reasons = {name: computed.reasons[name] for name in computed.reasons if name in statuses}
        lineage = {
            "phase": "PI-04",
            "feature_engine": FEATURE_SET_VERSION,
            "contract_version": FEATURE_CONTRACT_VERSION,
            "definition_digest": definition_payload["definition_digest"],
            "input_digest": input_digest,
            "leakage_safe": True,
            "bar_counts": {key: len(item.bars) for key, item in series.items()},
            "duplicates": {key: item.duplicates for key, item in series.items()},
            "out_of_order": {key: item.out_of_order for key, item in series.items()},
            "gaps": {key: item.gaps for key, item in series.items()},
            "invalid_bars": {key: item.invalid for key, item in series.items()},
            "partial_bars": {key: item.partial for key, item in series.items()},
            "future_bars": {key: item.future for key, item in series.items()},
            "timeframe_reasons": timeframe_reasons,
            "derived_timeframes": derived_timeframes,
            "fundamental_provenance": dict(fundamental_result.provenance),
            "news_provenance": [dict(row) for row in news_result.provenance],
            "context": context.provenance,
            "data_errors": self._last_error,
        }
        feature_set = FeatureSet(
            feature_set_id=feature_set_id,
            version=FEATURE_SET_VERSION,
            instrument_id=request.instrument_id,
            exchange=request.exchange or (source.exchange if source else ""),
            as_of=request.as_of,
            resolution=request.resolution,
            profile=FeatureProfileName(profile),
            values=values,
            statuses=statuses,
            reasons=reasons,
            feature_names=profile_names,
            timeframes=timeframes,
            timeframe_statuses=timeframe_statuses,
            source_first_at=source_first,
            source_last_at=source_last,
            retrieved_at=retrieved,
            generated_at=generated_at,
            data_snapshot_ids=tuple(
                dict.fromkeys(
                    source_id
                    for item in series.values()
                    for bar in item.bars
                    for source_id in _source_bar_ids(bar)
                )
            ),
            dataset_versions=(
                tuple(
                    f"{request.dataset_id}@{request.dataset_version}"
                    for _ in (0,)
                    if request.dataset_id and request.dataset_version
                )
            ),
            expected_count=expected_count,
            completeness=completeness,
            quality=quality,
            lineage=lineage,
        )
        regime = self._regime.detect(
            feature_set,
            previous=previous_regime,
            exchange=feature_set.exchange,
            generated_at=generated_at,
        )
        self._remember(feature_set)
        return FeatureGenerationResult(feature_set=feature_set, regime=regime)

    # -- data -----------------------------------------------------------------

    def _instrument(self, request: FeatureRequest, supplied: Instrument | None) -> Instrument | None:
        if supplied is not None:
            return supplied
        if self._market_data is None:
            return None
        try:
            return cast(Instrument, self._market_data.get_instrument(request.instrument_id))
        except Exception as exc:  # noqa: BLE001 - an unknown instrument is data, not a crash
            self._last_error = str(exc)
            return None

    def _read_bars(
        self,
        instrument_id: str,
        resolution: str,
        request: FeatureRequest,
    ) -> Sequence[OhlcvBar]:
        if self._market_data is None:
            return ()
        if request.dataset_id and request.dataset_version and self._dataset_store is None:
            self._last_error = "dataset membership was requested without a dataset store"
            return ()
        try:
            fetch_limit = (
                min(20_000, request.max_bars * 4)
                if request.dataset_id and request.dataset_version
                else request.max_bars
            )
            page = self._market_data.bars(
                instrument_id,
                interval=resolution,
                end=request.as_of,
                limit=fetch_limit,
                ascending=False,
            )
            items = list(reversed(tuple(page.items)))
        except Exception as exc:  # noqa: BLE001 - the feature row records the gap
            self._last_error = f"{resolution}: {exc}"
            return ()
        if request.dataset_id and request.dataset_version and self._dataset_store is not None:
            try:
                rows, _ = self._dataset_store.member_rows(
                    request.dataset_id,
                    request.dataset_version,
                    instrument_id=instrument_id,
                    end=request.as_of,
                    limit=request.max_bars * 4,
                    ascending=False,
                )
                allowed = {str(row["bar_id"]) for row in rows}
                items = [bar for bar in items if bar.bar_id in allowed]
                items = items[-request.max_bars :]
            except Exception as exc:  # noqa: BLE001 - lineage keeps the refusal visible
                self._last_error = f"dataset membership: {exc}"
        return items

    def _prepare_series(
        self,
        bars: Sequence[OhlcvBar],
        resolution: str,
        as_of: datetime,
        calendar: Any,
        *,
        max_bars: int = 5000,
    ) -> _BarSeries:
        try:
            interval = DataInterval(resolution)
        except ValueError:
            return _BarSeries(DataInterval.DAY_1, (), unavailable_reason=f"unsupported resolution {resolution!r}")
        if interval is DataInterval.SPOT:
            return _BarSeries(interval, (), unavailable_reason="SPOT is not a bar interval")
        result = _BarSeries(interval, ())
        order = sorted(range(len(bars)), key=lambda index: bars[index].observed_at)
        if order != list(range(len(bars))):
            result.out_of_order = sum(
                1 for index, item in zip(range(len(bars)), order) if index != item
            )
        ordered = [bars[index] for index in order]
        by_time: dict[datetime, OhlcvBar] = {}
        for bar in ordered:
            previous = by_time.get(bar.observed_at)
            if previous is not None:
                result.duplicates += 1
                # Prefer the later retrieval, then the less severe quality class.
                if (
                    bar.retrieved_at > previous.retrieved_at
                    or (
                        bar.retrieved_at == previous.retrieved_at
                        and _quality_rank(bar.quality) < _quality_rank(previous.quality)
                    )
                ):
                    by_time[bar.observed_at] = bar
            else:
                by_time[bar.observed_at] = bar
        accepted: list[OhlcvBar] = []
        for bar in sorted(by_time.values(), key=lambda item: item.observed_at):
            if bar.interval is not interval:
                result.invalid += 1
                continue
            if bar.partial:
                result.partial += 1
                continue
            if bar.quality not in _ACCEPTED_QUALITY:
                result.invalid += 1
                continue
            if bar.observed_at > as_of:
                result.future += 1
                continue
            if bar.retrieved_at > as_of:
                result.partial += 1
                continue
            try:
                available_at = _bar_close(bar, interval, calendar)
            except Exception:
                available_at = bar.observed_at + interval_duration(interval)
            if available_at > as_of:
                if bar.observed_at > as_of:
                    result.future += 1
                else:
                    result.partial += 1
                continue
            accepted.append(bar)
        accepted = accepted[-max_bars:]
        result.bars = tuple(accepted)
        if len(result.bars) >= 3:
            spacing = [
                (current.observed_at - previous.observed_at).total_seconds()
                for previous, current in zip(result.bars, result.bars[1:])
            ]
            positive = sorted(item for item in spacing if item > 0)
            if positive:
                median = positive[len(positive) // 2]
                result.gaps = sum(1 for item in spacing if item > median * 1.8)
        if not result.bars:
            result.unavailable_reason = "no validated completed observations before the cutoff"
        return result

    # -- calculations ---------------------------------------------------------

    def _calculate(
        self,
        series: _BarSeries,
        out: _Values,
        expected: Sequence[FeatureSpec],
        calendar: Any = None,
    ) -> None:
        self._calculate_core(series, out, calendar)
        self._calculate_long(series, out)
        # Re-mark anything the profile expects but this resolution did not fill.
        for spec in expected:
            out.statuses.setdefault(spec.name, FeatureStatus.UNAVAILABLE)
            out.reasons.setdefault(spec.name, "not available for this resolution/profile")

    def _calculate_core(
        self, series: _BarSeries, out: _Values, calendar: Any = None
    ) -> None:
        bars = series.bars
        n = len(bars)
        if n == 0:
            return
        close = [float(bar.close) for bar in bars]
        high = [float(bar.high) for bar in bars]
        low = [float(bar.low) for bar in bars]
        open_ = [float(bar.open) for bar in bars]
        volumes = [float(bar.volume) if bar.volume is not None else None for bar in bars]
        has_volume = all(item is not None for item in volumes)
        volume = [float(item or 0.0) for item in volumes]
        latest = close[-1]
        out.put("close", latest)

        def need(count: int, name: str) -> bool:
            if n < count:
                out.mark(name, FeatureStatus.UNAVAILABLE, f"needs {count} completed bars; have {n}")
                return False
            return True

        # Price -------------------------------------------------------------
        if need(2, "price_abs_change_1"):
            out.put("price_abs_change_1", latest - close[-2])
            out.put("return_1", latest / close[-2] - 1.0 if close[-2] > 0 else None)
            out.put(
                "log_return_1",
                math.log(latest / close[-2]) if latest > 0 and close[-2] > 0 else None,
            )
            out.put("price_gap", open_[-1] / close[-2] - 1.0 if close[-2] > 0 else None)
        if n >= 1:
            out.put(
                "open_close_return",
                latest / open_[-1] - 1.0 if open_[-1] > 0 else None,
            )
            out.put("high_low_range_pct", (high[-1] - low[-1]) / latest if latest > 0 else None)
            out.put("candle_body_pct", abs(latest - open_[-1]) / latest if latest > 0 else None)
            out.put(
                "upper_wick_pct",
                (high[-1] - max(open_[-1], latest)) / latest if latest > 0 else None,
            )
            out.put(
                "lower_wick_pct",
                (min(open_[-1], latest) - low[-1]) / latest if latest > 0 else None,
            )
            up = down = 0
            for index in range(n - 1, 0, -1):
                if close[index] > close[index - 1]:
                    if down:
                        break
                    up += 1
                elif close[index] < close[index - 1]:
                    if up:
                        break
                    down += 1
                else:
                    break
            out.put("consecutive_up", up, zero_status=False)
            out.put("consecutive_down", down, zero_status=False)
        if need(3, "return_prev_1"):
            out.put("return_prev_1", close[-2] / close[-3] - 1.0 if close[-3] > 0 else None)
        for period in (5, 10, 20):
            name = f"return_{period}"
            if need(period + 1, name):
                out.put(name, latest / close[-period - 1] - 1.0 if close[-period - 1] > 0 else None)
        if need(20, "rolling_high_20"):
            out.put("rolling_high_20", max(high[-20:]))
            out.put("rolling_low_20", min(low[-20:]))
            out.put(
                "distance_recent_high_20",
                latest / max(high[-20:]) - 1.0 if max(high[-20:]) > 0 else None,
            )
            out.put(
                "distance_recent_low_20",
                latest / min(low[-20:]) - 1.0 if min(low[-20:]) > 0 else None,
            )

        # Trend -------------------------------------------------------------
        from novacontrol.novamarket.strategies import indicators

        for period in (5, 10, 20, 50, 200):
            name = f"sma_{period}"
            if need(period, name):
                out.put(name, indicators.sma(close, period))
        for period in (5, 12, 26, 50):
            name = f"ema_{period}"
            if need(period, name):
                out.put(name, indicators.ema(close, period))
        for period in (10, 20):
            name = f"wma_{period}"
            if need(period, name):
                out.put(name, indicators.wma(close, period))
        if need(26, "ema_26"):
            macd_value = indicators.macd(close, 12, 26, 9)
            if macd_value is not None:
                out.put("macd", macd_value[0])
                if n >= 34:
                    out.put("macd_signal", macd_value[1])
                    out.put("macd_histogram", macd_value[2])
                    out.put("macd_normalized", float(macd_value[0]) / latest if latest > 0 else None)
                else:
                    for name in ("macd_signal", "macd_histogram", "macd_normalized"):
                        out.mark(name, FeatureStatus.UNAVAILABLE, "needs 34 completed bars")
            else:
                for name in ("macd", "macd_signal", "macd_histogram", "macd_normalized"):
                    out.mark(name, FeatureStatus.UNAVAILABLE, "needs 34 completed bars")
        if need(15, "rsi_14"):
            out.put("rsi_14", indicators.rsi_wilder(close, 14))
        stochastic = indicators.stochastic(high, low, close, 14, 3)
        if n >= 16:
            if stochastic is not None:
                out.put("stoch_k_14", stochastic[0])
                out.put("stoch_d_3", stochastic[1])
        else:
            out.mark("stoch_k_14", FeatureStatus.UNAVAILABLE, "needs 16 completed bars")
            out.mark("stoch_d_3", FeatureStatus.UNAVAILABLE, "needs 16 completed bars")
        if need(11, "roc_10"):
            out.put("roc_10", indicators.roc(close, 10))
            out.put("momentum_10", indicators.momentum(close, 10))
        if need(14, "williams_r_14"):
            out.put("williams_r_14", indicators.williams_r(high, low, close, 14))
        if need(29, "adx_14"):
            directional = indicators.directional_movement(high, low, close, 14)
            if directional is not None:
                out.put("adx_14", directional[0])
                out.put("plus_di_14", directional[1])
                out.put("minus_di_14", directional[2])
                out.put("directional_dx_14", directional[3])
            else:
                for name in ("adx_14", "plus_di_14", "minus_di_14", "directional_dx_14"):
                    out.mark(name, FeatureStatus.MISSING, "directional movement is undefined")
        else:
            for name in ("plus_di_14", "minus_di_14", "directional_dx_14"):
                out.mark(name, FeatureStatus.UNAVAILABLE, "needs 29 completed bars")
        if n >= 20:
            sma20 = _float(out.values.get("sma_20"))
            sma50 = indicators.sma(close, 50)
            out.put("distance_sma_20", latest / sma20 - 1.0 if sma20 else None)
            if n >= 50 and sma50 is not None:
                out.put("sma_cross_20_50", (sma20 - float(sma50)) / latest if sma20 is not None and latest > 0 else None)
                ema50 = indicators.ema(close, 50)
                out.put("distance_ema_50", latest / float(ema50) - 1.0 if ema50 is not None else None)
            else:
                for name in ("sma_cross_20_50", "distance_ema_50"):
                    out.mark(name, FeatureStatus.UNAVAILABLE, "needs 50 completed bars")
        else:
            out.mark("distance_sma_20", FeatureStatus.UNAVAILABLE, "needs 20 completed bars")
        if n >= 30:
            sma_series = indicators.sma_series(close, 20)
            current = sma_series[-1]
            prior = sma_series[-11] if len(sma_series) >= 11 else None
            if current is not None and prior is not None:
                out.put("sma_slope_20", (float(current) - float(prior)) / latest if latest > 0 else None)
            else:
                out.mark("sma_slope_20", FeatureStatus.UNAVAILABLE, "needs 30 completed bars")
        else:
            out.mark("sma_slope_20", FeatureStatus.UNAVAILABLE, "needs 30 completed bars")

        # Volatility --------------------------------------------------------
        if need(15, "atr_14"):
            atr_value = indicators.atr_wilder(high, low, close, 14)
            if atr_value is not None:
                out.put("atr_14", atr_value)
                out.put("atr_pct", float(atr_value) / latest if latest > 0 else None)
                atr_float = float(atr_value)
                out.put(
                    "atr_normalized_move_1",
                    (latest - close[-2]) / atr_float if atr_float > 0 and n >= 2 else None,
                )
            else:
                for name in ("atr_14", "atr_pct", "atr_normalized_move_1"):
                    out.mark(name, FeatureStatus.MISSING, "true range is undefined")
        else:
            for name in ("atr_14", "atr_pct", "atr_normalized_move_1"):
                out.mark(name, FeatureStatus.UNAVAILABLE, "needs 15 completed bars")
        if need(20, "bollinger_mid_20"):
            bands = indicators.bollinger(close, 20, 2.0)
            if bands is not None:
                middle, upper, lower = bands
                out.put("bollinger_mid_20", middle)
                out.put("bollinger_upper_20", upper)
                out.put("bollinger_lower_20", lower)
                width = float(upper) - float(lower)
                out.put("bollinger_width_pct", width / float(middle) if float(middle) > 0 else None)
                out.put("bollinger_position", (latest - float(lower)) / width if width > 0 else None)
            else:
                for name in (
                    "bollinger_mid_20",
                    "bollinger_upper_20",
                    "bollinger_lower_20",
                    "bollinger_width_pct",
                    "bollinger_position",
                ):
                    out.mark(name, FeatureStatus.MISSING, "bollinger bands are undefined")
            out.put("rolling_std_20", indicators.rolling_std(close, 20))
        else:
            for name in (
                "bollinger_mid_20",
                "bollinger_upper_20",
                "bollinger_lower_20",
                "bollinger_width_pct",
                "bollinger_position",
                "rolling_std_20",
            ):
                out.mark(name, FeatureStatus.UNAVAILABLE, "needs 20 completed bars")
        if need(21, "historical_volatility_20"):
            out.put("historical_volatility_20", indicators.historical_volatility(close, 20))
            short = indicators.historical_volatility(close, 5)
            long = indicators.historical_volatility(close, 20)
            out.put(
                "volatility_change_5_20",
                float(short) - float(long) if short is not None and long is not None else None,
            )
        else:
            out.mark("historical_volatility_20", FeatureStatus.UNAVAILABLE, "needs 21 completed bars")
            out.mark("volatility_change_5_20", FeatureStatus.UNAVAILABLE, "needs 21 completed bars")
        if need(20, "range_expansion_5_20"):
            ranges = [high[index] - low[index] for index in range(n)]
            short_range = sum(ranges[-5:]) / 5.0
            long_range = sum(ranges[-20:]) / 20.0
            out.put("range_expansion_5_20", short_range / long_range if long_range > 0 else None)

        # Volume ------------------------------------------------------------
        if not has_volume:
            for name in (
                "volume_sma_20",
                "relative_volume_20",
                "volume_roc_5",
                "obv",
                "obv_slope_10",
                "vpt",
                "vpt_slope_10",
                "mfi_14",
                "vwap",
                "distance_vwap",
            ):
                out.mark(name, FeatureStatus.MISSING, "volume is unavailable for one or more bars")
        else:
            if n >= 20:
                out.put("volume_sma_20", indicators.average_volume(volume, 20), zero_status=True)
                average = sum(volume[-20:]) / 20.0
                if average > 0:
                    out.put("relative_volume_20", volume[-1] / average)
                else:
                    out.mark(
                        "relative_volume_20",
                        FeatureStatus.MISSING,
                        "the 20-bar average volume is zero",
                    )
            else:
                out.mark("volume_sma_20", FeatureStatus.UNAVAILABLE, "needs 20 completed bars")
                out.mark("relative_volume_20", FeatureStatus.UNAVAILABLE, "needs 20 completed bars")
            if n >= 6:
                if volume[-6] > 0:
                    out.put("volume_roc_5", volume[-1] / volume[-6] - 1.0)
                else:
                    out.mark(
                        "volume_roc_5",
                        FeatureStatus.MISSING,
                        "the comparison volume is zero",
                    )
            else:
                out.mark("volume_roc_5", FeatureStatus.UNAVAILABLE, "needs 6 completed bars")
            out.put("obv", indicators.obv(close, volume))
            out.put("vpt", indicators.vpt(close, volume))
            if n >= 21:
                obv_values = indicators.obv_series(close, volume) or []
                vpt_values = indicators.vpt_series(close, volume) or []
                average = sum(volume[-20:]) / 20.0
                if len(obv_values) >= 11 and average > 0:
                    out.put(
                        "obv_slope_10",
                        (obv_values[-1] - obv_values[-11]) / (average * 10.0),
                    )
                    out.put(
                        "vpt_slope_10",
                        (vpt_values[-1] - vpt_values[-11]) / (average * 10.0)
                        if len(vpt_values) >= 11
                        else None,
                    )
                else:
                    out.mark("obv_slope_10", FeatureStatus.MISSING, "average volume is zero")
                    out.mark("vpt_slope_10", FeatureStatus.MISSING, "average volume is zero")
            else:
                out.mark("obv_slope_10", FeatureStatus.UNAVAILABLE, "needs 21 completed bars")
                out.mark("vpt_slope_10", FeatureStatus.UNAVAILABLE, "needs 21 completed bars")
            if n >= 15:
                out.put("mfi_14", indicators.money_flow_index(high, low, close, volume, 14))
            else:
                out.mark("mfi_14", FeatureStatus.UNAVAILABLE, "needs 15 completed bars")
            if series.resolution.is_intraday:
                session = _latest_session_bars(series.bars, calendar)
                vwap_value = indicators.vwap(
                    [float(bar.high) for bar in session],
                    [float(bar.low) for bar in session],
                    [float(bar.close) for bar in session],
                    [float(bar.volume) if bar.volume is not None else 0.0 for bar in session],
                )
                out.put("vwap", vwap_value)
                out.put(
                    "distance_vwap",
                    latest / float(vwap_value) - 1.0 if vwap_value is not None else None,
                )
            else:
                out.mark("vwap", FeatureStatus.UNAVAILABLE, "VWAP requires validated intraday bars")
                out.mark("distance_vwap", FeatureStatus.UNAVAILABLE, "VWAP requires validated intraday bars")

        # Structure ---------------------------------------------------------
        if n >= 25:
            pivot_highs = _confirmed_pivots(high, "high", lookback=20, wing=2)
            pivot_lows = _confirmed_pivots(low, "low", lookback=20, wing=2)
            if pivot_highs:
                out.put("swing_high_20", pivot_highs[-1])
            else:
                out.mark("swing_high_20", FeatureStatus.MISSING, "no confirmed pivot high")
            if pivot_lows:
                out.put("swing_low_20", pivot_lows[-1])
            else:
                out.mark("swing_low_20", FeatureStatus.MISSING, "no confirmed pivot low")
            if len(pivot_highs) >= 2:
                high_now, high_prior = pivot_highs[-1], pivot_highs[-2]
                out.put("higher_high", 1.0 if high_now > high_prior else 0.0, zero_status=False)
                out.put("lower_high", 1.0 if high_now < high_prior else 0.0, zero_status=False)
            else:
                for name in ("higher_high", "lower_high"):
                    out.mark(name, FeatureStatus.UNAVAILABLE, "needs two confirmed swing highs")
            if len(pivot_lows) >= 2:
                low_now, low_prior = pivot_lows[-1], pivot_lows[-2]
                out.put("higher_low", 1.0 if low_now > low_prior else 0.0, zero_status=False)
                out.put("lower_low", 1.0 if low_now < low_prior else 0.0, zero_status=False)
                if len(pivot_highs) >= 2:
                    out.put(
                        "trend_continuation",
                        1.0
                        if pivot_highs[-1] > pivot_highs[-2] and pivot_lows[-1] > pivot_lows[-2]
                        else 0.0,
                        zero_status=False,
                    )
                    out.put(
                        "trend_breakdown",
                        1.0
                        if pivot_highs[-1] < pivot_highs[-2] and pivot_lows[-1] < pivot_lows[-2]
                        else 0.0,
                        zero_status=False,
                    )
                else:
                    out.mark("trend_continuation", FeatureStatus.MISSING, "no confirmed swing high")
                    out.mark("trend_breakdown", FeatureStatus.MISSING, "no confirmed swing high")
            else:
                for name in ("higher_low", "lower_low", "trend_continuation", "trend_breakdown"):
                    out.mark(name, FeatureStatus.UNAVAILABLE, "needs two confirmed swing lows")
        else:
            for name in (
                "swing_high_20",
                "swing_low_20",
                "higher_high",
                "lower_high",
                "higher_low",
                "lower_low",
                "trend_continuation",
                "trend_breakdown",
            ):
                out.mark(name, FeatureStatus.UNAVAILABLE, "needs 25 completed bars")
        if need(21, "breakout_20"):
            prior_high = max(high[-21:-1])
            prior_low = min(low[-21:-1])
            out.put("breakout_20", 1.0 if latest > prior_high else 0.0, zero_status=False)
            out.put("breakdown_20", 1.0 if latest < prior_low else 0.0, zero_status=False)
            out.put(
                "range_bound_20",
                1.0 if prior_low <= latest <= prior_high else 0.0,
                zero_status=False,
            )
            out.put("resistance_proximity_20", prior_high / latest - 1.0 if latest > 0 else None)
            out.put(
                "support_proximity_20",
                latest / prior_low - 1.0 if prior_low > 0 else None,
            )
            out.put(
                "distance_trading_range_20",
                (latest - prior_low) / (prior_high - prior_low)
                if prior_high > prior_low
                else None,
            )

    def _calculate_long(self, series: _BarSeries, out: _Values) -> None:
        n = len(series.bars)
        close = [float(bar.close) for bar in series.bars]
        from novacontrol.novamarket.strategies import indicators

        if n >= 200:
            out.put("sma_200", indicators.sma(close, 200))
        else:
            out.mark("sma_200", FeatureStatus.UNAVAILABLE, f"needs 200 completed bars; have {n}")

    # -- context, fundamentals and news --------------------------------------

    def _context_features(
        self,
        request: FeatureRequest,
        primary: _BarSeries,
        *,
        benchmark_bars: Mapping[str, Sequence[OhlcvBar]] | None,
        sector_bars: Mapping[str, Sequence[OhlcvBar]] | None,
        context_bars: Mapping[str, Mapping[str, Sequence[OhlcvBar]]] | None,
        calendar: Any,
    ) -> "_Context":
        result = _Context()
        names = (
            "relative_return_1",
            "relative_return_20",
            "sector_relative_return_20",
            "market_volatility_20",
            "market_breadth",
        )
        for name in names:
            result.mark(name, FeatureStatus.UNAVAILABLE, "context data was not supplied")
        if not request.include_context:
            return result
        if not primary.available:
            for name in names:
                result.mark(
                    name,
                    FeatureStatus.UNAVAILABLE,
                    "no validated primary-resolution bar is available",
                )
            return result
        base_return_1, base_return_20 = _returns(primary.bars)
        if benchmark_bars:
            benchmark = self._prepare_series(
                _lookup_bars(benchmark_bars, request.resolution) or (),
                request.resolution,
                request.as_of,
                calendar,
                max_bars=request.max_bars,
            )
            if benchmark.available:
                bench_1, bench_20 = _returns(benchmark.bars)
                if base_return_1 is not None and bench_1 is not None:
                    result.put("relative_return_1", base_return_1 - bench_1)
                if base_return_20 is not None and bench_20 is not None:
                    result.put("relative_return_20", base_return_20 - bench_20)
                from novacontrol.novamarket.strategies import indicators

                volatility = indicators.historical_volatility(
                    [float(bar.close) for bar in benchmark.bars], 20
                )
                result.put("market_volatility_20", volatility)
                if request.exchange:
                    result.provenance["benchmark_exchange"] = request.exchange
                result.provenance["benchmark_bars"] = len(benchmark.bars)
            else:
                result.mark("relative_return_1", FeatureStatus.UNAVAILABLE, "benchmark has no validated bars")
                result.mark("relative_return_20", FeatureStatus.UNAVAILABLE, "benchmark has no validated bars")
                result.mark("market_volatility_20", FeatureStatus.UNAVAILABLE, "benchmark has no validated bars")
        if sector_bars:
            sector = self._prepare_series(
                _lookup_bars(sector_bars, request.resolution) or (),
                request.resolution,
                request.as_of,
                calendar,
                max_bars=request.max_bars,
            )
            if sector.available:
                _, sector_20 = _returns(sector.bars)
                if base_return_20 is not None and sector_20 is not None:
                    result.put("sector_relative_return_20", base_return_20 - sector_20)
                result.provenance["sector_bars"] = len(sector.bars)
        if context_bars:
            breadth_values: list[float] = []
            for _, instrument_bars in sorted(context_bars.items()):
                item = self._prepare_series(
                    _lookup_bars(instrument_bars, request.resolution) or (),
                    request.resolution,
                    request.as_of,
                    calendar,
                    max_bars=request.max_bars,
                )
                if len(item.bars) < 20:
                    continue
                closes = [float(bar.close) for bar in item.bars]
                average = sum(closes[-20:]) / 20.0
                breadth_values.append(1.0 if closes[-1] > average else 0.0)
            if breadth_values:
                result.put("market_breadth", sum(breadth_values) / len(breadth_values))
                result.provenance["breadth_instruments"] = len(breadth_values)
            else:
                result.mark("market_breadth", FeatureStatus.UNAVAILABLE, "no context instrument has 20 bars")
        return result

    def _fundamental_features(
        self,
        request: FeatureRequest,
        snapshots: Sequence[FundamentalSnapshot],
        primary: _BarSeries,
    ) -> FundamentalFeatureResult:
        names = (
            "fundamental_revenue",
            "fundamental_net_profit",
            "fundamental_cash_flow",
            "fundamental_dividend_per_share",
            "fundamental_revenue_growth",
            "fundamental_profit_growth",
            "fundamental_eps",
            "fundamental_pe",
            "fundamental_pb",
            "fundamental_roe",
            "fundamental_roce",
            "fundamental_debt_to_equity",
            "fundamental_operating_margin",
            "fundamental_profit_margin",
            "fundamental_cash_flow_growth",
            "fundamental_dividend_yield",
            "fundamental_age_days",
        )
        values: dict[str, float] = {}
        statuses = {name: FeatureStatus.UNAVAILABLE for name in names}
        reasons = {name: "not requested" for name in names}
        if not request.include_fundamentals:
            return FundamentalFeatureResult(values, statuses, reasons, {})
        if not primary.available:
            blocked = {
                name: "no validated primary-resolution bar is available" for name in names
            }
            return FundamentalFeatureResult({}, statuses, blocked, {"point_in_time": True})
        eligible = [
            item
            for item in snapshots
            if item.instrument_id == request.instrument_id
            and item.received_at <= request.as_of
            and item.source_observed_at <= request.as_of
            and item.quality in (DataQuality.VALID, DataQuality.WARNING)
        ]
        eligible.sort(key=lambda item: (item.received_at, item.source_observed_at, item.snapshot_id))
        if not eligible:
            reasons = {
                name: "no validated fundamental snapshot was available before the cutoff"
                for name in names
            }
            return FundamentalFeatureResult(
                values,
                statuses,
                reasons,
                {"available": 0, "point_in_time": True},
            )
        latest = eligible[-1]
        metrics = {str(key).lower(): _float(value) for key, value in latest.metrics.items()}
        prior = _prior_snapshot(eligible, latest)
        close = float(primary.bars[-1].close) if primary.available else None

        def put(name: str, value: float | None, reason: str = "") -> None:
            if value is None or not math.isfinite(value):
                statuses[name] = FeatureStatus.MISSING
                reasons[name] = reason or "the reported metric is insufficient to derive this value"
            else:
                values[name] = value
                statuses[name] = FeatureStatus.ZERO if value == 0 else FeatureStatus.VALUE
                reasons.pop(name, None)

        revenue = _metric(metrics, "revenue")
        profit = _metric(metrics, "net_profit", "net_income")
        equity = _metric(metrics, "shareholders_equity", "total_equity")
        debt = _metric(metrics, "total_debt", "debt")
        operating_income = _metric(metrics, "operating_income", "ebit")
        capital = _metric(metrics, "capital_employed")
        cash_flow = _metric(metrics, "operating_cash_flow", "cash_flow")
        dps = _metric(metrics, "dividend_per_share", "dividends_per_share")
        shares = metrics.get("shares_outstanding")
        eps = _metric(metrics, "eps", "earnings_per_share")
        if eps is None and profit is not None and shares is not None and shares > 0:
            eps = profit / shares
        reported_book_value = _metric(metrics, "book_value_per_share")
        book_value = (
            reported_book_value
            if reported_book_value is not None
            else (
                equity / shares
                if equity is not None and shares is not None and shares > 0
                else None
            )
        )
        prior_metrics = (
            {str(key).lower(): _float(value) for key, value in prior.metrics.items()}
            if prior is not None
            else {}
        )
        revenue_prior = _metric(prior_metrics, "revenue")
        profit_prior = _metric(prior_metrics, "net_profit", "net_income")
        cash_prior = _metric(prior_metrics, "operating_cash_flow", "cash_flow")
        put("fundamental_revenue", revenue)
        put("fundamental_net_profit", profit)
        put("fundamental_cash_flow", cash_flow)
        put("fundamental_dividend_per_share", dps)
        put(
            "fundamental_revenue_growth",
            revenue / revenue_prior - 1.0
            if revenue is not None and revenue_prior not in (None, 0)
            else None,
            "revenue and a prior-period revenue are required",
        )
        put(
            "fundamental_profit_growth",
            profit / profit_prior - 1.0
            if profit is not None and profit_prior not in (None, 0)
            else None,
            "net profit and prior-period net profit are required",
        )
        pe = _metric(metrics, "pe", "price_to_earnings")
        if pe is None and close is not None and eps not in (None, 0):
            pe = close / eps
        pb = _metric(metrics, "pb", "price_to_book")
        if pb is None and close is not None and book_value not in (None, 0):
            pb = close / book_value
        roe = _metric(metrics, "roe", "return_on_equity")
        if roe is None and profit is not None and equity not in (None, 0):
            roe = profit / equity
        roce = _metric(metrics, "roce", "return_on_capital_employed")
        if roce is None and operating_income is not None and capital not in (None, 0):
            roce = operating_income / capital
        debt_to_equity = _metric(metrics, "debt_to_equity", "de_ratio")
        if debt_to_equity is None and debt is not None and equity not in (None, 0):
            debt_to_equity = debt / equity
        operating_margin = _metric(metrics, "operating_margin", "operating_margin_pct")
        if operating_margin is None and operating_income is not None and revenue not in (None, 0):
            operating_margin = operating_income / revenue
        profit_margin = _metric(metrics, "profit_margin", "net_margin")
        if profit_margin is None and profit is not None and revenue not in (None, 0):
            profit_margin = profit / revenue
        dividend_yield = _metric(metrics, "dividend_yield")
        if dividend_yield is None and dps is not None and close is not None and close > 0:
            dividend_yield = dps / close
        put("fundamental_eps", eps)
        put(
            "fundamental_pe",
            pe,
            "a positive reported EPS and a close are required",
        )
        put(
            "fundamental_pb",
            pb,
            "reported book value per share and a close are required",
        )
        put(
            "fundamental_roe",
            roe,
            "net profit and shareholders' equity are required",
        )
        put(
            "fundamental_roce",
            roce,
            "operating income and capital employed are required",
        )
        put(
            "fundamental_debt_to_equity",
            debt_to_equity,
            "debt and shareholders' equity are required",
        )
        put(
            "fundamental_operating_margin",
            operating_margin,
            "operating income and revenue are required",
        )
        put(
            "fundamental_profit_margin",
            profit_margin,
            "net profit and revenue are required",
        )
        put(
            "fundamental_cash_flow_growth",
            cash_flow / cash_prior - 1.0
            if cash_flow is not None and cash_prior not in (None, 0)
            else None,
            "cash flow and prior-period cash flow are required",
        )
        put(
            "fundamental_dividend_yield",
            dividend_yield,
            "dividend per share and a close are required",
        )
        age = (request.as_of - latest.received_at).total_seconds() / 86_400.0
        put("fundamental_age_days", max(0.0, age))
        provenance = {
            "available": len(eligible),
            "point_in_time": True,
            "selected_snapshot_id": latest.snapshot_id,
            "fiscal_period": latest.fiscal_period,
            "source": latest.source,
            "source_observed_at": latest.source_observed_at.isoformat(),
            "available_at": latest.received_at.isoformat(),
            "prior_snapshot_id": prior.snapshot_id if prior is not None else "",
            "reported_metrics": sorted(metrics),
            "derived_features": [
                "fundamental_pe",
                "fundamental_pb",
                "fundamental_roe",
                "fundamental_roce",
                "fundamental_debt_to_equity",
                "fundamental_operating_margin",
                "fundamental_profit_margin",
                "fundamental_dividend_yield",
                "fundamental_age_days",
            ],
            "currency": latest.currency,
        }
        return FundamentalFeatureResult(
            values,
            statuses,
            reasons,
            provenance,
            selected_snapshot_id=latest.snapshot_id,
            available_at=latest.received_at,
        )

    def _news_features(
        self,
        request: FeatureRequest,
        events: Sequence[Any],
        primary: _BarSeries,
    ) -> NewsFeatureResult:
        names = (
            "news_count_5d",
            "news_count_20d",
            "news_importance_max_20d",
            "news_relevance_max_20d",
            "news_sentiment_mean_5d",
            "news_age_days",
            "news_earnings_flag",
            "news_corporate_action_flag",
            "news_regulatory_flag",
        )
        statuses = {name: FeatureStatus.UNAVAILABLE for name in names}
        reasons = {name: "news features were not requested" for name in names}
        if not request.include_news:
            return NewsFeatureResult({}, statuses, reasons, ())
        if not primary.available:
            reasons = {
                name: "no validated primary-resolution bar is available" for name in names
            }
            return NewsFeatureResult({}, statuses, reasons, ())
        eligible: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in events:
            record = _news_record(item, request.instrument_id, request.as_of)
            if record is None:
                continue
            fingerprint = str(record.get("fingerprint", ""))
            if fingerprint and fingerprint in seen:
                continue
            if fingerprint:
                seen.add(fingerprint)
            eligible.append(record)
        eligible.sort(key=lambda row: (row["received_at"], row["evidence_id"]))
        if not eligible:
            reasons = {
                name: "no validated event was available before the cutoff" for name in names
            }
            return NewsFeatureResult({}, statuses, reasons, ())
        values: dict[str, float] = {}
        five_days = request.as_of - timedelta(days=5)
        twenty_days = request.as_of - timedelta(days=20)
        recent_5 = [row for row in eligible if row["received_at"] >= five_days]
        recent_20 = [row for row in eligible if row["received_at"] >= twenty_days]
        values["news_count_5d"] = float(len(recent_5))
        values["news_count_20d"] = float(len(recent_20))
        statuses["news_count_5d"] = FeatureStatus.ZERO if not recent_5 else FeatureStatus.VALUE
        statuses["news_count_20d"] = FeatureStatus.ZERO if not recent_20 else FeatureStatus.VALUE
        reasons.pop("news_count_5d", None)
        reasons.pop("news_count_20d", None)
        importances = [
            float(row["importance"]) for row in recent_20 if row["importance"] is not None
        ]
        if importances:
            values["news_importance_max_20d"] = max(importances)
            statuses["news_importance_max_20d"] = FeatureStatus.VALUE
            reasons.pop("news_importance_max_20d", None)
        else:
            statuses["news_importance_max_20d"] = FeatureStatus.MISSING
            reasons["news_importance_max_20d"] = "events exist but none declares importance"
        relevances = [
            float(row["relevance"]) for row in recent_20 if row["relevance"] is not None
        ]
        if relevances:
            values["news_relevance_max_20d"] = max(relevances)
            statuses["news_relevance_max_20d"] = FeatureStatus.VALUE
            reasons.pop("news_relevance_max_20d", None)
        else:
            statuses["news_relevance_max_20d"] = FeatureStatus.UNAVAILABLE
            reasons["news_relevance_max_20d"] = "no validated relevance component is available"
        sentiments = [
            float(row["sentiment"])
            for row in recent_5
            if row["sentiment"] is not None and row["sentiment_validated"]
        ]
        if sentiments:
            values["news_sentiment_mean_5d"] = sum(sentiments) / len(sentiments)
            statuses["news_sentiment_mean_5d"] = FeatureStatus.VALUE
            reasons.pop("news_sentiment_mean_5d", None)
        else:
            statuses["news_sentiment_mean_5d"] = FeatureStatus.UNAVAILABLE
            reasons["news_sentiment_mean_5d"] = "no validated sentiment component is available"
        latest = eligible[-1]
        values["news_age_days"] = max(0.0, (request.as_of - latest["received_at"]).total_seconds() / 86_400.0)
        statuses["news_age_days"] = FeatureStatus.VALUE
        reasons.pop("news_age_days", None)
        category_text = " ".join(
            f"{row['event_type']} {row['category']} {row['subcategory']}".lower()
            for row in recent_20
        )
        for name, present, tokens in (
            ("news_earnings_flag", bool(recent_20), ("earnings", "result", "profit")),
            (
                "news_corporate_action_flag",
                bool(recent_20),
                ("dividend", "split", "merger", "acquisition", "buyback"),
            ),
            (
                "news_regulatory_flag",
                bool(recent_20),
                ("regulatory", "management", "board", "sebi", "sec", "lawsuit", "approval"),
            ),
        ):
            if present:
                values[name] = 1.0 if any(token in category_text for token in tokens) else 0.0
                statuses[name] = FeatureStatus.VALUE
                reasons.pop(name, None)
        provenance = tuple(
            {
                "evidence_id": row["evidence_id"],
                "event_type": row["event_type"],
                "category": row["category"],
                "subcategory": row["subcategory"],
                "headline": row["headline"],
                "instrument_ids": row["instrument_ids"],
                "source": row["source"],
                "source_url": row["source_url"],
                "published_at": row["published_at"].isoformat(),
                "occurred_at": (
                    row["occurred_at"].isoformat() if row["occurred_at"] else None
                ),
                "received_at": row["received_at"].isoformat(),
                "availability": row["received_at"].isoformat(),
                "importance": row["importance"],
                "relevance": row["relevance"],
                "sentiment": row["sentiment"],
                "sentiment_validated": row["sentiment_validated"],
                "fingerprint": row["fingerprint"],
                "radar_event_id": row.get("radar_event_id", ""),
                "radar_verification": row.get("radar_verification", ""),
                "radar_evidence_count": row.get("radar_evidence_count"),
            }
            for row in eligible[-50:]
        )
        return NewsFeatureResult(values, statuses, reasons, provenance)

    def _multi_timeframe_features(
        self,
        out: _Values,
        primary: _BarSeries,
        series: Mapping[str, _BarSeries],
        timeframes: Mapping[str, Mapping[str, float]],
        request: FeatureRequest,
    ) -> None:
        names = (
            "mtf_available_count",
            "mtf_alignment",
            "mtf_trend_agreement",
            "mtf_momentum_alignment",
            "mtf_volatility_ratio",
            "mtf_short_within_long_structure",
        )
        available = [key for key, row in timeframes.items() if row]
        if not primary.available:
            for name in names:
                out.mark(name, FeatureStatus.UNAVAILABLE, "no validated primary bar")
            return
        if not available:
            for name in names:
                out.mark(name, FeatureStatus.UNAVAILABLE, "no timeframe has validated observations")
            return
        trends: list[float] = []
        momentum: list[float] = []
        volatility: list[float] = []
        for resolution, row in timeframes.items():
            if not row:
                continue
            close = row.get("close")
            sma20 = row.get("sma_20")
            if close is not None and sma20 not in (None, 0):
                trends.append(1.0 if close > sma20 else -1.0 if close < sma20 else 0.0)
            ret5 = row.get("return_5")
            if ret5 is not None:
                momentum.append(1.0 if ret5 > 0 else -1.0 if ret5 < 0 else 0.0)
            atr_pct = row.get("atr_pct")
            if atr_pct is not None:
                volatility.append(float(atr_pct))
        out.put("mtf_available_count", float(len(available)), zero_status=False)
        if trends:
            out.put("mtf_alignment", sum(1 for value in trends if value > 0) / len(trends))
            out.put(
                "mtf_trend_agreement",
                1.0 - (sum(abs(value - (sum(trends) / len(trends))) for value in trends) / len(trends)),
                zero_status=False,
            )
        else:
            out.mark("mtf_alignment", FeatureStatus.MISSING, "no timeframe exposes an SMA20 trend")
            out.mark("mtf_trend_agreement", FeatureStatus.MISSING, "no timeframe exposes an SMA20 trend")
        if momentum:
            out.put("mtf_momentum_alignment", sum(1 for value in momentum if value > 0) / len(momentum))
        else:
            out.mark("mtf_momentum_alignment", FeatureStatus.MISSING, "no timeframe exposes return_5")
        if len(volatility) >= 2 and max(volatility) > 0:
            out.put("mtf_volatility_ratio", min(volatility) / max(volatility))
        else:
            out.mark("mtf_volatility_ratio", FeatureStatus.UNAVAILABLE, "needs two ATR-bearing timeframes")
        # Shortest close inside the longest available range, where supplied.
        ordered = sorted(
            (key for key in timeframes if timeframes[key]),
            key=lambda item: _INTERVAL_RANK.get(str(item).lower(), -1),
        )
        if len(ordered) >= 2 and primary.available:
            longest = timeframes[ordered[-1]]
            shortest = timeframes[ordered[0]]
            long_high, long_low = longest.get("rolling_high_20"), longest.get("rolling_low_20")
            close = shortest.get("close")
            if (
                close is not None
                and long_high is not None
                and long_low is not None
                and long_high > long_low
            ):
                out.put(
                    "mtf_short_within_long_structure",
                    (close - long_low) / (long_high - long_low),
                )
            else:
                out.mark(
                    "mtf_short_within_long_structure",
                    FeatureStatus.UNAVAILABLE,
                    "the longest timeframe lacks a 20-bar range",
                )
        else:
            out.mark(
                "mtf_short_within_long_structure",
                FeatureStatus.UNAVAILABLE,
                "needs two validated timeframes",
            )

    # -- helpers --------------------------------------------------------------

    def _quality(
        self,
        primary: _BarSeries,
        completeness: float,
        available: int,
        expected: int,
    ) -> FeatureQuality:
        if not primary.available or available == 0:
            return FeatureQuality.INVALID
        if completeness < 0.35:
            return FeatureQuality.INSUFFICIENT
        if completeness < 0.75 or primary.invalid or primary.partial or primary.future:
            return FeatureQuality.PARTIAL
        return FeatureQuality.VALID

    def _remember(self, feature_set: FeatureSet) -> None:
        if self._cache_size <= 0:
            return
        key = feature_set.feature_set_id
        self._cache[key] = feature_set
        self._cache_order.append(key)
        while len(self._cache_order) > self._cache_size:
            oldest = self._cache_order.popleft()
            self._cache.pop(oldest, None)


@dataclass(slots=True)
class _Context:
    values: dict[str, float] = field(default_factory=dict)
    statuses: dict[str, FeatureStatus] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    def mark(self, name: str, status: FeatureStatus, reason: str = "") -> None:
        self.statuses[name] = status
        if reason:
            self.reasons[name] = reason

    def put(self, name: str, value: Any) -> None:
        number = _float(value)
        if number is None:
            self.mark(name, FeatureStatus.MISSING, "context value is undefined")
            return
        self.values[name] = number
        self.statuses[name] = FeatureStatus.ZERO if number == 0 else FeatureStatus.VALUE


def _lookup_bars(
    mapping: Mapping[str, Sequence[OhlcvBar]] | None, resolution: str
) -> Sequence[OhlcvBar] | None:
    if mapping is None:
        return None
    if resolution in mapping:
        return mapping[resolution]
    canonical = str(resolution).lower()
    for key, value in mapping.items():
        if str(key).lower() == canonical:
            return value
    return None


def _metric(metrics: Mapping[str, float | None], *names: str) -> float | None:
    """First declared metric, preserving a real zero (unlike ``or`` chains)."""
    for name in names:
        if name in metrics:
            return metrics[name]
    return None


def _core_specs() -> tuple[FeatureSpec, ...]:
    names = (
        "close",
        "return_1",
        "return_5",
        "return_20",
        "sma_20",
        "sma_50",
        "ema_50",
        "rsi_14",
        "macd",
        "atr_pct",
        "adx_14",
        "bollinger_width_pct",
        "relative_volume_20",
        "vwap",
        "rolling_high_20",
        "rolling_low_20",
        "breakout_20",
        "breakdown_20",
    )
    specs: list[FeatureSpec] = [
        FeatureSpec(
            name="close",
            family=FeatureFamily.PRICE,
            description="Latest completed close.",
            formula="close_t",
            lookback="1 bar",
        )
    ]
    for name in names[1:]:
        if name in FEATURE_SPECS:
            specs.append(feature_spec(name))
    return tuple(specs)


def _prior_snapshot(
    snapshots: Sequence[FundamentalSnapshot], latest: FundamentalSnapshot
) -> FundamentalSnapshot | None:
    earlier = [
        item
        for item in snapshots
        if item.snapshot_id != latest.snapshot_id
        and item.received_at <= latest.received_at
        and item.fiscal_period != latest.fiscal_period
    ]
    if not earlier:
        return None
    return max(earlier, key=lambda item: (item.received_at, item.source_observed_at))


def _bar_close(bar: OhlcvBar, interval: DataInterval, calendar: Any) -> datetime:
    if interval.is_intraday:
        if calendar is not None:
            zone = calendar.local(bar.observed_at).tzinfo
            local = bar.observed_at.astimezone(zone)
            bounds = calendar.session_bounds(local.date())
            if bounds is not None and bounds[0] <= local < bounds[1]:
                close = min(local + interval_duration(interval), bounds[1])
                return cast(datetime, close.astimezone(UTC))
        return bar.observed_at + interval_duration(interval)
    if interval in (DataInterval.WEEK_1, DataInterval.MONTH_1):
        zone = calendar.local(bar.observed_at).tzinfo if calendar is not None else UTC
        local = bar.observed_at.astimezone(zone)
        if interval is DataInterval.WEEK_1:
            start = local.date() - timedelta(days=local.weekday())
            end = datetime.combine(start, datetime.min.time(), tzinfo=zone) + timedelta(days=7)
        else:
            first = local.date().replace(day=1)
            end = datetime.combine(
                (first + timedelta(days=32)).replace(day=1),
                datetime.min.time(),
                tzinfo=zone,
            )
        return end.astimezone(UTC)
    if calendar is not None:
        local = bar.observed_at.astimezone(calendar.local(bar.observed_at).tzinfo)
        bounds = calendar.session_bounds(local.date())
        if bounds is not None:
            return cast(datetime, bounds[1].astimezone(UTC))
    return bar.observed_at + interval_duration(interval)


def _finer_resolutions(resolution: str) -> tuple[str, ...]:
    """Finer validated intervals that may be aggregated into ``resolution``."""
    return {
        "5m": ("1m",),
        "15m": ("5m", "1m"),
        "30m": ("15m", "5m", "1m"),
        "1h": ("30m", "15m", "5m", "1m"),
        "1d": ("1h", "30m", "15m", "5m", "1m"),
        "1wk": ("1d", "1h", "30m", "15m", "5m", "1m"),
        "1mo": ("1wk", "1d", "1h", "30m", "15m", "5m", "1m"),
    }.get(str(resolution).lower(), ())


def _latest_session_bars(
    bars: Sequence[OhlcvBar], calendar: Any
) -> list[OhlcvBar]:
    """Bars from the most recent session only (VWAP resets per session)."""
    if not bars:
        return []
    latest = bars[-1]
    zone = calendar.local(latest.observed_at).tzinfo if calendar is not None else UTC
    session_day = latest.observed_at.astimezone(zone).date()
    selected: list[OhlcvBar] = []
    for bar in bars:
        if bar.observed_at.astimezone(zone).date() != session_day:
            continue
        if calendar is not None:
            bounds = calendar.session_bounds(session_day)
            if bounds is not None:
                local = bar.observed_at.astimezone(zone)
                if not (bounds[0] <= local < bounds[1]):
                    continue
        selected.append(bar)
    return selected


def _aggregate_bars(
    source: Sequence[OhlcvBar],
    resolution: str,
    as_of: datetime,
    calendar: Any,
) -> list[OhlcvBar]:
    """Aggregate completed finer bars; a partial current bucket is dropped."""
    try:
        interval = DataInterval(resolution)
    except ValueError:
        return []
    if interval is DataInterval.SPOT or not source:
        return []
    buckets: dict[str, tuple[datetime, datetime, list[OhlcvBar]]] = {}
    for bar in source:
        if interval.is_intraday:
            seconds = interval_duration(interval).total_seconds()
            zone = calendar.local(bar.observed_at).tzinfo if calendar is not None else UTC
            local = bar.observed_at.astimezone(zone)
            bounds = calendar.session_bounds(local.date()) if calendar is not None else None
            if calendar is not None and bounds is None:
                continue
            if bounds is not None and not (bounds[0] <= local < bounds[1]):
                continue
            base = bounds[0] if bounds is not None else local.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            elapsed = max(0.0, (local - base).total_seconds())
            start = base + timedelta(seconds=math.floor(elapsed / seconds) * seconds)
            end = start + interval_duration(interval)
        else:
            zone = calendar.local(bar.observed_at).tzinfo if calendar is not None else UTC
            local_day = bar.observed_at.astimezone(zone).date()
            if interval is DataInterval.DAY_1:
                start = datetime.combine(local_day, datetime.min.time(), tzinfo=zone)
                bounds = calendar.session_bounds(local_day) if calendar is not None else None
                if calendar is not None and bounds is None:
                    continue
                end = bounds[1] if bounds is not None else start + timedelta(days=1)
            elif interval is DataInterval.WEEK_1:
                monday = local_day - timedelta(days=local_day.weekday())
                start = datetime.combine(monday, datetime.min.time(), tzinfo=zone)
                end = start + timedelta(days=7)
            else:
                first = local_day.replace(day=1)
                start = datetime.combine(first, datetime.min.time(), tzinfo=zone)
                end = (
                    datetime.combine(
                        (first + timedelta(days=32)).replace(day=1),
                        datetime.min.time(),
                        tzinfo=zone,
                    )
                )
        if end.astimezone(UTC) > as_of:
            continue
        key = start.isoformat()
        if key in buckets:
            _, _, rows = buckets[key]
            rows.append(bar)
        else:
            buckets[key] = (start, end, [bar])
    aggregated: list[OhlcvBar] = []
    for _, (start, _end, rows) in sorted(buckets.items()):
        rows.sort(key=lambda item: item.observed_at)
        volumes = [item.volume for item in rows]
        volume = (
            sum((item for item in volumes if item is not None), Decimal("0"))
            if all(item is not None for item in volumes)
            else None
        )
        worst = max(rows, key=lambda item: _quality_rank(item.quality)).quality
        aggregated.append(
            OhlcvBar(
                bar_id=f"agg:{interval.value}:{start.isoformat()}:{rows[-1].bar_id}",
                instrument_id=rows[0].instrument_id,
                provider="derived",
                interval=interval,
                open=rows[0].open,
                high=max(item.high for item in rows),
                low=min(item.low for item in rows),
                close=rows[-1].close,
                currency=rows[0].currency,
                observed_at=start.astimezone(UTC),
                retrieved_at=max(item.retrieved_at for item in rows),
                source_timezone=rows[0].source_timezone,
                volume=volume,
                quality=worst,
                raw={"derived_from": [item.bar_id for item in rows]},
            )
        )
    return aggregated


def _quality_rank(value: DataQuality) -> int:
    return {
        DataQuality.VALID: 0,
        DataQuality.WARNING: 1,
        DataQuality.STALE: 2,
        DataQuality.SUSPECT: 3,
        DataQuality.INVALID: 4,
        DataQuality.UNAVAILABLE: 5,
    }.get(value, 6)


def _source_bar_ids(bar: OhlcvBar) -> tuple[str, ...]:
    raw = getattr(bar, "raw", None)
    if isinstance(raw, Mapping):
        derived = raw.get("derived_from")
        if isinstance(derived, Sequence) and not isinstance(derived, (str, bytes)):
            ids = tuple(str(item) for item in derived if str(item))
            if ids:
                return ids
    return (bar.bar_id,)


def _returns(bars: Sequence[OhlcvBar]) -> tuple[float | None, float | None]:
    if len(bars) < 2:
        return None, None
    close = [float(bar.close) for bar in bars]
    return (
        close[-1] / close[-2] - 1.0 if close[-2] > 0 else None,
        close[-1] / close[-21] - 1.0 if len(close) >= 21 and close[-21] > 0 else None,
    )


def _confirmed_pivots(
    values: Sequence[float], kind: str, *, lookback: int, wing: int
) -> list[float]:
    """Confirmed pivots in the lookback window, with flat tops/bottoms allowed.

    A plateau is a real swing level.  Equal adjacent pivot values are collapsed
    so a two-bar flat top is not compared with itself as a higher high.
    """
    start = max(wing, len(values) - lookback)
    end = len(values) - wing
    found: list[float] = []
    for index in range(start, end):
        window = values[index - wing : index + wing + 1]
        centre = values[index]
        if kind == "high" and centre >= max(window):
            found.append(centre)
        elif kind == "low" and centre <= min(window):
            found.append(centre)
    unique: list[float] = []
    for value in found:
        if not unique or value != unique[-1]:
            unique.append(value)
    return unique


def _news_identity(item: Any) -> str:
    evidence_id = getattr(item, "evidence_id", None)
    if evidence_id:
        return str(evidence_id)
    if isinstance(item, Mapping):
        return str(item.get("evidence_id") or item.get("event_id") or item.get("headline") or "")
    return str(item)


def _news_descriptor(item: Any) -> dict[str, Any]:
    """Stable content descriptor for reproducibility, not for deduplication."""
    if isinstance(item, NewsEventEvidence):
        return {
            "identity": _news_identity(item),
            "checksum": item.checksum,
            "event_type": item.event_type,
            "importance": item.metadata.get("importance"),
            "relevance": item.metadata.get("relevance"),
            "sentiment": item.metadata.get("sentiment"),
            "published_at": item.published_at.isoformat(),
            "received_at": item.received_at.isoformat(),
        }
    if isinstance(item, Mapping):
        return {
            "identity": _news_identity(item),
            "checksum": str(
                item.get("checksum") or item.get("fingerprint") or item.get("duplicate_key") or ""
            ),
            "event_type": str(item.get("event_type") or item.get("category") or ""),
            "importance": item.get("importance"),
            "relevance": item.get("relevance"),
            "sentiment": item.get("sentiment"),
            "published_at": str(item.get("published_at") or item.get("occurred_at") or ""),
            "received_at": str(
                item.get("received_at") or item.get("available_at") or item.get("retrieved_at") or ""
            ),
        }
    return {"identity": _news_identity(item)}


def _news_record(item: Any, instrument_id: str, as_of: datetime) -> dict[str, Any] | None:
    if isinstance(item, NewsEventEvidence):
        if instrument_id not in item.instrument_ids:
            return None
        if item.received_at > as_of or item.published_at > as_of:
            return None
        if item.quality not in (DataQuality.VALID, DataQuality.WARNING):
            return None
        metadata = dict(item.metadata)
        sentiment_raw = _first_present(metadata, "sentiment")
        sentiment = _float(sentiment_raw)
        validated = any(
            _flag(metadata.get(name))
            for name in ("sentiment_validated", "sentiment_evaluated", "sentiment_model")
        )
        importance = _float(
            _first_present(metadata, "importance", "importance_score", "materiality")
        )
        return {
            "evidence_id": item.evidence_id,
            "event_type": item.event_type,
            "category": str(metadata.get("category", item.event_type)),
            "subcategory": str(metadata.get("subcategory", "")),
            "headline": item.headline,
            "instrument_ids": list(item.instrument_ids),
            "source": item.source,
            "source_url": item.source_url,
            "published_at": item.published_at,
            "occurred_at": None,
            "received_at": item.received_at,
            "importance": importance,
            "relevance": _float(metadata.get("relevance")),
            "sentiment": sentiment,
            "sentiment_validated": validated,
            "fingerprint": item.checksum
            or str(metadata.get("duplicate_key", ""))
            or item.evidence_id,
        }
    if isinstance(item, Mapping):
        if str(item.get("quality", "valid")).lower() not in ("valid", "warning"):
            return None
        instruments = item.get("instrument_ids") or item.get("instruments") or ()
        if isinstance(instruments, str):
            instruments = (instruments,)
        if instrument_id not in {str(value) for value in instruments}:
            return None
        # NM-04 radar events expose published_at/occurred_at and retrieved_at
        # (the time the event became available), not the PI-01 ``received_at``
        # spelling.  Accept both contracts without dropping real radar events.
        published = _as_datetime(
            item.get("published_at")
            or item.get("occurred_at")
            or item.get("discovered_at")
        )
        received = _as_datetime(
            item.get("received_at")
            or item.get("available_at")
            or item.get("retrieved_at")
            or item.get("discovered_at")
        )
        if published is None or received is None or received > as_of or published > as_of:
            return None
        evidence_id = str(item.get("evidence_id") or item.get("event_id") or item.get("id") or "")
        if not evidence_id:
            return None
        metadata = dict(item.get("metadata") or {})
        importance = _news_importance(item, metadata)
        sentiment = _float(
            _first_present(item, "sentiment")
            if _first_present(item, "sentiment") is not None
            else _first_present(metadata, "sentiment")
        )
        validated = any(
            _flag(item.get(name)) or _flag(metadata.get(name))
            for name in ("sentiment_validated", "sentiment_model")
        )
        return {
            "evidence_id": evidence_id,
            "event_type": str(item.get("event_type") or item.get("category") or "event"),
            "category": str(item.get("category") or item.get("event_type") or "event"),
            "subcategory": str(item.get("subcategory") or ""),
            "headline": str(item.get("headline") or item.get("title") or ""),
            "instrument_ids": [str(value) for value in instruments],
            "source": str(item.get("source") or ""),
            "source_url": str(item.get("source_url") or item.get("url") or ""),
            "published_at": published,
            "occurred_at": _as_datetime(item.get("occurred_at")),
            "received_at": received,
            "importance": importance,
            "relevance": _float(
                _first_present(item, "relevance")
                if _first_present(item, "relevance") is not None
                else metadata.get("relevance")
            ),
            "sentiment": sentiment,
            "sentiment_validated": validated,
            "fingerprint": str(
                item.get("fingerprint")
                or item.get("checksum")
                or item.get("duplicate_key")
                or metadata.get("checksum")
                or evidence_id
            ),
            "radar_event_id": str(item.get("event_id") or ""),
            "radar_verification": str(item.get("verification") or ""),
            "radar_evidence_count": item.get("evidence_count"),
        }
    return None


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _first_present(mapping: Mapping[str, Any], *names: str) -> Any:
    """First present value, preserving a real zero or false."""
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def _news_importance(item: Mapping[str, Any], metadata: Mapping[str, Any]) -> float | None:
    """Numeric importance from PI-01 metadata or an NM-04 importance band."""
    raw = _first_present(item, "importance")
    if raw is None:
        raw = _first_present(metadata, "importance", "importance_score")
    numeric = _float(raw)
    if numeric is not None:
        return numeric
    if isinstance(raw, str):
        band = raw.strip().lower()
        if band in ("critical", "high", "medium", "moderate", "low"):
            return {
                "critical": 75.0,
                "high": 55.0,
                "medium": 35.0,
                "moderate": 35.0,
                "low": 0.0,
            }[band]
    classification = item.get("classification")
    if isinstance(classification, Mapping):
        score = _float(classification.get("score"))
        if score is not None:
            return score
    return None


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no")
    return bool(value)


__all__ = ["FeatureEngine"]
