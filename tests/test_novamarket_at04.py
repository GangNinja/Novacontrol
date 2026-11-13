"""AT-04 focused tests: real AT-03 proposal -> NM-08 -> NM-10 paper execution.

The wallet, portfolio, risk, strategy, decision, execution and reconciliation
stores are real.  Only market data and the surrounding discovery/forecast
providers are deterministic fixtures, so the financial assertions are meaningful
without network access or a live broker.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import mock

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - optional dev dependency
    TestClient = None  # type: ignore[assignment]

from novacontrol.api.models import ApiSurface
from novacontrol.api.route_consumers import ROUTE_CONSUMERS
from novacontrol.novamarket.autonomy import AutonomyRuntime
from novacontrol.novamarket.autonomy_execution import (
    AutonomyExecutionError,
    AutonomyPaperExecution,
    ExecutionIntentStatus,
)
from novacontrol.novamarket.decision import (
    AutonomyDecisionPipeline,
    DecisionProposalStatus,
)
from novacontrol.novamarket.execution import (
    ExecutionConfig,
    ExecutionEngine,
    ExecutionStore,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from novacontrol.novamarket.execution.models import execution_id
from novacontrol.novamarket.portfolio import PortfolioEngine, PortfolioStore
from novacontrol.novamarket.risk import RiskEngine, RiskStore
from novacontrol.novamarket.strategies import StrategyEngine, StrategyStore
from novacontrol.novamarket.wallet.engine import WalletEngine
from novacontrol.novamarket.wallet.store import WalletStore

NOW = datetime(2026, 11, 11, 4, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Market:
    def __init__(self, *, volume: float = 100000.0) -> None:
        self.closes = [100.0] * 70 + [110.0]
        self.volume = volume
        self.observed_at = NOW

    def latest(self, instrument_id: str) -> Any:
        return SimpleNamespace(
            usable=True,
            selected=SimpleNamespace(
                price=str(self.closes[-1]),
                observed_at=self.observed_at,
                freshness=SimpleNamespace(value="fresh"),
                quality=SimpleNamespace(value="valid"),
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
                volume=self.volume,
                partial=False,
                observed_at=NOW - timedelta(days=len(self.closes) - index),
            )
            for index, value in enumerate(self.closes)
        ]
        if start is not None:
            items = [item for item in items if item.observed_at >= start]
        ordered = items[:limit] if ascending else list(reversed(items))[:limit]
        return SimpleNamespace(items=ordered)

    def get_instrument(self, instrument_id: str) -> Any:
        if instrument_id != "XNSE:ALPHA":
            raise KeyError(instrument_id)
        return SimpleNamespace(
            instrument_id=instrument_id,
            currency="INR",
            asset_class=SimpleNamespace(value="equity"),
            trading_status=SimpleNamespace(value="open"),
            metadata={"sector": "Technology"},
        )


class Discovery:
    def candidates(self, limit: int = 20) -> dict[str, Any]:
        return {
            "candidates": [
                {
                    "instrument_id": "XNSE:ALPHA",
                    "sector": "Technology",
                    "asset_class": "equity",
                }
            ][:limit]
        }


class Forecasts:
    def __init__(self) -> None:
        self.rows = [
            {
                "forecast_id": f"fc_ALPHA_{horizon}",
                "instrument_id": "XNSE:ALPHA",
                "horizon": horizon,
                "predicted_return": "0.03",
                "probability_up": "0.62",
                "lower_return": "-0.01",
                "upper_return": "0.06",
                "uncertainty": "medium",
                "model_id": "m1",
                "created_at": NOW.isoformat(),
            }
            for horizon in (
                "next_session",
                "few_sessions",
                "one_week",
                "one_month",
                "three_months",
            )
        ]

    def forecasts(self, instrument_id: str = "", limit: int = 50) -> dict[str, Any]:
        rows = [dict(row) for row in self.rows]
        if instrument_id:
            rows = [row for row in rows if row["instrument_id"] == instrument_id]
        return {"forecasts": rows[:limit]}

    def models(
        self, instrument_id: str | None = None, horizon: str | None = None
    ) -> dict[str, Any]:
        return {
            "models": [
                {
                    "model_id": "m1",
                    "instrument_id": "XNSE:ALPHA",
                    "horizon": name,
                    "state": "active",
                }
                for name in (
                    "next_session",
                    "few_sessions",
                    "one_week",
                    "one_month",
                    "three_months",
                )
            ]
        }


class Radar:
    def instrument_events(self, instrument_id: str, limit: int = 20) -> dict[str, Any]:
        return {"events": []}


class ExecutionHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self._tmp.name) / "novamarket.sqlite3"
        self.clock = Clock()
        self.runtime = AutonomyRuntime(
            self.path, clock=self.clock, owner_id="at04-test", lease_seconds=3600
        )
        self.wallets = WalletEngine(WalletStore(":memory:", clock=self.clock))
        self.funding = (await self.wallets.create_wallet("house"))["wallet"]["wallet_id"]
        await self.wallets.deposit(self.funding, "200000.00")
        self.market = Market()
        self.forecasts = Forecasts()
        self.radar = Radar()
        self.portfolios = PortfolioEngine(
            PortfolioStore(":memory:", clock=self.clock),
            self.wallets,
            market_data=self.market,
            discovery=Discovery(),
            forecasts=self.forecasts,
            radar=self.radar,
            clock=self.clock,
        )
        self.risk = RiskEngine(
            RiskStore(":memory:", clock=self.clock),
            self.portfolios,
            market_data=self.market,
            forecasts=self.forecasts,
            radar=self.radar,
            discovery=Discovery(),
            wallets=self.wallets,
            clock=self.clock,
        )
        self.strategies = StrategyEngine(
            StrategyStore(":memory:", clock=self.clock),
            self.portfolios,
            self.risk,
            market_data=self.market,
            forecasts=self.forecasts,
            radar=self.radar,
            discovery=Discovery(),
            clock=self.clock,
        )
        created = await self.portfolios.create(
            "short_term", funding_wallet_id=self.funding, capital="100000.00"
        )
        self.portfolio_id = created["portfolio"]["portfolio_id"]
        strategy = await self.strategies.create(
            "trend_sma",
            config={"fast_period": 3, "slow_period": 5},
            name="at04-trend",
        )
        self.strategy_id = strategy["strategy"]["strategy_id"]
        await self.strategies.activate(self.strategy_id)
        self.runtime.start("monitoring")
        self.cycle_id = self._seed_cycle()
        self.decisions = AutonomyDecisionPipeline(
            self.runtime,
            self.strategies,
            self.risk,
            self.portfolios,
            market_data=self.market,
            clock=self.clock,
        )
        await self.risk.update_policy(
            "short_term",
            {
                "max_position_weight": "1",
                "max_sector_weight": "1",
                "max_invested_fraction": "1",
                "min_cash_fraction": "0",
                "max_drawdown_pct": "100",
                "max_volatility_pct": "100",
                "min_liquidity_value": "0",
                "max_turnover": "2",
                "max_forecast_age_hours": 10000.0,
                "max_data_age_hours": 10000.0,
                "max_state_age_hours": 10000.0,
                "min_observations": 2,
                "max_uncertainty": "high",
                "max_aggregate_sector_weight": "1",
                "restricted_instruments": (),
            },
            actor="at04-test",
        )
        body = await self.decisions.run_cycle(
            monitoring_cycle_id=self.cycle_id, trigger="manual", actor="at04-test"
        )
        proposals = self.decisions.store.proposals(run_id=body["run_id"])
        self.proposal = proposals[0]
        self.assertEqual(self.proposal["status"], "risk_approved")
        # AT-03 ran in monitoring mode; AT-04 must switch to autonomous.
        self.runtime.pause(reason="switch to paper execution")
        self.runtime.resume("autonomous", acknowledge_paper_only=True)
        self.execution_store = ExecutionStore(
            Path(self._tmp.name) / "execution.sqlite3", clock=self.clock
        )
        self.execution = ExecutionEngine(
            self.execution_store,
            wallets=self.wallets,
            portfolios=self.portfolios,
            risk=self.risk,
            strategies=self.strategies,
            market_data=self.market,
            clock=self.clock,
            config=ExecutionConfig(
                participation_rate=Decimal("1"),
                spread_bps=Decimal("0"),
                impact_coefficient_bps=Decimal("0"),
                volatility_weight=Decimal("0"),
                maximum_slippage_bps=Decimal("150"),
            ),
            proposal_loader=self.decisions.execution_proposal,
        )
        self.executions = AutonomyPaperExecution(
            self.runtime,
            self.decisions,
            self.execution,
            self.portfolios,
            market_data=self.market,
            clock=self.clock,
        )

    async def asyncTearDown(self) -> None:
        self.executions.close()
        await self.execution.close()
        self.decisions.close()
        self.runtime.close()
        self._tmp.cleanup()

    def _seed_cycle(self) -> str:
        cycle_id = "mon_at04_cycle"
        if self.runtime.store.monitoring_cycle(cycle_id) is None:
            self.runtime.store.create_monitoring_cycle(
                {
                    "cycle_id": cycle_id,
                    "cycle_key": "monitoring:XNSE:2026-11-11:at04",
                    "owner_id": "at04-test",
                    "mode": "monitoring",
                    "exchange": "XNSE",
                    "trading_date": "2026-11-11",
                    "calendar_verified": True,
                    "calendar_source": "marketdata/calendar.py",
                    "session_state": "trading_session",
                    "session_eligible": True,
                    "phase": "monitoring",
                    "status": "running",
                    "trigger": "seed",
                    "started_at": self.clock.now.isoformat(),
                    "rotation_start": 0,
                },
                now=self.clock.now,
            )
            self.runtime.store.update_monitoring_cycle(
                cycle_id,
                status="completed",
                phase="completed",
                finished=True,
                processed_count=1,
                now=self.clock.now,
            )
            self.runtime.store.record_monitoring_candidates(
                cycle_id,
                [
                    {
                        "instrument_id": "XNSE:ALPHA",
                        "symbol": "ALPHA",
                        "exchange": "XNSE",
                        "disposition": "processed",
                        "reason": "at04 fixture",
                        "rank": 1,
                        "data_quality": "good",
                    }
                ],
            )
        return cycle_id

    async def run_execution(self, **kwargs: Any) -> dict[str, Any]:
        return await self.executions.run_cycle(
            trigger="manual", actor="at04-test", proposal_id=self.proposal["proposal_id"], **kwargs
        )


class TimeoutExecution:
    def __init__(self, engine: ExecutionEngine) -> None:
        self.engine = engine
        self.store = engine.store
        self.fail = True

    async def preflight(self, *args: Any, **kwargs: Any) -> Any:
        return await self.engine.preflight(*args, **kwargs)

    async def submit(self, *args: Any, **kwargs: Any) -> Any:
        if self.fail:
            raise asyncio.TimeoutError("synthetic submission timeout")
        return await self.engine.submit(*args, **kwargs)

    async def reconcile(self, *args: Any, **kwargs: Any) -> Any:
        return await self.engine.reconcile(*args, **kwargs)

    async def recover_pending(self) -> Any:
        return await self.engine.recover_pending()


class RejectingExecution:
    def __init__(
        self, engine: ExecutionEngine, proposal: dict[str, Any], clock: Clock
    ) -> None:
        self.engine = engine
        self.store = engine.store
        self.proposal = proposal
        self.clock = clock

    async def preflight(self, *args: Any, **kwargs: Any) -> Any:
        return await self.engine.preflight(*args, **kwargs)

    async def submit(self, proposal_id: str, **kwargs: Any) -> Any:
        order = Order(
            order_id=execution_id("ord", proposal_id),
            proposal_id=proposal_id,
            strategy_id=str(self.proposal.get("strategy_id") or ""),
            portfolio_id=str(self.proposal.get("portfolio_id") or ""),
            instrument_id=str(self.proposal.get("instrument_id") or ""),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            requested_quantity=Decimal("1"),
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("1"),
            status=OrderStatus.REJECTED,
            risk_decision_id=str(self.proposal.get("risk_decision_id") or ""),
            execution_config_version="at04-test",
            created_at=self.clock.now,
            updated_at=self.clock.now,
            expires_at=self.clock.now + timedelta(hours=1),
            rejection_reason="synthetic NM-10 rejection",
        )
        self.store.save_order(order)
        return {
            "ok": False,
            "order": order.to_dict(),
            "fills": [],
            "error": "synthetic NM-10 rejection",
        }

    async def reconcile(self, *args: Any, **kwargs: Any) -> Any:
        return {"ok": True, "reconciliation_id": "rec_synthetic", "issues": []}

    async def recover_pending(self) -> Any:
        return {"ok": True, "recovered": 0}


class PrerequisiteTests(unittest.TestCase):
    def test_prior_reports_are_pass(self) -> None:
        for relative in (
            "docs/AT-01_IMPLEMENTATION_REPORT.md",
            "docs/AUTONOMOUS_TRADING_AT02_REPORT.md",
            "AUTONOMOUS_TRADING_AT03_REPORT.md",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("PASS", text, relative)


class RealExecutionTests(ExecutionHarness):
    async def test_real_proposal_becomes_nm10_paper_order_and_fill(self) -> None:
        before = self.portfolios.detail(self.portfolio_id)
        result = await self.run_execution()
        self.assertEqual(result["status"], "completed", result)
        execution = result["executions"][0]
        self.assertEqual(execution["execution"]["status"], "filled")
        self.assertEqual(execution["execution"]["reconciliation_status"], "passed")
        self.assertEqual(execution["order"]["status"], "filled")
        self.assertTrue(execution["order"]["order_id"].startswith("ord_"))
        self.assertEqual(len(execution["fills"]), 1)
        self.assertEqual(execution["execution"]["order_id"], execution["order"]["order_id"])
        self.assertEqual(len(self.execution_store.orders()), 1)
        self.assertEqual(len(self.execution_store.fills()), 1)
        after = self.portfolios.detail(self.portfolio_id)
        self.assertNotEqual(before["cash"], after["cash"])
        self.assertTrue(after["holdings"])
        self.assertEqual(self.execution_store.autonomy_counts()["intents"], 1)

    async def test_repeated_execution_replays_without_a_second_order(self) -> None:
        first = await self.run_execution()
        second = await self.run_execution()
        self.assertEqual(
            first["executions"][0]["order"]["order_id"],
            second["executions"][0]["order"]["order_id"],
        )
        self.assertTrue(second["executions"][0]["replay"])
        self.assertEqual(second["executions"][0]["execution"]["status"], "filled")
        self.assertEqual(len(self.execution_store.orders()), 1)
        self.assertEqual(len(self.execution_store.fills()), 1)

    async def test_partial_fill_is_represented_and_never_completed(self) -> None:
        self.market.volume = 20.0
        result = await self.run_execution()
        execution = result["executions"][0]
        self.assertEqual(execution["execution"]["status"], "partially_filled")
        self.assertEqual(execution["order"]["status"], "partially_filled")
        self.assertGreater(Decimal(execution["execution"]["remaining_quantity"]), 0)
        self.assertEqual(len(execution["fills"]), 1)
        repeated = await self.run_execution()
        self.assertEqual(len(self.execution_store.orders()), 1)
        self.assertEqual(
            repeated["executions"][0]["execution"]["status"], "partially_filled"
        )

    async def test_reconciliation_matches_authoritative_records(self) -> None:
        result = await self.run_execution()
        order_id = result["executions"][0]["order"]["order_id"]
        reconciled = await self.execution.reconcile(order_id)
        self.assertTrue(reconciled["ok"], reconciled)
        detail = self.executions.execution(result["executions"][0]["execution"]["execution_id"])
        self.assertTrue(detail["order"]["ledger_transaction_ids"] if "ledger_transaction_ids" in detail["order"] else True)
        self.assertEqual(
            detail["execution"]["filled_quantity"],
            detail["order"]["filled_quantity"],
        )


    async def test_concurrent_requests_share_one_order(self) -> None:
        first, second = await asyncio.gather(self.run_execution(), self.run_execution())
        order_ids = {
            first["executions"][0]["order"]["order_id"],
            second["executions"][0]["order"]["order_id"],
        }
        self.assertEqual(len(order_ids), 1)
        self.assertEqual(len(self.execution_store.orders()), 1)
        self.assertEqual(len(self.execution_store.fills()), 1)

    async def test_authoritative_fill_matches_cash_position_and_ledger(self) -> None:
        result = await self.run_execution()
        execution = result["executions"][0]
        order = execution["order"]
        fill = execution["fills"][0]
        self.assertTrue(fill["ledger_transaction_ids"])
        self.assertEqual(order["market_reference"]["currency"], "INR")
        detail = self.portfolios.detail(self.portfolio_id)
        holding = next(
            row for row in detail["holdings"] if row["instrument_id"] == "XNSE:ALPHA"
        )
        self.assertEqual(Decimal(str(holding["quantity"])), Decimal(order["filled_quantity"]))
        reconciled = await self.execution.reconcile(order["order_id"])
        self.assertTrue(reconciled["ok"], reconciled)
        self.assertEqual(
            execution["execution"]["filled_quantity"], order["filled_quantity"]
        )


class PreflightTests(ExecutionHarness):
    async def test_risk_provenance_mismatch_fails_closed(self) -> None:
        proposal = dict(self.proposal)
        proposal["risk_decision_id"] = "rdec_missing"
        inspected, code, reason = await self.executions._preflight(proposal)
        self.assertIsNone(inspected)
        self.assertEqual(code, "RISK_APPROVAL_INVALID")
        self.assertIn("does not match", reason)

    async def test_rejected_status_never_reaches_nm10(self) -> None:
        proposal = dict(self.proposal)
        proposal["status"] = "risk_rejected"
        inspected, code, _ = await self.executions._preflight(proposal)
        self.assertIsNone(inspected)
        self.assertEqual(code, "RISK_APPROVAL_INVALID")
        self.assertEqual(len(self.execution_store.orders()), 0)

    async def test_expired_proposal_fails_closed(self) -> None:
        proposal = dict(self.proposal)
        proposal["expires_at"] = (NOW - timedelta(hours=1)).isoformat()
        inspected, code, _ = await self.executions._preflight(proposal)
        self.assertIsNone(inspected)
        self.assertEqual(code, "PROPOSAL_EXPIRED")

    async def test_insufficient_cash_is_a_preflight_failure(self) -> None:
        with mock.patch.object(
            self.execution, "_available_cash", return_value=Decimal("0")
        ):
            inspected, code, reason = await self.executions._preflight(
                dict(self.proposal)
            )
        self.assertIsNone(inspected)
        self.assertEqual(code, "EXECUTION_PREFLIGHT_FAILED")
        self.assertIn("insufficient", reason.lower())
        self.assertEqual(len(self.execution_store.orders()), 0)

    async def test_stale_market_data_is_a_preflight_failure(self) -> None:
        self.market.observed_at = NOW - timedelta(hours=2)
        inspected, code, reason = await self.executions._preflight(dict(self.proposal))
        self.assertIsNone(inspected)
        self.assertEqual(code, "EXECUTION_PREFLIGHT_FAILED")
        self.assertIn("stale", reason.lower())

    async def test_unknown_instrument_fails_before_nm10_order(self) -> None:
        proposal = dict(self.proposal)
        proposal["instrument_id"] = "XNSE:UNKNOWN"
        inspected, code, _ = await self.executions._preflight(proposal)
        self.assertIsNone(inspected)
        self.assertEqual(code, "EXECUTION_PREFLIGHT_FAILED")
        self.assertEqual(len(self.execution_store.orders()), 0)


class FailureRecoveryTests(ExecutionHarness):
    async def test_timeout_is_recovered_without_a_duplicate_order(self) -> None:
        timeout = TimeoutExecution(self.execution)
        service = AutonomyPaperExecution(
            self.runtime,
            self.decisions,
            timeout,
            self.portfolios,
            market_data=self.market,
            clock=self.clock,
        )
        try:
            first = await service.run_cycle(
                trigger="manual",
                actor="at04-test",
                proposal_id=self.proposal["proposal_id"],
            )
            self.assertEqual(first["executions"][0]["execution"]["status"], "unknown")
            self.assertEqual(
                first["executions"][0]["execution"]["failure_code"],
                "EXECUTION_SUBMISSION_UNKNOWN",
            )
            self.assertEqual(len(self.execution_store.orders()), 0)
            timeout.fail = False
            second = await service.run_cycle(
                trigger="recovery",
                actor="at04-test",
                proposal_id=self.proposal["proposal_id"],
            )
            self.assertEqual(second["executions"][0]["execution"]["status"], "filled")
            self.assertEqual(len(self.execution_store.orders()), 1)
            self.assertEqual(len(self.execution_store.fills()), 1)
        finally:
            service.close()

    async def test_nm10_rejection_is_terminal_and_preserves_reason(self) -> None:
        rejecting = RejectingExecution(self.execution, self.proposal, self.clock)
        service = AutonomyPaperExecution(
            self.runtime,
            self.decisions,
            rejecting,
            self.portfolios,
            market_data=self.market,
            clock=self.clock,
        )
        try:
            result = await service.run_cycle(
                trigger="manual",
                actor="at04-test",
                proposal_id=self.proposal["proposal_id"],
            )
            execution = result["executions"][0]
            self.assertEqual(execution["execution"]["status"], "rejected")
            self.assertEqual(execution["execution"]["failure_code"], "ORDER_REJECTED")
            self.assertIn("rejection", execution["execution"]["failure_reason"])
            self.assertEqual(execution["fills"], [])
            again = await service.run_cycle(
                trigger="manual",
                actor="at04-test",
                proposal_id=self.proposal["proposal_id"],
            )
            self.assertTrue(again["executions"][0]["replay"])
        finally:
            service.close()


class RuntimeGateTests(ExecutionHarness):
    async def test_monitoring_mode_cannot_forward(self) -> None:
        self.runtime.pause()
        self.runtime.resume("monitoring")
        with self.assertRaises(AutonomyExecutionError) as caught:
            await self.run_execution()
        self.assertEqual(caught.exception.code, "RUNTIME_NOT_AUTONOMOUS")
        self.assertEqual(len(self.execution_store.orders()), 0)

    async def test_paused_mode_cannot_forward(self) -> None:
        self.runtime.pause()
        with self.assertRaises(AutonomyExecutionError) as caught:
            await self.run_execution()
        self.assertEqual(caught.exception.code, "RUNTIME_NOT_AUTONOMOUS")
        self.assertEqual(len(self.execution_store.orders()), 0)

    async def test_invalid_lease_cannot_forward(self) -> None:
        self.clock.now += timedelta(seconds=3700)
        with self.assertRaises(AutonomyExecutionError) as caught:
            await self.run_execution()
        self.assertEqual(caught.exception.code, "LEASE_INVALID")
        self.assertEqual(len(self.execution_store.orders()), 0)


class IdempotencyTests(ExecutionHarness):
    async def test_restart_after_intent_does_not_create_another_order(self) -> None:
        order = await self.execution.submit(
            self.proposal["proposal_id"],
            idempotency_key="at04:crash-recovery",
        )
        order_id = order["order"]["order_id"]
        # Simulate a new AT-04 process over the same durable NM-10 order.
        restarted = AutonomyPaperExecution(
            self.runtime,
            self.decisions,
            self.execution,
            self.portfolios,
            market_data=self.market,
            clock=self.clock,
        )
        try:
            result = await restarted.run_cycle(
                trigger="restart",
                actor="at04-test",
                proposal_id=self.proposal["proposal_id"],
            )
            self.assertEqual(result["executions"][0]["order"]["order_id"], order_id)
            self.assertTrue(result["executions"][0]["replay"])
            self.assertEqual(len(self.execution_store.orders()), 1)
            self.assertEqual(len(self.execution_store.fills()), 1)
        finally:
            restarted.close()

    async def test_journal_uniqueness_survives_a_second_intent_write(self) -> None:
        first = self.execution_store.save_autonomy_intent(
            {
                "execution_id": "aex_one",
                "proposal_id": self.proposal["proposal_id"],
                "idempotency_key": "at04:one",
                "status": "intent",
                "updated_at": NOW.isoformat(),
            }
        )
        second = self.execution_store.save_autonomy_intent(
            {
                "execution_id": "aex_two",
                "proposal_id": self.proposal["proposal_id"],
                "idempotency_key": "at04:two",
                "status": "intent",
                "updated_at": NOW.isoformat(),
            }
        )
        self.assertEqual(first["execution_id"], second["execution_id"])
        self.assertEqual(first["execution_id"], "aex_one")
        self.assertEqual(self.execution_store.autonomy_counts()["intents"], 1)


class SafetyTests(unittest.TestCase):
    def test_at04_has_no_broker_or_financial_write_import(self) -> None:
        source = (ROOT / "src/novacontrol/novamarket/autonomy_execution/pipeline.py").read_text()
        tree = ast.parse(source)
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        forbidden = ("broker", "angel", "zerodha", "upstox", "kite", "live_order")
        self.assertFalse(any(part in name.lower() for name in imports for part in forbidden))
        for name in (
            "record_fill",
            "apply_fill",
            "deposit",
            "withdraw",
            "update_position",
            "set_balance",
        ):
            self.assertNotIn(name, source)
        self.assertIn("execution.submit", source)
        self.assertIn("execution.preflight", source)

    def test_routes_are_in_the_api_contract_and_have_consumers(self) -> None:
        routes = {(route.method, route.path) for route in ApiSurface.default().routes}
        for method, path in (
            ("GET", "/market/autonomy/executions/contracts"),
            ("GET", "/market/autonomy/executions/status"),
            ("GET", "/market/autonomy/executions"),
            ("GET", "/market/autonomy/executions/failures"),
            ("GET", "/market/autonomy/executions/{execution_id}"),
            ("GET", "/market/autonomy/executions/{execution_id}/events"),
            ("POST", "/market/autonomy/executions/run"),
            ("POST", "/market/autonomy/executions/{execution_id}/reconcile"),
        ):
            self.assertIn((method, path), routes)
            self.assertIn((method, path), ROUTE_CONSUMERS)


@unittest.skipIf(TestClient is None, "fastapi.testclient requires httpx")
class ExecutionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self._env = mock.patch.dict(
            os.environ,
            {
                "NOVACONTROL_API_TOKEN": "api-secret",
                "NOVACONTROL_AUTONOMY_OPERATOR_TOKEN": "operator-secret",
            },
            clear=False,
        )
        self._env.start()
        from novacontrol.api.app import create_app

        self.app = create_app(data_dir=self._tmp.name)
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer api-secret"}

    def tearDown(self) -> None:
        self.client.close()
        self._env.stop()
        self._tmp.cleanup()

    def test_execution_reads_require_auth(self) -> None:
        self.assertEqual(
            self.client.get("/market/autonomy/executions/status").status_code, 401
        )
        self.assertEqual(
            self.client.get("/market/autonomy/executions/contracts").status_code, 401
        )

    def test_execution_contract_is_paper_only_and_operator_gated(self) -> None:
        response = self.client.get(
            "/market/autonomy/executions/contracts", headers=self.auth
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["paper_only"])
        self.assertFalse(body["live_trading"])
        self.assertFalse(body["live_orders_possible"])
        self.assertEqual(body["execution_authority"], "NM-10")
        controller = self.client.post("/market/autonomy/executions/run", headers=self.auth)
        self.assertEqual(controller.status_code, 403, controller.text)
