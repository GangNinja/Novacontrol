"""NM-09 acceptance tests: strategies, signals, proposals and risk handoff."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from novacontrol.novamarket.contracts import StrategyStateError, StrategyValidationError
from novacontrol.novamarket.portfolio import PortfolioEngine, PortfolioStore
from novacontrol.novamarket.risk import RiskEngine, RiskStore
from novacontrol.novamarket.strategies import (
    ProposalStatus,
    SignalType,
    StrategyEngine,
    StrategyStore,
)
from novacontrol.novamarket.wallet.engine import WalletEngine
from novacontrol.novamarket.wallet.store import WalletStore

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
SYMBOLS = ("XNSE:ALPHA", "XNSE:BETA", "XNSE:GAMMA")


class Market:
    def __init__(
        self,
        closes: list[float] | None = None,
        *,
        volumes: list[float] | None = None,
        age_hours: float = 0,
    ) -> None:
        self.closes = closes or [100.0] * 70 + [110.0]
        self.volumes = volumes or [100000.0] * len(self.closes)
        self.observed_at = NOW - timedelta(hours=age_hours)

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
        ascending: bool = True,
        start: datetime | None = None,
    ) -> Any:
        items = [
            SimpleNamespace(
                close=value,
                high=value * 1.01,
                low=value * 0.99,
                volume=self.volumes[index] if index < len(self.volumes) else 100000,
                observed_at=NOW - timedelta(days=len(self.closes) - index),
            )
            for index, value in enumerate(self.closes)
        ]
        if start is not None:
            items = [item for item in items if item.observed_at >= start]
        ordered = items[:limit] if ascending else list(reversed(items))[:limit]
        return SimpleNamespace(items=ordered)

    def get_instrument(self, instrument_id: str) -> Any:
        return SimpleNamespace(
            to_dict=lambda: {
                "instrument_id": instrument_id,
                "sector": "Technology",
                "asset_class": "equity",
            }
        )


class Discovery:
    def candidates(self, limit: int = 20) -> dict[str, Any]:
        return {
            "candidates": [
                {"instrument_id": "XNSE:GAMMA", "sector": "Technology", "asset_class": "equity"}
            ][:limit]
        }


class Forecasts:
    def __init__(self) -> None:
        self.rows = [
            {
                "forecast_id": f"fc_{symbol[-5:]}_{horizon}",
                "instrument_id": symbol,
                "horizon": horizon,
                "predicted_return": "0.03",
                "probability_up": "0.62",
                "lower_return": "-0.01",
                "upper_return": "0.06",
                "uncertainty": "medium",
                "model_id": "m1",
                "created_at": NOW.isoformat(),
            }
            for symbol in SYMBOLS
            for horizon in ("next_session", "few_sessions", "one_week", "one_month", "three_months")
        ]

    def forecasts(self, instrument_id: str = "", limit: int = 50) -> dict[str, Any]:
        rows = [dict(row) for row in self.rows]
        if instrument_id:
            rows = [row for row in rows if row["instrument_id"] == instrument_id]
        return {"forecasts": rows[:limit]}

    def models(self, instrument_id: str | None = None, horizon: str | None = None) -> dict[str, Any]:
        return {
            "models": [
                {
                    "model_id": "m1",
                    "instrument_id": symbol,
                    "horizon": name,
                    "state": "active",
                }
                for symbol in SYMBOLS
                for name in ("next_session", "few_sessions", "one_week", "one_month", "three_months")
            ]
        }


class Radar:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def instrument_events(self, instrument_id: str, limit: int = 20) -> dict[str, Any]:
        return {"events": [row for row in self.rows if row.get("instrument_id") == instrument_id][:limit]}


class Harness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.wallets = WalletEngine(WalletStore(":memory:"))
        self.funding = (await self.wallets.create_wallet("house"))["wallet"]["wallet_id"]
        await self.wallets.deposit(self.funding, "40000.00")
        self.market = Market()
        self.forecasts = Forecasts()
        self.radar = Radar()
        self.now = NOW
        self.portfolios = PortfolioEngine(
            PortfolioStore(":memory:"),
            self.wallets,
            market_data=self.market,
            discovery=Discovery(),
            forecasts=self.forecasts,
            radar=self.radar,
            clock=lambda: self.now,
        )
        self.risk = RiskEngine(
            RiskStore(":memory:"),
            self.portfolios,
            market_data=self.market,
            forecasts=self.forecasts,
            radar=self.radar,
            discovery=Discovery(),
            wallets=self.wallets,
            clock=lambda: self.now,
        )
        self.strategies = StrategyEngine(
            StrategyStore(":memory:"),
            self.portfolios,
            self.risk,
            market_data=self.market,
            forecasts=self.forecasts,
            radar=self.radar,
            discovery=Discovery(),
            clock=lambda: self.now,
        )
        created = await self.portfolios.create(
            "short_term", funding_wallet_id=self.funding, capital="10000.00"
        )
        self.portfolio_id = created["portfolio"]["portfolio_id"]

    async def make(self, catalog: str = "trend_sma", *, priority: int = 50, **config: Any) -> str:
        body = await self.strategies.create(
            catalog, config=config or None, name=f"{catalog}-test", priority=priority
        )
        sid = body["strategy"]["strategy_id"]
        await self.strategies.activate(sid)
        return sid

    def set_market(self, closes: list[float], volumes: list[float] | None = None) -> None:
        self.market.closes = closes
        self.market.volumes = volumes or [100000.0] * len(closes)
        self.market.observed_at = self.now

    async def signal_for(
        self, sid: str, signal_type: str = "buy", *, notional_price: str = "100.00"
    ) -> dict[str, Any]:
        from novacontrol.novamarket.strategies.models import StrategySignal, SignalType, strategy_id

        version = self.strategies.strategy(sid)["version"]
        signal = StrategySignal(
            signal_id=strategy_id("sig_", sid, signal_type, str(self.now.isoformat())),
            strategy_id=sid,
            strategy_version=version,
            instrument_id="XNSE:GAMMA",
            portfolio_id=self.portfolio_id,
            signal_type=SignalType(signal_type),
            created_at=self.now,
            data_cutoff=self.now,
            evaluation_horizon="short_term",
            indicators={},
            forecast_refs=(),
            event_refs=(),
            conditions=("test",),
            confidence=Decimal("0.5"),
            explanation="test signal",
            expires_at=self.now + timedelta(hours=1),
            reference_price=Decimal(notional_price),
            data_quality="measured",
        )
        self.strategies.store.save_signal(signal)
        return signal.to_dict()


class DefinitionTests(Harness):
    async def test_create_is_draft_and_validates_parameters(self) -> None:
        body = await self.strategies.create("trend_sma")
        self.assertEqual(body["strategy"]["status"], "draft")
        self.assertEqual(body["strategy"]["version"], "nm09-trend_sma-1.0")
        with self.assertRaises(StrategyValidationError):
            await self.strategies.create("trend_sma", config={"fast_period": 50, "slow_period": 20})

    async def test_configuration_changes_create_a_new_version(self) -> None:
        sid = await self.make()
        before = self.strategies.strategy(sid)["version"]
        body = self.strategies.update_config(sid, {"fast_period": 5}, actor="test")
        self.assertNotEqual(body["strategy"]["version"], before)
        self.assertEqual(self.strategies.versions(sid)["total"], 2)

    async def test_lifecycle_refuses_an_illegal_transition(self) -> None:
        body = await self.strategies.create("trend_sma")
        sid = body["strategy"]["strategy_id"]
        with self.assertRaises(StrategyStateError):
            await self.strategies.pause(sid)

    async def test_paused_strategy_cannot_evaluate(self) -> None:
        sid = await self.make()
        await self.strategies.pause(sid)
        with self.assertRaises(StrategyStateError):
            await self.strategies.evaluate(sid, self.portfolio_id)

    async def test_signal_buy_and_risk_assessed_proposal(self) -> None:
        sid = await self.make()
        body = await self.strategies.evaluate(
            sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"]
        )
        self.assertEqual(body["signals"][0]["signal_type"], "buy")
        signal_id = body["signals"][0]["signal_id"]
        proposed = await self.strategies.propose(signal_id, actor="test")
        proposal = proposed["proposal"]
        self.assertIn(proposal["status"], {"approved", "restricted", "rejected"})
        self.assertTrue(proposal["risk_decision_id"])
        self.assertFalse(proposal["is_executed"])

    async def test_repeated_evaluation_replays_the_same_signal(self) -> None:
        sid = await self.make()
        first = await self.strategies.evaluate(sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"])
        second = await self.strategies.evaluate(sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"])
        self.assertEqual(first["signals"][0]["signal_id"], second["signals"][0]["signal_id"])

    async def test_stale_market_data_produces_no_signal(self) -> None:
        sid = await self.make()
        self.market.observed_at = NOW - timedelta(hours=100)
        body = await self.strategies.evaluate(sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"])
        self.assertEqual(body["signals"][0]["signal_type"], "no_signal")
        self.assertEqual(body["signals"][0]["data_quality"], "stale")

    async def test_risk_rejection_is_never_approved(self) -> None:
        sid = await self.make()
        await self.risk.update_policy("short_term", {"max_position_weight": "0.01"}, actor="test")
        body = await self.strategies.evaluate(sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"])
        proposal = (await self.strategies.propose(body["signals"][0]["signal_id"]))["proposal"]
        self.assertEqual(proposal["status"], ProposalStatus.REJECTED.value)
        self.assertEqual(proposal["risk_outcome"], "rejected")

    async def test_forward_is_refused_without_nm10(self) -> None:
        sid = await self.make()
        body = await self.strategies.evaluate(sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"])
        proposal = (await self.strategies.propose(body["signals"][0]["signal_id"]))["proposal"]
        with self.assertRaises(StrategyStateError):
            await self.strategies.forward(proposal["proposal_id"])

    async def test_forward_hands_the_approved_proposal_to_nm10(self) -> None:
        sid = await self.make()
        body = await self.strategies.evaluate(sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"])
        proposal = (await self.strategies.propose(body["signals"][0]["signal_id"]))["proposal"]
        self.assertEqual(proposal["status"], "approved", proposal)
        calls: list[tuple[str, str]] = []

        async def forwarder(proposal_id: str, actor: str) -> dict[str, Any]:
            calls.append((proposal_id, actor))
            return {
                "ok": True,
                "replayed": False,
                "order": {"order_id": "ord_test", "status": "open"},
            }

        self.strategies.set_forwarder(forwarder)
        forwarded = await self.strategies.forward(proposal["proposal_id"], actor="tester")
        self.assertTrue(forwarded["ok"], forwarded)
        self.assertEqual(calls, [(proposal["proposal_id"], "tester")])
        self.assertEqual(forwarded["proposal"]["status"], "forwarded_to_execution")
        self.assertEqual(forwarded["proposal"]["execution_order_id"], "ord_test")
        self.assertEqual(forwarded["proposal"]["execution_status"], "open")

    async def test_opposing_signals_require_review(self) -> None:
        from novacontrol.novamarket.strategies.models import StrategySignal, strategy_id

        first = StrategySignal(
            signal_id=strategy_id("sig_", "a"),
            strategy_id="a",
            strategy_version="v1",
            instrument_id="XNSE:GAMMA",
            portfolio_id=self.portfolio_id,
            signal_type=SignalType.BUY,
            created_at=NOW,
            data_cutoff=NOW,
            evaluation_horizon="test",
            indicators={},
            forecast_refs=(),
            event_refs=(),
            conditions=("test",),
            confidence=Decimal("0.5"),
            explanation="buy",
            expires_at=NOW + timedelta(hours=1),
            reference_price=Decimal("100"),
            data_quality="measured",
        )
        second = StrategySignal(
            signal_id=strategy_id("sig_", "b"),
            strategy_id="b",
            strategy_version="v1",
            instrument_id="XNSE:GAMMA",
            portfolio_id=self.portfolio_id,
            signal_type=SignalType.SELL,
            created_at=NOW,
            data_cutoff=NOW,
            evaluation_horizon="test",
            indicators={},
            forecast_refs=(),
            event_refs=(),
            conditions=("test",),
            confidence=Decimal("0.5"),
            explanation="sell",
            expires_at=NOW + timedelta(hours=1),
            reference_price=Decimal("100"),
            data_quality="measured",
        )
        selected, conflicts = await self.strategies._resolve_conflicts([first, second], "review")
        self.assertEqual(selected, [])
        self.assertEqual(conflicts[0]["outcome"] if isinstance(conflicts[0], dict) else conflicts[0].outcome.value, "review_required")

    async def test_performance_is_honest_without_outcomes(self) -> None:
        sid = await self.make()
        report = await self.strategies.performance(sid)
        self.assertFalse(report["sufficient"])
        self.assertEqual(report["degradation"], "insufficient_evidence")
        self.assertFalse(report["is_executed_performance"])


class ApiTests(unittest.TestCase):
    def test_catalog_and_status_are_available(self) -> None:
        from tempfile import TemporaryDirectory
        from unittest import mock

        from fastapi.testclient import TestClient
        from novacontrol.api.app import create_app
        from novacontrol.application import NovaControlApplication
        from novacontrol.browser import NoopBrowserRunner
        from novacontrol.desktop import NoopDesktopRunner

        with TemporaryDirectory() as tmp:
            def build(**kwargs: Any) -> NovaControlApplication:
                kwargs["data_dir"] = tmp
                return NovaControlApplication(**kwargs)

            with mock.patch("novacontrol.api.app.NovaControlApplication", side_effect=build), mock.patch(
                "novacontrol.application.LocalDesktopRunner", NoopDesktopRunner
            ), mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner):
                with TestClient(create_app()) as client:
                    catalog = client.get("/market/strategies/catalog")
                    self.assertEqual(catalog.status_code, 200)
                    self.assertEqual(len(catalog.json()["strategies"]), 6)
                    status = client.get("/market/strategies/status")
                    self.assertEqual(status.status_code, 200)
                    self.assertTrue(status.json()["initialised"])
                    self.assertFalse(status.json()["executes_orders"])
                    created = client.post(
                        "/market/strategies",
                        json={"catalog_id": "trend_sma", "name": "api-strategy"},
                    )
                    self.assertEqual(created.status_code, 200)
                    strategy_id = created.json()["strategy"]["strategy_id"]
                    performance = client.get(
                        f"/market/strategies/{strategy_id}/performance"
                    )
                    self.assertEqual(performance.status_code, 200)
                    self.assertFalse(performance.json()["sufficient"])
                    self.assertFalse(performance.json()["is_executed_performance"])


class HoldingPortfolio:
    def __init__(self, portfolio_id: str) -> None:
        self.portfolio_id = portfolio_id

    def detail(self, portfolio_id: str) -> dict[str, Any]:
        return {
            "portfolio": {"portfolio_id": portfolio_id, "horizon": "short_term"},
            "valuation": {"total_value": "10000.00", "cash": "9000.00"},
            "holdings": [
                {
                    "instrument_id": "XNSE:GAMMA",
                    "market_value": "100.00",
                    "weight": "0.01",
                    "sector": "Technology",
                }
            ],
            "cash": "9000.00",
            "counts": {},
        }


class StubRisk:
    def __init__(self, outcome: str, *, restrictions: tuple[str, ...] = (), fail: bool = False) -> None:
        self.outcome = outcome
        self.restrictions = restrictions
        self.fail = fail

    async def assess(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("risk unavailable")
        return {
            "decision_id": "rdec_test",
            "outcome": self.outcome,
            "policy_version": "policy-1",
            "state_version": "state-1",
            "restrictions": list(self.restrictions),
        }


class CategoryTests(Harness):
    async def test_mean_reversion_buys_an_oversold_close(self) -> None:
        self.set_market([100.0] * 30 + [70.0])
        sid = await self.make("mean_reversion_rsi")
        body = await self.strategies.evaluate(sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"])
        self.assertEqual(body["signals"][0]["signal_type"], "buy")

    async def test_momentum_buys_a_volume_confirmed_breakout(self) -> None:
        self.set_market([100.0] * 40 + [120.0], [100000.0] * 40 + [300000.0])
        sid = await self.make("momentum_breakout")
        body = await self.strategies.evaluate(sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"])
        self.assertEqual(body["signals"][0]["signal_type"], "buy")

    async def test_volatility_buys_a_contraction_breakout(self) -> None:
        self.set_market([100.0] * 59 + [101.0])
        sid = await self.make("volatility_breakout")
        body = await self.strategies.evaluate(sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"])
        self.assertEqual(body["signals"][0]["signal_type"], "buy")

    async def test_event_strategy_uses_only_verified_high_importance_events(self) -> None:
        self.set_market([100.0] * 40 + [110.0])
        self.radar.rows = [
            {
                "event_id": "evt_positive",
                "instrument_id": "XNSE:GAMMA",
                "verification": "officially_confirmed",
                "importance": "high",
                "title": "earnings beat",
                "occurred_at": self.now.isoformat(),
            }
        ]
        sid = await self.make("event_driven")
        body = await self.strategies.evaluate(sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"])
        self.assertEqual(body["signals"][0]["signal_type"], "buy")
        self.assertEqual(body["signals"][0]["event_references"], ["evt_positive"])

    async def test_event_strategy_honours_critical_minimum_importance(self) -> None:
        self.set_market([100.0] * 40 + [110.0])
        self.radar.rows = [
            {
                "event_id": "evt_high",
                "instrument_id": "XNSE:GAMMA",
                "verification": "officially_confirmed",
                "importance": "high",
                "title": "earnings beat",
                "occurred_at": self.now.isoformat(),
            }
        ]
        sid = await self.make("event_driven", min_importance="critical")
        body = await self.strategies.evaluate(sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"])
        self.assertEqual(body["signals"][0]["signal_type"], "no_signal")
        self.assertIn("no_verified_high_importance_event", body["signals"][0]["triggered_conditions"])

    async def test_forecast_strategy_uses_a_valid_live_forecast(self) -> None:
        self.set_market([100.0] * 70 + [110.0])
        sid = await self.make("forecast_assisted")
        body = await self.strategies.evaluate(sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"])
        signal = body["signals"][0]
        self.assertEqual(signal["signal_type"], "buy")
        self.assertTrue(signal["forecast_references"])
        self.assertEqual(signal["horizon_days"], 3)

    async def test_forecast_strategy_holding_period_follows_forecast_horizon(self) -> None:
        self.set_market([100.0] * 70 + [110.0])
        sid = await self.make(
            "forecast_assisted",
            forecast_horizon="one_month",
            horizon_days=5,
        )
        body = await self.strategies.evaluate(
            sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"]
        )
        signal = body["signals"][0]
        self.assertEqual(signal["signal_type"], "buy")
        self.assertEqual(signal["horizon_days"], 30)

    async def test_forecast_strategy_refuses_stale_forecasts(self) -> None:
        self.set_market([100.0] * 70 + [110.0])
        for row in self.forecasts.rows:
            row["created_at"] = (self.now - timedelta(hours=100)).isoformat()
        sid = await self.make("forecast_assisted")
        body = await self.strategies.evaluate(sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"])
        self.assertEqual(body["signals"][0]["signal_type"], "no_signal")
        self.assertIn("forecast_stale", body["signals"][0]["triggered_conditions"])

    async def test_signal_records_the_portfolio_horizon_not_a_condition(self) -> None:
        sid = await self.make()
        body = await self.strategies.evaluate(sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"])
        self.assertEqual(body["signals"][0]["evaluation_horizon"], "short_term")


class RiskContractTests(Harness):
    async def test_restricted_risk_keeps_restrictions_and_is_not_executable(self) -> None:
        sid = await self.make()
        signal = await self.signal_for(sid)
        self.strategies.set_risk(StubRisk("restricted", restrictions=("reduce size",)))
        proposal = (await self.strategies.propose(signal["signal_id"]))["proposal"]
        self.assertEqual(proposal["status"], "restricted")
        self.assertEqual(proposal["risk_restrictions"], ["reduce size"])
        self.assertFalse(proposal["is_executed"])

    async def test_insufficient_risk_remains_pending_and_never_approved(self) -> None:
        sid = await self.make()
        signal = await self.signal_for(sid)
        self.strategies.set_risk(StubRisk("insufficient_data"))
        proposal = (await self.strategies.propose(signal["signal_id"]))["proposal"]
        self.assertEqual(proposal["status"], "pending_risk")
        self.assertNotIn(proposal["risk_outcome"], {"approved", "restricted", "rejected"})

    async def test_risk_failure_is_never_approval(self) -> None:
        sid = await self.make()
        signal = await self.signal_for(sid)
        self.strategies.set_risk(StubRisk("approved", fail=True))
        proposal = (await self.strategies.propose(signal["signal_id"]))["proposal"]
        self.assertEqual(proposal["status"], "pending_risk")
        self.assertIn("risk unavailable", proposal["risk_error"])

    async def test_a_signal_from_an_old_configuration_cannot_propose(self) -> None:
        sid = await self.make()
        body = await self.strategies.evaluate(sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"])
        self.strategies.update_config(sid, {"fast_period": 5}, actor="test")
        with self.assertRaises(StrategyStateError):
            await self.strategies.propose(body["signals"][0]["signal_id"])

    async def test_an_expired_signal_cannot_propose(self) -> None:
        sid = await self.make()
        body = await self.strategies.evaluate(sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"])
        self.now = self.now + timedelta(hours=2)
        with self.assertRaises(StrategyStateError):
            await self.strategies.propose(body["signals"][0]["signal_id"])

    async def test_sell_and_exit_notionals_are_bounded_by_the_holding(self) -> None:
        sid = await self.make()
        self.strategies._portfolios = HoldingPortfolio(self.portfolio_id)
        self.strategies.set_risk(StubRisk("approved"))
        sell = await self.signal_for(sid, "sell")
        with self.assertRaises(StrategyValidationError):
            await self.strategies.propose(sell["signal_id"], notional="200.00")
        exit_signal = await self.signal_for(sid, "exit")
        with self.assertRaises(StrategyValidationError):
            await self.strategies.propose(exit_signal["signal_id"], notional="50.00")

    async def test_no_portfolio_or_wallet_mutation_occurs(self) -> None:
        before = self.portfolios.detail(self.portfolio_id)
        sid = await self.make()
        body = await self.strategies.evaluate(sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"])
        await self.strategies.propose(body["signals"][0]["signal_id"])
        after = self.portfolios.detail(self.portfolio_id)
        self.assertEqual(before["valuation"], after["valuation"])
        self.assertEqual(before["holdings"], after["holdings"])


class ConflictAndPerformanceTests(Harness):
    async def test_block_policy_suppresses_even_duplicate_buys(self) -> None:
        high = await self.make(priority=80)
        low = await self.make(priority=20)
        first = await self.signal_for(high)
        second = await self.signal_for(low)
        selected, conflicts = await self.strategies._resolve_conflicts([
            self.strategies._signal(first["signal_id"]),
            self.strategies._signal(second["signal_id"]),
        ], "block")
        self.assertEqual(selected, [])
        self.assertEqual(conflicts[0].outcome.value, "blocked")

    async def test_priority_policy_selects_the_highest_priority_duplicate(self) -> None:
        high = await self.make(priority=80)
        low = await self.make(priority=20)
        first = await self.signal_for(high)
        second = await self.signal_for(low)
        selected, conflicts = await self.strategies._resolve_conflicts([
            self.strategies._signal(first["signal_id"]),
            self.strategies._signal(second["signal_id"]),
        ], "review")
        self.assertEqual(selected[0].strategy_id, high)
        self.assertEqual(conflicts[0].outcome.value, "consolidated")

    async def test_performance_uses_the_price_at_the_signal_horizon(self) -> None:
        from dataclasses import replace
        from novacontrol.novamarket.strategies.models import StrategySignal, strategy_id

        sid = await self.make()
        record = self.strategies._record(sid)
        old = self.now - timedelta(days=10)
        signal = StrategySignal(
            signal_id=strategy_id("sig_", "performance"),
            strategy_id=sid,
            strategy_version=record.version,
            instrument_id="XNSE:GAMMA",
            portfolio_id=self.portfolio_id,
            signal_type=SignalType.BUY,
            created_at=old,
            data_cutoff=old,
            evaluation_horizon="short_term",
            indicators={},
            forecast_refs=(),
            event_refs=(),
            conditions=("test",),
            confidence=Decimal("0.5"),
            explanation="test",
            expires_at=old + timedelta(hours=1),
            reference_price=Decimal("100.00"),
            data_quality="measured",
            horizon_days=5,
        )
        self.strategies.store.save_signal(signal)
        closes = [100.0] * 20
        closes[15] = 110.0
        closes[-1] = 300.0
        self.set_market(closes)
        report = await self.strategies.performance(sid)
        self.assertEqual(report["observations"], 1)
        self.assertGreater(Decimal(report["average_return"]), Decimal("0.05"))

    async def test_strategy_event_names_are_published(self) -> None:
        names: list[str] = []

        async def sink(name: str, payload: dict[str, Any]) -> None:
            names.append(name)

        self.strategies.set_publisher(sink)
        sid = await self.make()
        body = await self.strategies.evaluate(sid, self.portfolio_id, instrument_ids=["XNSE:GAMMA"])
        await self.strategies.propose(body["signals"][0]["signal_id"])
        self.assertIn("novamarket.strategy.proposal.risk.requested", names)
        self.assertNotIn("risk.requested", self.strategies.last_error)

    async def test_invalid_priority_asset_class_and_empty_horizon_are_rejected(self) -> None:
        with self.assertRaises(StrategyValidationError):
            await self.strategies.create("trend_sma", priority="not-an-int")
        with self.assertRaises(StrategyValidationError):
            await self.strategies.create("trend_sma", asset_classes=["bond"])
        with self.assertRaises(StrategyValidationError):
            await self.strategies.create("trend_sma", horizons=[])


class RemainingContractTests(Harness):
    async def test_requires_review_risk_is_pending_not_approved(self) -> None:
        sid = await self.make()
        signal = await self.signal_for(sid)
        self.strategies.set_risk(StubRisk("requires_review"))
        proposal = (await self.strategies.propose(signal["signal_id"]))["proposal"]
        self.assertEqual(proposal["status"], "pending_risk")
        self.assertFalse(proposal["is_executed"])

    async def test_a_short_sell_signal_is_refused(self) -> None:
        sid = await self.make()
        signal = await self.signal_for(sid, "sell")
        with self.assertRaises(StrategyStateError):
            await self.strategies.propose(signal["signal_id"])

    async def test_horizon_mismatch_is_classified_from_step_days(self) -> None:
        from dataclasses import replace

        high = await self.make(priority=80)
        low = await self.make(priority=20)
        first = self.strategies._signal((await self.signal_for(high))["signal_id"])
        second = self.strategies._signal((await self.signal_for(low, "sell"))["signal_id"])
        _selected, conflicts = await self.strategies._resolve_conflicts(
            [replace(first, horizon_days=3), replace(second, horizon_days=7)], "review"
        )
        self.assertEqual(conflicts[0].conflict_type.value, "horizon_mismatch")

    async def test_consolidated_run_links_the_conflict_to_its_proposal(self) -> None:
        high = await self.make(priority=80)
        low = await self.make(priority=20)
        body = await self.strategies.run(
            self.portfolio_id,
            strategy_ids=[high, low],
            conflict_policy="review",
            auto_propose=True,
        )
        self.assertEqual(len(body["conflicts"]), 1)
        self.assertEqual(len(body["proposals"]), 1)
        self.assertEqual(
            body["proposals"][0]["conflict_id"], body["conflicts"][0]["conflict_id"]
        )

    async def test_repeated_blocked_run_is_idempotent(self) -> None:
        high = await self.make(priority=80)
        low = await self.make(priority=20)
        first = await self.strategies.run(
            self.portfolio_id, strategy_ids=[high, low], conflict_policy="block"
        )
        second = await self.strategies.run(
            self.portfolio_id, strategy_ids=[high, low], conflict_policy="block"
        )
        self.assertEqual(first["proposals"], [])
        self.assertEqual(second["proposals"], [])
        self.assertEqual(self.strategies.conflicts()["total"], 1)

    async def test_performance_update_event_is_published(self) -> None:
        names: list[str] = []

        async def sink(name: str, payload: dict[str, Any]) -> None:
            names.append(name)

        self.strategies.set_publisher(sink)
        sid = await self.make()
        await self.strategies.performance(sid)
        self.assertIn("novamarket.strategy.performance.updated", names)


class ApiErrorTests(unittest.TestCase):
    def test_unknown_strategy_and_proposal_are_404(self) -> None:
        from tempfile import TemporaryDirectory
        from unittest import mock

        from fastapi.testclient import TestClient
        from novacontrol.api.app import create_app
        from novacontrol.application import NovaControlApplication
        from novacontrol.browser import NoopBrowserRunner
        from novacontrol.desktop import NoopDesktopRunner

        with TemporaryDirectory() as tmp:
            def build(**kwargs: Any) -> NovaControlApplication:
                kwargs["data_dir"] = tmp
                return NovaControlApplication(**kwargs)

            with mock.patch("novacontrol.api.app.NovaControlApplication", side_effect=build), mock.patch(
                "novacontrol.application.LocalDesktopRunner", NoopDesktopRunner
            ), mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner):
                with TestClient(create_app()) as client:
                    self.assertEqual(client.get("/market/strategies/strat_missing").status_code, 404)
                    self.assertEqual(
                        client.get("/market/strategies/proposals/sprop_missing").status_code, 404
                    )
                    self.assertEqual(
                        client.post("/market/strategies/proposals/sprop_missing/forward").status_code,
                        404,
                    )


if __name__ == "__main__":
    unittest.main()
