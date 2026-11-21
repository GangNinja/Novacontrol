"""Focused PI-21 advanced autonomous portfolio-management tests.

The fixture records look like the existing NM-07/NM-08/NM-10/PI-12..PI-20
contracts.  They are deterministic test evidence, not market data, execution
results or performance claims.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from novacontrol.api.models import ApiSurface
from novacontrol.api.route_consumers import ROUTE_CONSUMERS
from novacontrol.novamarket.portfolio_management import (
    CycleStatus,
    ManagementSources,
    PortfolioManagementConflict,
    PortfolioManagementNotFound,
    PortfolioManagementService,
    PortfolioManagementStateError,
    PortfolioManagementValidationError,
    authority_contract,
)
from novacontrol.novamarket.contracts import LifecycleState
from novacontrol.novamarket.portfolio_management.store import PortfolioManagementStore
from novacontrol.novamarket.service import NovaMarketService

NOW = datetime.now(UTC)
PORTFOLIO = "pf-pi21"
INFY = "XNSE:INFY"
TCS = "XNSE:TCS"


def _holding(
    instrument: str = INFY,
    *,
    quantity: float = 10,
    average: float = 1000,
    price: float | None = 1200,
    value: float | None = 12000,
    weight: float = 0.12,
    sector: str = "Technology",
    pnl: float | None = 2000,
    priced_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "instrument_id": instrument,
        "quantity": str(quantity),
        "average_cost": str(average),
        "last_price": None if price is None else str(price),
        "market_value": None if value is None else str(value),
        "unrealized_pnl": None if pnl is None else str(pnl),
        "weight": str(weight),
        "sector": sector,
        "priced_at": (priced_at or NOW).isoformat(),
        "opened_at": (NOW - timedelta(days=5)).isoformat(),
    }


def _forecast(
    instrument: str = INFY,
    *,
    direction: str = "up",
    expected: float = 0.05,
    horizon: str = "5d",
    uncertainty: str = "low",
    forecast_id: str = "f1",
    model_id: str = "model-a",
    validity: str = "valid",
    generated_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "forecast_id": forecast_id,
        "instrument_id": instrument,
        "horizon": horizon,
        "generated_at": (generated_at or NOW).isoformat(),
        "validity": validity,
        "direction": direction,
        "expected_return": str(expected),
        "uncertainty": {"class": uncertainty},
        "model_id": model_id,
        "model_version": "1",
        "data_freshness": {"state": "fresh"},
    }


class FakeSources:
    def __init__(self) -> None:
        self.mode = "autonomous"
        self.kill_switch = False
        self.detail = {
            "portfolio": {
                "portfolio_id": PORTFOLIO,
                "horizon": "swing",
                "created_at": (NOW - timedelta(days=10)).isoformat(),
                "status": "active",
            },
            "valuation": {
                "total_value": "100000",
                "invested": "80000",
                "cash_weight": "0.2",
                "unrealized_pnl": "1000",
                "realized_pnl": "500",
                "total_pnl": "1500",
                "return_pct": "1.5",
            },
            "cash": "20000",
            "holdings": [
                _holding(),
                _holding(
                    TCS,
                    quantity=5,
                    average=2000,
                    price=1800,
                    value=9000,
                    weight=0.09,
                    pnl=-1000,
                ),
            ],
        }
        self.forecasts = {
            INFY: [_forecast()],
            TCS: [_forecast(TCS, direction="down", expected=-0.04, forecast_id="f2")],
        }
        self.risk = {
            "outcome": "pass",
            "policy": {"policy_id": "swing", "version": "1"},
            "restrictions": [],
            "breaches": [],
            "positions": [],
        }
        self.optimization = {
            "run_id": "opt-1",
            "status": "completed",
            "target_weights": {INFY: 0.2, TCS: 0.1},
            "allocation": [],
        }
        self.proposals = [
            {
                "proposal_id": "prop-approved",
                "portfolio_id": PORTFOLIO,
                "instrument_id": INFY,
                "side": "buy",
                "status": "risk_approved",
                "risk_outcome": "approved",
                "risk_decision_id": "risk-1",
                "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            }
        ]
        self.pending: list[dict[str, object]] = []
        self.reservations: list[dict[str, object]] = []
        self.fills: list[dict[str, object]] = []
        self.reserved = "1000"
        self.exits: list[dict[str, object]] = []
        self.policies: list[dict[str, object]] = [
            {
                "policy_id": "policy-1",
                "status": "validated",
                "horizon": "5d",
                "regime": "bull",
                "experiment": True,
            }
        ]
        self.agent = {"decision_id": "agent-1", "action": "buy", "cycle_id": "cycle-a"}
        self.regime = {"primary": "bull", "confidence": 0.65}
        self.execution_status = "filled"
        self.reconcile_status = "filled"
        self.executions: list[str] = []
        self.reconciliations: list[str] = []
        self.recoveries: list[str] = []
        self.raise_execution: Exception | None = None

    def sources(self) -> ManagementSources:
        return ManagementSources(
            portfolio_list=lambda limit: {"portfolios": [{"portfolio_id": PORTFOLIO}]},
            portfolio_detail=lambda portfolio_id: self.detail,
            pending_orders=lambda portfolio_id: {"orders": list(self.pending)},
            reservations=lambda portfolio_id: {"reservations": list(self.reservations)},
            reserved_cash=lambda portfolio_id: self.reserved,
            fills=lambda portfolio_id: {"fills": list(self.fills)},
            risk_portfolio=lambda portfolio_id: dict(self.risk),
            exit_configurations=lambda limit: list(self.exits),
            forecast_views=self._forecasts,
            instrument_evidence=self._instrument,
            agent_decisions=lambda instrument_id, limit: {"decisions": [dict(self.agent)]},
            optimization_latest=lambda portfolio_id, horizon: dict(self.optimization),
            training_policies=lambda limit: {"policies": list(self.policies)},
            at03_proposals=lambda portfolio_id: [dict(row) for row in self.proposals],
            runtime_status=self._runtime,
            execute_proposal=self._execute,
            reconcile_execution=self._reconcile,
            recover_autonomy=self._recover,
            valuation_history=lambda portfolio_id, limit: {"valuations": []},
        )

    def _forecasts(self, instrument_id: str | None, horizon: str) -> dict[str, object]:
        if instrument_id:
            return {"forecasts": list(self.forecasts.get(instrument_id, []))}
        rows = [row for values in self.forecasts.values() for row in values]
        return {"forecasts": rows}

    def _instrument(self, instrument_id: str) -> dict[str, object]:
        return {
            "instrument": {
                "instrument_id": instrument_id,
                "sector": "Technology",
                "price": "1200",
                "price_at": NOW.isoformat(),
            },
            "chart": {"rows": [{"volume": 100000}]},
            "regime": dict(self.regime),
            "research": {
                "run_id": f"research-{instrument_id}",
                "status": "completed",
                "as_of": NOW.isoformat(),
                "thesis": {
                    "stance": "bullish",
                    "positive_factors": [{"label": "stored evidence"}],
                    "negative_factors": [],
                },
            },
        }

    def _runtime(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "runtime_state": "running" if self.mode == "autonomous" else "stopped",
            "lease": {"valid": self.mode == "autonomous"},
            "kill_switch": self.kill_switch,
        }

    def _execute(self, proposal_id: str, actor: str) -> dict[str, object]:
        self.executions.append(proposal_id)
        if self.raise_execution is not None:
            raise self.raise_execution
        return {
            "ok": True,
            "proposal_id": proposal_id,
            "execution": {
                "execution_id": f"exec-{len(self.executions)}",
                "status": self.execution_status,
            },
        }

    def _reconcile(self, execution_id: str) -> dict[str, object]:
        self.reconciliations.append(execution_id)
        return {
            "ok": True,
            "execution": {
                "execution_id": execution_id,
                "status": self.reconcile_status,
                "filled_quantity": "4",
                "remaining_quantity": "1",
            },
        }

    def _recover(self, reason: str) -> dict[str, object]:
        self.recoveries.append(reason)
        self.mode = "disabled"
        return {"ok": True, "mode": "disabled", "auto_resumed": False}


def _service(sources: FakeSources, directory: str) -> PortfolioManagementService:
    return PortfolioManagementService(
        sources.sources(),
        store_path=Path(directory) / "portfolio_management.sqlite3",
        clock=lambda: NOW,
    )


def _run(service: PortfolioManagementService, **kwargs: object) -> dict[str, object]:
    return asyncio.run(
        service.run_cycle(
            portfolio_id=str(kwargs.pop("portfolio_id", PORTFOLIO)),
            horizon=str(kwargs.pop("horizon", "5d")),
            trigger=str(kwargs.pop("trigger", "manual")),
            request_id=str(kwargs.pop("request_id", "request-1")),
            actor="operator",
        )
    )


class ContractAndStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.sources = FakeSources()
        self.service = _service(self.sources, self.tmp.name)

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def test_contract_names_every_authority_and_no_mutation(self) -> None:
        contract = self.service.contract()
        self.assertEqual(contract["phase"], "PI-21")
        self.assertTrue(contract["read_only_financial"])
        authorities = contract["authorities"]
        self.assertEqual(authorities["risk_authority"], "NM-08")
        self.assertEqual(authorities["execution_authority"], "NM-10")
        self.assertFalse(authorities["order_submission"])
        self.assertFalse(authorities["financial_mutation"])
        self.assertFalse(authorities["risk_override"])
        self.assertEqual(authorities["nc_inr_rate"], "1.0")

    def test_authority_contract_blocks_direct_financial_actions(self) -> None:
        contract = authority_contract()
        for key in (
            "portfolio_mutation",
            "ledger_write",
            "order_submission",
            "risk_override",
            "policy_activation",
            "broker_connected",
            "live_trading",
        ):
            self.assertFalse(contract[key], key)

    def test_status_reports_mode_lease_and_kill_switch(self) -> None:
        status = self.service.status(operator_authorized=True, operator_configured=True)
        self.assertTrue(status["initialised"])
        self.assertEqual(status["mode"], "autonomous")
        self.assertTrue(status["lease_valid"])
        self.assertFalse(status["kill_switch"])
        self.assertTrue(status["operator_authorized"])
        self.assertEqual(status["store"]["financial_tables"], [])
        self.assertTrue(status["store"]["read_only_financial"])

    def test_navigation_has_all_required_views_and_no_financial_controls(self) -> None:
        navigation = self.service.navigation()
        ids = {row["id"] for row in navigation["views"]}
        self.assertEqual(
            ids,
            {
                "overview",
                "positions",
                "opportunities",
                "capital",
                "rebalancing",
                "decisions",
                "autonomy",
            },
        )
        self.assertEqual(navigation["direct_financial_controls"], [])

    def test_config_validation_rejects_unknown_and_unsafe_values(self) -> None:
        with self.assertRaises(PortfolioManagementValidationError):
            self.service.update_config({"not_a_setting": 1})
        with self.assertRaises(PortfolioManagementValidationError):
            self.service.update_config({"cash_reserve_target": "1.2"})
        updated = self.service.update_config(
            {"cash_reserve_target": "0.15", "max_position_weight": "0.18"}, actor="operator"
        )
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["config"]["cash_reserve_target"], "0.15")
        history = self.service.config()["history"]
        self.assertEqual(len(history), 2)

    def test_case_insensitive_horizon_and_unknown_horizon_refusal(self) -> None:
        cycle = _run(self.service, horizon="60m")
        self.assertEqual(cycle["horizon"], "60m")
        self.assertEqual(cycle["horizon_family"], "intraday")
        with self.assertRaises(PortfolioManagementValidationError):
            _run(self.service, horizon="7d", request_id="bad-horizon")


class SnapshotAndPositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.sources = FakeSources()
        self.service = _service(self.sources, self.tmp.name)

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def test_authoritative_snapshot_preserves_nc_inr_and_provenance(self) -> None:
        cycle = _run(self.service)
        snapshot = cycle["snapshot"]
        self.assertEqual(float(snapshot["valuation"]["total_value"]), 100000.0)
        for row in snapshot["positions"]:
            self.assertEqual(row["currency"], "INR")
            self.assertTrue(row["nova_coin"])
        self.assertEqual(snapshot["data_versions"]["market_prices"][0]["instrument_id"], INFY)

    def test_position_assessment_uses_forward_evidence(self) -> None:
        cycle = _run(self.service)
        infy = next(row for row in cycle["positions"] if row["instrument_id"] == INFY)
        tcs = next(row for row in cycle["positions"] if row["instrument_id"] == TCS)
        self.assertIn(infy["assessment"], {"strong_position", "hold_position"})
        self.assertIn(infy["action"], {"hold", "add"})
        self.assertTrue(infy["supporting_evidence"])
        self.assertIn(tcs["assessment"], {"reduce_position", "exit_candidate", "review_position"})
        self.assertTrue(tcs["contradictory_evidence"])
        self.assertEqual(infy["score_kind"], "evidence_score_not_probability")

    def test_position_assessment_is_not_pnl_only(self) -> None:
        self.sources.forecasts[TCS] = [
            _forecast(TCS, direction="up", expected=0.06, forecast_id="f2")
        ]
        self.sources.agent = {"decision_id": "agent-1", "action": "buy"}
        self.sources.detail["holdings"][1]["unrealized_pnl"] = "-1000"
        cycle = _run(self.service)
        tcs = next(row for row in cycle["positions"] if row["instrument_id"] == TCS)
        self.assertNotIn(tcs["assessment"], {"exit_candidate"})
        self.assertTrue(tcs["supporting_evidence"])

    def test_missing_price_is_not_zero_and_waits_for_evidence(self) -> None:
        self.sources.detail["holdings"][0]["last_price"] = None
        self.sources.detail["holdings"][0]["market_value"] = None
        self.sources.detail["holdings"][0]["priced_at"] = None
        cycle = _run(self.service)
        infy = next(row for row in cycle["positions"] if row["instrument_id"] == INFY)
        self.assertTrue(infy["missing_price"])
        self.assertTrue(infy["stale"])
        self.assertEqual(cycle["data_status"], "stale")
        self.assertEqual(cycle["status"], CycleStatus.WAITING_FOR_EVIDENCE.value)
        self.assertIn("missing_price", cycle["warnings"])

    def test_stale_valuation_is_detected(self) -> None:
        self.sources.detail["holdings"][0]["priced_at"] = (NOW - timedelta(days=2)).isoformat()
        cycle = _run(self.service)
        infy = next(row for row in cycle["positions"] if row["instrument_id"] == INFY)
        self.assertTrue(infy["stale"])
        self.assertIn("stale_valuation", cycle["warnings"])

    def test_conflicting_forecasts_preserve_uncertainty(self) -> None:
        self.sources.forecasts[INFY] = [
            _forecast(INFY, direction="up", expected=0.05, forecast_id="a", model_id="a"),
            _forecast(INFY, direction="down", expected=-0.03, forecast_id="b", model_id="b"),
        ]
        cycle = _run(self.service)
        infy = next(row for row in cycle["positions"] if row["instrument_id"] == INFY)
        self.assertEqual(infy["forecast"]["direction"], "uncertain")
        self.assertEqual(infy["forecast"]["agreement"], 0.0)
        self.assertTrue(any("disagree" in item for item in infy["contradictory_evidence"]))

    def test_stale_forecast_is_rejected_not_averaged(self) -> None:
        self.sources.forecasts[INFY] = [
            _forecast(
                INFY,
                generated_at=NOW - timedelta(days=30),
                forecast_id="old",
            )
        ]
        cycle = _run(self.service)
        infy = next(row for row in cycle["positions"] if row["instrument_id"] == INFY)
        self.assertFalse(infy["forecast"]["available"])
        self.assertEqual(infy["forecast"]["rejected"][0]["forecast_id"], "old")

    def test_at05_protective_configuration_downgrades_exit_recommendation(self) -> None:
        self.sources.exits = [
            {
                "portfolio_id": PORTFOLIO,
                "instrument_id": TCS,
                "stop_loss_price": "1500",
                "trailing_stop_percent": "5",
                "target_price": "2400",
            }
        ]
        cycle = _run(self.service)
        tcs = next(row for row in cycle["positions"] if row["instrument_id"] == TCS)
        self.assertIn(tcs["assessment"], {"review_position", "hold_position"})
        self.assertIn(tcs["exit_recommendation"], {"wait", "review_required"})
        self.assertTrue(any("AT-05" in item for item in tcs["contradictory_evidence"]))

    def test_many_positions_are_bounded_and_not_truncated(self) -> None:
        self.sources.detail["holdings"] = [
            _holding(f"XNSE:M{index:02d}", weight=0.01, value=1000) for index in range(25)
        ]
        self.sources.detail["valuation"]["total_value"] = "100000"
        cycle = _run(self.service)
        self.assertEqual(len(cycle["positions"]), 25)
        self.assertLessEqual(cycle["rebalance"]["change_count"], 10)
        self.assertGreaterEqual(len(cycle["decisions"]), 25)

    def test_market_regime_transition_is_recorded_without_forcing_a_trade(self) -> None:
        self.sources.regime = {"primary": "bear", "confidence": 0.81}
        self.sources.forecasts[INFY] = [
            _forecast(INFY, direction="down", expected=-0.03, forecast_id="regime-down")
        ]
        cycle = _run(self.service)
        infy = next(row for row in cycle["positions"] if row["instrument_id"] == INFY)
        self.assertEqual(infy["regime"]["primary"], "bear")
        self.assertIn(infy["assessment"], {"review_position", "reduce_position", "exit_candidate"})
        self.assertFalse(cycle["financial_mutation"])
        self.assertFalse(cycle["order_submission"])

    def test_portfolio_with_no_positions_does_not_invent_positions(self) -> None:
        self.sources.detail["holdings"] = []
        self.sources.detail["valuation"]["total_value"] = "20000"
        self.sources.forecasts = {}
        cycle = _run(self.service)
        self.assertEqual(cycle["positions"], [])
        self.assertEqual(cycle["decisions"][0]["action"], "retain_cash")


class OpportunityCapitalCashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.sources = FakeSources()
        self.service = _service(self.sources, self.tmp.name)

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def test_opportunity_requires_valid_forecast(self) -> None:
        cycle = _run(self.service)
        self.assertTrue(cycle["opportunities"])
        for row in cycle["opportunities"]:
            self.assertIn(row["forecast_direction"], {"up", "down", "neutral", "uncertain"})
            self.assertTrue(row["forecast_versions"])
            self.assertEqual(row["score_kind"], "evidence_score_not_probability")

    def test_no_forecasts_means_no_opportunities(self) -> None:
        self.sources.forecasts = {}
        cycle = _run(self.service)
        self.assertEqual(cycle["opportunities"], [])
        self.assertIn(
            cycle["capital"]["recommendation"],
            {"retain_cash", "no_deployment", "wait_for_confirmation"},
        )

    def test_insufficient_forecast_is_excluded(self) -> None:
        self.sources.forecasts[INFY] = [_forecast(INFY, validity="expired", forecast_id="expired")]
        cycle = _run(self.service)
        self.assertEqual(cycle["opportunities"], [])

    def test_capital_deployment_never_exceeds_available(self) -> None:
        cycle = _run(self.service)
        capital = cycle["capital"]
        self.assertGreaterEqual(capital["available_nc"], 0)
        self.assertGreaterEqual(capital["proposed_deployment_nc"], 0)
        self.assertLessEqual(
            capital["proposed_deployment_nc"],
            capital["available_nc"],
        )
        self.assertFalse(capital["cash_mutation"])
        self.assertFalse(capital["unused_cash_is_failure"])

    def test_no_cash_retains_cash_without_creating_money(self) -> None:
        self.sources.detail["cash"] = "0"
        self.sources.reserved = "0"
        cycle = _run(self.service)
        capital = cycle["capital"]
        cash = cycle["cash"]
        self.assertEqual(capital["recommendation"], "no_deployment")
        self.assertIn(cash["recommendation"], {"preserve_reserves", "retain_cash"})
        self.assertFalse(cash["cash_created"])
        self.assertFalse(cash["balance_modified"])

    def test_unavailable_cash_is_not_treated_as_zero(self) -> None:
        del self.sources.detail["cash"]
        self.sources.detail["valuation"]["cash"] = None
        cycle = _run(self.service)
        self.assertIsNone(cycle["capital"]["available_nc"])
        self.assertEqual(cycle["capital"]["recommendation"], "wait_for_confirmation")
        self.assertIn("available_cash_unavailable", cycle["warnings"])

    def test_reserved_capital_reduces_deployable_cash(self) -> None:
        self.sources.reserved = "19000"
        cycle = _run(self.service)
        capital = cycle["capital"]
        self.assertEqual(capital["reserved_nc"], 19000)
        self.assertLessEqual(capital["deployable_nc"], 1000)

    def test_cash_reserve_target_is_respected(self) -> None:
        self.service.update_config({"cash_reserve_target": "0.5"}, actor="operator")
        cycle = _run(self.service, request_id="reserve")
        capital = cycle["capital"]
        self.assertEqual(capital["reserve_target_fraction"], 0.5)
        self.assertEqual(capital["reserve_target_nc"], 50000)

    def test_pending_buy_commitments_reduce_available_and_deployable(self) -> None:
        self.sources.reserved = "0"
        self.sources.pending = [
            {
                "order_id": "order-1",
                "status": "open",
                "side": "buy",
                "remaining_quantity": "2",
                "reference_price": "1200",
                "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            }
        ]
        cycle = _run(self.service)
        self.assertEqual(cycle["capital"]["pending_commitments_nc"], 2400)
        self.assertEqual(cycle["capital"]["available_nc"], 17600)
        self.assertEqual(cycle["capital"]["deployable_nc"], 7600)
        self.assertLessEqual(
            cycle["capital"]["proposed_deployment_nc"],
            cycle["capital"]["deployable_nc"],
        )

    def test_multiple_buy_decisions_share_one_cash_budget(self) -> None:
        self.sources.forecasts = {
            f"XNSE:N{index}": [_forecast(f"XNSE:N{index}", expected=0.08)] for index in range(4)
        }
        cycle = _run(self.service)
        buys = [
            row
            for row in cycle["decisions"]
            if row["action"] in {"open", "add"} and row["side"] == "buy"
        ]
        self.assertTrue(buys)
        self.assertLessEqual(
            sum(float(row["notional_nc"]) for row in buys),
            float(cycle["capital"]["deployable_nc"]) + 0.01,
        )
        self.assertTrue(any(row["action"] == "wait" for row in cycle["decisions"]))

    def test_unconfirmed_forecast_does_not_create_an_entry(self) -> None:
        self.sources.forecasts = {
            "XNSE:NEW": [_forecast("XNSE:NEW", expected=0.08, validity="unconfirmed")]
        }
        cycle = _run(self.service)
        self.assertFalse(any(row["action"] in {"open", "add"} for row in cycle["decisions"]))
        self.assertFalse(any(row["instrument_id"] == "XNSE:NEW" for row in cycle["opportunities"]))

    def test_max_positions_blocks_new_entries_but_not_existing_reviews(self) -> None:
        self.service.update_config({"max_positions": 2}, actor="operator")
        self.sources.forecasts = {
            "XNSE:NEW1": [_forecast("XNSE:NEW1", expected=0.08)],
            "XNSE:NEW2": [_forecast("XNSE:NEW2", expected=0.08)],
        }
        cycle = _run(self.service)
        new_rows = [row for row in cycle["opportunities"] if not row["existing_position"]]
        self.assertTrue(new_rows)
        self.assertTrue(all(not row["eligible"] for row in new_rows))
        self.assertTrue(all(row["reason"] == "max_positions_limit" for row in new_rows))
        self.assertEqual(len(cycle["positions"]), 2)

    def test_missing_total_valuation_waits_and_cannot_handoff(self) -> None:
        self.sources.detail["valuation"]["total_value"] = None
        cycle = _run(self.service)
        self.assertTrue(cycle["snapshot"]["missing_valuation"])
        self.assertEqual(cycle["data_status"], "unavailable")
        self.assertEqual(cycle["status"], "waiting_for_evidence")
        self.assertIn("valuation_unavailable", cycle["warnings"])
        with self.assertRaises(PortfolioManagementStateError) as raised:
            asyncio.run(self.service.handoff_cycle(cycle["cycle_id"], actor="operator"))
        self.assertEqual(raised.exception.code, "CYCLE_NOT_HANDOFFABLE")
        self.assertEqual(self.sources.executions, [])

    def test_pending_orders_and_reservations_are_observed(self) -> None:
        self.sources.pending = [
            {
                "order_id": "order-1",
                "status": "open",
                "side": "buy",
                "remaining_quantity": "2",
                "reference_price": "1200",
                "expires_at": (NOW - timedelta(minutes=1)).isoformat(),
            }
        ]
        self.sources.reservations = [
            {
                "reservation_id": "res-1",
                "order_id": "missing-order",
                "status": "active",
                "kind": "cash",
                "remaining": "100",
            }
        ]
        cycle = _run(self.service)
        self.assertEqual(cycle["snapshot"]["pending_orders"][0]["order_id"], "order-1")
        codes = {row["code"] for row in cycle["snapshot"]["anomalies"]}
        self.assertIn("expired_pending_order", codes)
        self.assertIn("reservation_without_active_order", codes)

    def test_exposure_concentration_warning_is_research_only(self) -> None:
        self.sources.detail["holdings"][0]["market_value"] = "45000"
        self.sources.detail["holdings"][0]["weight"] = "0.45"
        self.sources.detail["holdings"][1]["market_value"] = "30000"
        self.sources.detail["holdings"][1]["weight"] = "0.30"
        cycle = _run(self.service)
        exposure = cycle["exposure"]
        codes = {row["code"] for row in exposure["warnings"]}
        self.assertIn("excessive_instrument_concentration", codes)
        self.assertIn("sector_imbalance", codes)
        self.assertTrue(exposure["research_thresholds_override_risk"] is False)
        self.assertEqual(exposure["risk_authority"], "NM-08")

    def test_high_sector_weight_makes_opportunity_ineligible(self) -> None:
        self.sources.detail["holdings"] = [
            _holding(INFY, value=40000, weight=0.40),
            _holding(TCS, quantity=1, price=1000, value=1000, weight=0.01),
        ]
        self.sources.forecasts = {"XNSE:NEW": [_forecast("XNSE:NEW", expected=0.08)]}
        cycle = _run(self.service)
        new = next(row for row in cycle["opportunities"] if row["instrument_id"] == "XNSE:NEW")
        self.assertFalse(new["eligible"])
        self.assertEqual(new["reason"], "sector_concentration_limit")

    def test_horizon_specific_opportunity_does_not_leak(self) -> None:
        self.sources.forecasts = {"XNSE:NEW": [_forecast("XNSE:NEW", horizon="5d", expected=0.08)]}
        cycle = _run(self.service, horizon="1d", request_id="1d")
        self.assertEqual(cycle["opportunities"], [])


class RebalancingAndDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.sources = FakeSources()
        self.service = _service(self.sources, self.tmp.name)

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def test_rebalancing_is_bounded_and_never_executes(self) -> None:
        cycle = _run(self.service)
        rebalance = cycle["rebalance"]
        self.assertLessEqual(rebalance["change_count"], 10)
        self.assertFalse(rebalance["executes_orders"])
        self.assertFalse(rebalance["financial_mutation"])
        self.assertTrue(rebalance["requires_pi12"])
        self.assertTrue(rebalance["requires_nm08"])

    def test_turnover_limit_suppresses_changes(self) -> None:
        self.service.update_config({"max_turnover": "0.0001"}, actor="operator")
        cycle = _run(self.service, request_id="turnover")
        self.assertEqual(cycle["rebalance"]["changes"], [])
        self.assertIn("turnover", cycle["rebalance"]["reason"])

    def test_cooldown_suppresses_non_urgent_rebalancing(self) -> None:
        self.sources.detail["portfolio"]["last_rebalanced_at"] = (
            NOW - timedelta(hours=1)
        ).isoformat()
        self.sources.forecasts[TCS] = [
            _forecast(TCS, direction="up", expected=0.05, forecast_id="f2")
        ]
        cycle = _run(self.service)
        self.assertEqual(cycle["rebalance"]["changes"], [])
        self.assertIn("cooldown", cycle["rebalance"]["reason"])

    def test_decision_has_explainability_and_impact_fields(self) -> None:
        cycle = _run(self.service)
        decision = next(row for row in cycle["decisions"] if row["instrument_id"] == INFY)
        for key in (
            "decision_id",
            "cycle_id",
            "portfolio_id",
            "instrument_id",
            "action",
            "supporting_evidence",
            "contradictory_evidence",
            "portfolio_impact",
            "capital_impact",
            "forecast_uncertainty",
            "risk_engine_reference",
            "decision_timestamp",
            "configuration_version",
        ):
            self.assertIn(key, decision)
        self.assertTrue(decision["explanation"])
        self.assertFalse(decision["financial_mutation"])
        self.assertFalse(decision["order_submission"])

    def test_retain_cash_is_a_valid_portfolio_decision(self) -> None:
        self.sources.forecasts = {}
        self.sources.detail["holdings"] = []
        cycle = _run(self.service)
        actions = {row["action"] for row in cycle["decisions"]}
        self.assertIn("retain_cash", actions)

    def test_rl_policy_remains_advisory(self) -> None:
        cycle = _run(self.service)
        policy = cycle["positions"][0]["policy_advisory"]
        self.assertTrue(policy["experimental"])
        self.assertTrue(policy["advisory_only"])
        self.assertFalse(policy["activation_available"])
        self.assertFalse(policy["production_authority"])
        self.assertNotIn("activate", self.service.contract()["workflow"])

    def test_analytics_separates_outcomes_from_pnl(self) -> None:
        cycle = _run(self.service)
        analytics = cycle["analytics"]
        self.assertIsNone(analytics["recommendation_accuracy"]["value"])
        self.assertEqual(analytics["decision_quality"]["status"], "not_inferred_from_pnl")
        self.assertTrue(analytics["profitability_is_not_decision_quality"])
        self.assertIsNotNone(analytics["realized_pnl_nc"])

    def test_multi_agent_evidence_is_preserved(self) -> None:
        self.sources.agent = {
            "decision_id": "agent-1",
            "cycle_id": "cycle-a",
            "action": "buy",
            "supporting_agents": ["market_research"],
            "contradicting_agents": ["risk_analysis"],
            "unresolved_conflicts": ["regime disagreement"],
            "explanation": "stored decision",
        }
        cycle = _run(self.service)
        infy = next(row for row in cycle["positions"] if row["instrument_id"] == INFY)
        self.assertEqual(infy["agent"]["supporting_agents"], ["market_research"])
        self.assertEqual(infy["agent"]["contradicting_agents"], ["risk_analysis"])
        self.assertEqual(infy["agent"]["unresolved_conflicts"], ["regime disagreement"])
        self.assertTrue(infy["agent"]["advisory_only"])


class LifecycleAndHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.sources = FakeSources()
        self.service = _service(self.sources, self.tmp.name)

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def test_cycle_is_persisted_and_queryable(self) -> None:
        cycle = _run(self.service)
        loaded = self.service.cycle(cycle["cycle_id"])
        self.assertEqual(loaded["cycle_id"], cycle["cycle_id"])
        self.assertTrue(loaded["events"])
        self.assertEqual(self.service.cycles()["total"], 1)
        self.assertEqual(self.service.positions(PORTFOLIO)["total"], 2)
        self.assertTrue(self.service.opportunities(PORTFOLIO)["opportunities"])
        self.assertEqual(self.service.capital(PORTFOLIO)["portfolio_id"], PORTFOLIO)
        self.assertEqual(self.service.cash(PORTFOLIO)["portfolio_id"], PORTFOLIO)
        self.assertEqual(self.service.exposure(PORTFOLIO)["portfolio_id"], PORTFOLIO)
        self.assertEqual(self.service.rebalance(PORTFOLIO)["portfolio_id"], PORTFOLIO)
        self.assertEqual(self.service.analytics(PORTFOLIO)["portfolio_id"], PORTFOLIO)

    def test_unknown_cycle_is_404_typed(self) -> None:
        with self.assertRaises(PortfolioManagementNotFound):
            self.service.cycle("missing")

    def test_idempotent_review_replays_one_cycle(self) -> None:
        first = _run(self.service, request_id="same")
        second = _run(self.service, request_id="same")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["cycle_id"], second["cycle_id"])
        self.assertEqual(self.service.cycles()["total"], 1)

    def test_manual_review_is_allowed_while_disabled(self) -> None:
        self.sources.mode = "disabled"
        cycle = _run(self.service)
        self.assertEqual(cycle["status"], CycleStatus.MONITORING_ONLY.value)
        self.assertFalse(cycle["order_submission"])

    def test_scheduled_review_refused_while_disabled_or_paused(self) -> None:
        for mode in ("disabled", "paused"):
            self.sources.mode = mode
            with self.assertRaises(PortfolioManagementStateError):
                _run(self.service, trigger="scheduled", request_id=f"scheduled-{mode}")

    def test_scheduled_review_runs_only_in_autonomous_mode(self) -> None:
        cycle = _run(self.service, trigger="scheduled", request_id="scheduled-ok")
        self.assertEqual(cycle["trigger"], "scheduled")
        self.assertEqual(cycle["status"], "completed")
        self.assertFalse(cycle["order_submission"])

    def test_kill_switch_blocks_scheduled_review(self) -> None:
        self.sources.kill_switch = True
        with self.assertRaises(PortfolioManagementStateError):
            _run(self.service, trigger="scheduled", request_id="kill-scheduled")

    def test_one_active_cycle_is_enforced_by_the_store(self) -> None:
        store = self.service.store
        payload = {"phase": "PI-21"}
        store.claim_cycle(
            cycle_id="cycle-a",
            idempotency_key="a",
            portfolio_id=PORTFOLIO,
            trigger="manual",
            config_version="1",
            snapshot_version="s1",
            payload=payload,
            started_at=NOW,
        )
        with self.assertRaises(PortfolioManagementConflict):
            store.claim_cycle(
                cycle_id="cycle-b",
                idempotency_key="b",
                portfolio_id=PORTFOLIO,
                trigger="manual",
                config_version="1",
                snapshot_version="s1",
                payload=payload,
                started_at=NOW + timedelta(seconds=1),
            )

    def test_handoff_requires_autonomous_mode(self) -> None:
        cycle = _run(self.service)
        self.sources.mode = "monitoring"
        with self.assertRaises(PortfolioManagementStateError):
            asyncio.run(self.service.handoff_cycle(cycle["cycle_id"], actor="operator"))
        self.assertEqual(self.sources.executions, [])

    def test_kill_switch_blocks_handoff(self) -> None:
        cycle = _run(self.service)
        self.sources.kill_switch = True
        with self.assertRaises(PortfolioManagementStateError):
            asyncio.run(self.service.handoff_cycle(cycle["cycle_id"], actor="operator"))
        self.assertEqual(self.sources.executions, [])

    def test_handoff_only_forwards_matching_risk_approved_proposal(self) -> None:
        cycle = _run(self.service)
        result = asyncio.run(self.service.handoff_cycle(cycle["cycle_id"], actor="operator"))
        self.assertEqual(result["handoff"]["status"], "executed")
        self.assertEqual(self.sources.executions, ["prop-approved"])
        self.assertEqual(result["handoff"]["executions"][0]["proposal_id"], "prop-approved")

    def test_monitoring_cycle_cannot_handoff_after_runtime_switch(self) -> None:
        self.sources.mode = "monitoring"
        cycle = _run(self.service)
        self.assertEqual(cycle["status"], "monitoring_only")
        self.sources.mode = "autonomous"
        with self.assertRaises(PortfolioManagementStateError) as raised:
            asyncio.run(self.service.handoff_cycle(cycle["cycle_id"], actor="operator"))
        self.assertEqual(raised.exception.code, "CYCLE_NOT_HANDOFFABLE")
        self.assertEqual(self.sources.executions, [])

    def test_handoff_requires_proposal_state_reread(self) -> None:
        cycle = _run(self.service)

        def fail(portfolio_id: str) -> list[dict[str, object]]:
            raise RuntimeError("risk store unavailable")

        self.service._sources.at03_proposals = fail
        with self.assertRaises(PortfolioManagementStateError) as raised:
            asyncio.run(self.service.handoff_cycle(cycle["cycle_id"], actor="operator"))
        self.assertEqual(raised.exception.code, "RISK_PROPOSAL_READ_FAILED")
        self.assertEqual(self.sources.executions, [])

    def test_handoff_revalidates_proposal_before_submission(self) -> None:
        cycle = _run(self.service)
        self.sources.proposals[0]["status"] = "risk_rejected"
        self.sources.proposals[0]["risk_outcome"] = "rejected"
        result = asyncio.run(self.service.handoff_cycle(cycle["cycle_id"], actor="operator"))
        self.assertEqual(result["handoff"]["status"], "failed")
        self.assertFalse(result["handoff"]["order_submission"])
        self.assertEqual(self.sources.executions, [])

    def test_handoff_without_risk_approved_proposal_submits_nothing(self) -> None:
        self.sources.proposals = [
            {
                "proposal_id": "prop-rejected",
                "portfolio_id": PORTFOLIO,
                "instrument_id": INFY,
                "side": "buy",
                "status": "risk_rejected",
                "risk_outcome": "rejected",
            }
        ]
        cycle = _run(self.service)
        result = asyncio.run(self.service.handoff_cycle(cycle["cycle_id"], actor="operator"))
        self.assertEqual(result["handoff"]["status"], "waiting_for_risk_assessment")
        self.assertEqual(self.sources.executions, [])
        self.assertFalse(result["handoff"]["order_submission"])

    def test_duplicate_handoff_does_not_repeat_an_order(self) -> None:
        cycle = _run(self.service)
        first = asyncio.run(self.service.handoff_cycle(cycle["cycle_id"], actor="operator"))
        second = asyncio.run(self.service.handoff_cycle(cycle["cycle_id"], actor="operator"))
        self.assertTrue(second["replayed"])
        self.assertEqual(first["handoff"]["executions"], second["handoff"]["executions"])
        self.assertEqual(self.sources.executions, ["prop-approved"])

    def test_partial_fill_is_not_treated_as_filled(self) -> None:
        self.sources.execution_status = "partially_filled"
        self.sources.reconcile_status = "partially_filled"
        cycle = _run(self.service)
        result = asyncio.run(self.service.handoff_cycle(cycle["cycle_id"], actor="operator"))
        self.assertEqual(result["handoff"]["status"], "partially_executed")
        self.assertEqual(result["handoff"]["executions"][0]["status"], "partially_filled")

    def test_pending_order_is_not_assumed_filled(self) -> None:
        self.sources.execution_status = "submitted"
        self.sources.reconcile_status = "submitted"
        cycle = _run(self.service)
        result = asyncio.run(self.service.handoff_cycle(cycle["cycle_id"], actor="operator"))
        self.assertEqual(result["handoff"]["status"], "order_pending")

    def test_unknown_execution_requires_reconciliation(self) -> None:
        self.sources.execution_status = "reconciliation_required"
        cycle = _run(self.service)
        result = asyncio.run(self.service.handoff_cycle(cycle["cycle_id"], actor="operator"))
        self.assertEqual(result["handoff"]["status"], "reconciliation_required")

    def test_reconcile_uses_nm10_and_updates_outcome(self) -> None:
        self.sources.execution_status = "submitted"
        self.sources.reconcile_status = "filled"
        cycle = _run(self.service)
        asyncio.run(self.service.handoff_cycle(cycle["cycle_id"], actor="operator"))
        result = asyncio.run(self.service.reconcile_cycle(cycle["cycle_id"], actor="operator"))
        self.assertEqual(result["handoff"]["status"], "executed")
        self.assertEqual(self.sources.reconciliations, ["exec-1"])
        self.assertEqual(result["handoff"]["executions"][0]["filled_quantity"], "4")
        self.assertEqual(result["handoff"]["reconciliation_authority"], "NM-10")

    def test_execution_refusal_is_recorded_without_fabricated_fill(self) -> None:
        self.sources.raise_execution = RuntimeError("NM-10 unavailable")
        cycle = _run(self.service)
        result = asyncio.run(self.service.handoff_cycle(cycle["cycle_id"], actor="operator"))
        self.assertEqual(result["handoff"]["status"], "failed")
        self.assertIn("NM-10 unavailable", result["handoff"]["executions"][0]["message"])
        self.assertEqual(result["handoff"]["executions"][0]["status"], "failed")
        self.assertFalse(result["handoff"]["order_submission"])

    def test_recovery_reports_autonomy_failure_without_losing_local_recovery(self) -> None:
        self.service.store.claim_cycle(
            cycle_id="interrupted",
            idempotency_key="interrupted",
            portfolio_id=PORTFOLIO,
            trigger="manual",
            config_version="1",
            snapshot_version="s1",
            payload={"phase": "PI-21"},
            started_at=NOW - timedelta(hours=2),
        )

        def fail(reason: str) -> dict[str, object]:
            raise RuntimeError("autonomy unavailable")

        self.service._sources.recover_autonomy = fail
        result = asyncio.run(self.service.recover(reason="restart"))
        self.assertFalse(result["ok"])
        self.assertFalse(result["recovery_complete"])
        self.assertFalse(result["mode_verified"])
        self.assertEqual(result["management_cycles_interrupted"], 1)
        self.assertIn("autonomy_recovery", result["errors"])
        self.assertEqual(self.service.cycle("interrupted")["status"], "interrupted")

    def test_recovery_interrupts_local_cycles_and_returns_to_disabled(self) -> None:
        store = self.service.store
        store.claim_cycle(
            cycle_id="interrupted",
            idempotency_key="interrupted",
            portfolio_id=PORTFOLIO,
            trigger="manual",
            config_version="1",
            snapshot_version="s1",
            payload={"phase": "PI-21"},
            started_at=NOW - timedelta(hours=2),
        )
        result = asyncio.run(self.service.recover(reason="restart"))
        self.assertEqual(result["management_cycles_interrupted"], 1)
        self.assertEqual(result["mode"], "disabled")
        self.assertFalse(result["auto_resumed"])
        self.assertTrue(self.sources.recoveries)
        self.assertEqual(self.service.cycle("interrupted")["status"], "interrupted")


class PersistenceAndIsolationTests(unittest.TestCase):
    def test_store_has_no_financial_tables_and_is_separate(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio_management.sqlite3"
            store = PortfolioManagementStore(path)
            status = store.status()
            names = {
                row[0]
                for row in store._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            store.close()
            self.assertEqual(status["financial_tables"], [])
            self.assertTrue(
                all(name.startswith("pm_") or name == "sqlite_sequence" for name in names),
                names,
            )
            self.assertNotIn("financial_tables", {name for name in names if "order" in name})

    def test_config_history_is_immutable(self) -> None:
        with TemporaryDirectory() as directory:
            sources = FakeSources()
            service = _service(sources, directory)
            service.update_config({"cash_reserve_target": "0.2"}, actor="operator")
            service.update_config({"cash_reserve_target": "0.25"}, actor="operator")
            history = service.config()["history"]
            self.assertEqual([row["version"] for row in history], [3, 2, 1])
            service.close()

    def test_store_is_bounded_and_rejects_unknown_records(self) -> None:
        with TemporaryDirectory() as directory:
            sources = FakeSources()
            service = _service(sources, directory)
            try:
                _run(service)
                self.assertLessEqual(len(service.cycles(limit=1000)["items"]), 100)
                with self.assertRaises(PortfolioManagementNotFound):
                    service.snapshot("missing-portfolio")
            finally:
                service.close()


class ApiSurfaceTests(unittest.TestCase):
    def test_every_pi21_route_is_registered_and_declared_no_render(self) -> None:
        surface = ApiSurface.default()
        routes = {(row.method, row.path) for row in surface.routes}
        expected = {
            ("GET", "/market/portfolio-management/status"),
            ("GET", "/market/portfolio-management/contracts"),
            ("GET", "/market/portfolio-management/navigation"),
            ("GET", "/market/portfolio-management/config"),
            ("PUT", "/market/portfolio-management/config"),
            ("POST", "/market/portfolio-management/review"),
            ("GET", "/market/portfolio-management/cycles"),
            ("GET", "/market/portfolio-management/cycles/{cycle_id}"),
            ("GET", "/market/portfolio-management/cycles/{cycle_id}/events"),
            ("POST", "/market/portfolio-management/cycles/{cycle_id}/handoff"),
            ("POST", "/market/portfolio-management/cycles/{cycle_id}/reconcile"),
            ("POST", "/market/portfolio-management/recover"),
            ("GET", "/market/portfolio-management/portfolios/{portfolio_id}/snapshot"),
            ("GET", "/market/portfolio-management/portfolios/{portfolio_id}/positions"),
            ("GET", "/market/portfolio-management/portfolios/{portfolio_id}/opportunities"),
            ("GET", "/market/portfolio-management/portfolios/{portfolio_id}/decisions"),
            ("GET", "/market/portfolio-management/portfolios/{portfolio_id}/capital"),
            ("GET", "/market/portfolio-management/portfolios/{portfolio_id}/cash"),
            ("GET", "/market/portfolio-management/portfolios/{portfolio_id}/exposure"),
            ("GET", "/market/portfolio-management/portfolios/{portfolio_id}/rebalancing"),
            ("GET", "/market/portfolio-management/portfolios/{portfolio_id}/analytics"),
        }
        self.assertEqual(expected - routes, set())
        for method, path in expected:
            self.assertEqual(ROUTE_CONSUMERS[(method, path)], "no-render")

    def test_no_pi21_route_is_a_direct_financial_mutation(self) -> None:
        surface = ApiSurface.default()
        pi21 = [
            row for row in surface.routes if row.path.startswith("/market/portfolio-management")
        ]
        self.assertTrue(pi21)
        for row in pi21:
            self.assertNotIn("execute", row.path)
            self.assertNotIn("order", row.path)
            self.assertNotIn("fill", row.path)


class ApplicationAndApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
        except Exception as exc:  # pragma: no cover - optional dev dependency
            raise unittest.SkipTest(f"fastapi.testclient requires httpx: {exc}") from exc
        cls._tmp = TemporaryDirectory()
        cls._env = mock.patch.dict(
            os.environ,
            {
                "NOVACONTROL_API_TOKEN": "pi21-test-token",
                "NOVACONTROL_AUTONOMY_OPERATOR_TOKEN": "pi21-operator-token",
            },
            clear=False,
        )
        cls._env.start()
        from novacontrol.api.app import create_app

        cls._client = TestClient(create_app(data_dir=cls._tmp.name))
        cls._client.__enter__()
        cls._headers = {"Authorization": "Bearer pi21-test-token"}
        cls._operator = {**cls._headers, "X-Autonomy-Operator-Token": "pi21-operator-token"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls._client.__exit__(None, None, None)
        cls._env.stop()
        cls._tmp.cleanup()

    def test_actual_application_opens_pi21_service(self) -> None:
        status = self._client.get("/market/portfolio-management/status", headers=self._operator)
        self.assertEqual(status.status_code, 200, status.text)
        body = status.json()
        self.assertEqual(body["phase"], "PI-21")
        self.assertTrue(body["initialised"])
        self.assertTrue(body["operator_authorized"])
        self.assertFalse(body["safety"]["direct_financial_mutation"])
        self.assertFalse(body["safety"]["direct_order_submission"])

    def test_pi21_routes_require_api_authentication(self) -> None:
        response = self._client.get("/market/portfolio-management/status")
        self.assertEqual(response.status_code, 401)
        response = self._client.get("/market/portfolio-management/contracts")
        self.assertEqual(response.status_code, 401)

    def test_review_requires_autonomy_operator_permission(self) -> None:
        response = self._client.post(
            "/market/portfolio-management/review",
            headers=self._headers,
            json={"horizon": "5d"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["detail"]["error"], "autonomy_operator_forbidden")

    def test_review_with_operator_reports_no_portfolio_without_fabrication(self) -> None:
        response = self._client.post(
            "/market/portfolio-management/review",
            headers=self._operator,
            json={"horizon": "5d"},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"]["error"], "PORTFOLIO_MANAGEMENT_VALIDATION")

    def test_contracts_and_config_are_readable(self) -> None:
        contracts = self._client.get(
            "/market/portfolio-management/contracts", headers=self._headers
        )
        self.assertEqual(contracts.status_code, 200, contracts.text)
        self.assertIn("authorities", contracts.json())
        config = self._client.get("/market/portfolio-management/config", headers=self._headers)
        self.assertEqual(config.status_code, 200, config.text)
        self.assertGreaterEqual(config.json()["version"], 1)

    def test_config_update_requires_operator_and_never_mutates_finances(self) -> None:
        denied = self._client.put(
            "/market/portfolio-management/config",
            headers=self._headers,
            json={"cash_reserve_target": "0.2"},
        )
        self.assertEqual(denied.status_code, 403)
        allowed = self._client.put(
            "/market/portfolio-management/config",
            headers=self._operator,
            json={"cash_reserve_target": "0.2"},
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertFalse(allowed.json()["financial_mutation"])
        self.assertEqual(allowed.json()["config"]["cash_reserve_target"], "0.2")

    def test_unknown_cycle_returns_404(self) -> None:
        response = self._client.get(
            "/market/portfolio-management/cycles/missing", headers=self._headers
        )
        self.assertEqual(response.status_code, 404, response.text)

    def test_frontend_source_contains_no_direct_financial_controls(self) -> None:
        root = Path(__file__).resolve().parents[1] / "web" / "src"
        page = (root / "pages" / "PortfolioManagement.tsx").read_text(encoding="utf-8")
        nav = (root / "nav.ts").read_text(encoding="utf-8")
        app = (root / "App.tsx").read_text(encoding="utf-8")
        self.assertIn('id: "portfoliomanagement"', nav)
        self.assertIn("PortfolioManagementPage", app)
        for word in (
            "Command overview",
            "Position intelligence",
            "Opportunities",
            "Capital & cash",
            "Rebalancing",
            "Decision history",
            "Autonomous status",
        ):
            self.assertIn(word, page)
        for forbidden in (
            "/market/paper/orders",
            "/market/execution/order",
            "submitOrder",
            "applyProposal",
        ):
            self.assertNotIn(forbidden, page)


class ScheduledReviewTests(unittest.TestCase):
    """The scheduled trigger is invoked by the one autonomy runtime tick."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.sources = FakeSources()
        self.now = NOW
        self.service = PortfolioManagementService(
            self.sources.sources(),
            store_path=Path(self.tmp.name) / "portfolio_management.sqlite3",
            clock=lambda: self.now,
            scheduled_interval_seconds=60,
        )

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def _maybe(self) -> dict[str, object]:
        return asyncio.run(self.service.maybe_run())

    def test_refuses_while_autonomy_is_disabled_or_paused(self) -> None:
        for mode in ("disabled", "paused"):
            self.sources.mode = mode
            outcome = self._maybe()
            self.assertEqual(outcome["status"], "refused")
            self.assertEqual(
                outcome["reason_code"],
                "RUNTIME_DISABLED" if mode == "disabled" else "RUNTIME_PAUSED",
            )
            self.assertFalse(outcome["orders_possible"])
        self.assertEqual(self.service.cycles()["total"], 0)

    def test_refuses_without_a_valid_runtime_lease(self) -> None:
        self.sources.mode = "monitoring"  # monitoring, but no lease is held
        outcome = self._maybe()
        self.assertEqual(outcome["status"], "refused")
        self.assertEqual(outcome["reason_code"], "LEASE_INVALID")
        self.assertEqual(self.service.cycles()["total"], 0)

    def test_kill_switch_blocks_the_scheduled_review(self) -> None:
        self.sources.kill_switch = True
        outcome = self._maybe()
        self.assertEqual(outcome["status"], "refused")
        self.assertEqual(outcome["reason_code"], "KILL_SWITCH")
        self.assertEqual(self.service.cycles()["total"], 0)

    def test_runs_a_scheduled_cycle_and_respects_its_cadence(self) -> None:
        first = self._maybe()
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["trigger"], "scheduled")
        self.assertEqual(first["portfolio_id"], PORTFOLIO)
        self.assertFalse(first["financial_mutation"])
        cycles = self.service.cycles()["items"]
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]["trigger"], "scheduled")

        not_due = self._maybe()
        self.assertEqual(not_due["status"], "not_due")
        self.assertGreater(float(not_due["retry_after_seconds"]), 0.0)
        self.assertEqual(self.service.cycles()["total"], 1)

        self.now = NOW + timedelta(seconds=61)
        second = self._maybe()
        self.assertEqual(second["status"], "completed")
        self.assertNotEqual(second["cycle_id"], first["cycle_id"])
        self.assertEqual(self.service.cycles()["total"], 2)

    def test_scheduled_review_uses_the_portfolios_own_horizon(self) -> None:
        """A scheduled review analyses the portfolio's declared horizon, not 5d."""

        cases = (("long_term", "90d"), ("short_term", "1d"))
        for index, (declared, expected) in enumerate(cases):
            if index:
                self.now = self.now + timedelta(seconds=61)
            self.sources.detail["portfolio"]["horizon"] = declared
            outcome = self._maybe()
            self.assertEqual(outcome["status"], "completed")
            self.assertEqual(outcome["horizon"], expected)
            cycle = next(
                row
                for row in self.service.cycles()["items"]
                if row["cycle_id"] == outcome["cycle_id"]
            )
            self.assertEqual(cycle["horizon"], expected)
            self.assertEqual(cycle["portfolio_id"], PORTFOLIO)

        # A portfolio that declares nothing usable keeps the 5d default.
        self.now = self.now + timedelta(seconds=61)
        self.sources.detail["portfolio"]["horizon"] = ""
        defaulted = self._maybe()
        self.assertEqual(defaulted["status"], "completed")
        self.assertEqual(defaulted["horizon"], "5d")

    def test_scheduled_reviews_rotate_through_the_book_and_wrap(self) -> None:
        """Every portfolio gets a review; the cursor is durable and wraps."""

        book = [
            {"portfolio_id": "pf-a"},
            {"portfolio_id": "pf-b"},
            {"portfolio_id": "pf-c"},
        ]
        detail = self.sources.detail

        def portfolio_detail(portfolio_id: str) -> dict[str, object]:
            return {
                **detail,
                "portfolio": {**detail["portfolio"], "portfolio_id": portfolio_id},
            }

        sources = replace(
            self.sources.sources(),
            portfolio_list=lambda limit: {"portfolios": [dict(row) for row in book]},
            portfolio_detail=portfolio_detail,
        )
        path = Path(self.tmp.name) / "rotation.sqlite3"

        def build() -> PortfolioManagementService:
            return PortfolioManagementService(
                sources,
                store_path=path,
                clock=lambda: self.now,
                scheduled_interval_seconds=60,
            )

        def reviewed(service: PortfolioManagementService) -> str:
            outcome = asyncio.run(service.maybe_run())
            self.assertEqual(outcome["status"], "completed", outcome)
            return str(outcome["portfolio_id"])

        seen: list[str] = []
        first = build()
        try:
            seen.append(reviewed(first))
            self.now = self.now + timedelta(seconds=61)
            seen.append(reviewed(first))
            self.assertEqual(
                first.status()["scheduler"]["cursor_portfolio_id"], "pf-b"
            )
        finally:
            first.close()

        # The cursor is durable: a new service on the same store continues where
        # the last one stopped and wraps past the end of the book.
        resumed = build()
        try:
            self.now = self.now + timedelta(seconds=61)
            seen.append(reviewed(resumed))
            self.now = self.now + timedelta(seconds=61)
            seen.append(reviewed(resumed))
        finally:
            resumed.close()
        self.assertEqual(seen, ["pf-a", "pf-b", "pf-c", "pf-a"])

    def test_without_a_portfolio_it_refuses_instead_of_inventing_one(self) -> None:
        with mock.patch.object(self.service, "_portfolio_rows", return_value=[]):
            outcome = self._maybe()
        self.assertEqual(outcome["status"], "refused")
        self.assertEqual(outcome["reason_code"], "NO_PORTFOLIO")
        self.assertEqual(self.service.cycles()["total"], 0)

    def test_scheduler_state_names_the_hosting_runtime(self) -> None:
        self.assertEqual(self._maybe()["status"], "completed")
        scheduler = self.service.status()["scheduler"]
        self.assertEqual(scheduler["hosted_by"], "novamarket_runtime")
        self.assertFalse(scheduler["second_scheduler"])
        self.assertTrue(scheduler["running"])
        self.assertEqual(scheduler["interval_seconds"], 60)
        self.assertEqual(scheduler["last_cycle_id"], self.service.cycles()["items"][0]["cycle_id"])
        self.assertIsNotNone(scheduler["next_due_at"])
        self.assertEqual(self.service.contract()["scheduler"]["hosted_by"], "novamarket_runtime")


class RuntimeTickHostingTests(unittest.TestCase):
    """The existing single runtime tick is what invokes the scheduled review."""

    def setUp(self) -> None:
        # No data directory: this test exercises tick wiring, not storage.
        self.service = NovaMarketService()

    def tearDown(self) -> None:
        self.service._portfolio_management_service = None

    def _tick_with(self, mode: str, lease_valid: bool) -> tuple[list[int], dict[str, object]]:
        calls: list[int] = []

        class _Coordinator:
            async def maybe_run(self) -> dict[str, object]:
                calls.append(1)
                return {"ok": True, "status": "completed", "cycle_id": "pmc_test"}

            def status(self, **kwargs: object) -> dict[str, object]:
                return {"ok": True, "scheduler": {"hosted_by": "novamarket_runtime"}}

        class _Closed:
            closed = True

        class _Runtime:
            def status(self) -> dict[str, object]:
                return {"mode": mode, "lease": {"valid": lease_valid}}

        self.service._lifecycle = mock.Mock(state=LifecycleState.RUNNING)
        self.service._autonomy_runtime = _Runtime()
        self.service._autonomy_monitoring = _Closed()
        self.service._decision_pipeline = _Closed()
        self.service._autonomy_execution = _Closed()
        self.service._autonomy_exits = _Closed()
        self.service._portfolio_management_service = _Coordinator()
        asyncio.run(self.service._runtime_tick())
        return calls, self.service.portfolio_management_status()

    def test_the_tick_runs_the_scheduled_review_once(self) -> None:
        calls, status = self._tick_with("monitoring", True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(status["scheduler_error"], "")

    def test_the_tick_does_not_open_the_coordinator_while_disabled(self) -> None:
        self.service._lifecycle = mock.Mock(state=LifecycleState.RUNNING)
        self.service._autonomy_runtime = mock.Mock(
            status=lambda: {"mode": "disabled", "lease": {"valid": False}}
        )
        asyncio.run(self.service._runtime_tick())
        self.assertIsNone(self.service._portfolio_management_service)
        self.assertEqual(self.service._portfolio_management_scheduler_error, "")


if __name__ == "__main__":
    unittest.main()
