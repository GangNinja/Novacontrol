"""Post-PI-24 multi-timeframe intelligence tests.

Covers the 20m context → 5m setup → 1m timing matrix, data-integrity refusals,
the one-minute scheduler horizon and the strategy/risk integration seam.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from novacontrol.novamarket.marketdata.models import DataInterval
from novacontrol.novamarket.multitimeframe import (
    ConflictStatus,
    MarketCondition,
    MultiTimeframeAnalyzer,
    MultiTimeframeConfig,
    SetupState,
    TimeframeDirection,
    TimeframeLabel,
    TimeframeLayer,
    TimeframeOutcome,
    TimingState,
    Uncertainty,
)
from novacontrol.novamarket.portfolio import PortfolioEngine, PortfolioStore
from novacontrol.novamarket.scheduling.schedule import build_checkpoints, build_cycle
from novacontrol.novamarket.strategies import StrategyEngine, StrategyStore
from novacontrol.novamarket.wallet.engine import WalletEngine
from novacontrol.novamarket.wallet.store import WalletStore

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


def make_bars(
    interval: DataInterval,
    *,
    step_minutes: int,
    start: float,
    slope: float,
    count: int,
    end: datetime = NOW,
    partial_last: bool = False,
    duplicate_at: int | None = None,
    gap_at: int | None = None,
    reverse: bool = False,
) -> list[Any]:
    bars = []
    first = end - timedelta(minutes=step_minutes * (count - 1))
    for index in range(count):
        observed = first + timedelta(minutes=step_minutes * index)
        if gap_at is not None and index >= gap_at:
            observed += timedelta(minutes=step_minutes * 3)
        close = start + slope * index
        bars.append(
            SimpleNamespace(
                bar_id=f"{interval.value}-{index}",
                open=close - 0.01,
                high=close + 0.03,
                low=close - 0.03,
                close=close,
                volume=1000.0 + index,
                observed_at=observed,
                partial=partial_last and index == count - 1,
                usable=True,
            )
        )
    if duplicate_at is not None and 0 < duplicate_at < len(bars):
        bars.insert(duplicate_at, bars[duplicate_at - 1])
    if reverse:
        bars = list(reversed(bars))
    return bars


class SeriesMarket:
    def __init__(self, series: dict[str, list[Any]]) -> None:
        self.series = series
        self.calls: list[str] = []

    def bars(
        self,
        instrument_id: str,
        *,
        interval: DataInterval | str | None = None,
        limit: int = 400,
        ascending: bool = False,
        **_: Any,
    ) -> Any:
        key = DataInterval(interval).value if interval is not None else "1d"
        self.calls.append(key)
        items = list(self.series.get(key, []))
        if not ascending:
            items = list(reversed(items))
        return SimpleNamespace(items=items[:limit])


def bullish_series(**overrides: Any) -> dict[str, list[Any]]:
    five = make_bars(
        DataInterval.MINUTE_5, step_minutes=5, start=100.0, slope=0.08, count=80
    )
    one = make_bars(
        DataInterval.MINUTE_1, step_minutes=1, start=106.4, slope=0.01, count=30
    )
    return {"5m": overrides.get("5m", five), "1m": overrides.get("1m", one)}


def layer(
    label: TimeframeLabel,
    direction: TimeframeDirection,
    *,
    interval: str = "5m",
    available: bool = True,
    stale: bool = False,
    confidence: str = "0.80",
    condition: MarketCondition | None = None,
    setup_state: SetupState | None = None,
    timing_state: TimingState | None = None,
    reasons: tuple[str, ...] = (),
) -> TimeframeLayer:
    return TimeframeLayer(
        label=label,
        interval=interval,
        direction=direction,
        available=available,
        stale=stale,
        observations=40,
        confidence=Decimal(confidence),
        as_of=NOW,
        reasons=reasons,
        condition=condition,
        setup_state=setup_state,
        timing_state=timing_state,
    )


class ResolutionMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = MultiTimeframeAnalyzer(SeriesMarket({}), clock=lambda: NOW)

    def resolve(
        self,
        context: TimeframeLayer,
        setup: TimeframeLayer,
        timing: TimeframeLayer,
    ) -> tuple[TimeframeOutcome, ConflictStatus, Uncertainty]:
        outcome, conflict, _direction, uncertainty, _reasons = self.analyzer._resolve(
            context, setup, timing
        )
        return outcome, conflict, uncertainty

    def bull_context(self) -> TimeframeLayer:
        return layer(
            TimeframeLabel.CONTEXT,
            TimeframeDirection.BULLISH,
            condition=MarketCondition.TRENDING,
        )

    def bull_setup(self) -> TimeframeLayer:
        return layer(
            TimeframeLabel.SETUP,
            TimeframeDirection.BULLISH,
            setup_state=SetupState.CONFIRMED,
        )

    def bull_timing(self) -> TimeframeLayer:
        return layer(
            TimeframeLabel.TIMING,
            TimeframeDirection.BULLISH,
            interval="1m",
            timing_state=TimingState.ENTRY_LONG,
        )

    def test_all_three_agree_is_a_buy_candidate(self) -> None:
        outcome, conflict, uncertainty = self.resolve(
            self.bull_context(), self.bull_setup(), self.bull_timing()
        )
        self.assertIs(outcome, TimeframeOutcome.BUY_CANDIDATE)
        self.assertIs(conflict, ConflictStatus.ALIGNED)
        self.assertIs(uncertainty, Uncertainty.LOW)

    def test_all_three_bearish_is_a_sell_candidate(self) -> None:
        outcome, _, _ = self.resolve(
            layer(TimeframeLabel.CONTEXT, TimeframeDirection.BEARISH),
            layer(
                TimeframeLabel.SETUP,
                TimeframeDirection.BEARISH,
                setup_state=SetupState.CONFIRMED,
            ),
            layer(
                TimeframeLabel.TIMING,
                TimeframeDirection.BEARISH,
                interval="1m",
                timing_state=TimingState.ENTRY_SHORT,
            ),
        )
        self.assertIs(outcome, TimeframeOutcome.SELL_CANDIDATE)

    def test_one_minute_opposition_waits_for_confirmation(self) -> None:
        outcome, conflict, _ = self.resolve(
            self.bull_context(),
            self.bull_setup(),
            layer(
                TimeframeLabel.TIMING,
                TimeframeDirection.BEARISH,
                interval="1m",
                timing_state=TimingState.ENTRY_SHORT,
            ),
        )
        self.assertIs(outcome, TimeframeOutcome.WAIT_FOR_CONFIRMATION)
        self.assertIs(conflict, ConflictStatus.PARTIAL)

    def test_setup_against_context_is_rejected(self) -> None:
        outcome, conflict, _ = self.resolve(
            self.bull_context(),
            layer(
                TimeframeLabel.SETUP,
                TimeframeDirection.BEARISH,
                setup_state=SetupState.CONFLICTING,
            ),
            layer(
                TimeframeLabel.TIMING,
                TimeframeDirection.BEARISH,
                interval="1m",
                timing_state=TimingState.ENTRY_SHORT,
            ),
        )
        self.assertIs(outcome, TimeframeOutcome.REJECT_SIGNAL)
        self.assertIs(conflict, ConflictStatus.CONFLICT)

    def test_bearish_context_with_bullish_lower_frames_is_rejected(self) -> None:
        outcome, _, _ = self.resolve(
            layer(TimeframeLabel.CONTEXT, TimeframeDirection.BEARISH),
            self.bull_setup(),
            self.bull_timing(),
        )
        self.assertIs(outcome, TimeframeOutcome.REJECT_SIGNAL)

    def test_all_disagree_is_rejected(self) -> None:
        outcome, conflict, _ = self.resolve(
            self.bull_context(),
            layer(
                TimeframeLabel.SETUP,
                TimeframeDirection.BEARISH,
                setup_state=SetupState.CONFLICTING,
            ),
            layer(
                TimeframeLabel.TIMING,
                TimeframeDirection.BEARISH,
                interval="1m",
                timing_state=TimingState.EXIT_LONG,
            ),
        )
        self.assertIs(outcome, TimeframeOutcome.REJECT_SIGNAL)
        self.assertIs(conflict, ConflictStatus.CONFLICT)

    def test_neutral_context_holds(self) -> None:
        outcome, _, _ = self.resolve(
            layer(
                TimeframeLabel.CONTEXT,
                TimeframeDirection.NEUTRAL,
                condition=MarketCondition.SIDEWAYS,
            ),
            self.bull_setup(),
            self.bull_timing(),
        )
        self.assertIs(outcome, TimeframeOutcome.HOLD)

    def test_high_volatility_blocks_a_timed_entry(self) -> None:
        outcome, _, _ = self.resolve(
            layer(
                TimeframeLabel.CONTEXT,
                TimeframeDirection.BULLISH,
                condition=MarketCondition.HIGH_VOLATILITY,
            ),
            self.bull_setup(),
            self.bull_timing(),
        )
        self.assertIs(outcome, TimeframeOutcome.WAIT_FOR_CONFIRMATION)

    def test_missing_timeframe_never_becomes_a_signal(self) -> None:
        outcome, conflict, uncertainty = self.resolve(
            self.bull_context(),
            layer(
                TimeframeLabel.SETUP,
                TimeframeDirection.NEUTRAL,
                available=False,
                setup_state=SetupState.ABSENT,
            ),
            self.bull_timing(),
        )
        self.assertIs(outcome, TimeframeOutcome.WAIT_FOR_CONFIRMATION)
        self.assertIs(conflict, ConflictStatus.INSUFFICIENT_DATA)
        self.assertIs(uncertainty, Uncertainty.HIGH)

    def test_stale_timeframe_never_becomes_a_signal(self) -> None:
        outcome, conflict, _ = self.resolve(
            self.bull_context(),
            self.bull_setup(),
            layer(
                TimeframeLabel.TIMING,
                TimeframeDirection.BULLISH,
                interval="1m",
                stale=True,
                timing_state=TimingState.ENTRY_LONG,
            ),
        )
        self.assertIs(outcome, TimeframeOutcome.WAIT_FOR_CONFIRMATION)
        self.assertIs(conflict, ConflictStatus.INSUFFICIENT_DATA)

    def test_exit_timing_is_not_an_entry(self) -> None:
        outcome, _, _ = self.resolve(
            self.bull_context(),
            self.bull_setup(),
            layer(
                TimeframeLabel.TIMING,
                TimeframeDirection.BULLISH,
                interval="1m",
                timing_state=TimingState.EXIT_LONG,
            ),
        )
        self.assertIs(outcome, TimeframeOutcome.WAIT_FOR_CONFIRMATION)

    def test_low_confidence_is_not_traded(self) -> None:
        outcome, _, _ = self.resolve(
            self.bull_context(),
            layer(
                TimeframeLabel.SETUP,
                TimeframeDirection.BULLISH,
                setup_state=SetupState.CONFIRMED,
                confidence="0.30",
            ),
            self.bull_timing(),
        )
        self.assertIs(outcome, TimeframeOutcome.WAIT_FOR_CONFIRMATION)


class AnalyzerDataTests(unittest.TestCase):
    def build(self, series: dict[str, list[Any]]) -> tuple[MultiTimeframeAnalyzer, SeriesMarket]:
        market = SeriesMarket(series)
        return MultiTimeframeAnalyzer(market, clock=lambda: NOW), market

    def test_trending_series_produce_a_buy_candidate(self) -> None:
        analyzer, _ = self.build(bullish_series())
        decision = analyzer.analyse("XNSE:TEST")
        self.assertIs(decision.outcome, TimeframeOutcome.BUY_CANDIDATE)
        self.assertIs(decision.context.direction, TimeframeDirection.BULLISH)
        self.assertIs(decision.setup.setup_state, SetupState.CONFIRMED)
        self.assertIs(decision.timing.timing_state, TimingState.ENTRY_LONG)
        self.assertEqual(decision.candidate_side, "buy")

    def test_descending_series_produce_a_sell_candidate(self) -> None:
        five = make_bars(
            DataInterval.MINUTE_5, step_minutes=5, start=120.0, slope=-0.08, count=80
        )
        one = make_bars(
            DataInterval.MINUTE_1, step_minutes=1, start=113.0, slope=-0.01, count=30
        )
        analyzer, _ = self.build({"5m": five, "1m": one})
        decision = analyzer.analyse("XNSE:TEST")
        self.assertIs(decision.outcome, TimeframeOutcome.SELL_CANDIDATE)
        self.assertIs(decision.timing.timing_state, TimingState.ENTRY_SHORT)

    def test_missing_one_minute_series_waits_and_names_it(self) -> None:
        analyzer, _ = self.build(bullish_series(**{"1m": []}))
        decision = analyzer.analyse("XNSE:TEST")
        self.assertIs(decision.outcome, TimeframeOutcome.WAIT_FOR_CONFIRMATION)
        self.assertIn("1m", decision.missing)
        self.assertEqual(decision.candidate_side, "")

    def test_stale_one_minute_data_waits(self) -> None:
        stale = make_bars(
            DataInterval.MINUTE_1,
            step_minutes=1,
            start=106.4,
            slope=0.01,
            count=30,
            end=NOW - timedelta(minutes=10),
        )
        analyzer, _ = self.build(bullish_series(**{"1m": stale}))
        decision = analyzer.analyse("XNSE:TEST")
        self.assertIs(decision.outcome, TimeframeOutcome.WAIT_FOR_CONFIRMATION)
        self.assertTrue(decision.timing.stale)

    def test_missing_one_minute_candle_invalidates_timing(self) -> None:
        gapped = make_bars(
            DataInterval.MINUTE_1,
            step_minutes=1,
            start=106.4,
            slope=0.01,
            count=30,
            gap_at=15,
        )
        analyzer, _ = self.build(bullish_series(**{"1m": gapped}))
        decision = analyzer.analyse("XNSE:TEST")
        self.assertIs(decision.outcome, TimeframeOutcome.WAIT_FOR_CONFIRMATION)
        self.assertFalse(decision.timing.available)

    def test_incomplete_final_candle_is_excluded(self) -> None:
        partial = make_bars(
            DataInterval.MINUTE_1,
            step_minutes=1,
            start=106.4,
            slope=0.01,
            count=15,
            partial_last=True,
        )
        analyzer, _ = self.build(bullish_series(**{"1m": partial}))
        decision = analyzer.analyse("XNSE:TEST")
        self.assertIs(decision.outcome, TimeframeOutcome.WAIT_FOR_CONFIRMATION)
        self.assertFalse(decision.timing.available)
        self.assertTrue(
            any("partial" in reason for reason in decision.timing.reasons)
        )

    def test_duplicate_timestamps_are_collapsed(self) -> None:
        duplicated = make_bars(
            DataInterval.MINUTE_1,
            step_minutes=1,
            start=106.4,
            slope=0.01,
            count=30,
            duplicate_at=20,
        )
        analyzer, _ = self.build(bullish_series(**{"1m": duplicated}))
        decision = analyzer.analyse("XNSE:TEST")
        self.assertEqual(decision.timing.observations, 30)
        self.assertTrue(
            any("duplicate" in reason for reason in decision.timing.reasons)
        )
        self.assertIs(decision.outcome, TimeframeOutcome.BUY_CANDIDATE)

    def test_out_of_order_bars_are_sorted(self) -> None:
        reversed_bars = make_bars(
            DataInterval.MINUTE_1,
            step_minutes=1,
            start=106.4,
            slope=0.01,
            count=30,
            reverse=True,
        )
        analyzer, _ = self.build(bullish_series(**{"1m": reversed_bars}))
        decision = analyzer.analyse("XNSE:TEST")
        self.assertIs(decision.outcome, TimeframeOutcome.BUY_CANDIDATE)
        closes = [
            bar.close for bar in analyzer._series("XNSE:TEST", "1m", NOW).bars
        ]
        self.assertEqual(closes, sorted(closes))

    def test_future_bars_are_never_visible_to_a_historical_decision(self) -> None:
        future = make_bars(
            DataInterval.MINUTE_1,
            step_minutes=1,
            start=106.7,
            slope=0.05,
            count=5,
            end=NOW + timedelta(minutes=5),
        )
        series = bullish_series(**{"1m": bullish_series()["1m"] + future})
        analyzer, _ = self.build(series)
        decision = analyzer.analyse("XNSE:TEST")
        self.assertEqual(decision.timing.observations, 30)
        self.assertIsNotNone(decision.timing.as_of)
        assert decision.timing.as_of is not None
        self.assertLessEqual(decision.timing.as_of, NOW)
        self.assertIs(decision.outcome, TimeframeOutcome.BUY_CANDIDATE)

    def test_sudden_one_minute_move_stops_entry_timing(self) -> None:
        spiked = make_bars(
            DataInterval.MINUTE_1,
            step_minutes=1,
            start=100.0,
            slope=0.01,
            count=30,
        )
        spiked[-1].close = spiked[-2].close * 1.08
        spiked[-1].high = spiked[-1].close + 0.05
        spiked[-1].low = spiked[-2].close - 0.05
        analyzer, _ = self.build(bullish_series(**{"1m": spiked}))
        decision = analyzer.analyse("XNSE:TEST")
        self.assertIs(decision.outcome, TimeframeOutcome.WAIT_FOR_CONFIRMATION)
        self.assertTrue(
            any("sudden" in reason for reason in decision.timing.reasons)
        )

    def test_high_volatility_context_blocks_trade(self) -> None:
        five = make_bars(
            DataInterval.MINUTE_5, step_minutes=5, start=100.0, slope=0.0, count=80
        )
        for index, bar in enumerate(five):
            move = 6.0 if index % 2 == 0 else -6.0
            bar.close = 100.0 + move
            bar.high = bar.close + 4.0
            bar.low = bar.close - 4.0
        one = make_bars(
            DataInterval.MINUTE_1, step_minutes=1, start=106.4, slope=0.01, count=30
        )
        analyzer, _ = self.build({"5m": five, "1m": one})
        decision = analyzer.analyse("XNSE:TEST")
        self.assertIs(decision.context.condition, MarketCondition.HIGH_VOLATILITY)
        self.assertIs(decision.outcome, TimeframeOutcome.WAIT_FOR_CONFIRMATION)

    def test_series_are_reused_within_the_cache_ttl(self) -> None:
        analyzer, market = self.build(bullish_series())
        analyzer.analyse("XNSE:TEST")
        first = len(market.calls)
        analyzer.analyse("XNSE:TEST")
        self.assertEqual(len(market.calls), first)

    def test_unknown_configuration_setting_is_refused(self) -> None:
        config = MultiTimeframeConfig()
        with self.assertRaises(ValueError):
            config.with_overrides({"not_a_setting": 1})
        with self.assertRaises(ValueError):
            config.with_overrides({"context_bucket_minutes": 7})


class OneMinuteSchedulerTests(unittest.TestCase):
    def test_one_minute_forecast_horizon_accepts_only_one_minute_data(self) -> None:
        from novacontrol.novamarket.prediction.models import (
            HORIZON_SPECS,
            ForecastHorizon,
        )

        spec = HORIZON_SPECS[ForecastHorizon.MINUTES_1]
        self.assertEqual(spec.duration, timedelta(minutes=1))
        self.assertTrue(spec.accepts_resolution("1m"))
        self.assertTrue(spec.accepts_resolution("spot"))
        self.assertFalse(spec.accepts_resolution("5m"))

    def test_one_minute_horizon_has_a_real_ordered_cycle(self) -> None:
        start = datetime(2026, 11, 20, 6, 0, tzinfo=UTC)
        cycle = build_cycle(
            instrument_id="XNSE:MTF",
            exchange="XNSE",
            horizon="1m",
            now=start,
        )
        self.assertEqual(cycle.horizon.value, "1m")
        self.assertEqual(cycle.target_at - start, timedelta(minutes=1))
        self.assertEqual(cycle.initial_at, start)
        self.assertLess(cycle.initial_at, cycle.first_confirmation_at)
        self.assertLess(cycle.first_confirmation_at, cycle.final_confirmation_at)
        self.assertLess(cycle.final_confirmation_at, cycle.target_at)
        checkpoints = build_checkpoints(cycle)
        self.assertEqual(len(checkpoints), 4)
        self.assertEqual(checkpoints[0].scheduled_at, start)
        self.assertEqual(checkpoints[-1].scheduled_at, cycle.target_at)


class SpyRisk:
    def __init__(self) -> None:
        self.calls = 0

    async def assess(self, action: str, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "outcome": "approved",
            "decision_id": "rdec_multitimeframe_test",
            "policy_version": "test",
            "restrictions": [],
        }


class FakeDecision:
    def __init__(self, outcome: str = "buy_candidate", reason: str = "aligned") -> None:
        self.outcome = SimpleNamespace(value=outcome)
        self.reasons = (reason,)
        self.confidence = Decimal("0.80")

    def allows_signal(self, signal_type: str) -> bool:
        side = str(signal_type).lower()
        if side in {"exit", "hold", "no_signal"}:
            return True
        return side == {"buy_candidate": "buy", "sell_candidate": "sell"}.get(
            self.outcome.value, ""
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "reasons": list(self.reasons),
            "confidence": format(self.confidence, "f"),
        }


class FakeAnalyzer:
    def __init__(self, decision: FakeDecision | None = None, *, raises: bool = False) -> None:
        self.decision = decision or FakeDecision()
        self.raises = raises

    def analyse(self, instrument_id: str) -> FakeDecision:
        if self.raises:
            raise RuntimeError("bars unavailable")
        return self.decision


class StrategyMarket:
    def __init__(self) -> None:
        self.closes = [100.0] * 70 + [110.0]
        self.observed_at = NOW

    def latest(self, instrument_id: str) -> Any:
        return SimpleNamespace(
            usable=True,
            selected=SimpleNamespace(
                price=str(self.closes[-1]), observed_at=self.observed_at
            ),
        )

    def bars(
        self,
        instrument_id: str,
        *,
        limit: int = 200,
        ascending: bool = False,
        start: datetime | None = None,
        **_: Any,
    ) -> Any:
        items = [
            SimpleNamespace(
                close=value,
                high=value * 1.01,
                low=value * 0.99,
                volume=100000.0,
                observed_at=NOW - timedelta(days=len(self.closes) - index),
            )
            for index, value in enumerate(self.closes)
        ]
        if start is not None:
            items = [item for item in items if item.observed_at >= start]
        ordered = items[:limit] if ascending else list(reversed(items))[:limit]
        return SimpleNamespace(items=ordered)


class StrategyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.wallets = WalletEngine(WalletStore(":memory:"))
        funding = (await self.wallets.create_wallet("house"))["wallet"]["wallet_id"]
        await self.wallets.deposit(funding, "80000.00")
        self.market = StrategyMarket()
        self.portfolios = PortfolioEngine(
            PortfolioStore(":memory:"),
            self.wallets,
            market_data=self.market,
            clock=lambda: NOW,
        )
        self.risk = SpyRisk()
        self.strategies = StrategyEngine(
            StrategyStore(":memory:"),
            self.portfolios,
            self.risk,
            market_data=self.market,
            clock=lambda: NOW,
        )
        created = await self.portfolios.create(
            "short_term", funding_wallet_id=funding, capital="20000.00"
        )
        self.short_portfolio = created["portfolio"]["portfolio_id"]
        medium = await self.portfolios.create(
            "medium_term", funding_wallet_id=funding, capital="20000.00"
        )
        self.medium_portfolio = medium["portfolio"]["portfolio_id"]
        body = await self.strategies.create("trend_sma", name="mtf-gate")
        self.strategy_id = body["strategy"]["strategy_id"]
        await self.strategies.activate(self.strategy_id)

    async def run_strategies(self, portfolio_id: str) -> dict[str, Any]:
        return await self.strategies.run(
            portfolio_id,
            strategy_ids=[self.strategy_id],
            instrument_ids=["XNSE:GAMMA"],
        )

    async def test_rejected_hierarchy_creates_no_proposal_or_risk_decision(self) -> None:
        self.strategies.set_timeframe_analyzer(
            FakeAnalyzer(FakeDecision("reject_signal", "5m opposes 20m"))
        )
        body = await self.run_strategies(self.short_portfolio)
        self.assertEqual(body["signals"][0]["signal_type"], "buy")
        self.assertEqual(body["proposals"], [])
        self.assertEqual(self.risk.calls, 0)
        self.assertTrue(body["timeframe"]["applied"])
        self.assertEqual(len(body["timeframe"]["rejected"]), 1)
        self.assertEqual(
            body["timeframe"]["rejected"][0]["timeframe_outcome"], "reject_signal"
        )

    async def test_aligned_hierarchy_still_goes_through_risk(self) -> None:
        self.strategies.set_timeframe_analyzer(FakeAnalyzer(FakeDecision()))
        body = await self.run_strategies(self.short_portfolio)
        self.assertEqual(len(body["proposals"]), 1)
        self.assertEqual(self.risk.calls, 1)
        proposal = body["proposals"][0]
        self.assertEqual(proposal["status"], "approved")
        self.assertEqual(proposal["risk_decision_id"], "rdec_multitimeframe_test")

    async def test_approved_candidate_can_use_the_existing_forwarder(self) -> None:
        forwarded: list[str] = []

        async def forward(proposal_id: str, actor: str) -> dict[str, Any]:
            forwarded.append(proposal_id)
            return {
                "ok": True,
                "order": {"order_id": "ord_multitimeframe", "status": "submitted"},
            }

        self.strategies.set_forwarder(forward)
        self.strategies.set_timeframe_analyzer(FakeAnalyzer(FakeDecision()))
        body = await self.run_strategies(self.short_portfolio)
        proposal_id = body["proposals"][0]["proposal_id"]
        result = await self.strategies.forward(proposal_id, actor="test")
        self.assertTrue(result["ok"])
        self.assertEqual(forwarded, [proposal_id])

    async def test_swing_portfolio_keeps_existing_logic(self) -> None:
        self.strategies.set_timeframe_analyzer(
            FakeAnalyzer(FakeDecision("reject_signal", "not applied to swing"))
        )
        body = await self.run_strategies(self.medium_portfolio)
        self.assertFalse(body["timeframe"]["applied"])
        self.assertEqual(body["timeframe"]["rejected"], [])
        self.assertEqual(len(body["proposals"]), 1)

    async def test_analyzer_failure_blocks_new_day_trading_entries(self) -> None:
        self.strategies.set_timeframe_analyzer(FakeAnalyzer(raises=True))
        body = await self.run_strategies(self.short_portfolio)
        self.assertEqual(body["proposals"], [])
        self.assertEqual(self.risk.calls, 0)
        self.assertEqual(len(body["timeframe"]["errors"]), 1)


if __name__ == "__main__":
    unittest.main()
