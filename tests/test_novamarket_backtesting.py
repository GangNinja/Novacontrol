"""NM-11 backtesting tests: leakage, orders, risk, accounting and reproducibility.

The fixtures are deterministic and synthetic. No historical market data is
fabricated and no profitability claim is made: the assertions are about
mechanism (point-in-time ordering, fills, fees, persistence), never about a
strategy making money.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from novacontrol.novamarket.backtesting import (
    BacktestConfig,
    BacktestEngine,
    BacktestError,
    BacktestStore,
    BacktestingService,
    HistoricalBar,
    SimulatedOrder,
    benchmark_metrics,
    calculate_metrics,
    compare,
    regime_analysis,
)
from novacontrol.novamarket.backtesting.core import _expiry_time
from novacontrol.novamarket.execution.config import ExecutionConfig
from novacontrol.novamarket.execution.models import OrderSide, OrderType, parse_time
from novacontrol.novamarket.portfolio.models import Position

NOW = datetime(2026, 1, 2, 15, 30, tzinfo=timezone.utc)

#: NSE 2026 weekday closures (NM-02's calendar is the source of truth; this list
#: only keeps the synthetic fixture's dates realistic).
_NSE_HOLIDAYS = {
    date(2026, 1, 15),
    date(2026, 1, 26),
    date(2026, 3, 3),
    date(2026, 3, 26),
    date(2026, 3, 31),
    date(2026, 4, 3),
    date(2026, 4, 14),
    date(2026, 5, 1),
    date(2026, 5, 28),
    date(2026, 6, 26),
    date(2026, 9, 14),
    date(2026, 10, 2),
    date(2026, 10, 20),
    date(2026, 11, 10),
    date(2026, 11, 24),
    date(2026, 12, 25),
}


def _session_moments(count: int) -> list[datetime]:
    day = NOW.date()
    moments: list[datetime] = []
    while len(moments) < count:
        if day.weekday() < 5 and day not in _NSE_HOLIDAYS:
            moments.append(datetime.combine(day, NOW.timetz()))
        day += timedelta(days=1)
    return moments
ZERO_FEES = ExecutionConfig(
    participation_rate=Decimal("1"),
    spread_bps=Decimal("0"),
    impact_coefficient_bps=Decimal("0"),
    volatility_weight=Decimal("0"),
    brokerage_bps=Decimal("0"),
    exchange_bps=Decimal("0"),
    sebi_bps=Decimal("0"),
    stamp_duty_bps=Decimal("0"),
    gst_percent=Decimal("0"),
    minimum_total_fee=Decimal("0"),
    require_fresh_data=False,
)


def series(
    instrument: str = "XNSE:ALPHA",
    *,
    length: int = 30,
    start: Decimal = Decimal("100"),
    step: Decimal = Decimal("-1"),
    volume: Decimal = Decimal("10000"),
    every: int = 1,
) -> list[HistoricalBar]:
    """A V-shaped close series: falls for half the window, then recovers."""
    rows: list[HistoricalBar] = []
    for index, at in enumerate(_session_moments(length)):
        if index % every:
            continue
        if index < length // 2:
            close = start + step * index
        else:
            close = start + step * (length // 2 - 1) + (index - length // 2 + 1)
        rows.append(
            HistoricalBar(
                instrument_id=instrument,
                observed_at=at,
                available_at=at,
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=volume,
                source="fixture",
            )
        )
    return rows


def bars(*, length: int = 30, future: bool = False) -> list[HistoricalBar]:
    rows = series(length=length)
    if future:
        rows[-1] = HistoricalBar(
            instrument_id="XNSE:ALPHA",
            observed_at=rows[-1].observed_at,
            available_at=rows[-1].observed_at + timedelta(days=1),
            open=rows[-1].open,
            high=rows[-1].high,
            low=rows[-1].low,
            close=rows[-1].close,
            volume=rows[-1].volume,
            source="fixture",
        )
    return rows


def config(**changes) -> BacktestConfig:
    values = {
        "name": "trend fixture",
        "strategy_catalog_id": "trend_sma",
        "strategy_version": "nm09-trend_sma-1.0",
        "strategy_config": {"fast_period": 3, "slow_period": 5, "horizon_days": 5},
        "instruments": ("XNSE:ALPHA",),
        "start": NOW,
        "end": NOW + timedelta(days=90),
        "initial_capital": Decimal("100000"),
        "execution_config": ZERO_FEES,
    }
    values.update(changes)
    return BacktestConfig(**values)


def bar(
    at: datetime,
    *,
    open_: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
    volume: str = "10000",
    tradable: bool = True,
) -> HistoricalBar:
    return HistoricalBar(
        instrument_id="XNSE:ALPHA",
        observed_at=at,
        available_at=at,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        source="fixture",
        tradable=tradable,
    )


def position(
    *,
    instrument: str = "XNSE:ALPHA",
    quantity: str = "0",
    average_cost: str = "0.00",
    price: str | None = None,
) -> Position:
    return Position(
        portfolio_id="bt",
        instrument_id=instrument,
        quantity=Decimal(quantity),
        average_cost=Decimal(average_cost),
        realized_pnl=Decimal("0.00"),
        sector="unknown",
        asset_class="equity",
        opened_at=NOW,
        updated_at=NOW,
        last_price=Decimal(price) if price is not None else None,
        priced_at=NOW if price is not None else None,
    )


def order(
    *,
    side: OrderSide = OrderSide.BUY,
    order_type: OrderType = OrderType.MARKET,
    qty: str = "100",
    limit: str | None = None,
    trigger: str | None = None,
    expires: datetime | None = None,
) -> SimulatedOrder:
    at = NOW
    return SimulatedOrder(
        order_id="ord_1",
        backtest_id="bt_1",
        instrument_id="XNSE:ALPHA",
        side=side,
        order_type=order_type,
        requested_quantity=Decimal(qty),
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal(qty),
        decision_at=at,
        expires_at=expires or at + timedelta(days=10),
        status="open",
        limit_price=Decimal(limit) if limit is not None else None,
        trigger_price=Decimal(trigger) if trigger is not None else None,
    )


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bt.sqlite3"
        self.store = BacktestStore(self.path)
        self.engine = BacktestEngine(self.store, clock=lambda: NOW)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def execute(self, row: SimulatedOrder, current: HistoricalBar, history=None, **kwargs):
        cfg = kwargs.pop("config", None) or config()
        positions = kwargs.pop("positions", None) or {
            "XNSE:ALPHA": position()
        }
        cash = kwargs.pop("cash", Decimal("100000"))
        return self.engine._execute_order(
            "bt_1", cfg, row, current, list(history or []), positions, cash
        )


class ConfigurationTests(_StoreCase):
    def test_config_is_hashed_and_unknown_fields_are_refused(self) -> None:
        self.assertEqual(config().config_hash, config().config_hash)
        with self.assertRaises(BacktestError):
            BacktestConfig.from_mapping({**config().to_dict(), "not_a_field": 1})
        with self.assertRaises(BacktestError):
            config(strategy_catalog_id="missing")
        with self.assertRaises(BacktestError):
            config(start=NOW, end=NOW)
        with self.assertRaises(BacktestError):
            config(evaluation_frequency="1m")

    def test_order_types_are_validated_and_normalised(self) -> None:
        for order_type in ("market", "limit", "stop", "stop_limit"):
            with self.subTest(order_type=order_type):
                self.assertEqual(config(order_type=order_type).order_type, order_type)
        with self.assertRaises(BacktestError):
            config(order_type="iceberg")
        with self.assertRaises(BacktestError):
            config(order_ttl_bars=0)

    def test_strategy_parameters_are_validated(self) -> None:
        with self.assertRaises(BacktestError):
            config(strategy_config={"fast_period": 9, "slow_period": 2})


class LeakageTests(_StoreCase):
    def test_a_bar_unavailable_at_the_decision_time_is_excluded(self) -> None:
        result = self.engine.run(config(), bars(future=True))
        self.assertTrue(any("unavailable" in warning for warning in result.warnings))
        self.assertEqual(result.data_quality["unavailable_bars"], 1)

    def test_an_order_never_fills_at_the_signal_close(self) -> None:
        result = self.engine.run(config(), bars())
        self.assertTrue(result.trades)
        by_time = {
            (row.instrument_id, row.observed_at): row
            for row in bars()
        }
        for trade in result.trades:
            self.assertGreater(trade.executed_at, trade.decision_at)
            executed = by_time[(trade.instrument_id, trade.executed_at)]
            # Market orders fill from the next bar's open, never its close.
            self.assertEqual(trade.price, executed.open)
            self.assertEqual(trade.reference_price, executed.open)

    def test_liquidity_uses_the_previous_bar_not_the_current_one(self) -> None:
        cfg = config(execution_config=ZERO_FEES.with_changes(participation_rate=Decimal("0.001")))
        previous = bar(NOW, volume="10000")
        current = bar(NOW + timedelta(days=1), volume="0")
        row, state = self.execute(order(), current, [previous], config=cfg)
        self.assertEqual(row.status, "partially_filled")
        self.assertEqual(row.filled_quantity, Decimal("10.0000"))
        self.assertTrue(state["trades"][0].partial)

    def test_future_events_are_not_visible(self) -> None:
        engine = BacktestEngine(
            self.store,
            event_loader=lambda instrument, at: [
                {"event_id": "past", "instrument_id": instrument, "published_at": (NOW - timedelta(days=1)).isoformat()},
                {"event_id": "future", "instrument_id": instrument, "published_at": (NOW + timedelta(days=1)).isoformat()},
                {"event_id": "other", "instrument_id": "XNSE:BETA", "published_at": (NOW - timedelta(days=1)).isoformat()},
                {"event_id": "broken", "instrument_id": instrument, "published_at": "not-a-time"},
            ],
        )
        visible = engine._available_events("XNSE:ALPHA", NOW)
        self.assertEqual([row["event_id"] for row in visible], ["past"])

    def test_forecasts_respect_creation_cutoff_model_and_status(self) -> None:
        engine = BacktestEngine(
            self.store,
            forecast_loader=lambda instrument, at: [
                {"forecast_id": "ok", "created_at": (NOW - timedelta(days=1)).isoformat(), "data_cutoff": (NOW - timedelta(days=1)).isoformat(), "model_version": "m1", "status": "active"},
                {"forecast_id": "future", "created_at": (NOW + timedelta(days=1)).isoformat(), "model_version": "m1", "status": "active"},
                {"forecast_id": "cutoff", "created_at": (NOW - timedelta(days=2)).isoformat(), "data_cutoff": (NOW + timedelta(days=1)).isoformat(), "model_version": "m1", "status": "active"},
                {"forecast_id": "model", "created_at": (NOW - timedelta(days=1)).isoformat(), "model_version": "m2", "status": "active"},
                {"forecast_id": "failed", "created_at": (NOW - timedelta(days=1)).isoformat(), "model_version": "m1", "status": "failed"},
            ],
        )
        cfg = config(forecast_model_version="m1")
        visible = engine._available_forecasts("XNSE:ALPHA", NOW, cfg)
        self.assertEqual([row["forecast_id"] for row in visible], ["ok"])

    def test_a_halted_bar_does_not_produce_a_decision_or_a_fill(self) -> None:
        rows = bars()
        rows[12] = HistoricalBar(
            instrument_id="XNSE:ALPHA",
            observed_at=rows[12].observed_at,
            available_at=rows[12].available_at,
            open=rows[12].open,
            high=rows[12].high,
            low=rows[12].low,
            close=rows[12].close,
            volume=rows[12].volume,
            source="fixture",
            tradable=False,
        )
        result = self.engine.run(config(), rows)
        self.assertGreaterEqual(result.data_quality["halted_bars"], 1)
        halted_at = rows[12].observed_at.isoformat()
        self.assertFalse([row for row in result.signals if row.at.isoformat() == halted_at])
        self.assertFalse(
            [row for row in result.trades if row.executed_at.isoformat() == halted_at]
        )

    def test_a_bar_on_an_exchange_holiday_is_excluded(self) -> None:
        rows = bars()
        holiday = datetime.combine(date(2026, 1, 15), NOW.timetz())
        rows.append(
            HistoricalBar(
                instrument_id="XNSE:ALPHA",
                observed_at=holiday,
                available_at=holiday,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("10000"),
                source="fixture",
            )
        )
        result = self.engine.run(config(), rows)
        self.assertEqual(result.data_quality["closed_bars"], 1)
        self.assertTrue(any("exchange closed" in item for item in result.warnings))
        self.assertFalse(
            [row for row in result.equity if row["at"] == holiday.isoformat()]
        )

    def test_duplicate_bars_are_removed_not_double_counted(self) -> None:
        rows = bars()
        rows.insert(4, rows[3])
        result = self.engine.run(config(), rows)
        self.assertEqual(result.data_quality["duplicates_removed"], 1)
        self.assertLessEqual(len(result.equity), len(rows))


class OrderSimulationTests(_StoreCase):
    def test_market_buy_fills_at_the_open(self) -> None:
        row, state = self.execute(
            order(), bar(NOW + timedelta(days=1)), [bar(NOW)]
        )
        self.assertEqual(row.status, "filled")
        self.assertEqual(len(state["trades"]), 1)
        self.assertEqual(state["trades"][0].price, Decimal("100.00"))
        self.assertEqual(state["trades"][0].fees, Decimal("0.00"))

    def test_limit_buy_waits_until_the_limit_is_touched(self) -> None:
        cfg = config(order_type="limit", limit_offset_bps=Decimal("500"))
        waiting = bar(NOW + timedelta(days=1), low="99.6", high="101")
        row, state = self.execute(
            order(order_type=OrderType.LIMIT, limit="95.00"), waiting, [waiting], config=cfg
        )
        self.assertEqual(row.status, "open")
        self.assertFalse(state["trades"])

        touched = bar(NOW + timedelta(days=1), open_="100", high="101", low="94")
        row, state = self.execute(
            order(order_type=OrderType.LIMIT, limit="95.00"), touched, [touched], config=cfg
        )
        self.assertEqual(row.status, "filled")
        self.assertEqual(state["trades"][0].price, Decimal("95.00"))

    def test_stop_buy_triggers_at_the_stop_and_not_before(self) -> None:
        cfg = config(order_type="stop", stop_offset_bps=Decimal("50"))
        quiet = bar(NOW + timedelta(days=1), high="100.4")
        row, state = self.execute(
            order(order_type=OrderType.STOP, trigger="100.50"), quiet, [quiet], config=cfg
        )
        self.assertEqual(row.status, "open")
        self.assertFalse(state["trades"])

        crossed = bar(NOW + timedelta(days=1), open_="100", high="101", low="99")
        row, state = self.execute(
            order(order_type=OrderType.STOP, trigger="100.50"), crossed, [crossed], config=cfg
        )
        self.assertEqual(row.status, "filled")
        self.assertEqual(state["trades"][0].price, Decimal("100.50"))

    def test_stop_limit_needs_both_the_stop_and_the_limit(self) -> None:
        cfg = config(
            order_type="stop_limit",
            stop_offset_bps=Decimal("50"),
            limit_offset_bps=Decimal("25"),
        )
        collapsed = bar(
            NOW + timedelta(days=1), open_="101", high="102", low="94", close="99"
        )
        row, state = self.execute(
            order(order_type=OrderType.STOP_LIMIT, trigger="100.50", limit="99.75"),
            collapsed,
            [collapsed],
            config=cfg,
        )
        self.assertEqual(row.status, "filled")
        self.assertLessEqual(state["trades"][0].price, Decimal("99.75"))

    def test_partial_fills_keep_the_remainder_pending_until_expiry(self) -> None:
        cfg = config(
            order_ttl_bars=3,
            execution_config=ZERO_FEES.with_changes(participation_rate=Decimal("0.001")),
        )
        previous = bar(NOW, volume="10000")
        rows = [previous]
        cash = Decimal("100000")
        positions = {"XNSE:ALPHA": position()}
        row = order(qty="100", expires=NOW + timedelta(days=3))
        fills = Decimal("0")
        for index in range(1, 5):
            current = bar(NOW + timedelta(days=index))
            row, state = self.engine._execute_order(
                "bt_1", cfg, row, current, rows, positions, cash
            )
            cash = state["cash"]
            for trade in state["trades"]:
                fills += trade.quantity
            positions["XNSE:ALPHA"] = replace(
                positions["XNSE:ALPHA"],
                quantity=row.filled_quantity,
                average_cost=Decimal("100"),
            )
            rows.append(current)
            if row.status != "partially_filled":
                break
        self.assertEqual(fills, Decimal("30.0000"))
        self.assertEqual(row.status, "expired")
        self.assertLess(row.filled_quantity, row.requested_quantity)

    def test_order_expiry_is_counted_in_bars_across_a_weekend(self) -> None:
        friday = NOW
        saturday = NOW + timedelta(days=1)
        monday = NOW + timedelta(days=3)
        schedule = [friday, monday]
        self.assertEqual(_expiry_time(schedule, friday, 1), monday)
        # A one-bar TTL must survive the calendar-day gap.
        row, state = self.execute(order(expires=monday), bar(monday), [bar(friday)])
        self.assertEqual(row.status, "filled")
        self.assertTrue(state["trades"])

        after = bar(monday + timedelta(days=1))
        row, state = self.execute(order(expires=monday), after, [bar(monday)])
        self.assertEqual(row.status, "expired")
        self.assertFalse(state["trades"])

    def test_insufficient_cash_rejects_rather_than_going_negative(self) -> None:
        cfg = config(
            execution_config=ZERO_FEES.with_changes(brokerage_bps=Decimal("1000"))
        )
        row, state = self.execute(
            order(qty="100"), bar(NOW + timedelta(days=1)), [bar(NOW)], config=cfg, cash=Decimal("10")
        )
        self.assertEqual(row.status, "rejected")
        self.assertIn("insufficient simulated cash", row.rejection_reason)
        self.assertFalse(state["trades"])
        self.assertEqual(state["cash"], Decimal("10"))

    def test_a_sell_without_a_position_is_rejected(self) -> None:
        row, state = self.execute(
            order(side=OrderSide.SELL), bar(NOW + timedelta(days=1)), [bar(NOW)]
        )
        self.assertEqual(row.status, "rejected")
        self.assertFalse(state["trades"])

    def test_fees_and_realized_pnl_are_recorded_on_the_trade(self) -> None:
        cfg = config(execution_config=ExecutionConfig(participation_rate=Decimal("1"), require_fresh_data=False))
        buy_bar = bar(NOW + timedelta(days=1), open_="100", high="101", low="99")
        positions = {"XNSE:ALPHA": position()}
        row, state = self.execute(order(qty="10"), buy_bar, [bar(NOW)], config=cfg, positions=positions)
        self.assertTrue(state["trades"])
        self.assertGreater(state["trades"][0].fees, 0)
        positions["XNSE:ALPHA"] = replace(
            positions["XNSE:ALPHA"],
            quantity=row.filled_quantity,
            average_cost=state["trades"][0].price,
        )
        cash = state["cash"]

        sell_bar = bar(
            NOW + timedelta(days=2), open_="110", high="111", low="109", close="110"
        )
        row, state = self.execute(
            order(side=OrderSide.SELL, qty="10"),
            sell_bar,
            [buy_bar],
            config=cfg,
            positions=positions,
            cash=cash,
        )
        trade = state["trades"][0]
        self.assertGreater(trade.realized_pnl, 0)
        self.assertEqual(trade.position_after, Decimal("0.0000"))


class RiskPolicyTests(_StoreCase):
    def decide(
        self,
        cfg,
        action="buy",
        notional="10000",
        cash="100000",
        total="100000",
        peak="100000",
        extra=None,
    ):
        positions = {
            "XNSE:ALPHA": position(quantity="1", average_cost="100", price="100")
        }
        if extra:
            positions.update(extra)
        return self.engine._risk_decision(
            cfg,
            "XNSE:ALPHA",
            action,
            Decimal(notional),
            NOW,
            positions,
            Decimal(cash),
            Decimal(total),
            Decimal(peak),
            volatility_pct=Decimal("5"),
            liquidity=Decimal("1000000"),
        )

    def test_restricted_instruments_block_entries_but_allow_exits(self) -> None:
        cfg = config(risk_policy={"restricted_instruments": ["XNSE:ALPHA"]})
        self.assertEqual(self.decide(cfg, "buy").outcome, "rejected")
        self.assertEqual(self.decide(cfg, "sell").outcome, "approved")

    def test_position_and_gross_limits_reject_oversized_entries(self) -> None:
        cfg = config(max_position_weight=Decimal("0.01"))
        self.assertEqual(self.decide(cfg).outcome, "rejected")
        self.assertIn("of the book", self.decide(cfg).reason)

        gross = config(max_gross_exposure=Decimal("0.01"))
        self.assertEqual(
            self.decide(
                gross,
                extra={
                    "XNSE:BETA": position(
                        instrument="XNSE:BETA",
                        quantity="900",
                        average_cost="100",
                        price="100",
                    )
                },
            ).outcome,
            "rejected",
        )

    def test_drawdown_blocks_new_entries_without_blocking_de_risking(self) -> None:
        cfg = config(risk_policy={"max_drawdown": "0.05"})
        self.assertEqual(self.decide(cfg, "buy", total="90000", peak="100000").outcome, "rejected")
        self.assertEqual(self.decide(cfg, "sell", total="90000", peak="100000").outcome, "approved")

    def test_cash_reserve_restricts_an_entry(self) -> None:
        cfg = config(risk_policy={"min_cash_fraction": "0.95"})
        decision = self.decide(cfg)
        self.assertEqual(decision.outcome, "restricted")
        self.assertIn("cash would be", decision.reason)

    def test_decisions_are_persisted_with_the_policy_version(self) -> None:
        result = self.engine.run(config(risk_policy={"restricted_instruments": ["XNSE:ALPHA"]}), bars())
        self.assertTrue(result.risk_decisions)
        self.assertTrue(all(row.policy_version == "nm08-backtest-v1" for row in result.risk_decisions))
        self.assertTrue(all(row.outcome != "approved" for row in result.risk_decisions))
        self.assertFalse(result.trades)
        self.assertGreater(result.metrics["rejections"], 0)


class AccountingTests(_StoreCase):
    def test_cash_and_positions_reconcile_on_every_equity_row(self) -> None:
        result = self.engine.run(config(), bars())
        self.assertTrue(result.trades)
        for row in result.equity:
            cash = Decimal(row["cash"])
            value = Decimal(row["positions_value"])
            self.assertGreaterEqual(cash, 0)
            self.assertEqual(Decimal(row["equity"]), cash + value)
        self.assertEqual(
            result.metrics["total_fees"],
            str(sum((trade.fees for trade in result.trades), Decimal("0"))),
        )

    def test_multi_instrument_equity_is_one_row_per_moment_valued_at_last_close(self) -> None:
        alpha = series("XNSE:ALPHA", length=30, start=Decimal("100"))
        beta = series("XNSE:BETA", length=30, start=Decimal("50"), step=Decimal("-1"), every=2)
        cfg = config(instruments=("XNSE:ALPHA", "XNSE:BETA"))
        result = self.engine.run(cfg, [*alpha, *beta])
        expected_moments = {row.observed_at for row in [*alpha, *beta]}
        self.assertEqual(len(result.equity), len(expected_moments))
        self.assertTrue(any(trade.instrument_id == "XNSE:BETA" for trade in result.trades))
        beta_times = {row.observed_at for row in beta}
        # A missing bar is a closed/no-session gap: nothing is fabricated there.
        self.assertTrue(
            all(
                trade.executed_at in beta_times
                for trade in result.trades
                if trade.instrument_id == "XNSE:BETA"
            )
        )

        closes: dict[str, Decimal] = {}
        positions = {"XNSE:ALPHA": Decimal("0"), "XNSE:BETA": Decimal("0")}
        trades = sorted(result.trades, key=lambda trade: trade.executed_at)
        cursor = 0
        all_bars = sorted([*alpha, *beta], key=lambda row: row.observed_at)
        bar_cursor = 0
        for row in result.equity:
            at = parse_time(row["at"])
            while cursor < len(trades) and trades[cursor].executed_at <= at:
                trade = trades[cursor]
                positions[trade.instrument_id] = trade.position_after
                cursor += 1
            while bar_cursor < len(all_bars) and all_bars[bar_cursor].observed_at <= at:
                current = all_bars[bar_cursor]
                closes[current.instrument_id] = current.close
                bar_cursor += 1
            expected = sum(
                (positions[item] * closes.get(item, Decimal("0")) for item in positions),
                Decimal("0"),
            ).quantize(Decimal("0.01"))
            self.assertEqual(Decimal(row["positions_value"]), expected, row["at"])

    def test_metrics_report_insufficient_observations_honestly(self) -> None:
        body = calculate_metrics([], [], initial_capital=Decimal("100"))
        self.assertFalse(body["available"])
        self.assertIn("two equity observations", body["reason"])


class BenchmarkRobustnessTests(_StoreCase):
    def test_benchmark_and_regimes_are_reported_when_supplied(self) -> None:
        benchmark = series("XNSE:BM", length=30, start=Decimal("200"))
        result = self.engine.run(config(benchmark="XNSE:BM"), [*bars(), *benchmark])
        self.assertTrue(result.benchmark["available"])
        self.assertIn("excess_return", result.benchmark)
        self.assertTrue(result.robustness["available"])
        self.assertIn("descriptive", result.robustness["warning"])

    def test_benchmark_metrics_need_two_bars(self) -> None:
        self.assertFalse(benchmark_metrics([], initial_capital=Decimal("100"))["available"])

    def test_regime_analysis_without_a_benchmark_is_unclassified(self) -> None:
        result = self.engine.run(config(), bars())
        self.assertTrue(result.robustness["available"])
        self.assertIn("unclassified", result.robustness["basis"])


class PersistenceReproducibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bt.sqlite3"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_two_independent_runs_reproduce_the_same_hash_and_verify(self) -> None:
        first_store = BacktestStore(self.path)
        second_store = BacktestStore(Path(self.tmp.name) / "second.sqlite3")
        try:
            first = BacktestEngine(first_store, clock=lambda: NOW).run(config(), bars())
            second = BacktestEngine(second_store, clock=lambda: NOW).run(config(), bars())
            self.assertNotEqual(first.backtest_id, second.backtest_id)
            self.assertEqual(first.repro_hash, second.repro_hash)
            self.assertTrue(BacktestEngine(first_store).verify(first.backtest_id)["ok"])
            self.assertTrue(BacktestEngine(second_store).verify(second.backtest_id)["ok"])
        finally:
            first_store.close()
            second_store.close()

    def test_a_completed_run_is_immutable(self) -> None:
        store = BacktestStore(self.path)
        try:
            result = BacktestEngine(store, clock=lambda: NOW).run(config(), bars())
            with self.assertRaises(BacktestError):
                store.save(result)
            with self.assertRaises(sqlite3.IntegrityError):
                store._connection.execute(
                    "UPDATE backtests SET status='created' WHERE backtest_id=?",
                    (result.backtest_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                store._connection.execute("DELETE FROM backtest_runs")
        finally:
            store.close()

    def test_a_completed_run_survives_reopening_the_store(self) -> None:
        store = BacktestStore(self.path)
        try:
            result = BacktestEngine(store, clock=lambda: NOW).run(config(), bars())
        finally:
            store.close()
        reopened = BacktestStore(self.path)
        try:
            self.assertEqual(reopened.status(result.backtest_id), "completed")
            self.assertEqual(reopened.config(result.backtest_id).config_hash, result.config.config_hash)
            self.assertEqual(reopened.result_model(result.backtest_id).repro_hash, result.repro_hash)
            self.assertEqual(reopened.record(result.backtest_id)["error"], "")
            listing = reopened.list()
            self.assertEqual(listing[0]["backtest_id"], result.backtest_id)
            self.assertEqual(listing[0]["status"], "completed")
        finally:
            reopened.close()

    def test_created_and_failed_rows_are_distinguishable(self) -> None:
        store = BacktestStore(self.path)
        engine = BacktestEngine(store, clock=lambda: NOW)
        try:
            created_id = store.create(config(name="pending"), NOW)
            self.assertIsNone(store.result_model(created_id))
            self.assertEqual(store.status(created_id), "created")
            self.assertEqual(store.list()[0]["status"], "created")
            with self.assertRaises(BacktestError):
                engine.execute(created_id, bars(length=4))
            self.assertEqual(store.status(created_id), "failed")
            self.assertTrue(store.record(created_id)["error"])
            # A failed attempt can be retried with a usable dataset.
            result = engine.execute(created_id, bars())
            self.assertEqual(result.backtest_id, created_id)
            self.assertEqual(store.status(created_id), "completed")
        finally:
            store.close()

    def test_an_in_flight_replay_observes_cancellation(self) -> None:
        store = BacktestStore(self.path)
        holder: dict[str, str] = {}

        def loader(instrument: str, at: datetime):
            if holder.get("id"):
                engine.cancel(holder["id"])
            return ()

        engine = BacktestEngine(store, clock=lambda: NOW, event_loader=loader)
        try:
            created = store.create(config(), NOW)
            holder["id"] = created
            with self.assertRaises(BacktestError):
                engine.execute(created, bars())
            self.assertEqual(store.status(created), "cancelled")
        finally:
            store.close()

    def test_a_concurrent_cancel_aborts_a_running_replay(self) -> None:
        store = BacktestStore(self.path)
        started = threading.Event()

        def loader(instrument: str, at: datetime):
            started.set()
            time.sleep(0.01)
            return ()

        engine = BacktestEngine(store, clock=lambda: NOW, event_loader=loader)
        created = store.create(config(), NOW)
        errors: list[BacktestError] = []

        def run() -> None:
            try:
                engine.execute(created, bars())
            except BacktestError as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        try:
            thread.start()
            self.assertTrue(started.wait(5))
            engine.cancel(created)
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertTrue(errors)
            self.assertEqual(store.status(created), "cancelled")
        finally:
            store.close()

    def test_force_creates_a_new_run_with_the_same_hash(self) -> None:
        store = BacktestStore(self.path)
        engine = BacktestEngine(store, clock=lambda: NOW)
        try:
            first = engine.run(config(), bars())
            second = engine.run(config(), bars(), force=True)
            self.assertNotEqual(first.backtest_id, second.backtest_id)
            self.assertEqual(first.repro_hash, second.repro_hash)
        finally:
            store.close()


class MetricsTests(unittest.TestCase):
    def test_drawdown_and_returns_follow_the_equity_curve(self) -> None:
        equity = [
            {"at": "2026-01-01T00:00:00+00:00", "equity": "100"},
            {"at": "2026-01-02T00:00:00+00:00", "equity": "110"},
            {"at": "2026-01-03T00:00:00+00:00", "equity": "99"},
        ]
        body = calculate_metrics(equity, [], initial_capital=Decimal("100"))
        self.assertTrue(body["available"])
        self.assertEqual(body["max_drawdown"], round((99 - 110) / 110, 8))
        self.assertEqual(Decimal(body["final_value"]), Decimal("99"))

    def test_comparison_needs_two_results(self) -> None:
        with self.assertRaises(BacktestError):
            compare([])


class _FakeQuality:
    value = "valid"


class _FakeBar:
    def __init__(self, row: HistoricalBar) -> None:
        self.observed_at = row.observed_at
        self.retrieved_at = row.observed_at
        self.open = row.open
        self.high = row.high
        self.low = row.low
        self.close = row.close
        self.volume = row.volume
        self.provider = row.source
        self.quality = _FakeQuality()
        self.partial = False
        self.provider_metadata = {"adjusted": False}


class _FakeMarketData:
    def __init__(self, rows: list[HistoricalBar]) -> None:
        self._rows = rows

    def bars(self, instrument_id, *, interval, start, end, limit, offset, ascending):
        rows = [row for row in self._rows if row.instrument_id == instrument_id]
        page = rows[offset : offset + limit]
        return SimpleNamespace(items=[_FakeBar(row) for row in page], total=len(rows))


def _service_payload(name: str = "service fixture") -> dict:
    return {
        "name": name,
        "strategy_catalog_id": "trend_sma",
        "strategy_version": "nm09-trend_sma-1.0",
        "strategy_config": {"fast_period": 3, "slow_period": 5, "horizon_days": 5},
        "instruments": ["XNSE:ALPHA"],
        "start": NOW.isoformat(),
        "end": (NOW + timedelta(days=90)).isoformat(),
        "initial_capital": "100000",
        "execution_config": ZERO_FEES.to_dict(),
    }


class BacktestingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.events: list[tuple[str, dict]] = []

        async def publish(name: str, payload: dict) -> None:
            self.events.append((name, payload))

        self.service = BacktestingService(
            Path(self.tmp.name) / "service.sqlite3",
            market_data=_FakeMarketData(bars()),
            publisher=publish,
            clock=lambda: NOW,
        )

    async def asyncTearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    async def test_validate_loads_point_in_time_bars(self) -> None:
        body = await self.service.create(_service_payload())
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "created")
        validated = self.service.validate(_service_payload(), load_data=True)
        self.assertTrue(validated["data_checked"])
        self.assertEqual(validated["bars"], 30)
        self.assertEqual(validated["warnings"], [])

    async def test_create_is_idempotent_and_get_reports_created_status(self) -> None:
        first = await self.service.create(_service_payload())
        second = await self.service.create(_service_payload())
        self.assertEqual(first["backtest_id"], second["backtest_id"])
        self.assertTrue(second.get("replayed"))
        body = self.service.get(first["backtest_id"])
        self.assertEqual(body["status"], "created")
        self.assertFalse(body["completed"])
        with self.assertRaises(BacktestError):
            self.service.results(first["backtest_id"])

    async def test_start_persists_a_verifiable_completed_run(self) -> None:
        created = await self.service.create(_service_payload())
        started = await self.service.start(created["backtest_id"])
        self.assertEqual(started["status"], "completed")
        self.assertTrue(started["trades"])
        self.assertTrue(started["metrics"]["available"])

        body = self.service.get(created["backtest_id"])
        self.assertTrue(body["completed"])
        self.assertIn("bars_hash", body["data_quality"])
        self.assertTrue(self.service.results(created["backtest_id"])["metrics"]["available"])
        self.assertTrue(self.service.trades(created["backtest_id"])["trades"])
        self.assertTrue(self.service.orders(created["backtest_id"])["orders"])
        self.assertTrue(self.service.decisions(created["backtest_id"])["risk_decisions"])
        self.assertTrue(self.service.signals(created["backtest_id"])["signals"])
        self.assertIn("bars", self.service.data_quality(created["backtest_id"]))

        verified = await self.service.verify(created["backtest_id"])
        self.assertTrue(verified["ok"])

        replayed = await self.service.start(created["backtest_id"])
        self.assertTrue(replayed["replayed"])

        names = [name for name, _ in self.events]
        for expected in (
            "novamarket.backtest.created",
            "novamarket.backtest.started",
            "novamarket.backtest.progress.updated",
            "novamarket.backtest.replay.checkpoint.created",
            "novamarket.backtest.performance.calculated",
            "novamarket.backtest.completed",
            "novamarket.backtest.reproducibility.verified",
        ):
            self.assertIn(expected, names)

    async def test_cancelled_runs_never_start(self) -> None:
        created = await self.service.create(_service_payload("cancel fixture"))
        cancelled = await self.service.cancel(created["backtest_id"])
        self.assertEqual(cancelled["backtest"]["status"], "cancelled")
        with self.assertRaises(BacktestError):
            await self.service.start(created["backtest_id"])

    async def test_compare_and_sensitivity_use_completed_runs_only(self) -> None:
        first = await self.service.create(_service_payload("first"))
        second = await self.service.create(_service_payload("second"))
        await self.service.start(first["backtest_id"])
        await self.service.start(second["backtest_id"])
        compared = self.service.compare([first["backtest_id"], second["backtest_id"]])
        self.assertEqual(compared["count"], 2)
        self.assertTrue(compared["persisted"])
        self.assertEqual(self.service.comparisons()["total"], 1)

        pending = await self.service.create(_service_payload("pending"))
        with self.assertRaises(BacktestError):
            self.service.compare([pending["backtest_id"], first["backtest_id"]])

        sensitivity = await self.service.sensitivity(
            first["backtest_id"],
            [{**_service_payload("variant"), "strategy_config": {"fast_period": 2, "slow_period": 4, "horizon_days": 5}}],
        )
        self.assertEqual(sensitivity["count"], 1)
        self.assertFalse(sensitivity["variants"][0]["out_of_sample"])
        self.assertTrue(sensitivity["persisted"])
        self.assertEqual(self.service.comparisons()["total"], 2)

    async def test_walk_forward_reports_out_of_sample_folds(self) -> None:
        created = await self.service.create(_service_payload("walk"))
        body = await self.service.walk_forward(created["backtest_id"], 8, 5)
        self.assertTrue(body["persisted"])
        self.assertGreaterEqual(body["count"], 1)
        self.assertTrue(
            all(
                row["out_of_sample"]
                for row in body["folds"]
                if not row.get("skipped")
            )
        )
        self.assertEqual(self.service.comparisons()["total"], 1)

    async def test_status_counts_persisted_runs(self) -> None:
        created = await self.service.create(_service_payload("status fixture"))
        before = self.service.status()
        self.assertEqual(before["total"], 1)
        self.assertEqual(before["completed"], 0)
        await self.service.start(created["backtest_id"])
        after = self.service.status()
        self.assertEqual(after["completed"], 1)
        self.assertTrue(after["isolated"])
        self.assertFalse(after["real_money"])


class RealMarketDataIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """The service reads through the real NM-02 store, not a test double."""

    async def test_service_loads_bars_from_the_nm02_store(self) -> None:
        from novacontrol.novamarket.marketdata.models import (
            AssetClass,
            DataQuality,
            Instrument,
            OhlcvBar,
        )
        from novacontrol.novamarket.marketdata.store import MarketDataStore

        with tempfile.TemporaryDirectory() as tmp:
            market = MarketDataStore(Path(tmp) / "market.sqlite3", clock=lambda: NOW)
            try:
                market.upsert_instrument(
                    Instrument.create(
                        symbol="ALPHA",
                        name="Alpha fixture",
                        exchange="NSE",
                        mic="XNSE",
                        asset_class=AssetClass.EQUITY,
                        currency="INR",
                        timezone="Asia/Kolkata",
                        provider_symbols={"fixture": "XNSE:ALPHA"},
                    )
                )
                for index, at in enumerate(_session_moments(30)):
                    close = (
                        Decimal("100") - Decimal(index)
                        if index < 15
                        else Decimal("85") + Decimal(index - 15)
                    )
                    market.append_bar(
                        OhlcvBar(
                            bar_id=f"bar_{index}",
                            instrument_id="XNSE:ALPHA",
                            provider="fixture",
                            interval="1d",
                            open=close + 1,
                            high=close + 2,
                            low=close - 1,
                            close=close,
                            currency="INR",
                            observed_at=at,
                            retrieved_at=at,
                            source_timezone="Asia/Kolkata",
                            volume=Decimal("10000"),
                            quality=DataQuality.VALID,
                        )
                    )
                service = BacktestingService(
                    Path(tmp) / "backtests.sqlite3",
                    market_data=market,
                    clock=lambda: NOW,
                )
                try:
                    created = await service.create(_service_payload("nm02 fixture"))
                    started = await service.start(created["backtest_id"])
                    self.assertEqual(started["status"], "completed")
                    self.assertGreater(started["data_quality"]["bars"], 0)
                    self.assertIn("fixture", started["data_quality"]["sources"])
                finally:
                    service.close()
            finally:
                market.close()


if __name__ == "__main__":
    unittest.main()
