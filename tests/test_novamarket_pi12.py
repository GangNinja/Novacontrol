"""PI-12 focused tests: stored forecasts -> AT-03 -> NM-08 -> NM-10 -> outcomes.

The portfolio, wallet, risk, decision, execution and reconciliation stores are
real.  Market data, discovery, forecasts and radar are deterministic fixtures;
no network, broker or real-money path is used.
"""

from __future__ import annotations

import asyncio
import sqlite3
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import mock

from novacontrol.core.events import EventBus
from novacontrol.novamarket.autonomy import AutonomyRuntime
from novacontrol.novamarket.autonomy_execution import AutonomyPaperExecution
from novacontrol.novamarket.decision import (
    AutonomyDecisionPipeline,
    DecisionProposalStatus,
    ForecastDecisionConfig,
    ForecastDecisionConfigError,
    ForecastDecisionEvaluator,
    ForecastDecisionSource,
    ForecastDecisionType,
    ForecastOutcomeTracker,
    ForecastRiskAdapter,
    DecisionStateError,
)
from novacontrol.novamarket.decision.store import DecisionStore
from novacontrol.novamarket.marketdata.models import (
    Instrument,
    OhlcvBar,
    PriceObservation,
    TradingStatus,
)
from novacontrol.novamarket.execution import (
    ExecutionConfig,
    ExecutionEngine,
    ExecutionStore,
    OrderStatus,
)
from novacontrol.novamarket.portfolio import PortfolioEngine, PortfolioStore
from novacontrol.novamarket.service import NovaMarketService
from novacontrol.novamarket.prediction.models import (
    CalibrationStatus,
    ConfirmationStage,
    DataInterval,
    DataQuality,
    Direction,
    ForecastHorizon,
    ForecastOutput,
    ForecastValidity,
    Freshness,
    PredictionUncertainty,
    ProbabilityStatus,
)
from novacontrol.novamarket.risk import RiskEngine, RiskStore
from novacontrol.novamarket.strategies import StrategyEngine, StrategyStore
from novacontrol.novamarket.wallet.engine import WalletEngine
from novacontrol.novamarket.wallet.store import WalletStore

NOW = datetime(2026, 11, 11, 4, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Market:
    def __init__(self) -> None:
        self.closes = [100.0] * 70 + [110.0]
        self.observed_at = NOW
        self.usable = True
        self.freshness = "fresh"
        self.quality = "valid"
        self.bar_volume = 100000.0
        self.future: list[tuple[datetime, float]] = []
        self.partial_times: set[datetime] = set()

    def latest(self, instrument_id: str) -> Any:
        return SimpleNamespace(
            usable=self.usable,
            selected=SimpleNamespace(
                price=str(self.closes[-1]),
                observed_at=self.observed_at,
                freshness=SimpleNamespace(value=self.freshness),
                quality=SimpleNamespace(value=self.quality),
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
                volume=self.bar_volume,
                partial=False,
                observed_at=NOW - timedelta(days=len(self.closes) - index - 1),
            )
            for index, value in enumerate(self.closes)
        ]
        items.extend(
            SimpleNamespace(
                close=value,
                high=value * 1.01,
                low=value * 0.99,
                volume=self.bar_volume,
                partial=at in self.partial_times,
                observed_at=at,
            )
            for at, value in self.future
        )
        if start is not None:
            items = [item for item in items if item.observed_at >= start]
        items.sort(key=lambda item: item.observed_at)
        return SimpleNamespace(items=items[-limit:] if limit else items)

    def get_instrument(self, instrument_id: str) -> Any:
        return SimpleNamespace(
            instrument_id=instrument_id,
            currency="INR",
            asset_class=SimpleNamespace(value="equity"),
            trading_status=SimpleNamespace(value="open"),
            metadata={"sector": "Technology"},
            to_dict=lambda: {
                "instrument_id": instrument_id,
                "sector": "Technology",
                "asset_class": "equity",
                "currency": "INR",
            },
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


class RiskForecasts:
    def __init__(self) -> None:
        self.created_at = NOW
        self.rows = [
            {
                "forecast_id": f"fc_risk_{horizon}",
                "instrument_id": "XNSE:ALPHA",
                "horizon": horizon,
                "predicted_return": "0.03",
                "probability_up": "0.65",
                "uncertainty": "low",
                "model_id": "ensemble",
                "created_at": self.created_at.isoformat(),
            }
            for horizon in ("few_sessions", "one_month", "three_months")
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
                    "model_id": "ensemble",
                    "instrument_id": "XNSE:ALPHA",
                    "horizon": name,
                    "state": "active",
                }
                for name in ("few_sessions", "one_month", "three_months")
            ]
        }


class Radar:
    def instrument_events(self, instrument_id: str, limit: int = 20) -> dict[str, Any]:
        return {"events": []}


class ForecastSource:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = [make_forecast("3d", "up", 0.03)]
        self.evaluation = {
            "available": True,
            "reports": [{"evaluation_id": "pev_1", "status": "completed"}],
            "automatic_promotion": False,
        }

    def forecasts(self, instrument_id: str) -> dict[str, Any]:
        return {"forecasts": [dict(row) for row in self.rows]}

    def evaluation_evidence(self, instrument_id: str, horizon: str) -> dict[str, Any]:
        return dict(self.evaluation)


def make_forecast(
    horizon: str,
    direction: str,
    expected: float,
    *,
    target_days: int | None = None,
    uncertainty: str = "medium",
    disagreement: float = 0.10,
    validity: str = "confirmed",
    stage: str = "t_minus_1",
    probability: float | None = None,
    generated_at: datetime = NOW,
) -> dict[str, Any]:
    probability = (
        probability
        if probability is not None
        else (0.65 if direction == "up" else 0.65 if direction == "down" else 0.40)
    )
    p_up = probability if direction == "up" else (1.0 - probability) * 0.6
    p_down = probability if direction == "down" else (1.0 - probability) * 0.6
    p_neutral = max(0.0, 1.0 - p_up - p_down)
    days = target_days if target_days is not None else max(1, int(horizon[:-1]))
    return {
        "forecast_id": f"fc_{horizon}_{direction}_{expected}",
        "lifecycle_id": "life_pi12",
        "lifecycle_stage": stage,
        "instrument_id": "XNSE:ALPHA",
        "exchange": "XNSE",
        "forecast_horizon": horizon,
        "predicted_direction": direction,
        "predicted_return": expected,
        "predicted_price": "113.30",
        "reference_price": "110.00",
        "lower_return": expected - 0.01,
        "upper_return": expected + 0.02,
        "forecast_uncertainty": uncertainty,
        "uncertainty_width": 0.03,
        "p_up": p_up,
        "p_down": p_down,
        "p_neutral": p_neutral,
        "probability_status": "calibrated",
        "calibration_status": "calibrated",
        "disagreement": disagreement,
        "model_id": "ensemble",
        "model_version": "ens-1.0",
        "ensemble_id": "ens_run_1",
        "member_versions": ["m1:v1"],
        "feature_set_version": "fs1",
        "dataset_version": "ds1",
        "validity": validity,
        "data_freshness": "fresh",
        "data_quality": "valid",
        "data_snapshot_id": "snap_1",
        "generated_at": generated_at.isoformat(),
        "data_observed_at": generated_at.isoformat(),
        "target_at": (generated_at + timedelta(days=days)).isoformat(),
        "reference_at": generated_at.isoformat(),
        "validity_conditions": [],
        "digest": "digest_1",
        "reasons": [],
        "metadata": {"phase": "PI-12-test"},
    }


def portfolio_doc(portfolio_id: str, *, holding: bool = False) -> dict[str, Any]:
    holdings = []
    if holding:
        holdings = [
            {
                "instrument_id": "XNSE:ALPHA",
                "quantity": "100",
                "average_cost": "100",
                "market_value": "11000",
                "weight": "0.11",
                "unrealized_pnl": "1000",
                "holding_days": 3,
                "sector": "Technology",
            }
        ]
    return {
        "portfolio": {
            "portfolio_id": portfolio_id,
            "horizon": "short_term",
            "status": "active",
            "initial_capital": "100000.00",
            "updated_at": NOW.isoformat(),
        },
        "holdings": holdings,
        "cash": "89000.00" if holding else "100000.00",
        "valuation": {
            "total_value": "100000.00",
            "cash": "89000.00" if holding else "100000.00",
        },
        "counts": {"positions": len(holdings), "valuations": len(holdings)},
    }


class Harness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self._tmp.name) / "novamarket.sqlite3"
        self.clock = Clock()
        self.runtime = AutonomyRuntime(
            self.path, clock=self.clock, owner_id="pi12-test", lease_seconds=3600
        )
        self.wallets = WalletEngine(WalletStore(":memory:", clock=self.clock))
        self.funding = (await self.wallets.create_wallet("house"))["wallet"]["wallet_id"]
        await self.wallets.deposit(self.funding, "200000.00")
        self.market = Market()
        self.risk_forecasts = RiskForecasts()
        self.radar = Radar()
        self.portfolios = PortfolioEngine(
            PortfolioStore(":memory:", clock=self.clock),
            self.wallets,
            market_data=self.market,
            discovery=Discovery(),
            forecasts=self.risk_forecasts,
            radar=self.radar,
            clock=self.clock,
        )
        self.risk = RiskEngine(
            RiskStore(":memory:", clock=self.clock),
            self.portfolios,
            market_data=self.market,
            forecasts=self.risk_forecasts,
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
            forecasts=self.risk_forecasts,
            radar=self.radar,
            discovery=Discovery(),
            clock=self.clock,
        )
        created = await self.portfolios.create(
            "short_term", funding_wallet_id=self.funding, capital="100000.00"
        )
        self.portfolio_id = created["portfolio"]["portfolio_id"]
        self.source = ForecastSource()
        self.runtime.start("monitoring")
        self.cycle_id = self._seed_cycle()
        self.pipeline = AutonomyDecisionPipeline(
            self.runtime,
            self.strategies,
            self.risk,
            self.portfolios,
            market_data=self.market,
            forecasts=self.source,
            clock=self.clock,
            open_orders=lambda portfolio_id, instrument_id: [],
        )
        await self.permissive_risk()

    async def asyncTearDown(self) -> None:
        self.pipeline.close()
        self.runtime.close()
        self._tmp.cleanup()

    async def permissive_risk(self) -> None:
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
            actor="pi12-test",
        )

    def _seed_cycle(self) -> str:
        cycle_id = "mon_pi12_cycle"
        if self.runtime.store.monitoring_cycle(cycle_id) is None:
            self.runtime.store.create_monitoring_cycle(
                {
                    "cycle_id": cycle_id,
                    "cycle_key": "monitoring:XNSE:2026-11-11:pi12",
                    "owner_id": "pi12-test",
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
                        "reason": "pi12 fixture",
                        "rank": 1,
                        "data_quality": "good",
                    }
                ],
            )
        return cycle_id

    async def run_once(self) -> dict[str, Any]:
        return await self.pipeline.run_cycle(
            monitoring_cycle_id=self.cycle_id, trigger="manual", actor="pi12-test"
        )

    def decisions(self) -> list[dict[str, Any]]:
        return self.pipeline.store.prediction_decisions(limit=50)["items"]

    async def seed_holding(self) -> None:
        original = self.portfolios.detail

        def with_holding(portfolio_id: str) -> dict[str, Any]:
            body = dict(original(portfolio_id))
            body.update(portfolio_doc(portfolio_id, holding=True))
            return body

        self._detail_patch = mock.patch.object(
            self.portfolios, "detail", side_effect=with_holding
        )
        self._detail_patch.start()
        self.addCleanup(self._detail_patch.stop)


class ForecastEvaluatorTests(Harness):
    async def test_buy_is_a_real_risk_approved_proposal_without_portfolio_mutation(self) -> None:
        before = self.portfolios.detail(self.portfolio_id)
        body = await self.run_once()
        self.assertEqual(body["prediction_decisions_by_type"]["BUY"], 1)
        rows = self.decisions()
        self.assertEqual(len(rows), 1)
        decision = rows[0]
        self.assertEqual(decision["decision_type"], "BUY")
        self.assertEqual(decision["horizon"], "3d")
        self.assertEqual(decision["status"], "risk_approved")
        self.assertTrue(decision["primary_forecast_id"].startswith("fc_3d_up"))
        self.assertEqual(decision["forecast_version"], "ens-1.0")
        self.assertEqual(decision["ensemble_version"], "ens_run_1")
        self.assertEqual(decision["risk_outcome"], "approved")
        self.assertTrue(decision["proposal_id"].startswith("dprop_"))
        self.assertTrue(decision["reasons"])
        self.assertTrue(decision["observations"] is not None)
        self.assertFalse(decision["automatic_promotion"])
        detail = self.pipeline.prediction_decision_detail(decision["decision_id"])
        self.assertEqual(detail["proposal"]["status"], "risk_approved")
        self.assertEqual(detail["risk"]["outcome"], "approved")
        self.assertEqual(detail["signal"]["prediction_decision_id"], decision["decision_id"])
        self.assertEqual(detail["signal"]["forecast_horizon"], "3d")
        self.assertEqual(self.portfolios.detail(self.portfolio_id), before)

    async def test_nm08_rejection_is_recorded_and_has_no_order(self) -> None:
        await self.risk.update_policy(
            "short_term", {"restricted_instruments": ("XNSE:ALPHA",)}, actor="test"
        )
        body = await self.run_once()
        self.assertEqual(body["prediction_decisions_by_type"]["BUY"], 1)
        decision = self.decisions()[0]
        self.assertEqual(decision["status"], "risk_rejected")
        self.assertEqual(decision["risk_outcome"], "rejected")
        self.assertTrue(decision["risk_result"]["violations"])
        view = self.pipeline.execution_proposal(decision["proposal_id"])
        self.assertIsNotNone(view)
        assert view is not None
        self.assertFalse(view.get("execution_allowed", False))
        self.assertEqual(view["status"], "risk_rejected")
        self.assertEqual(decision["order_id"], "")

    async def test_sell_requires_and_uses_an_existing_long_position(self) -> None:
        await self.seed_holding()
        self.source.rows = [
            make_forecast("3d", "down", -0.03, target_days=3)
        ]
        body = await self.run_once()
        self.assertEqual(body["prediction_decisions_by_type"]["SELL"], 1)
        decision = self.decisions()[0]
        self.assertEqual(decision["decision_type"], "SELL")
        self.assertEqual(
            decision["status"],
            "risk_approved",
            decision.get("risk_result"),
        )
        self.assertEqual(decision["portfolio_context"]["holding_quantity"], "100")
        self.assertEqual(decision["portfolio_context"]["sector"], "Technology")
        self.assertIsInstance(decision["portfolio_context"]["constraints"], list)
        proposal = self.pipeline.store.proposal(decision["proposal_id"])
        self.assertEqual(proposal["side"], "sell")
        self.assertEqual(proposal["action"], "reduce_position")

    async def test_downward_forecast_without_holding_cannot_create_a_short(self) -> None:
        self.source.rows = [make_forecast("3d", "down", -0.03, target_days=3)]
        body = await self.run_once()
        self.assertEqual(body["prediction_decisions_by_type"]["NO_ACTION"], 1)
        decision = self.decisions()[0]
        self.assertEqual(decision["decision_type"], "NO_ACTION")
        self.assertEqual(decision["proposal_id"], "")
        self.assertIn("short position", " ".join(decision["reasons"]))

    async def test_hold_for_an_existing_position_creates_no_proposal(self) -> None:
        await self.seed_holding()
        body = await self.run_once()
        self.assertEqual(body["prediction_decisions_by_type"]["HOLD"], 1)
        decision = self.decisions()[0]
        self.assertEqual(decision["decision_type"], "HOLD")
        self.assertEqual(decision["proposal_id"], "")
        self.assertIn("retained", " ".join(decision["reasons"]))

    async def test_wait_for_uncertainty_and_disagreement(self) -> None:
        self.source.rows = [
            make_forecast("3d", "up", 0.03, target_days=3, uncertainty="high")
        ]
        await self.run_once()
        self.assertEqual(self.decisions()[0]["decision_type"], "WAIT")
        self.assertIn("uncertainty", " ".join(self.decisions()[0]["contradictions"]))
        self.clock.now = NOW
        self.runtime.close()
        self.runtime = AutonomyRuntime(
            Path(self._tmp.name) / "second.sqlite3",
            clock=self.clock,
            owner_id="pi12-test-2",
            lease_seconds=3600,
        )
        self.runtime.start("monitoring")
        self.cycle_id = self._seed_cycle()
        self.source.rows = [
            make_forecast("3d", "up", 0.03, target_days=3, disagreement=0.80)
        ]
        self.pipeline.close()
        self.pipeline = AutonomyDecisionPipeline(
            self.runtime,
            self.strategies,
            self.risk,
            self.portfolios,
            market_data=self.market,
            forecasts=self.source,
            clock=self.clock,
            open_orders=lambda portfolio_id, instrument_id: [],
        )
        await self.run_once()
        row = self.decisions()[0]
        self.assertEqual(row["decision_type"], "WAIT")
        self.assertIn("disagreement", " ".join(row["contradictions"]))

    async def test_missing_forecast_is_no_action(self) -> None:
        self.source.rows = []
        body = await self.run_once()
        self.assertEqual(body["prediction_decisions_by_type"]["NO_ACTION"], 1)
        row = self.decisions()[0]
        self.assertEqual(row["decision_type"], "NO_ACTION")
        self.assertIn("no stored forecast", " ".join(row["reasons"]))
        # A decision with no forecast has no measurable target and must not sit
        # at the head of the outcome queue forever.
        self.assertEqual(
            self.pipeline.store.pending_prediction_decisions(limit=10), []
        )

    async def test_reopening_the_store_records_the_current_schema_version(self) -> None:
        connection = self.pipeline.store._connection
        connection.execute(
            "UPDATE decision_meta SET value = '1' WHERE key = 'schema_version'"
        )
        connection.commit()
        reopened = DecisionStore(self.runtime.store)
        row = connection.execute(
            "SELECT value FROM decision_meta WHERE key = 'schema_version'"
        ).fetchone()
        self.assertEqual(reopened.schema_version, 2)
        self.assertEqual(row["value"], "2")

    async def test_inconsistent_direction_and_return_is_not_a_signal(self) -> None:
        self.source.rows = [make_forecast("3d", "up", -0.03, target_days=3)]
        body = await self.run_once()
        self.assertEqual(body["prediction_decisions_by_type"]["WAIT"], 1)
        decision = self.decisions()[0]
        self.assertEqual(decision["decision_type"], "WAIT")
        self.assertIn(
            "upward direction", " ".join(decision["contradictions"])
        )
        self.assertEqual(decision["proposal_id"], "")

    async def test_invalid_probabilities_are_not_a_signal(self) -> None:
        self.source.rows = [
            {**make_forecast("3d", "up", 0.03, target_days=3), "p_up": 1.2}
        ]
        body = await self.run_once()
        self.assertEqual(body["prediction_decisions_by_type"]["WAIT"], 1)
        decision = self.decisions()[0]
        self.assertEqual(decision["decision_type"], "WAIT")
        self.assertIn("outside [0, 1]", " ".join(decision["contradictions"]))
        self.assertEqual(decision["proposal_id"], "")

    async def test_unconfirmed_or_stale_forecast_is_not_a_signal(self) -> None:
        self.source.rows = [
            make_forecast("3d", "up", 0.03, target_days=3, validity="unconfirmed")
        ]
        body = await self.run_once()
        self.assertEqual(body["prediction_decisions_by_type"]["WAIT"], 1)
        self.assertIn(
            "validity", " ".join(self.decisions()[0]["contradictions"])
        )

    async def test_multi_horizon_conflict_waits_instead_of_merging(self) -> None:
        self.source.rows = [
            make_forecast("3d", "up", 0.03, target_days=3),
            make_forecast("1d", "down", -0.03, target_days=1),
        ]
        body = await self.run_once()
        self.assertEqual(body["prediction_decisions_by_type"]["WAIT"], 1)
        decision = self.decisions()[0]
        self.assertEqual(decision["decision_type"], "WAIT")
        self.assertIn("disagree", " ".join(decision["reasons"]))
        self.assertGreaterEqual(len(decision["horizon_evidence"]), 2)

    async def test_incompatible_horizon_is_not_applied(self) -> None:
        self.source.rows = [
            make_forecast("90d", "up", 0.20, target_days=90)
        ]
        body = await self.run_once()
        self.assertEqual(body["prediction_decisions_by_type"]["NO_ACTION"], 1)
        self.assertIn(
            "configured horizons", " ".join(self.decisions()[0]["reasons"])
        )

    async def test_pending_paper_order_blocks_a_duplicate_decision(self) -> None:
        self.pipeline.close()
        self.pipeline = AutonomyDecisionPipeline(
            self.runtime,
            self.strategies,
            self.risk,
            self.portfolios,
            market_data=self.market,
            forecasts=self.source,
            clock=self.clock,
            open_orders=lambda portfolio_id, instrument_id: [
                {"order_id": "ord_existing", "status": "accepted"}
            ],
        )
        body = await self.run_once()
        self.assertEqual(body["prediction_decisions_by_type"]["WAIT"], 1)
        self.assertEqual(self.decisions()[0]["proposal_id"], "")
        self.assertIn("reconciled", " ".join(self.decisions()[0]["reasons"]))

    async def test_verified_high_impact_event_blocks_new_entry(self) -> None:
        class EventRadar:
            def instrument_events(self, instrument_id: str, limit: int = 20) -> dict[str, Any]:
                return {
                    "events": [
                        {
                            "event_id": "evt_high",
                            "verification": "officially_confirmed",
                            "importance": "high",
                            "headline": "Verified company event",
                        }
                    ]
                }

        self.pipeline.close()
        self.pipeline = AutonomyDecisionPipeline(
            self.runtime,
            self.strategies,
            self.risk,
            self.portfolios,
            market_data=self.market,
            forecasts=self.source,
            radar=EventRadar(),
            clock=self.clock,
            open_orders=lambda portfolio_id, instrument_id: [],
        )
        body = await self.run_once()
        self.assertEqual(body["prediction_decisions_by_type"]["WAIT"], 1)
        self.assertIn("event", " ".join(self.decisions()[0].get("reasons") or []))

    async def test_decision_audit_is_immutable_and_idempotent(self) -> None:
        first = await self.run_once()
        second = await self.run_once()
        rows = self.decisions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.pipeline.store.proposals(run_id=first["run_id"])), 1)
        self.assertEqual(second["prediction_skipped_existing"], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.pipeline.store.transaction():
                self.pipeline.store._connection.execute(
                    "UPDATE prediction_decisions SET decision_type='SELL'"
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.pipeline.store.transaction():
                self.pipeline.store._connection.execute(
                    "DELETE FROM prediction_decisions"
                )
        detail = self.pipeline.prediction_decision_detail(rows[0]["decision_id"])
        self.assertTrue(detail["events"])

    async def test_policy_persists_without_promoting_a_model(self) -> None:
        result = self.pipeline.set_forecast_policy(
            {
                "min_expected_return": "0.02",
                "min_probability": "0.60",
                "horizons": {
                    "short_term": ["1h", "1d", "3d"],
                    "medium_term": ["1d", "3d", "5d", "30d"],
                    "long_term": ["30d", "90d"],
                },
                "priority": {
                    "short_term": ["1h", "1d", "3d"],
                    "medium_term": ["5d", "3d", "1d", "30d"],
                    "long_term": ["90d", "30d"],
                },
            }
        )
        self.assertEqual(result["policy"]["min_expected_return"], 0.02)
        self.assertFalse(result["policy"]["automatic_promotion"])
        policy = self.pipeline.forecast_policy()
        self.assertEqual(policy["source"], "stored")
        self.assertEqual(policy["policy"]["min_probability"], 0.6)
        # A restart re-opens the pipeline; the persisted policy must still be
        # used rather than silently falling back to defaults because the stored
        # response carries descriptive metadata.
        self.pipeline.close()
        self.pipeline = AutonomyDecisionPipeline(
            self.runtime,
            self.strategies,
            self.risk,
            self.portfolios,
            market_data=self.market,
            forecasts=self.source,
            clock=self.clock,
            open_orders=lambda portfolio_id, instrument_id: [],
        )
        reopened = self.pipeline.forecast_policy()
        self.assertEqual(reopened["source"], "stored")
        self.assertEqual(reopened["policy"]["min_expected_return"], 0.02)
        self.assertEqual(reopened["policy"]["min_probability"], 0.6)
        self.assertEqual(
            reopened["policy"]["horizons"]["short_term"], ["1h", "1d", "3d"]
        )


class EndToEndTests(Harness):
    async def test_execution_fill_and_outcome_are_separate_evidence(self) -> None:
        body = await self.run_once()
        decision = self.decisions()[0]
        self.assertEqual(decision["status"], "risk_approved")
        self.runtime.pause(reason="switch to paper execution")
        self.runtime.resume("autonomous", acknowledge_paper_only=True)
        execution_store = ExecutionStore(
            Path(self._tmp.name) / "execution.sqlite3", clock=self.clock
        )
        execution = ExecutionEngine(
            execution_store,
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
            proposal_loader=self.pipeline.execution_proposal,
        )
        try:
            executions = AutonomyPaperExecution(
                self.runtime,
                self.pipeline,
                execution,
                self.portfolios,
                market_data=self.market,
                clock=self.clock,
            )
            try:
                result = await executions.run_cycle(
                    trigger="manual",
                    actor="pi12-test",
                    proposal_id=decision["proposal_id"],
                )
            finally:
                executions.close()
            self.assertGreaterEqual(result.get("total", 0), 1)
            order = execution_store.order_for_proposal(decision["proposal_id"])
            self.assertIsNotNone(order)
            self.assertEqual(order.status.value, "filled")
            tracker = ForecastOutcomeTracker(
                self.pipeline.store,
                self.market,
                execution_provider=execution,
                clock=self.clock,
            )
            before_target = tracker.track(limit=10)
            self.assertEqual(before_target["measured_count"], 0)
            synced = self.pipeline.prediction_decision_detail(decision["decision_id"])
            self.assertEqual(synced["decision"]["status"], "filled")
            self.clock.now = NOW + timedelta(days=3, hours=1)
            self.market.future.append((self.clock.now, 120.0))
            after_target = tracker.track(limit=10)
            self.assertEqual(after_target["measured_count"], 1)
            outcome = after_target["measured"][0]
            self.assertTrue(outcome["forecast"]["correct"])
            self.assertTrue(outcome["decision"]["correct"])
            self.assertEqual(outcome["execution"]["status"], "filled")
            self.assertEqual(outcome["execution"]["fill_count"], 1)
            self.assertGreater(Decimal(outcome["portfolio"]["paper_pnl"]), 0)
            self.assertFalse(outcome["automatic_promotion"])
            self.assertFalse(outcome["learning_applied"])
            self.assertEqual(
                outcome["forecast"]["basis"],
                "forecast direction compared with the target-period return",
            )
            self.assertNotEqual(
                outcome["forecast"]["basis"], outcome["decision"]["basis"]
            )
            repeated = tracker.track(limit=10)
            self.assertEqual(repeated["measured_count"], 0)
        finally:
            await execution.close()

    async def test_partial_fill_is_reconciled_without_a_duplicate_order(self) -> None:
        await self.run_once()
        decision = self.decisions()[0]
        self.assertEqual(decision["status"], "risk_approved")
        self.runtime.pause(reason="switch to paper execution")
        self.runtime.resume("autonomous", acknowledge_paper_only=True)
        self.market.bar_volume = 20.0
        execution_store = ExecutionStore(
            Path(self._tmp.name) / "execution-partial.sqlite3", clock=self.clock
        )
        execution = ExecutionEngine(
            execution_store,
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
            proposal_loader=self.pipeline.execution_proposal,
        )
        try:
            executions = AutonomyPaperExecution(
                self.runtime,
                self.pipeline,
                execution,
                self.portfolios,
                market_data=self.market,
                clock=self.clock,
            )
            try:
                first = await executions.run_cycle(
                    trigger="manual",
                    actor="pi12-test",
                    proposal_id=decision["proposal_id"],
                )
                second = await executions.run_cycle(
                    trigger="manual",
                    actor="pi12-test",
                    proposal_id=decision["proposal_id"],
                )
            finally:
                executions.close()
            self.assertEqual(first.get("total"), 1)
            self.assertEqual(second.get("total"), 1)
            orders = execution_store.orders(limit=10)
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0].status, OrderStatus.PARTIALLY_FILLED)
            self.assertGreater(orders[0].filled_quantity, Decimal("0"))
            self.assertGreater(orders[0].remaining_quantity, Decimal("0"))
            tracker = ForecastOutcomeTracker(
                self.pipeline.store,
                self.market,
                execution_provider=execution,
                clock=self.clock,
            )
            tracker.track(limit=10)
            detail = self.pipeline.prediction_decision_detail(decision["decision_id"])
            self.assertEqual(detail["decision"]["status"], "partially_filled")
            events = self.pipeline.store.prediction_decision_events(
                decision["decision_id"]
            )
            self.assertTrue(
                any(
                    event["kind"] == "execution.partially_filled"
                    for event in events
                )
            )
        finally:
            await execution.close()

    async def test_pending_order_statuses_are_audited_as_submitted(self) -> None:
        await self.run_once()
        decision = self.decisions()[0]
        tracker = ForecastOutcomeTracker(
            self.pipeline.store,
            self.market,
            clock=self.clock,
        )
        for status in ("created", "pending_validation", "cancel_pending"):
            self.assertTrue(
                tracker._sync_execution_status(
                    {**decision, "status": "risk_approved"},
                    {"status": status, "order_id": "ord_pending"},
                )
            )
            current = self.pipeline.store.prediction_decision(
                decision["decision_id"]
            )
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current["status"], "order_submitted")
        kinds = {
            event["kind"]
            for event in self.pipeline.store.prediction_decision_events(
                self.decisions()[0]["decision_id"]
            )
        }
        self.assertTrue(
            {"execution.created", "execution.pending_validation", "execution.cancel_pending"}
            .issubset(kinds)
        )

    async def test_partial_target_bar_is_not_used_as_an_outcome(self) -> None:
        await self.run_once()
        decision = self.decisions()[0]
        target = NOW + timedelta(days=3, hours=1)
        self.clock.now = target
        self.market.future.append((target, 120.0))
        self.market.partial_times.add(target)
        tracker = ForecastOutcomeTracker(
            self.pipeline.store,
            self.market,
            clock=self.clock,
        )
        first = tracker.track(limit=10)
        self.assertEqual(first["measured_count"], 0)
        self.assertEqual(first["pending_count"], 1)
        self.market.partial_times.clear()
        second = tracker.track(limit=10)
        self.assertEqual(second["measured_count"], 1)
        self.assertEqual(
            second["measured"][0]["decision_id"], decision["decision_id"]
        )

    async def test_json_string_bar_timestamp_is_measured(self) -> None:
        await self.run_once()
        decision = self.decisions()[0]
        target = datetime.fromisoformat(
            str(decision["target_at"]).replace("Z", "+00:00")
        )
        self.clock.now = target

        class MappingMarket:
            def bars(self, instrument_id: str, **kwargs: Any) -> Any:
                return {
                    "bars": [
                        {
                            "close": "120",
                            "observed_at": target.isoformat(),
                            "partial": False,
                        }
                    ]
                }

        tracker = ForecastOutcomeTracker(
            self.pipeline.store,
            MappingMarket(),
            clock=self.clock,
        )
        result = tracker.track(limit=10)
        self.assertEqual(result["measured_count"], 1)
        self.assertEqual(result["measured"][0]["decision_id"], decision["decision_id"])
        self.assertEqual(result["measured"][0]["actual_at"], target.isoformat())

    async def test_nm10_preflight_rejection_creates_no_order_or_fill(self) -> None:
        await self.run_once()
        decision = self.decisions()[0]
        self.assertEqual(decision["status"], "risk_approved")
        self.runtime.pause(reason="switch to paper execution")
        self.runtime.resume("autonomous", acknowledge_paper_only=True)
        self.market.usable = False
        self.market.freshness = "stale"
        execution_store = ExecutionStore(
            Path(self._tmp.name) / "execution-rejected.sqlite3", clock=self.clock
        )
        execution = ExecutionEngine(
            execution_store,
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
            proposal_loader=self.pipeline.execution_proposal,
        )
        try:
            before = self.portfolios.detail(self.portfolio_id)
            executions = AutonomyPaperExecution(
                self.runtime,
                self.pipeline,
                execution,
                self.portfolios,
                market_data=self.market,
                clock=self.clock,
            )
            try:
                await executions.run_cycle(
                    trigger="manual",
                    actor="pi12-test",
                    proposal_id=decision["proposal_id"],
                )
            finally:
                executions.close()
            order = execution_store.order_for_proposal(decision["proposal_id"])
            self.assertIsNone(order)
            self.assertEqual(self.portfolios.detail(self.portfolio_id), before)
            detail = self.pipeline.prediction_decision_detail(decision["decision_id"])
            self.assertEqual(
                detail["decision"]["status"], "execution_preflight_failed"
            )
            self.assertEqual(
                detail["decision"]["failure_code"], "EXECUTION_PREFLIGHT_FAILED"
            )
            self.assertIn("market price", detail["decision"]["execution_error"].lower())
            self.assertEqual(detail["decision"]["order_id"], "")
            self.assertEqual(
                detail["decision"]["execution"]["status"],
                "execution_preflight_failed",
            )
            self.assertNotIn(
                detail["decision"]["status"],
                {"filled", "partially_filled", "order_rejected"},
            )
        finally:
            await execution.close()

    async def test_nm10_submission_timeout_is_persisted_on_decision(self) -> None:
        await self.run_once()
        decision = self.decisions()[0]
        self.assertEqual(decision["status"], "risk_approved")
        self.runtime.pause(reason="switch to paper execution")
        self.runtime.resume("autonomous", acknowledge_paper_only=True)
        execution_store = ExecutionStore(
            Path(self._tmp.name) / "execution-timeout.sqlite3", clock=self.clock
        )
        execution = ExecutionEngine(
            execution_store,
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
            proposal_loader=self.pipeline.execution_proposal,
        )
        try:
            executions = AutonomyPaperExecution(
                self.runtime,
                self.pipeline,
                execution,
                self.portfolios,
                market_data=self.market,
                clock=self.clock,
            )
            try:
                with mock.patch.object(
                    execution,
                    "submit",
                    new=mock.AsyncMock(
                        side_effect=asyncio.TimeoutError(
                            "synthetic submission timeout"
                        )
                    ),
                ):
                    first = await executions.run_cycle(
                        trigger="manual",
                        actor="pi12-test",
                        proposal_id=decision["proposal_id"],
                    )
                execution_detail = first["executions"][0]["execution"]
                self.assertEqual(execution_detail["status"], "unknown")
                self.assertEqual(
                    execution_detail["failure_code"],
                    "EXECUTION_SUBMISSION_UNKNOWN",
                )
                self.assertIsNone(
                    execution_store.order_for_proposal(decision["proposal_id"])
                )
                failed = self.pipeline.prediction_decision_detail(
                    decision["decision_id"]
                )["decision"]
                self.assertEqual(
                    failed["status"], "execution_submission_failed"
                )
                self.assertEqual(
                    failed["failure_code"], "EXECUTION_SUBMISSION_UNKNOWN"
                )
                self.assertIn("timed out", failed["execution_error"].lower())
                self.assertEqual(failed["order_id"], "")
                self.assertEqual(
                    failed["execution"]["execution_id"],
                    execution_detail["execution_id"],
                )
                self.assertTrue(
                    any(
                        event["kind"] == "execution.submission_failed"
                        for event in failed["events"]
                    )
                )

                second = await executions.run_cycle(
                    trigger="recovery",
                    actor="pi12-test",
                    proposal_id=decision["proposal_id"],
                )
                self.assertEqual(
                    second["executions"][0]["execution"]["status"], "filled"
                )
                recovered = self.pipeline.prediction_decision_detail(
                    decision["decision_id"]
                )["decision"]
                self.assertEqual(recovered["status"], "filled")
                self.assertTrue(recovered["order_id"].startswith("ord_"))
            finally:
                executions.close()
        finally:
            await execution.close()


class ServiceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_service_attaches_pi12_source_and_risk_adapter(self) -> None:
        service = NovaMarketService(
            event_bus=EventBus(continue_on_error=True, history=50)
        )
        try:
            await service.enable(reason="pi12 service integration")
            status = service.autonomy_decision_status()
            self.assertTrue(status["initialised"])
            contract = service.autonomy_decision_contract()
            self.assertTrue(contract["prediction_integration"]["configured"])
            self.assertEqual(
                contract["prediction_integration"]["forecast_sources"],
                ["PI-08", "PI-10", "PI-11"],
            )
            self.assertIsNotNone(service._pi12_decision_source_cache)
            self.assertIsInstance(
                service._risk_engine._forecasts, ForecastRiskAdapter
            )
            policy = service.autonomy_forecast_policy()
            self.assertEqual(policy["policy"]["version"], "pi12-policy-1.0")
            updated = service.autonomy_forecast_policy_update(
                {"min_expected_return": "0.02"}
            )
            self.assertEqual(updated["policy"]["min_expected_return"], 0.02)
            self.assertEqual(
                service.autonomy_prediction_decisions()["total"], 0
            )
            self.assertEqual(service.autonomy_prediction_outcomes()["total"], 0)
            self.assertEqual(
                service.track_prediction_outcomes()["measured_count"], 0
            )
            self.assertFalse(service.autonomy_decision_status()["live_trading"])
            self.assertFalse(service.autonomy_decision_status()["orders_possible"])
            with mock.patch.object(
                service, "_execution_engine", SimpleNamespace(store=None)
            ):
                with self.assertRaises(RuntimeError):
                    service._pi12_open_orders("pf_test", "XNSE:ALPHA")
        finally:
            await service.shutdown(reason="pi12 test teardown")


class ApplicationWorkflowTests(unittest.IsolatedAsyncioTestCase):
    """PI-12 through the real application service and its actual stores."""

    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.clock = Clock()
        self.service = NovaMarketService(
            data_dir=self._tmp.name,
            clock=self.clock,
            event_bus=EventBus(continue_on_error=True, history=100),
        )
        await self.service.enable(reason="pi12 application end-to-end")
        started = self.service.autonomy_start(
            "monitoring", reason="pi12 application end-to-end"
        )
        self.assertEqual(started["mode"], "monitoring")
        self.runtime = self.service._ensure_autonomy()
        self.cycle_id = "mon_pi12_application_cycle"
        self.instrument_id = "XNSE:ALPHA"
        self._seed_market()
        wallet = await self.service.create_wallet("pi12-application")
        self.wallet_id = wallet["wallet"]["wallet_id"]
        await self.service.wallet_deposit(self.wallet_id, "200000.00")
        created = await self.service.portfolio_create(
            "short_term",
            name="PI-12 application portfolio",
            funding_wallet_id=self.wallet_id,
            capital="100000.00",
        )
        self.portfolio_id = created["portfolio"]["portfolio_id"]
        strategy = await self.service.create_strategy(
            "trend_sma",
            config={"fast_period": 3, "slow_period": 5},
            name="pi12 application fallback probe",
        )
        self.strategy_id = strategy["strategy"]["strategy_id"]
        await self.service.strategy_transition(
            self.strategy_id, "activate", actor="pi12-application-test"
        )
        await self.service.risk_update_policy(
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
            actor="pi12-application-test",
        )
        self._seed_cycle()
        self._seed_forecast("BUY", generated_at=self.clock.now)

    async def asyncTearDown(self) -> None:
        await self.service.shutdown(reason="pi12 application test teardown")
        self._tmp.cleanup()

    def _seed_market(self) -> None:
        engine = self.service._ensure_market_data()
        store = self.service._market_store
        self.assertIsNotNone(store)
        assert store is not None
        store.upsert_instrument(
            Instrument.create(
                symbol="ALPHA",
                name="Alpha Industries",
                exchange="XNSE",
                currency="INR",
                timezone="Asia/Kolkata",
                provider_symbols={"yahoo": "ALPHA.NS"},
                trading_status=TradingStatus.OPEN,
                metadata={"sector": "Technology"},
            )
        )
        for offset in range(70):
            close = Decimal("100") + (Decimal(offset) / Decimal("10"))
            store.append_bar(self._bar(close, observed_at=NOW - timedelta(days=69 - offset)))
        self._append_quote(Decimal("110"))
        self.assertIsNotNone(engine.get_instrument(self.instrument_id))

    def _bar(self, close: Decimal, *, observed_at: datetime) -> OhlcvBar:
        return OhlcvBar.create(
            instrument_id=self.instrument_id,
            provider="yahoo",
            interval=DataInterval.DAY_1,
            open=close,
            high=close + Decimal("1"),
            low=close - Decimal("1"),
            close=close,
            volume=100000,
            currency="INR",
            observed_at=observed_at,
            retrieved_at=observed_at,
            source_timezone="Asia/Kolkata",
            freshness=Freshness.FRESH,
        )

    def _append_quote(self, price: Decimal) -> None:
        store = self.service._market_store
        assert store is not None
        store.append_observation(
            PriceObservation.create(
                instrument_id=self.instrument_id,
                provider="yahoo",
                price=price,
                currency="INR",
                observed_at=self.clock.now,
                retrieved_at=self.clock.now,
                source_timezone="Asia/Kolkata",
                freshness=Freshness.FRESH,
            )
        )

    def _seed_cycle(self) -> None:
        store = self.runtime.store
        store.create_monitoring_cycle(
            {
                "cycle_id": self.cycle_id,
                "cycle_key": "monitoring:XNSE:2026-11-11:pi12-application",
                "owner_id": self.runtime.owner_id,
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
        store.update_monitoring_cycle(
            self.cycle_id,
            status="completed",
            phase="completed",
            finished=True,
            processed_count=1,
            now=self.clock.now,
        )
        store.record_monitoring_candidates(
            self.cycle_id,
            [
                {
                    "instrument_id": self.instrument_id,
                    "symbol": "ALPHA",
                    "exchange": "XNSE",
                    "disposition": "processed",
                    "reason": "pi12 application fixture",
                    "rank": 1,
                    "data_quality": "good",
                }
            ],
        )

    def _seed_forecast(self, direction: str, *, generated_at: datetime) -> str:
        upward = direction == "BUY"
        expected = 0.03 if upward else -0.03
        reference = Decimal("110")
        forecast_id = f"ens_pi12_application_{direction.lower()}"
        forecast = ForecastOutput(
            forecast_id=forecast_id,
            lifecycle_id=f"life_pi12_application_{direction.lower()}",
            lifecycle_stage=ConfirmationStage.FINAL,
            instrument_id=self.instrument_id,
            exchange="XNSE",
            currency="INR",
            reference_price=reference,
            reference_at=generated_at,
            predicted_price=reference * (Decimal("1") + Decimal(str(expected))),
            predicted_return=expected,
            direction=Direction.UP if upward else Direction.DOWN,
            lower_return=-0.01 if upward else -0.05,
            upper_return=0.05 if upward else -0.01,
            lower_price=reference * (Decimal("0.99") if upward else Decimal("0.95")),
            upper_price=reference * (Decimal("1.05") if upward else Decimal("0.99")),
            uncertainty=PredictionUncertainty.MEDIUM,
            uncertainty_width=0.06,
            p_up=0.65 if upward else 0.20,
            p_down=0.15 if upward else 0.65,
            p_neutral=0.20 if upward else 0.15,
            probability_status=ProbabilityStatus.CALIBRATED,
            calibration_status=CalibrationStatus.CALIBRATED,
            confidence=0.65,
            ensemble_id=f"ens_pi12_application_{direction.lower()}",
            model_id="ensemble",
            model_version="ens-v1",
            member_versions=("m1:v1",),
            forecast_horizon=ForecastHorizon.DAYS_3,
            target_at=generated_at + timedelta(days=3),
            generated_at=generated_at,
            data_observed_at=generated_at,
            data_received_at=generated_at,
            data_resolution=DataInterval.DAY_1,
            data_freshness=Freshness.FRESH,
            data_quality=DataQuality.VALID,
            data_snapshot_id=f"snap_pi12_application_{direction.lower()}",
            validity=ForecastValidity.CONFIRMED,
            validity_conditions=(),
            disagreement=0.10,
            digest=f"digest-pi12-application-{direction.lower()}",
            feature_set_version="fs-pi12-application",
            dataset_version="ds-pi12-application",
        )
        self.assertTrue(self.service.prediction_service().store.save_forecast(forecast))
        return forecast_id

    def _latest_decision(self, decision_type: str) -> dict[str, Any]:
        body = self.service.autonomy_prediction_decisions(
            portfolio_id=self.portfolio_id,
            instrument_id=self.instrument_id,
            decision_type=decision_type,
            limit=10,
        )
        self.assertEqual(body["total"], 1)
        return dict(body["items"][0])

    async def _submit_once_and_replay(
        self, proposal_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        first = await self.service.submit_paper_order(
            proposal_id, actor="pi12-application-test"
        )
        second = await self.service.submit_paper_order(
            proposal_id, actor="pi12-application-test"
        )
        self.assertTrue(first["ok"], first.get("error"))
        self.assertFalse(first["replayed"])
        self.assertTrue(second["ok"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["order"]["order_id"], second["order"]["order_id"])
        return first, second

    async def test_buy_sell_workflow_creates_no_duplicate_orders(self) -> None:
        buy_run = await self.service.autonomy_decision_run(
            monitoring_cycle_id=self.cycle_id,
            trigger="manual",
            actor="pi12-application-test",
        )
        self.assertEqual(buy_run["status"], "completed")
        self.assertEqual(buy_run["prediction_decisions_by_type"]["BUY"], 1)
        buy_decision = self._latest_decision("BUY")
        self.assertEqual(buy_decision["status"], "risk_approved")
        buy_order, _ = await self._submit_once_and_replay(buy_decision["proposal_id"])
        self.assertIn(buy_order["order"]["status"], {"filled", "partially_filled"})
        self.assertGreater(Decimal(buy_order["order"]["filled_quantity"]), 0)

        portfolio = self.service._portfolio_service().detail(self.portfolio_id)
        self.assertTrue(
            any(
                row.get("instrument_id") == self.instrument_id
                for row in portfolio.get("holdings", ())
            )
        )

        self.clock.now = NOW + timedelta(seconds=30)
        store = self.service._market_store
        assert store is not None
        store.append_bar(self._bar(Decimal("111"), observed_at=self.clock.now))
        self._append_quote(Decimal("111"))
        self._seed_forecast("SELL", generated_at=self.clock.now)

        sell_run = await self.service.autonomy_decision_run(
            monitoring_cycle_id=self.cycle_id,
            trigger="manual",
            actor="pi12-application-test",
        )
        self.assertEqual(sell_run["status"], "completed")
        self.assertEqual(sell_run["prediction_decisions_by_type"]["SELL"], 1)
        sell_decision = self._latest_decision("SELL")
        self.assertEqual(sell_decision["status"], "risk_approved")
        sell_order, _ = await self._submit_once_and_replay(sell_decision["proposal_id"])
        self.assertIn(sell_order["order"]["status"], {"filled", "partially_filled"})
        self.assertGreater(Decimal(sell_order["order"]["filled_quantity"]), 0)

        orders = self.service.paper_orders(portfolio_id=self.portfolio_id)
        self.assertEqual(orders["total"], 2)
        order_ids = [row["order_id"] for row in orders["orders"]]
        self.assertEqual(len(order_ids), len(set(order_ids)))
        self.assertEqual(
            {row["side"] for row in orders["orders"]}, {"buy", "sell"}
        )

    async def test_forecast_evidence_outage_takes_no_action(self) -> None:
        active = self.service.strategies(status="active")
        self.assertEqual(active["total"], 1)
        prediction_store = self.service.prediction_service().store
        with (
            mock.patch.object(
                prediction_store,
                "list_forecasts",
                side_effect=RuntimeError("prediction store unavailable"),
            ),
            self.assertRaises(DecisionStateError) as caught,
        ):
            await self.service.autonomy_decision_run(
                monitoring_cycle_id=self.cycle_id,
                trigger="manual",
                actor="pi12-application-test",
            )
        self.assertEqual(caught.exception.code, "PIPELINE_FAILED")
        self.assertIn("required forecast evidence", caught.exception.message)
        self.assertEqual(
            self.service.autonomy_decision_proposals(
                portfolio_id=self.portfolio_id
            )["total"],
            0,
        )
        self.assertEqual(
            self.service.strategy_signals(portfolio_id=self.portfolio_id)["total"], 0
        )
        self.assertEqual(self.service.paper_orders(portfolio_id=self.portfolio_id)["total"], 0)

    async def test_open_order_state_outage_takes_no_action(self) -> None:
        execution = self.service._ensure_execution()
        with (
            mock.patch.object(
                execution.store,
                "orders",
                side_effect=RuntimeError("order store unavailable"),
            ),
            self.assertRaises(DecisionStateError) as caught,
        ):
            await self.service.autonomy_decision_run(
                monitoring_cycle_id=self.cycle_id,
                trigger="manual",
                actor="pi12-application-test",
            )
        self.assertEqual(caught.exception.code, "PIPELINE_FAILED")
        self.assertIn("open-order state", caught.exception.message)
        self.assertEqual(
            self.service.autonomy_decision_proposals(
                portfolio_id=self.portfolio_id
            )["total"],
            0,
        )
        self.assertEqual(
            self.service.strategy_signals(portfolio_id=self.portfolio_id)["total"], 0
        )
        self.assertEqual(self.service.paper_orders(portfolio_id=self.portfolio_id)["total"], 0)


class ComponentTests(unittest.IsolatedAsyncioTestCase):
    def test_policy_rejects_unknown_nested_portfolio_horizons(self) -> None:
        payload = ForecastDecisionConfig().to_dict()
        payload["horizons"]["intraday"] = ["1h"]
        with self.assertRaises(ForecastDecisionConfigError):
            ForecastDecisionConfig.from_mapping(payload)
        payload = ForecastDecisionConfig().to_dict()
        payload["priority"]["intraday"] = ["1h"]
        with self.assertRaises(ForecastDecisionConfigError):
            ForecastDecisionConfig.from_mapping(payload)

    async def test_real_forecast_output_contract_keys_are_consumed(self) -> None:
        forecast = ForecastOutput(
            forecast_id="ens_pi12_contract",
            lifecycle_id="life_pi12_contract",
            lifecycle_stage=ConfirmationStage.FINAL,
            instrument_id="XNSE:ALPHA",
            exchange="XNSE",
            currency="INR",
            reference_price=Decimal("110"),
            reference_at=NOW,
            predicted_price=Decimal("113.30"),
            predicted_return=0.03,
            direction=Direction.UP,
            lower_return=-0.01,
            upper_return=0.05,
            lower_price=Decimal("108.90"),
            upper_price=Decimal("115.50"),
            uncertainty=PredictionUncertainty.MEDIUM,
            uncertainty_width=0.04,
            p_up=0.65,
            p_down=0.20,
            p_neutral=0.15,
            probability_status=ProbabilityStatus.CALIBRATED,
            calibration_status=CalibrationStatus.CALIBRATED,
            confidence=0.65,
            ensemble_id="ens_contract_1",
            model_id="ensemble",
            model_version="ens-v1",
            member_versions=("m1:v1",),
            forecast_horizon=ForecastHorizon.DAYS_3,
            target_at=NOW + timedelta(days=3),
            generated_at=NOW,
            data_observed_at=NOW,
            data_received_at=NOW,
            data_resolution=DataInterval.DAY_1,
            data_freshness=Freshness.FRESH,
            data_quality=DataQuality.VALID,
            data_snapshot_id="snap_contract",
            validity=ForecastValidity.CONFIRMED,
            validity_conditions=(),
            disagreement=0.10,
            digest="contract-digest",
            feature_set_version="fs-contract",
            dataset_version="ds-contract",
        )
        evaluator = ForecastDecisionEvaluator()
        draft = evaluator.evaluate(
            instrument_id="XNSE:ALPHA",
            exchange="XNSE",
            portfolio={
                "portfolio_id": "pfl_contract",
                "horizon": "short_term",
                "total_value": Decimal("100000"),
                "detail": portfolio_doc("pfl_contract"),
            },
            forecasts=[forecast.to_dict()],
            now=NOW,
        )
        self.assertEqual(draft.decision_type, ForecastDecisionType.BUY, draft.reasons)
        self.assertEqual(draft.horizon, "3d")
        self.assertEqual(draft.primary_forecast["forecast_id"], "ens_pi12_contract")
        self.assertEqual(draft.primary_forecast["validity"], "confirmed")
        self.assertEqual(draft.primary_forecast["generated_at"], NOW.isoformat())

    async def test_source_reader_merges_pi08_and_pi10_without_generating(self) -> None:
        class Ensemble:
            def forecasts(self, *, instrument_id: str, limit: int = 200) -> dict[str, Any]:
                return {"forecasts": [make_forecast("1h", "up", 0.01)]}

        class Outlook:
            def latest(self, instrument_id: str) -> dict[str, Any]:
                return {
                    "results": [
                        {
                            "horizon": "30d",
                            "status": "stored",
                            "forecast": make_forecast("30d", "up", 0.08, target_days=30),
                            "context": {"regime": "trend"},
                        }
                    ]
                }

        source = ForecastDecisionSource(
            Ensemble(), Outlook(), None, clock=lambda: NOW
        )
        rows = source.forecasts("XNSE:ALPHA")
        self.assertEqual({row["forecast_horizon"] for row in rows}, {"1h", "30d"})
        self.assertGreaterEqual(len(rows[1].get("pi10_context") or {}), 1)

    async def test_risk_adapter_only_maps_horizons_and_states(self) -> None:
        class Primary:
            def forecasts(self, instrument_id: str = "", limit: int = 50) -> dict[str, Any]:
                return {"forecasts": []}

            def models(
                self, instrument_id: str | None = None, horizon: str | None = None
            ) -> dict[str, Any]:
                return {"models": []}

        adapter = ForecastRiskAdapter(
            Primary(),
            lambda instrument_id: {
                "forecasts": [
                    {
                        "forecast_id": "fc_30d",
                        "instrument_id": instrument_id,
                        "horizon": "one_month",
                        "created_at": NOW.isoformat(),
                        "uncertainty": "medium",
                        "model_id": "ensemble",
                    }
                ],
                "models": [
                    {
                        "model_id": "ensemble",
                        "instrument_id": instrument_id,
                        "horizon": "one_month",
                        "state": "validated",
                    }
                ],
            },
            clock=lambda: NOW,
        )
        self.assertEqual(len(adapter.forecasts("XNSE:ALPHA")["forecasts"]), 1)
        self.assertEqual(adapter.models("XNSE:ALPHA", "one_month")["models"][0]["state"], "validated")

    async def test_no_broker_or_live_trading_imports_were_added(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "novacontrol" / "novamarket"
        paths = [
            root / "decision" / "forecast.py",
            root / "decision" / "outcomes.py",
            root / "decision" / "pipeline.py",
        ]
        forbidden = ("broker", "ccxt", "alpaca", "interactive_brokers", "live_order")
        for path in paths:
            text = path.read_text(encoding="utf-8").lower()
            imports = "\n".join(
                line
                for line in text.splitlines()
                if line.strip().startswith(("import ", "from "))
            )
            for token in forbidden:
                self.assertNotIn(token, imports, f"{path.name} imports {token!r}")
            self.assertIn("paper", text)


if __name__ == "__main__":
    unittest.main()
