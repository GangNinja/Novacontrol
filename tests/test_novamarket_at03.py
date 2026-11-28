"""AT-03 focused tests: decisions, provenance, idempotency and safety.

The harness uses the real NM-09 StrategyEngine, real NM-08 RiskEngine and real
portfolio engine.  Market data is deterministic, and every test compares the
financial snapshot before and after a decision run.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import sqlite3
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import mock

try:
    from fastapi.testclient import TestClient  # requires httpx
except Exception:  # pragma: no cover - optional dev dependency
    TestClient = None  # type: ignore[assignment]

from novacontrol.api.models import ApiSurface
from novacontrol.api.route_consumers import ROUTE_CONSUMERS
from novacontrol.novamarket.autonomy import AutonomyRuntime
from novacontrol.novamarket.decision import (
    AutonomyDecisionPipeline,
    DecisionError,
    DecisionErrorCode,
    DecisionProposalStatus,
    DecisionRun,
    DecisionRunStatus,
    DecisionStateError,
)
from novacontrol.novamarket.decision.store import DecisionStore
from novacontrol.novamarket.portfolio import PortfolioEngine, PortfolioStore
from novacontrol.novamarket.risk import RiskEngine, RiskStore
from novacontrol.novamarket.strategies import StrategyEngine, StrategyStore
from novacontrol.novamarket.wallet.engine import WalletEngine
from novacontrol.novamarket.wallet.store import WalletStore

NOW = datetime(2026, 11, 11, 4, 0, tzinfo=UTC)  # 09:30 IST, verified session


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Market:
    def __init__(self, closes: list[float] | None = None) -> None:
        self.closes = closes or [100.0] * 70 + [110.0]
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
        ascending: bool = True,
        start: datetime | None = None,
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
                {
                    "instrument_id": "XNSE:GAMMA",
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


class Harness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self._tmp.name) / "novamarket.sqlite3"
        self.clock = Clock()
        self.runtime = AutonomyRuntime(
            self.path, clock=self.clock, owner_id="at03-test", lease_seconds=600
        )
        self.wallets = WalletEngine(WalletStore(":memory:"))
        self.funding = (await self.wallets.create_wallet("house"))["wallet"]["wallet_id"]
        await self.wallets.deposit(self.funding, "200000.00")
        self.market = Market()
        self.forecasts = Forecasts()
        self.radar = Radar()
        self.portfolios = PortfolioEngine(
            PortfolioStore(":memory:"),
            self.wallets,
            market_data=self.market,
            discovery=Discovery(),
            forecasts=self.forecasts,
            radar=self.radar,
            clock=self.clock,
        )
        self.risk = RiskEngine(
            RiskStore(":memory:"),
            self.portfolios,
            market_data=self.market,
            forecasts=self.forecasts,
            radar=self.radar,
            discovery=Discovery(),
            wallets=self.wallets,
            clock=self.clock,
        )
        self.strategies = StrategyEngine(
            StrategyStore(":memory:"),
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
            name="at03-trend",
        )
        self.strategy_id = strategy["strategy"]["strategy_id"]
        await self.strategies.activate(self.strategy_id)
        self.runtime.start("monitoring")
        self._seed_cycle()
        self.pipeline = AutonomyDecisionPipeline(
            self.runtime,
            self.strategies,
            self.risk,
            self.portfolios,
            market_data=self.market,
            clock=self.clock,
        )

    async def asyncTearDown(self) -> None:
        self.pipeline.close()
        self.runtime.close()
        self._tmp.cleanup()

    def _seed_cycle(
        self,
        *,
        quality: str = "good",
        eligible: bool = True,
        status: str = "completed",
        instrument: str = "XNSE:ALPHA",
        exchange: str = "XNSE",
    ) -> str:
        cycle_id = f"mon_at03_{exchange}_{quality}_{eligible}_{status}_{instrument[-5:]}"
        existing = self.runtime.store.monitoring_cycle(cycle_id)
        if existing is None:
            self.runtime.store.create_monitoring_cycle(
                {
                    "cycle_id": cycle_id,
                    "cycle_key": f"monitoring:XNSE:2026-11-11:{cycle_id}",
                    "owner_id": "at03-test",
                    "mode": "monitoring",
                    "exchange": "XNSE",
                    "trading_date": "2026-11-11",
                    "calendar_verified": True,
                    "calendar_source": "marketdata/calendar.py",
                    "session_state": "trading_session",
                    "session_eligible": eligible,
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
                status=status,
                phase="completed" if status == "completed" else "monitoring",
                finished=True,
                processed_count=1 if status == "completed" else 0,
                now=self.clock.now,
            )
            self.runtime.store.record_monitoring_candidates(
                cycle_id,
                [
                    {
                        "instrument_id": instrument,
                        "symbol": instrument.split(":", 1)[1],
                        "exchange": exchange,
                        "disposition": "processed" if status == "completed" else "deferred",
                        "reason": "test fixture",
                        "rank": 1,
                        "data_quality": quality,
                    }
                ],
            )
        return cycle_id

    def snapshot(self) -> dict[str, Any]:
        detail = self.portfolios.detail(self.portfolio_id)
        wallet = self.wallets.wallet(self.funding)
        return {
            "cash": detail["cash"],
            "holdings": detail["holdings"],
            "valuation": detail["valuation"],
            "funding": wallet["wallet"]["balance"],
        }

    async def run_once(self, cycle_id: str | None = None) -> dict[str, Any]:
        return await self.pipeline.run_cycle(
            monitoring_cycle_id=cycle_id or self._seed_cycle(),
            trigger="manual",
            actor="at03-test",
        )


class RuntimeGateTests(Harness):
    async def test_disabled_runtime_refuses_before_any_record(self) -> None:
        self.runtime.stop()
        with self.assertRaises(DecisionError) as caught:
            await self.run_once()
        self.assertEqual(caught.exception.code, "RUNTIME_DISABLED")
        self.assertEqual(self.pipeline.store.metrics()["runs"], 0)

    async def test_paused_runtime_refuses(self) -> None:
        self.runtime.pause()
        with self.assertRaises(DecisionError) as caught:
            await self.run_once()
        self.assertEqual(caught.exception.code, "RUNTIME_PAUSED")
        self.assertEqual(self.pipeline.store.metrics()["runs"], 0)

    async def test_expired_lease_refuses(self) -> None:
        self.clock.now += timedelta(seconds=601)
        with self.assertRaises(DecisionError) as caught:
            await self.run_once()
        self.assertEqual(caught.exception.code, "LEASE_INVALID")

    async def test_ineligible_monitoring_cycle_refuses(self) -> None:
        cycle = self._seed_cycle(eligible=False, status="completed")
        with self.assertRaises(DecisionError) as caught:
            await self.run_once(cycle)
        self.assertEqual(
            caught.exception.code, "MONITORING_CYCLE_NOT_ELIGIBLE"
        )


class PipelineTests(Harness):
    async def test_real_strategy_signal_proposal_and_nm08_decision(self) -> None:
        before = self.snapshot()
        body = await self.run_once()
        self.assertEqual(body["ok"], True)
        self.assertGreaterEqual(body["evaluations"], 1)
        self.assertGreaterEqual(body["signals"], 1)
        self.assertGreaterEqual(body["proposals"], 1)
        proposals = self.pipeline.store.proposals(run_id=body["run_id"])
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(proposal["instrument_id"], "XNSE:ALPHA")
        self.assertEqual(proposal["execution_allowed"], False)
        self.assertIn(
            proposal["status"],
            {
                DecisionProposalStatus.RISK_APPROVED.value,
                DecisionProposalStatus.RISK_REJECTED.value,
                DecisionProposalStatus.RISK_RESTRICTED.value,
                DecisionProposalStatus.RISK_REVIEW.value,
            },
        )
        detail = self.pipeline.proposal_detail(proposal["proposal_id"])
        self.assertIsNotNone(detail["signal"])
        self.assertIsNotNone(detail["evaluation"])
        self.assertIsNotNone(detail["risk"])
        self.assertEqual(
            detail["risk"]["proposal_id"], proposal["proposal_id"]
        )
        self.assertEqual(
            detail["signal"]["signal_id"], proposal["signal_id"]
        )
        self.assertEqual(
            detail["evaluation"]["evaluation_id"],
            detail["signal"]["evaluation_id"],
        )
        self.assertEqual(self.snapshot(), before)

    async def test_legacy_venue_candidate_is_canonicalised_to_its_mic(self) -> None:
        body = await self.run_once(self._seed_cycle(exchange="NSE"))
        self.assertTrue(body["ok"], body)
        self.assertGreaterEqual(body["evaluations"], 1)
        proposals = self.pipeline.store.proposals(run_id=body["run_id"])
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["instrument_id"], "XNSE:ALPHA")

    async def test_cross_exchange_candidate_is_still_refused(self) -> None:
        cycle = self._seed_cycle(exchange="BSE")
        with self.assertRaises(DecisionError) as caught:
            await self.run_once(cycle)
        self.assertEqual(
            caught.exception.code, DecisionErrorCode.NO_ELIGIBLE_CANDIDATES.value
        )

    async def test_attribute_based_instruments_are_eligible_on_nse_and_nyse(self) -> None:
        # Market-data seams may return a read-only record.  Its identity is
        # accepted only when it is the requested MIC:SYMBOL, and the venue must
        # match the configured cycle exchange.
        original = self.market.get_instrument
        self.market.get_instrument = lambda instrument_id: SimpleNamespace(
            instrument_id=instrument_id,
            mic=instrument_id.split(":", 1)[0],
            exchange=instrument_id.split(":", 1)[0],
        )
        try:
            for mic in ("XNSE", "XNYS"):
                with self.subTest(mic=mic):
                    cycle = self._seed_cycle(
                        instrument=f"{mic}:ALPHA", exchange=mic
                    )
                    candidates = self.pipeline._candidates(cycle)
                    self.assertEqual(len(candidates), 1)
                    self.assertEqual(candidates[0]["instrument_id"], f"{mic}:ALPHA")
                    self.assertEqual(candidates[0]["exchange"], mic)

            closed_cycle = self._seed_cycle(
                instrument="XNSE:CLOSED", exchange="XNYS"
            )
            self.assertEqual(self.pipeline._candidates(closed_cycle), [])
        finally:
            self.market.get_instrument = original

    async def test_an_instrument_lookup_with_another_identity_fails_closed(self) -> None:
        self.market.get_instrument = lambda _instrument_id: SimpleNamespace(
            instrument_id="XNSE:OTHER"
        )
        cycle = self._seed_cycle()
        with self.assertRaises(DecisionError) as caught:
            await self.run_once(cycle)
        self.assertEqual(
            caught.exception.code, DecisionErrorCode.NO_ELIGIBLE_CANDIDATES.value
        )

    async def test_a_lookup_mic_must_match_the_candidate_prefix_and_exchange(self) -> None:
        original = self.market.get_instrument
        self.market.get_instrument = lambda instrument_id: SimpleNamespace(
            instrument_id=instrument_id,
            mic="XNYS",
        )
        try:
            cycle = self._seed_cycle()  # XNSE:ALPHA, configured exchange XNSE
            self.assertEqual(self.pipeline._candidates(cycle), [])
            with self.assertRaises(DecisionError) as caught:
                await self.run_once(cycle)
            self.assertEqual(
                caught.exception.code,
                DecisionErrorCode.NO_ELIGIBLE_CANDIDATES.value,
            )
        finally:
            self.market.get_instrument = original

    async def test_repeat_run_reuses_signal_proposal_and_risk(self) -> None:
        cycle = self._seed_cycle()
        first = await self.run_once(cycle)
        metrics = self.pipeline.store.metrics()
        second = await self.run_once(cycle)
        after = self.pipeline.store.metrics()
        self.assertEqual(after["proposals"], metrics["proposals"])
        self.assertEqual(after["risk_assessments"], metrics["risk_assessments"])
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(
            len(self.pipeline.store.runs_for_cycle(cycle)),
            2,
        )

    async def test_stale_candidate_produces_no_proposal(self) -> None:
        cycle = self._seed_cycle(quality="stale")
        body = await self.run_once(cycle)
        self.assertGreaterEqual(body["invalid"], 1)
        self.assertEqual(
            self.pipeline.store.proposals(run_id=body["run_id"]), []
        )

    async def test_no_action_does_not_become_a_proposal(self) -> None:
        self.market.closes = [100.0] * 71
        body = await self.run_once(self._seed_cycle())
        self.assertEqual(
            self.pipeline.store.proposals(run_id=body["run_id"]), []
        )

    async def test_new_entry_at_the_position_cap_is_skipped_with_reason(self) -> None:
        original = self.portfolios.detail

        def capped(portfolio_id: str) -> dict[str, Any]:
            body = dict(original(portfolio_id))
            body["holdings"] = [
                {
                    "instrument_id": f"XNSE:CAP{index}",
                    "quantity": "1",
                    "market_value": "1000.00",
                }
                for index in range(6)
            ]
            return body

        with mock.patch.object(self.portfolios, "detail", side_effect=capped):
            body = await self.run_once(self._seed_cycle())
        self.assertEqual(self.pipeline.store.proposals(run_id=body["run_id"]), [])
        self.assertGreaterEqual(body["invalid"], 1)
        events = self.pipeline.store.events(run_id=body["run_id"])
        skipped = [row for row in events if row.get("kind") == "proposal.skipped"]
        self.assertTrue(skipped)
        self.assertIn("max_positions", skipped[-1]["message"])

    async def test_risk_rejection_is_recorded_and_stays_non_executable(self) -> None:
        await self.risk.update_policy(
            "short_term", {"restricted_instruments": ("XNSE:ALPHA",)}, actor="test"
        )
        body = await self.run_once(self._seed_cycle())
        proposals = self.pipeline.store.proposals(run_id=body["run_id"])
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(proposal["status"], "risk_rejected")
        self.assertEqual(proposal["risk_outcome"], "rejected")
        risk = self.pipeline.store.risk_assessment(proposal["proposal_id"])
        self.assertIsNotNone(risk)
        self.assertTrue(risk["violations"])
        self.assertFalse(proposal["execution_allowed"])

    async def test_real_approved_decision_is_never_marked_executed(self) -> None:
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
            actor="test",
        )
        before = self.snapshot()
        body = await self.run_once(self._seed_cycle())
        proposals = self.pipeline.store.proposals(run_id=body["run_id"])
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(proposal["status"], "risk_approved")
        self.assertEqual(proposal["risk_outcome"], "approved")
        self.assertFalse(proposal["is_executed"])
        self.assertFalse(proposal["execution_allowed"])
        self.assertFalse(proposal["orders_possible"])
        self.assertEqual(self.snapshot(), before)

    async def test_recovery_marks_an_interrupted_run_failed(self) -> None:
        run = DecisionRun(
            run_id="drun_interrupted",
            selection_key="interrupted",
            monitoring_cycle_id=self._seed_cycle(),
            mode="monitoring",
            status=DecisionRunStatus.RUNNING,
            trigger="manual",
            portfolio_ids=(self.portfolio_id,),
            strategy_ids=(self.strategy_id,),
            instrument_ids=("XNSE:ALPHA",),
            evaluations=0,
            signals=0,
            proposals=0,
            approved=0,
            rejected=0,
            invalid=0,
            errors=0,
            started_at=self.clock.now,
            finished_at=None,
            error_code="",
            error="",
            actor="test",
        )
        self.pipeline.store.create_run(run)
        self.assertEqual(self.pipeline.recover(), 1)
        stored = self.pipeline.store.run("drun_interrupted")
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["error_code"], "CYCLE_CANCELLED")

    async def test_risk_engine_failure_is_not_approval(self) -> None:
        self.risk.assess = None  # type: ignore[assignment]
        with self.assertRaises(DecisionStateError) as caught:
            await self.run_once(self._seed_cycle())
        self.assertEqual(caught.exception.code, "RISK_ENGINE_UNAVAILABLE")
        proposal = self.pipeline.store.proposals()[0]
        self.assertEqual(proposal["status"], "risk_pending")
        self.assertIn("risk engine", proposal["risk_error"].lower())

    async def test_concurrent_run_is_refused(self) -> None:
        gate = asyncio.Event()
        original = self.pipeline._assess_proposal

        async def slow(*args: Any, **kwargs: Any) -> Any:
            await gate.wait()
            return await original(*args, **kwargs)

        self.pipeline._assess_proposal = slow  # type: ignore[assignment]
        first = asyncio.create_task(self.run_once(self._seed_cycle()))
        await asyncio.sleep(0.01)
        with self.assertRaises(DecisionStateError) as caught:
            await self.pipeline.run_cycle(trigger="manual")
        self.assertEqual(caught.exception.code, "ALREADY_RUNNING")
        gate.set()
        await first


@unittest.skipIf(TestClient is None, "fastapi.testclient requires httpx")
class DecisionApiTests(unittest.TestCase):
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
        self.operator = {
            "Authorization": "Bearer api-secret",
            "X-Autonomy-Operator-Token": "operator-secret",
        }

    def tearDown(self) -> None:
        self.client.close()
        self._env.stop()
        self._tmp.cleanup()

    def test_decision_reads_are_authenticated_and_non_trading(self) -> None:
        for path in (
            "/market/autonomy/decisions/contracts",
            "/market/autonomy/decisions/status",
            "/market/autonomy/decisions/runs",
            "/market/autonomy/decisions/signals",
            "/market/autonomy/decisions/proposals",
            "/market/autonomy/decisions/predictions",
            "/market/autonomy/decisions/outcomes",
            "/market/autonomy/decisions/forecast-policy",
        ):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        contracts = self.client.get(
            "/market/autonomy/decisions/contracts", headers=self.auth
        )
        self.assertEqual(contracts.status_code, 200, contracts.text)
        body = contracts.json()
        self.assertTrue(body["paper_only"])
        self.assertFalse(body["live_trading"])
        self.assertFalse(body["orders_possible"])
        self.assertFalse(body["execution_implemented"])
        status = self.client.get(
            "/market/autonomy/decisions/status", headers=self.auth
        )
        self.assertEqual(status.status_code, 200, status.text)
        self.assertTrue(status.json()["paper_only"])
        policy = self.client.get(
            "/market/autonomy/decisions/forecast-policy", headers=self.auth
        )
        self.assertEqual(policy.status_code, 200, policy.text)
        self.assertTrue(policy.json()["policy"]["horizons"]["short_term"])
        decisions = self.client.get(
            "/market/autonomy/decisions/predictions", headers=self.auth
        )
        self.assertEqual(decisions.status_code, 200, decisions.text)
        self.assertEqual(decisions.json()["total"], 0)
        outcomes = self.client.get(
            "/market/autonomy/decisions/outcomes", headers=self.auth
        )
        self.assertEqual(outcomes.status_code, 200, outcomes.text)
        self.assertEqual(outcomes.json()["total"], 0)

    def test_decision_run_requires_the_operator_permission(self) -> None:
        refused = self.client.post(
            "/market/autonomy/decisions/run", headers=self.auth, json={}
        )
        self.assertEqual(refused.status_code, 403, refused.text)

    def test_decision_run_refuses_without_a_ready_monitoring_cycle(self) -> None:
        response = self.client.post(
            "/market/autonomy/decisions/run", headers=self.operator, json={}
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn(
            response.json()["detail"]["error"],
            {
                "RUNTIME_DISABLED",
                "RUNTIME_PAUSED",
                "LEASE_INVALID",
                "MONITORING_CYCLE_REQUIRED",
                "NO_ELIGIBLE_CANDIDATES",
                "NO_ACTIVE_STRATEGY",
            },
        )

    def test_decision_limits_and_unknown_ids(self) -> None:
        for path in (
            "/market/autonomy/decisions/runs",
            "/market/autonomy/decisions/signals",
            "/market/autonomy/decisions/proposals",
            "/market/autonomy/decisions/predictions",
            "/market/autonomy/decisions/outcomes",
        ):
            response = self.client.get(
                path, params={"limit": 0}, headers=self.auth
            )
            self.assertEqual(response.status_code, 422, (path, response.text))
        missing_run = self.client.get(
            "/market/autonomy/decisions/runs/drun_missing", headers=self.auth
        )
        self.assertEqual(missing_run.status_code, 404, missing_run.text)
        missing_proposal = self.client.get(
            "/market/autonomy/decisions/proposals/dprop_missing",
            headers=self.auth,
        )
        self.assertEqual(missing_proposal.status_code, 404, missing_proposal.text)
        missing_prediction = self.client.get(
            "/market/autonomy/decisions/predictions/pdec_missing",
            headers=self.auth,
        )
        self.assertEqual(missing_prediction.status_code, 404, missing_prediction.text)
        invalid_type = self.client.get(
            "/market/autonomy/decisions/predictions",
            params={"decision_type": "SHORT"},
            headers=self.auth,
        )
        self.assertEqual(invalid_type.status_code, 422, invalid_type.text)
        refused_policy = self.client.put(
            "/market/autonomy/decisions/forecast-policy",
            headers=self.auth,
            json={},
        )
        self.assertEqual(refused_policy.status_code, 403, refused_policy.text)
        refused_outcomes = self.client.post(
            "/market/autonomy/decisions/outcomes/run", headers=self.auth, json={}
        )
        self.assertEqual(refused_outcomes.status_code, 403, refused_outcomes.text)
        invalid_limit = self.client.post(
            "/market/autonomy/decisions/outcomes/run",
            headers=self.operator,
            json={"limit": "many"},
        )
        self.assertEqual(invalid_limit.status_code, 422, invalid_limit.text)

    def test_every_at03_route_has_a_contract_and_consumer(self) -> None:
        expected = {
            ("GET", "/market/autonomy/decisions/contracts"),
            ("GET", "/market/autonomy/decisions/status"),
            ("GET", "/market/autonomy/decisions/runs"),
            ("GET", "/market/autonomy/decisions/runs/{run_id}"),
            ("GET", "/market/autonomy/decisions/signals"),
            ("GET", "/market/autonomy/decisions/proposals"),
            ("GET", "/market/autonomy/decisions/proposals/{proposal_id}"),
            ("GET", "/market/autonomy/decisions/predictions"),
            ("GET", "/market/autonomy/decisions/predictions/{decision_id}"),
            ("GET", "/market/autonomy/decisions/outcomes"),
            ("POST", "/market/autonomy/decisions/outcomes/run"),
            ("GET", "/market/autonomy/decisions/forecast-policy"),
            ("PUT", "/market/autonomy/decisions/forecast-policy"),
            ("POST", "/market/autonomy/decisions/run"),
        }
        self.assertTrue(expected.issubset(ROUTE_CONSUMERS))
        surface = {(route.method, route.path) for route in ApiSurface.default().routes}
        self.assertTrue(expected.issubset(surface))


class SafetyTests(unittest.TestCase):
    def test_at03_has_no_financial_mutation_or_execution_call(self) -> None:
        root = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "novacontrol"
            / "novamarket"
            / "decision"
        )
        forbidden = {
            "apply",
            "submit",
            "forward",
            "propose",
            "create_order",
            "record_fill",
        }
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = ""
                    if isinstance(node.func, ast.Attribute):
                        name = node.func.attr
                    elif isinstance(node.func, ast.Name):
                        name = node.func.id
                    self.assertNotIn(
                        name, forbidden, f"{path.name}:{node.lineno} calls {name}"
                    )

    def test_store_is_additive_and_does_not_touch_financial_tables(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "novacontrol"
            / "novamarket"
            / "decision"
            / "store.py"
        ).read_text(encoding="utf-8")
        for table in (
            "wallets",
            "wallet_entries",
            "portfolio_positions",
            "portfolio_valuations",
            "execution_orders",
            "execution_fills",
        ):
            self.assertNotIn(f"INSERT INTO {table}", source)
            self.assertNotIn(f"UPDATE {table}", source)


if __name__ == "__main__":
    unittest.main()
