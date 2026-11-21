"""PI-04 focused verification against independent numerical fixtures."""

from __future__ import annotations

import asyncio
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from novacontrol.novamarket.features import (
    FEATURE_SET_VERSION,
    FeatureEngine,
    FeatureFamily,
    FeatureProfileName,
    FeatureQuality,
    FeatureRequest,
    FeatureService,
    FeatureSet,
    FeatureStatus,
    FeatureStore,
    PreprocessingMethod,
    RegimeDetector,
    RegimeLabel,
    MissingPolicy,
    diagnose,
    fit_preprocessor,
    profile_features,
    transform,
)
from novacontrol.novamarket.config import FeatureSettings
from novacontrol.novamarket.datasets.models import DatasetBuildRequest
from novacontrol.novamarket.marketdata.models import (
    AssetClass,
    DataInterval,
    DataQuality,
    Instrument,
    OhlcvBar,
)
from novacontrol.novamarket.marketdata.store import BarPage
from novacontrol.novamarket.service import NovaMarketService
from novacontrol.core.events import EventBus
from novacontrol.persistence import JsonStateStore
from novacontrol.novamarket.prediction.models import FundamentalSnapshot, NewsEventEvidence
from novacontrol.novamarket.strategies import indicators


def make_bars(
    closes: list[float],
    *,
    interval: DataInterval = DataInterval.DAY_1,
    volume: float | None = 1000.0,
    start: datetime | None = None,
    quality: DataQuality = DataQuality.VALID,
) -> list[OhlcvBar]:
    step = {
        DataInterval.MINUTE_1: timedelta(minutes=1),
        DataInterval.MINUTE_5: timedelta(minutes=5),
        DataInterval.HOUR_1: timedelta(hours=1),
        DataInterval.DAY_1: timedelta(days=1),
        DataInterval.WEEK_1: timedelta(days=7),
        DataInterval.MONTH_1: timedelta(days=30),
    }[interval]
    if start is None:
        start = datetime(2025, 1, 6, 9, 30, tzinfo=UTC) if interval.is_intraday else datetime(2025, 1, 6, tzinfo=UTC)
    bars: list[OhlcvBar] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        observed = start + step * index
        open_price = previous
        high = max(open_price, close) * 1.001
        low = min(open_price, close) * 0.999
        bars.append(
            OhlcvBar(
                bar_id=f"bar_{interval.value}_{index}",
                instrument_id="XNSE:RELIANCE",
                provider="fixture",
                interval=interval,
                open=Decimal(str(open_price)),
                high=Decimal(str(high)),
                low=Decimal(str(low)),
                close=Decimal(str(close)),
                currency="INR",
                observed_at=observed,
                retrieved_at=observed + timedelta(minutes=30),
                source_timezone="UTC",
                volume=(
                    None
                    if volume is None
                    else Decimal(str(volume if volume == 0 else volume + index))
                ),
                quality=quality,
            )
        )
        previous = close
    return bars


def as_of_for(bars: list[OhlcvBar], interval: DataInterval = DataInterval.DAY_1) -> datetime:
    close = bars[-1].observed_at + {
        DataInterval.HOUR_1: timedelta(hours=1),
        DataInterval.DAY_1: timedelta(days=1),
    }[interval]
    return close + timedelta(minutes=30)


class FakeMarketData:
    def __init__(self, bars_by_interval: dict[str, list[OhlcvBar]]) -> None:
        self.bars_by_interval = bars_by_interval
        self.calls = 0

    def get_instrument(self, instrument_id: str) -> Instrument:
        return Instrument(
            instrument_id=instrument_id,
            symbol="RELIANCE",
            name="Reliance Industries",
            asset_class=AssetClass.EQUITY,
            exchange="NSE",
            mic="XNSE",
            currency="INR",
            timezone="Asia/Kolkata",
        )

    def bars(self, instrument_id: str, *, interval: str, end: datetime, limit: int, ascending: bool = False) -> BarPage:
        self.calls += 1
        items = [
            bar
            for bar in self.bars_by_interval.get(str(interval), ())
            if bar.observed_at <= end
        ]
        items = items[-limit:]
        if not ascending:
            items = list(reversed(items))
        return BarPage(tuple(items), len(items), limit, 0)


# -- indicator reference fixtures --------------------------------------------


def test_indicators_match_independent_reference_values() -> None:
    assert indicators.sma([1, 2, 3, 4], 2) == Decimal("3.5000")
    assert indicators.ema([1, 2, 3], 2) == Decimal("2.5000")
    assert indicators.wma([1, 2, 3, 4], 3) == Decimal("3.3333")
    assert indicators.rsi_wilder([float(value) for value in range(1, 16)], 14) == Decimal("100.0000")
    assert indicators.rsi_wilder([float(value) for value in range(15, 0, -1)], 14) == Decimal("0.0000")
    assert indicators.rsi_wilder([5.0] * 20, 14) == Decimal("50.0000")
    assert indicators.atr_wilder([11.0] * 20, [9.0] * 20, [10.0] * 20, 14) == Decimal("2.0000")
    assert indicators.macd([10.0] * 60) == (Decimal("0.0000"), Decimal("0.0000"), Decimal("0.0000"))
    stochastic = indicators.stochastic([11.0] * 20, [9.0] * 20, [10.0] * 20, 14, 3)
    assert stochastic == (Decimal("50.0000"), Decimal("50.0000"))
    upward = indicators.directional_movement(
        [float(index + 2) for index in range(40)],
        [float(index) for index in range(40)],
        [float(index + 1) for index in range(40)],
        14,
    )
    assert upward is not None
    assert upward[1] == Decimal("50.0000") and upward[2] == Decimal("0.0000")
    assert upward[0] > Decimal("90")
    assert indicators.roc([10.0, 11.0], 1) == Decimal("10.0000")
    assert indicators.momentum([10.0, 11.0], 1) == Decimal("1.0000")
    assert indicators.williams_r([11.0] * 14, [9.0] * 14, [10.0] * 14, 14) == Decimal("-50.0000")
    assert indicators.rolling_std([1.0, 2.0, 3.0], 3) == Decimal("0.8165")
    assert indicators.historical_volatility([10.0] * 30, 20) == Decimal("0.0000")
    assert indicators.obv([10.0, 11.0, 10.0], [100.0, 200.0, 300.0]) == Decimal("-100.0000")
    assert indicators.money_flow_index([11.0] * 20, [9.0] * 20, [10.0] * 20, [100.0] * 20, 14) == Decimal("50.0000")
    assert indicators.vwap([11.0], [9.0], [10.0], [100.0]) == Decimal("10.0000")
    assert indicators.bollinger([10.0] * 20, 20, 2.0) == (
        Decimal("10.0000"),
        Decimal("10.0000"),
        Decimal("10.0000"),
    )
    assert indicators.true_range(11.0, 9.0, 10.0) == 2.0
    assert math.isclose(float(indicators.vpt([10.0, 11.0], [100.0, 100.0])), 10.0, rel_tol=1e-4)


def test_indicators_return_none_rather_than_inventing_a_short_series() -> None:
    assert indicators.sma([1.0], 2) is None
    assert indicators.ema([1.0], 2) is None
    assert indicators.rsi_wilder([1.0], 14) is None
    assert indicators.atr_wilder([1.0], [1.0], [1.0], 14) is None
    assert indicators.macd([1.0] * 10) is None
    assert indicators.directional_movement([1.0] * 10, [1.0] * 10, [1.0] * 10, 14) is None
    assert indicators.money_flow_index([1.0], [1.0], [1.0], [1.0], 14) is None


# -- feature engine -----------------------------------------------------------


def test_feature_engine_generates_price_indicator_and_structure_features() -> None:
    closes = [100.0 + index * 0.75 + (2.0 if index % 9 == 0 else 0.0) for index in range(260)]
    bars = make_bars(closes)
    request = FeatureRequest(
        instrument_id="XNSE:RELIANCE",
        as_of=as_of_for(bars),
        resolution="1d",
        profile="swing",
        timeframes=("1wk",),
    )
    result = FeatureEngine().generate(request, bars={"1d": bars, "1wk": bars})
    feature_set = result.feature_set
    assert feature_set.instrument_id == "XNSE:RELIANCE"
    assert feature_set.version == FEATURE_SET_VERSION
    assert feature_set.quality in (FeatureQuality.VALID, FeatureQuality.PARTIAL)
    assert feature_set.values["close"] == pytest.approx(closes[-1], rel=1e-6)
    assert feature_set.values["sma_20"] == pytest.approx(sum(closes[-20:]) / 20, rel=1e-4)
    assert feature_set.values["return_1"] == pytest.approx(closes[-1] / closes[-2] - 1, rel=1e-5)
    assert feature_set.values["rsi_14"] > 50
    assert feature_set.values["atr_14"] > 0
    assert feature_set.values["macd"] is not None
    assert feature_set.values["adx_14"] > 0
    assert feature_set.values["breakout_20"] in (0.0, 1.0)
    assert "trend_continuation" in feature_set.statuses
    assert feature_set.source_last_at is not None and feature_set.source_last_at <= request.as_of
    assert {"1d", "1wk"}.issubset(feature_set.timeframes)
    assert "sma_20" in feature_set.timeframes["1wk"]
    assert feature_set.digest_value
    vector = feature_set.to_prediction_vector()
    assert vector.feature_set_id == feature_set.feature_set_id
    assert vector.values["rsi_14"] == feature_set.values["rsi_14"]


def test_intraday_vwap_and_distance_are_generated_from_volume_bearing_bars() -> None:
    closes = [100.0 + index * 0.05 for index in range(60)]
    bars = make_bars(closes, interval=DataInterval.HOUR_1)
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of_for(bars, DataInterval.HOUR_1),
            resolution="1h",
            profile="intraday",
        ),
        bars={"1h": bars},
    )
    feature_set = result.feature_set
    assert feature_set.values["vwap"] > 0
    assert feature_set.values["distance_vwap"] == pytest.approx(
        feature_set.values["close"] / feature_set.values["vwap"] - 1.0,
        rel=1e-5,
    )
    assert feature_set.statuses["vwap"] is FeatureStatus.VALUE
    latest_session = [
        bar for bar in bars if bar.observed_at.date() == bars[-1].observed_at.date()
    ]
    expected_vwap = sum(
        (float(bar.high) + float(bar.low) + float(bar.close)) / 3.0 * float(bar.volume or 0)
        for bar in latest_session
    ) / sum(float(bar.volume or 0) for bar in latest_session)
    assert feature_set.values["vwap"] == pytest.approx(expected_vwap, rel=1e-6)


def test_market_structure_uses_confirmed_swings_and_detects_higher_highs_and_lows() -> None:
    closes: list[float] = []
    base = 100.0
    for cycle in range(18):
        closes.extend(base + index * 0.8 for index in range(4))
        closes.extend(base + 2.4 - index * 0.8 for index in range(4))
        base += 1.5
    bars = make_bars(closes)
    feature_set = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of_for(bars),
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": bars},
    ).feature_set
    assert feature_set.values["higher_high"] == pytest.approx(1.0)
    assert feature_set.values["higher_low"] == pytest.approx(1.0)
    assert feature_set.values["trend_continuation"] == pytest.approx(1.0)
    assert feature_set.values["support_proximity_20"] >= 0.0
    assert feature_set.values["resistance_proximity_20"] >= 0.0
    assert feature_set.values["distance_trading_range_20"] == pytest.approx(
        (feature_set.values["close"] - feature_set.values["rolling_low_20"])
        / (feature_set.values["rolling_high_20"] - feature_set.values["rolling_low_20"]),
        rel=1e-4,
    )


def test_profiles_are_not_identical_and_vwap_is_explicitly_unavailable_on_daily_bars() -> None:
    closes = [100.0 + index * 0.3 for index in range(220)]
    bars = make_bars(closes)
    engine = FeatureEngine()
    swing = engine.generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of_for(bars),
            resolution="1d",
            profile="intraday",
            timeframes=(),
        ),
        bars={"1d": bars},
    ).feature_set
    long = engine.generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of_for(bars),
            resolution="1d",
            profile="long_term",
            timeframes=(),
        ),
        bars={"1d": bars},
    ).feature_set
    assert "vwap" in swing.statuses
    assert swing.statuses["vwap"] is FeatureStatus.UNAVAILABLE
    assert "sma_200" not in swing.statuses
    assert "sma_200" in long.statuses
    assert long.values["sma_200"] is not None
    assert set(swing.feature_names) != set(long.feature_names)


def test_flat_price_and_zero_volume_are_not_division_errors() -> None:
    bars = make_bars([100.0] * 80, volume=0.0)
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of_for(bars),
            resolution="1d",
            profile="intraday",
        ),
        bars={"1d": bars},
    )
    feature_set = result.feature_set
    assert feature_set.values["rsi_14"] == pytest.approx(50.0)
    assert feature_set.values["atr_pct"] >= 0.0
    assert feature_set.values["bollinger_width_pct"] == pytest.approx(0.0)
    assert feature_set.statuses["relative_volume_20"] is FeatureStatus.MISSING
    assert feature_set.statuses["vwap"] is FeatureStatus.UNAVAILABLE
    assert result.regime.primary in (RegimeLabel.SIDEWAYS, RegimeLabel.LOW_VOLATILITY)
    assert result.regime.confidence is None


def test_insufficient_history_reports_unavailable_and_uncertain() -> None:
    bars = make_bars([100.0, 101.0, 100.5, 102.0, 101.5])
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of_for(bars),
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": bars},
    )
    assert result.feature_set.quality in (FeatureQuality.INSUFFICIENT, FeatureQuality.INVALID)
    assert result.feature_set.statuses["rsi_14"] is FeatureStatus.UNAVAILABLE
    assert result.regime.primary is RegimeLabel.UNCERTAIN
    assert result.regime.data_quality == "insufficient"
    assert "rsi_14" not in result.feature_set.values
    vector = result.feature_set.to_prediction_vector()
    assert math.isfinite(vector.values["close"])


def test_duplicate_out_of_order_invalid_parts_and_future_bars_are_excluded() -> None:
    closes = [100.0 + index for index in range(80)]
    bars = make_bars(closes)
    as_of = as_of_for(bars)
    duplicate = OhlcvBar(
        bar_id="duplicate_latest",
        instrument_id="XNSE:RELIANCE",
        provider="fixture",
        interval=DataInterval.DAY_1,
        open=bars[-1].open,
        high=Decimal("1000"),
        low=Decimal("50"),
        close=Decimal("999"),
        currency="INR",
        observed_at=bars[-1].observed_at,
        retrieved_at=bars[-1].retrieved_at + timedelta(hours=1),
        source_timezone="UTC",
        volume=Decimal("1000"),
        quality=DataQuality.VALID,
    )
    out_of_order = list(reversed(bars)) + [duplicate]
    invalid = make_bars([9999.0], start=as_of - timedelta(days=5), quality=DataQuality.INVALID)[0]
    future = make_bars([12345.0], start=as_of + timedelta(days=1))[0]
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of,
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": [*out_of_order, invalid, future]},
    )
    lineage = result.feature_set.lineage
    assert lineage["duplicates"]["1d"] == 1
    assert lineage["out_of_order"]["1d"] > 0
    assert lineage["invalid_bars"]["1d"] >= 1
    assert lineage["future_bars"]["1d"] >= 1
    assert result.feature_set.values["close"] == pytest.approx(999.0)
    assert result.feature_set.source_last_at <= as_of


def test_market_closure_gap_is_counted_without_invalidating_the_series() -> None:
    bars = make_bars([100.0 + index * 0.25 for index in range(80)])
    closed = bars[:40] + bars[42:]
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of_for(closed),
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": closed},
    )
    assert result.feature_set.lineage["gaps"]["1d"] == 1
    assert result.feature_set.lineage["invalid_bars"]["1d"] == 0
    assert result.feature_set.values["close"] == pytest.approx(float(closed[-1].close))
    assert result.feature_set.quality in (
        FeatureQuality.VALID,
        FeatureQuality.PARTIAL,
    )


def test_legitimate_extreme_move_is_retained_not_discarded_as_invalid() -> None:
    closes = [100.0] * 40 + [130.0] + [131.0] * 20
    bars = make_bars(closes)
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of_for(bars),
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": bars},
    )
    assert result.feature_set.values["close"] == pytest.approx(131.0)
    assert result.feature_set.statuses["return_1"] is FeatureStatus.ZERO
    assert result.feature_set.values["return_1"] == pytest.approx(0.0)


def test_missing_primary_resolution_is_not_silently_replaced_by_another_timeframe() -> None:
    daily = make_bars([100.0 + index * 0.3 for index in range(90)])
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of_for(daily),
            resolution="1h",
            profile="intraday",
            timeframes=("1d",),
        ),
        bars={"1d": daily},
    )
    feature_set = result.feature_set
    assert feature_set.quality is FeatureQuality.INVALID
    assert "close" not in feature_set.values
    assert feature_set.timeframes["1h"] == {}
    assert result.regime.primary is RegimeLabel.UNCERTAIN


def test_multi_timeframe_alignment_uses_only_completed_timeframes() -> None:
    daily = make_bars(
        [100.0 + index * 0.4 for index in range(100)],
        interval=DataInterval.DAY_1,
        start=datetime(2024, 1, 1, tzinfo=UTC),
    )
    hourly = make_bars(
        [100.0 + index * 0.05 for index in range(100)],
        interval=DataInterval.HOUR_1,
        start=datetime(2025, 1, 1, 9, 30, tzinfo=UTC),
    )
    as_of = hourly[-2].observed_at + timedelta(hours=1)
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of,
            resolution="1h",
            profile="intraday",
            timeframes=("1d",),
        ),
        bars={"1h": hourly, "1d": daily},
    )
    feature_set = result.feature_set
    assert feature_set.timeframes["1h"]
    assert feature_set.timeframes["1d"]
    assert feature_set.values["mtf_available_count"] == pytest.approx(2.0)
    assert 0.0 <= feature_set.values["mtf_alignment"] <= 1.0
    assert feature_set.values["mtf_short_within_long_structure"] is not None
    assert all(bar.observed_at <= as_of for bar in hourly[-1:])
    assert feature_set.lineage["future_bars"]["1h"] == 0


def test_multi_timeframe_aggregation_uses_completed_weekly_buckets_only() -> None:
    daily = make_bars(
        [100.0 + index * 0.2 for index in range(100)],
        interval=DataInterval.DAY_1,
        start=datetime(2025, 1, 6, tzinfo=UTC),
    )
    as_of = daily[-1].observed_at + timedelta(days=1)
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of,
            resolution="1d",
            profile="swing",
            timeframes=("1wk",),
        ),
        bars={"1d": daily},
    )
    assert result.feature_set.lineage["derived_timeframes"]["1wk"] == "1d"
    assert result.feature_set.timeframes["1wk"]
    assert result.feature_set.values["mtf_available_count"] == pytest.approx(2.0)

    partial = make_bars(
        [100.0 + index * 0.2 for index in range(10)],
        interval=DataInterval.DAY_1,
        start=datetime(2025, 1, 6, tzinfo=UTC),
    )
    partial_as_of = partial[-1].observed_at + timedelta(hours=8)
    partial_result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=partial_as_of,
            resolution="1d",
            profile="swing",
            timeframes=("1wk",),
        ),
        bars={"1d": partial},
    )
    assert partial_result.feature_set.timeframes["1wk"]
    assert partial_result.feature_set.lineage["bar_counts"]["1wk"] == 1
    assert partial_result.feature_set.lineage["derived_timeframes"]["1wk"] == "1d"


def test_a_not_yet_closed_daily_bar_is_not_visible_at_an_intraday_cutoff() -> None:
    instrument = Instrument(
        instrument_id="XNSE:RELIANCE",
        symbol="RELIANCE",
        name="Reliance Industries",
        asset_class=AssetClass.EQUITY,
        exchange="NSE",
        mic="XNSE",
        currency="INR",
        timezone="Asia/Kolkata",
        provider_symbols={"fixture": "RELIANCE"},
    )
    bars = make_bars([100.0 + index * 0.5 for index in range(50)])
    last = bars[-1]
    local_open = last.observed_at.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
    cutoff = local_open.replace(hour=15, minute=0)
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=cutoff,
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": bars},
        instrument=instrument,
    )
    assert result.feature_set.lineage["partial_bars"]["1d"] >= 1
    assert result.feature_set.source_last_at is not None
    assert result.feature_set.source_last_at < last.observed_at
    assert result.feature_set.values["close"] == pytest.approx(float(bars[-2].close))


def test_context_features_are_explicit_when_validated_inputs_are_supplied() -> None:
    bars = make_bars([100.0 + index * 0.3 for index in range(80)])
    benchmark = make_bars([200.0 + index * 0.1 for index in range(80)])
    sector = make_bars([50.0 + index * 0.2 for index in range(80)])
    peers = {
        "A": make_bars([10.0 + index * 0.1 for index in range(80)]),
        "B": make_bars([20.0 - index * 0.02 for index in range(80)]),
        "C": make_bars([30.0 + index * 0.03 for index in range(80)]),
    }
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of_for(bars),
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": bars},
        benchmark_bars={"1d": benchmark},
        sector_bars={"1d": sector},
        context_bars={key: {"1d": value} for key, value in peers.items()},
    )
    feature_set = result.feature_set
    assert feature_set.values["relative_return_20"] is not None
    assert feature_set.values["sector_relative_return_20"] is not None
    assert feature_set.values["market_volatility_20"] is not None
    assert feature_set.values["market_breadth"] == pytest.approx(2.0 / 3.0)
    assert feature_set.lineage["context"]["breadth_instruments"] == 3


def test_fundamental_features_are_point_in_time_and_distinguish_reported_from_derived() -> None:
    bars = make_bars([100.0 + index * 0.2 for index in range(220)])
    as_of = as_of_for(bars)
    latest = FundamentalSnapshot(
        snapshot_id="fsn_latest",
        instrument_id="XNSE:RELIANCE",
        fiscal_period="FY2025",
        source_observed_at=as_of - timedelta(days=20),
        received_at=as_of - timedelta(days=10),
        currency="INR",
        metrics={
            "revenue": "120",
            "net_profit": "24",
            "eps": "5",
            "shareholders_equity": "60",
            "total_debt": "30",
            "operating_income": "12",
            "operating_cash_flow": "18",
            "dividend_per_share": "1",
            "shares_outstanding": "10",
        },
        source="fixture-filing",
    )
    prior = FundamentalSnapshot(
        snapshot_id="fsn_prior",
        instrument_id="XNSE:RELIANCE",
        fiscal_period="FY2024",
        source_observed_at=as_of - timedelta(days=400),
        received_at=as_of - timedelta(days=390),
        currency="INR",
        metrics={"revenue": "100", "net_profit": "20", "operating_cash_flow": "15"},
        source="fixture-filing",
    )
    future = FundamentalSnapshot(
        snapshot_id="fsn_future",
        instrument_id="XNSE:RELIANCE",
        fiscal_period="FY2026",
        source_observed_at=as_of - timedelta(days=1),
        received_at=as_of + timedelta(days=1),
        currency="INR",
        metrics={"eps": "999", "revenue": "999"},
        source="fixture-filing",
    )
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of,
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": bars},
        fundamentals=(prior, latest, future),
    )
    feature_set = result.feature_set
    assert feature_set.values["fundamental_eps"] == pytest.approx(5.0)
    assert feature_set.values["fundamental_revenue"] == pytest.approx(120.0)
    assert feature_set.values["fundamental_net_profit"] == pytest.approx(24.0)
    assert feature_set.values["fundamental_cash_flow"] == pytest.approx(18.0)
    assert feature_set.values["fundamental_dividend_per_share"] == pytest.approx(1.0)
    assert feature_set.values["fundamental_revenue_growth"] == pytest.approx(0.2)
    assert feature_set.values["fundamental_profit_growth"] == pytest.approx(0.2)
    assert feature_set.values["fundamental_pe"] == pytest.approx(feature_set.values["close"] / 5.0)
    assert feature_set.values["fundamental_debt_to_equity"] == pytest.approx(0.5)
    assert feature_set.values["fundamental_profit_margin"] == pytest.approx(0.2)
    provenance = feature_set.lineage["fundamental_provenance"]
    assert provenance["selected_snapshot_id"] == "fsn_latest"
    assert provenance["available_at"] == latest.received_at.isoformat()
    assert provenance["reported_metrics"]
    assert "fundamental_pe" in provenance["derived_features"]
    assert feature_set.statuses["fundamental_eps"] is FeatureStatus.VALUE


def test_a_fundamental_published_after_the_cutoff_is_not_visible() -> None:
    bars = make_bars([100.0] * 60)
    as_of = as_of_for(bars)
    snapshot = FundamentalSnapshot(
        snapshot_id="fsn_late",
        instrument_id="XNSE:RELIANCE",
        fiscal_period="FY2026",
        source_observed_at=as_of - timedelta(hours=1),
        received_at=as_of + timedelta(hours=1),
        currency="INR",
        metrics={"eps": "42"},
        source="fixture-filing",
    )
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of,
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": bars},
        fundamentals=(snapshot,),
    )
    assert result.feature_set.statuses["fundamental_eps"] is FeatureStatus.UNAVAILABLE
    assert "fundamental_eps" not in result.feature_set.values


def test_news_features_deduplicate_preserve_availability_and_do_not_invent_sentiment() -> None:
    bars = make_bars([100.0] * 80)
    as_of = as_of_for(bars)
    evidence = NewsEventEvidence(
        evidence_id="news_1",
        instrument_ids=("XNSE:RELIANCE",),
        event_type="earnings",
        headline="Quarterly results",
        source="fixture-wire",
        published_at=as_of - timedelta(days=1),
        received_at=as_of - timedelta(hours=20),
        checksum="same-article",
        metadata={"category": "earnings", "importance": 4.0, "sentiment": 0.7},
    )
    duplicate = NewsEventEvidence(
        evidence_id="news_2",
        instrument_ids=("XNSE:RELIANCE",),
        event_type="earnings",
        headline="Quarterly results",
        source="fixture-wire",
        published_at=as_of - timedelta(days=1),
        received_at=as_of - timedelta(hours=20),
        checksum="same-article",
        metadata={"category": "earnings", "importance": 4.0, "sentiment": 0.7},
    )
    future = NewsEventEvidence(
        evidence_id="news_future",
        instrument_ids=("XNSE:RELIANCE",),
        event_type="earnings",
        headline="Future results",
        source="fixture-wire",
        published_at=as_of + timedelta(hours=1),
        received_at=as_of + timedelta(hours=1),
        checksum="future",
        metadata={"importance": 5.0},
    )
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of,
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": bars},
        news_events=(evidence, duplicate, future),
    )
    feature_set = result.feature_set
    assert feature_set.values["news_count_5d"] == pytest.approx(1.0)
    assert feature_set.values["news_importance_max_20d"] == pytest.approx(4.0)
    assert feature_set.values["news_earnings_flag"] == pytest.approx(1.0)
    assert feature_set.statuses["news_sentiment_mean_5d"] is FeatureStatus.UNAVAILABLE
    assert "news_sentiment_mean_5d" not in feature_set.values
    provenance = feature_set.lineage["news_provenance"]
    assert len(provenance) == 1
    assert provenance[0]["published_at"] == evidence.published_at.isoformat()
    assert provenance[0]["availability"] == evidence.received_at.isoformat()
    assert feature_set.values["news_age_days"] >= 0


# -- regimes -----------------------------------------------------------------


def test_nm04_radar_event_shape_is_accepted_without_received_at() -> None:
    bars = make_bars([100.0] * 80)
    as_of = as_of_for(bars)
    radar_event = {
        "event_id": "radar_1",
        "title": "Quarterly results announced",
        "category": "earnings",
        "subcategory": "results",
        "instruments": ["XNSE:RELIANCE"],
        "published_at": (as_of - timedelta(days=1)).isoformat(),
        "occurred_at": (as_of - timedelta(days=2)).isoformat(),
        "retrieved_at": (as_of - timedelta(hours=20)).isoformat(),
        "discovered_at": (as_of - timedelta(hours=20)).isoformat(),
        "importance": "high",
        "relevance": 0.8,
        "verification": "confirmed",
        "fingerprint": "radar-fingerprint",
        "evidence_count": 2,
        "classification": {"score": 55.0},
    }
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of,
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": bars},
        news_events=(radar_event,),
    )
    feature_set = result.feature_set
    assert feature_set.values["news_count_20d"] == pytest.approx(1.0)
    assert feature_set.values["news_importance_max_20d"] == pytest.approx(55.0)
    assert feature_set.values["news_relevance_max_20d"] == pytest.approx(0.8)
    assert feature_set.values["news_earnings_flag"] == pytest.approx(1.0)
    provenance = feature_set.lineage["news_provenance"]
    assert provenance[0]["evidence_id"] == "radar_1"
    assert provenance[0]["radar_event_id"] == "radar_1"
    assert provenance[0]["received_at"] == radar_event["retrieved_at"]
    assert provenance[0]["occurred_at"] == radar_event["occurred_at"]


def test_regime_detection_strong_uptrend_and_transition() -> None:
    bars = make_bars([100.0 + index * 1.1 for index in range(160)])
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of_for(bars),
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": bars},
    )
    assert result.regime.primary in (RegimeLabel.STRONG_UP, RegimeLabel.WEAK_UP)
    assert result.regime.confidence is None
    assert result.regime.confidence_calibrated is False
    assert result.regime.method == "pi04-regime-1.0"
    assert result.regime.supporting_features
    previous = result.regime
    later = make_bars([100.0 + index * 1.1 for index in range(161)])
    later_result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of_for(later),
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": later},
        previous_regime=previous,
    )
    assert later_result.regime.transition is not None
    assert later_result.regime.transition.previous is previous.primary


def test_regime_detection_strong_or_weak_downtrend() -> None:
    bars = make_bars([300.0 - index * 1.2 for index in range(160)])
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of_for(bars),
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": bars},
    )
    assert result.regime.primary in (RegimeLabel.STRONG_DOWN, RegimeLabel.WEAK_DOWN)


def test_regime_detection_flat_sideways_and_high_volatility_secondary() -> None:
    flat = make_bars([100.0] * 120)
    flat_result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of_for(flat),
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": flat},
    )
    assert flat_result.regime.primary in (
        RegimeLabel.SIDEWAYS,
        RegimeLabel.LOW_VOLATILITY,
    )
    volatile_closes = [100.0]
    for index in range(119):
        volatile_closes.append(volatile_closes[-1] * (1.12 if index % 4 == 0 else 0.98))
    volatile = make_bars(volatile_closes)
    volatile_result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of_for(volatile),
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": volatile},
    )
    assert volatile_result.regime.primary is not RegimeLabel.UNCERTAIN
    assert (
        RegimeLabel.HIGH_VOLATILITY in volatile_result.regime.secondary
        or volatile_result.regime.primary is RegimeLabel.HIGH_VOLATILITY
    )


def test_regime_detection_can_report_uncertain_without_fabricating_confidence() -> None:
    bars = make_bars([100.0] * 8)
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of_for(bars),
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": bars},
    )
    assert result.regime.primary is RegimeLabel.UNCERTAIN
    assert result.regime.confidence is None
    assert result.regime.reasons


def test_regime_detector_can_be_called_directly_with_a_previous_label() -> None:
    bars = make_bars([100.0] * 10)
    feature_set = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of_for(bars),
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": bars},
    ).feature_set
    result = RegimeDetector().detect(feature_set, previous=RegimeLabel.SIDEWAYS)
    assert result.transition is not None
    assert result.transition.changed is (result.primary is not RegimeLabel.SIDEWAYS)


# -- preprocessing and selection --------------------------------------------


def test_preprocessing_fits_training_rows_only_and_preserves_missingness() -> None:
    training = [{"a": 1.0, "b": 10.0}, {"a": 2.0, "b": 20.0}, {"a": 3.0, "b": 30.0}]
    fit = fit_preprocessor(
        training,
        method=PreprocessingMethod.STANDARD,
        missing_policy=MissingPolicy.MEDIAN,
    )
    assert fit.parameters["center"]["a"] == pytest.approx(2.0)
    transformed = transform(fit, {"a": 2.0, "b": 20.0})
    assert transformed.values["a"] == pytest.approx(0.0)
    missing = transform(fit, {"a": None, "b": 20.0})
    assert missing.statuses["a"] is FeatureStatus.IMPUTED
    assert missing.imputed["a"] == pytest.approx(0.0)
    unchanged = fit_preprocessor(training, method=PreprocessingMethod.STANDARD)
    assert unchanged.parameters["center"]["a"] == pytest.approx(2.0)
    future = transform(unchanged, {"a": 1000.0, "b": 20.0})
    assert future.outliers == ("a",)
    assert fit.parameters["center"]["a"] == pytest.approx(2.0)


def test_preprocessing_never_silently_invents_a_missing_value() -> None:
    fit = fit_preprocessor(
        [{"a": 1.0}, {"a": 2.0}, {"a": 3.0}],
        method=PreprocessingMethod.ROBUST,
        missing_policy=MissingPolicy.DROP,
    )
    result = transform(fit, {"a": None})
    assert result.statuses["a"] is FeatureStatus.MISSING
    assert "a" not in result.values
    assert result.imputed == {}
    with pytest.raises(ValueError):
        transform(
            fit_preprocessor(
                [{"a": 1.0}, {"a": 2.0}],
                missing_policy=MissingPolicy.ERROR,
            ),
            {"a": None},
        )


def test_selection_reports_correlation_missingness_and_never_deletes() -> None:
    rows = [
        {"constant": 2.0, "a": float(index), "b": float(index * 2), "sparse": None if index < 8 else 1.0}
        for index in range(10)
    ]
    report = diagnose(rows, missing_threshold=0.5, correlation_threshold=0.9)
    issues = {(item.feature, str(item.issue)) for item in report.decisions}
    assert ("constant", "zero_variance") in issues or ("constant", "low_variance") in issues
    assert ("b", "highly_correlated") in issues
    assert ("sparse", "excessive_missingness") in issues
    assert all(item.retained for item in report.decisions)
    assert report.to_dict()["automatic_deletion"] is False


# -- persistence and service integration ------------------------------------


def test_feature_store_is_traceable_immutable_and_deduplicates(tmp_path: Path) -> None:
    bars = make_bars([100.0 + index * 0.2 for index in range(120)])
    result = FeatureEngine().generate(
        FeatureRequest(
            instrument_id="XNSE:RELIANCE",
            as_of=as_of_for(bars),
            resolution="1d",
            profile="swing",
        ),
        bars={"1d": bars},
    )
    store = FeatureStore(tmp_path / "features.sqlite3")
    try:
        saved, duplicate = store.save(
            result.feature_set,
            result.regime,
            generation_id="fg_test",
            definitions=FeatureEngine().definition_payload(),
        )
        assert saved is True and duplicate is False
        saved_again, duplicate_again = store.save(
            result.feature_set,
            result.regime,
            generation_id="fg_test_2",
        )
        assert saved_again is False and duplicate_again is True
        loaded = store.get_feature_set(result.feature_set.feature_set_id)
        assert loaded is not None and loaded.digest_value == result.feature_set.digest_value
        assert store.latest_feature_set("XNSE:RELIANCE") is not None
        assert store.get_regime(result.regime.regime_id) is not None
        assert store.stats()["feature_sets"] == 1
        immutable = sqlite3.connect(tmp_path / "features.sqlite3")
        try:
            with pytest.raises(sqlite3.DatabaseError):
                immutable.execute("DELETE FROM feature_sets")
            with pytest.raises(sqlite3.DatabaseError):
                immutable.execute("UPDATE feature_definitions SET digest='changed'")
        finally:
            immutable.close()
    finally:
        store.close()


def test_feature_service_persists_and_emits_typed_events(tmp_path: Path) -> None:
    bars = make_bars([100.0 + index * 0.5 for index in range(140)])
    market = FakeMarketData({"1d": bars})
    events: list[tuple[str, dict]] = []
    service = FeatureService(
        market_data=market,
        store=FeatureStore(tmp_path / "service.sqlite3"),
        event_sink=lambda event_type, payload: events.append((str(event_type), dict(payload))),
    )
    request = FeatureRequest(
        instrument_id="XNSE:RELIANCE",
        as_of=as_of_for(bars),
        resolution="1d",
        profile="swing",
    )
    try:
        first = asyncio.run(service.generate(request))
        second = asyncio.run(service.generate(request))
        assert first.persisted is True
        assert first.feature_set.feature_set_id == second.feature_set.feature_set_id
        assert second.duplicate is True
        assert service.latest_feature_set("XNSE:RELIANCE") is not None
        assert service.latest_regime("XNSE:RELIANCE") is not None
        assert any(item[0] == "novamarket.features.generated" for item in events)
        assert any(item[0] == "novamarket.regime.detected" for item in events)
        assert service.status()["statistics"]["feature_sets"] == 1
        assert service.contract()["authority"]["orders_emitted"] is False
        service.fit_preprocessing([{"a": 1.0}, {"a": 2.0}, {"a": 3.0}])
        service.diagnose([{"a": 1.0}, {"a": 2.0}, {"a": 3.0}])
        assert any(item[0] == "novamarket.features.preprocessing.fitted" for item in events)
        assert any(item[0] == "novamarket.features.selection.completed" for item in events)
    finally:
        service.close()


def test_feature_service_uses_configured_regime_thresholds_and_missing_policy() -> None:
    settings = FeatureSettings(
        strong_adx=30.0,
        trend_adx=25.0,
        high_volatility_range_expansion=1.4,
        low_volatility_range_expansion=0.6,
        missing_policy="drop",
    )
    service = FeatureService(market_data=FakeMarketData({}), settings=settings)
    try:
        assert service.engine._regime.strong_adx == pytest.approx(30.0)
        assert service.engine._regime.trend_adx == pytest.approx(25.0)
        fit = service.fit_preprocessing([{"a": 1.0}, {"a": 2.0}, {"a": 3.0}])
        assert fit.parameters["missing_policy"] == "drop"
    finally:
        service.close()


def test_feature_service_reports_a_transition_from_its_persisted_previous_regime(tmp_path: Path) -> None:
    first_closes = [100.0 + index * 0.8 for index in range(120)]
    first_bars = make_bars(first_closes)
    market = FakeMarketData({"1d": first_bars})
    service = FeatureService(
        market_data=market,
        store=FeatureStore(tmp_path / "transitions.sqlite3"),
    )
    try:
        first = asyncio.run(
            service.generate(
                FeatureRequest(
                    instrument_id="XNSE:RELIANCE",
                    as_of=as_of_for(first_bars),
                    resolution="1d",
                    profile="swing",
                )
            )
        )
        second_closes = first_closes + [first_closes[-1] - index * 1.2 for index in range(1, 60)]
        second_bars = make_bars(second_closes)
        second = asyncio.run(
            service.generate(
                FeatureRequest(
                    instrument_id="XNSE:RELIANCE",
                    as_of=as_of_for(second_bars),
                    resolution="1d",
                    profile="swing",
                )
            )
        )
        assert second.regime.transition is not None
        assert second.regime.transition.previous is first.regime.primary
        assert service.latest_regime("XNSE:RELIANCE") is not None
    finally:
        service.close()


def test_actual_novamarket_service_generates_from_the_existing_market_store(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus = EventBus(continue_on_error=True, history=200)
        service = NovaMarketService(
            store=JsonStateStore(tmp_path),
            event_bus=bus,
            data_dir=tmp_path,
        )
        try:
            await service.enable(reason="PI-04 integration test")
            market = service._ensure_market_data()
            instrument = Instrument(
                instrument_id="XNSE:RELIANCE",
                symbol="RELIANCE",
                name="Reliance Industries",
                asset_class=AssetClass.EQUITY,
                exchange="NSE",
                mic="XNSE",
                currency="INR",
                timezone="Asia/Kolkata",
                provider_symbols={"fixture": "RELIANCE"},
            )
            market.register_instrument(instrument)
            bars = make_bars([100.0 + index * 0.6 for index in range(140)])
            assert service._market_store is not None
            for bar in bars:
                service._market_store.append_bar(bar)
            request = FeatureRequest(
                instrument_id="XNSE:RELIANCE",
                as_of=as_of_for(bars),
                resolution="1d",
                profile="swing",
            )
            body = await service.generate_features(request)
            assert body["persisted"] is True
            assert body["feature_set"]["values"]["close"] == pytest.approx(float(bars[-1].close))
            assert body["authorities"]["features_authorize_action"] is False
            assert service.feature_status()["initialised"] is True
            assert service.feature_contract()["implemented"] is True
            assert service.latest_feature_set("XNSE:RELIANCE") is not None
            assert service.latest_regime("XNSE:RELIANCE") is not None
            assert service.status()["features"]["implemented"] is True
            granted = service.status()["permissions"]["granted"]
            assert "market.feature.generate" in granted
        finally:
            await service.shutdown(reason="PI-04 integration teardown")

    asyncio.run(scenario())


def test_feature_generation_honours_an_actual_pi02_dataset_membership(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = NovaMarketService(
            store=JsonStateStore(tmp_path),
            event_bus=EventBus(continue_on_error=True, history=100),
            data_dir=tmp_path,
        )
        try:
            await service.enable(reason="PI-04 dataset integration test")
            market = service._ensure_market_data()
            instrument = Instrument(
                instrument_id="XNSE:RELIANCE",
                symbol="RELIANCE",
                name="Reliance Industries",
                asset_class=AssetClass.EQUITY,
                exchange="NSE",
                mic="XNSE",
                currency="INR",
                timezone="Asia/Kolkata",
                provider_symbols={"fixture": "RELIANCE"},
            )
            market.register_instrument(instrument)
            bars = make_bars([100.0 + index * 0.4 for index in range(140)])
            assert service._market_store is not None
            for bar in bars:
                service._market_store.append_bar(bar)
            built = await service.build_historical_dataset(
                DatasetBuildRequest(
                    dataset_id="pi04-feature-source",
                    instruments=("XNSE:RELIANCE",),
                    resolution=DataInterval.DAY_1,
                    start_at=bars[0].observed_at,
                    end_at=bars[-1].observed_at + timedelta(days=1),
                    allow_partial=True,
                    min_coverage_ratio=0.5,
                ),
                fetch=False,
            )
            request = FeatureRequest(
                instrument_id="XNSE:RELIANCE",
                as_of=as_of_for(bars),
                resolution="1d",
                profile="swing",
                dataset_id="pi04-feature-source",
                dataset_version=str(built["version"]),
            )
            body = await service.generate_features(request)
            assert body["persisted"] is True
            assert body["feature_set"]["dataset_versions"] == [
                f"pi04-feature-source@{built['version']}"
            ]
            assert body["feature_set"]["values"]["close"] == pytest.approx(float(bars[-1].close))
        finally:
            await service.shutdown(reason="PI-04 dataset integration teardown")

    asyncio.run(scenario())


def test_incremental_identical_input_reuses_the_cached_feature_row() -> None:
    bars = make_bars([100.0 + index * 0.1 for index in range(90)])
    engine = FeatureEngine(cache_size=4)
    request = FeatureRequest(
        instrument_id="XNSE:RELIANCE",
        as_of=as_of_for(bars),
        resolution="1d",
        profile="swing",
    )
    first = engine.generate(request, bars={"1d": bars})
    second = engine.generate(request, bars={"1d": bars})
    assert first.feature_set.feature_set_id == second.feature_set.feature_set_id
    assert engine.cache_entries == 1
    engine.clear_cache()
    assert engine.cache_entries == 0
