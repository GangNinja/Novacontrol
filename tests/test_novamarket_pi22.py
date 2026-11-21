"""Focused PI-22 advanced risk-intelligence tests.

The fixtures are deterministic analytical evidence.  They are not market
data, forecasts, execution results or performance claims.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from novacontrol.api.models import ApiSurface
from novacontrol.api.route_consumers import ROUTE_CONSUMERS
from novacontrol.novamarket.portfolio.models import ValuationSnapshot
from novacontrol.novamarket.portfolio.store import PortfolioStore
from novacontrol.novamarket.risk_intelligence import (
    RiskIntelligenceService,
    RiskIntelligenceSources,
    RiskIntelligenceNotFound,
    RiskIntelligenceValidationError,
    RiskThresholds,
)

NOW = datetime(2026, 3, 20, 12, 0, tzinfo=UTC)
PORTFOLIO = "pf-pi22"
AAA = "XNSE:AAA"
BBB = "XNSE:BBB"
CCC = "XNSE:CCC"
DDD = "XNSE:DDD"


def _bars(
    instrument: str,
    *,
    days: int = 90,
    base: float = 100.0,
    drift: float = 0.001,
    volume: float = 100_000.0,
    decline_last: int = 0,
    offset_days: int = 0,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    close = base
    for index in range(days):
        at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index + offset_days)
        if decline_last and index >= days - decline_last:
            close *= 0.94
        else:
            close *= 1.0 + drift
        close += ((index % 5) - 2) * 0.07
        rows.append(
            {
                "at": at.isoformat(),
                "open": close,
                "high": close + 0.6,
                "low": close - 0.6,
                "close": close,
                "volume": volume,
                "provider": "fixture",
            }
        )
    return rows


def _holding(
    instrument: str,
    *,
    value: float,
    weight: float,
    sector: str = "Technology",
    industry: str = "Software",
    price: float = 100.0,
    average: float = 95.0,
    priced_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "instrument_id": instrument,
        "quantity": "10",
        "average_cost": str(average),
        "last_price": str(price),
        "market_value": str(value),
        "unrealized_pnl": str(value - average * 10),
        "weight": str(weight),
        "sector": sector,
        "industry": industry,
        "priced_at": (priced_at or NOW).isoformat(),
    }


def _valuations(*, drawdown: bool = False, days: int = 90) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    value = 100_000.0
    for index in range(days):
        if drawdown and index > 50:
            value *= 0.985
        rows.append(
            {
                "at": (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)).isoformat(),
                "total_value": str(round(value, 4)),
            }
        )
    return rows


class FakeSources:
    def __init__(self) -> None:
        self.mode = "monitoring"
        self.kill_switch = False
        self.drawdown = False
        self.missing_bars: set[str] = set()
        self.stale_prices = False
        self.decline_last = 0
        self.forecast_direction = "up"
        self.forecast_generated = NOW - timedelta(hours=1)
        self.agent_action = "hold"
        self.regime = "bull"
        self.regime_confidence = 0.8
        self.spread_bps = 6.0
        self.low_volume = False
        self.nm08_portfolio = {"outcome": "approved", "decision_id": "nm08-portfolio-1"}
        self.nm08_position = {"outcome": "approved", "decision_id": "nm08-position-1"}
        self.detail = self._detail()

    def _detail(self) -> dict[str, object]:
        priced_at = NOW - timedelta(hours=100) if self.stale_prices else NOW
        return {
            "portfolio": {"portfolio_id": PORTFOLIO, "name": "PI-22 fixture", "horizon": "long"},
            "valuation": {
                "snapshot_id": "valuation-1",
                "at": NOW.isoformat(),
                "total_value": "100000",
                "invested": "80000",
                "unrealized_pnl": "2500",
                "realized_pnl": "500",
            },
            "cash": "20000",
            "holdings": [
                _holding(AAA, value=30000, weight=0.30, priced_at=priced_at),
                _holding(
                    BBB,
                    value=25000,
                    weight=0.25,
                    priced_at=priced_at,
                    sector="Financials",
                    industry="Banks",
                ),
                _holding(
                    CCC,
                    value=15000,
                    weight=0.15,
                    priced_at=priced_at,
                    sector="Energy",
                    industry="Oil",
                ),
                _holding(
                    DDD,
                    value=10000,
                    weight=0.10,
                    priced_at=priced_at,
                    sector="Healthcare",
                    industry="Pharma",
                ),
            ],
        }

    def portfolio_list(self, limit: int) -> dict[str, object]:
        del limit
        return {"portfolios": [self.detail["portfolio"]]}

    def portfolio_detail(self, portfolio_id: str) -> dict[str, object]:
        if portfolio_id != PORTFOLIO:
            return {}
        return self.detail

    def valuation_history(self, portfolio_id: str, limit: int) -> dict[str, object]:
        del portfolio_id
        rows = _valuations(drawdown=self.drawdown)
        return {"valuations": rows[-limit:]}

    def bars(self, instrument_id: str, limit: int) -> list[dict[str, object]]:
        if instrument_id in self.missing_bars:
            return []
        params = {"volume": 10.0 if self.low_volume else 100_000.0}
        if instrument_id == AAA:
            params.update({"base": 100.0, "drift": 0.001, "decline_last": self.decline_last})
        elif instrument_id == BBB:
            # Deliberately identical to AAA so timestamp-aligned correlation is 1.0.
            params.update({"base": 100.0, "drift": 0.001, "decline_last": self.decline_last})
        elif instrument_id == CCC:
            params.update({"base": 40.0, "drift": -0.0005})
        else:
            params.update({"base": 70.0, "drift": 0.0008})
        return _bars(instrument_id, **params)[-limit:]

    def forecast_views(self, instrument_id: str, horizon: str) -> dict[str, object]:
        return {
            "forecasts": [
                {
                    "forecast_id": f"{instrument_id}-{horizon}",
                    "model_id": "fixture-model",
                    "generated_at": self.forecast_generated.isoformat(),
                    "validity": "confirmed",
                    "direction": self.forecast_direction,
                    "expected_return": "0.02",
                    "uncertainty": {"class": "low"},
                }
            ]
        }

    def instrument_evidence(self, instrument_id: str) -> dict[str, object]:
        return {
            "instrument": {
                "instrument_id": instrument_id,
                "sector": "Technology",
                "bid_ask_spread_bps": self.spread_bps,
            },
            "regime": {"primary": self.regime, "confidence": self.regime_confidence},
            "research": {"thesis": {"stance": "neutral"}, "negative_factor_count": 0},
        }

    def agent_decisions(self, instrument_id: str, limit: int) -> list[dict[str, object]]:
        del instrument_id, limit
        return [{"action": self.agent_action, "evidence": ["fixture"]}]

    def optimization_latest(self, portfolio_id: str, horizon: str) -> dict[str, object]:
        del portfolio_id, horizon
        return {}

    def risk_portfolio(self, portfolio_id: str) -> dict[str, object]:
        del portfolio_id
        return dict(self.nm08_portfolio)

    def risk_position(self, portfolio_id: str, instrument_id: str) -> dict[str, object]:
        del portfolio_id, instrument_id
        return dict(self.nm08_position)

    def runtime_status(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "available": True,
            "kill_switch": self.kill_switch,
        }

    def pending_orders(self, portfolio_id: str) -> dict[str, object]:
        del portfolio_id
        return {
            "ok": True,
            "orders": [
                {
                    "status": "open",
                    "side": "buy",
                    "remaining_quantity": "10",
                    "reference_price": "50",
                }
            ],
        }

    def reserved_cash(self, portfolio_id: str) -> str:
        del portfolio_id
        return "1000"


def sources(fake: FakeSources) -> RiskIntelligenceSources:
    return RiskIntelligenceSources(
        portfolio_list=fake.portfolio_list,
        portfolio_detail=fake.portfolio_detail,
        valuation_history=fake.valuation_history,
        bars=fake.bars,
        forecast_views=fake.forecast_views,
        instrument_evidence=fake.instrument_evidence,
        agent_decisions=fake.agent_decisions,
        optimization_latest=fake.optimization_latest,
        risk_portfolio=fake.risk_portfolio,
        risk_position=fake.risk_position,
        runtime_status=fake.runtime_status,
        pending_orders=fake.pending_orders,
        reserved_cash=fake.reserved_cash,
    )


def service(fake: FakeSources | None = None, *, path: str = ":memory:") -> RiskIntelligenceService:
    return RiskIntelligenceService(
        sources(fake or FakeSources()), store_path=path, clock=lambda: NOW
    )


def analyze(svc: RiskIntelligenceService, **kwargs: object) -> dict[str, object]:
    return asyncio.run(svc.analyze(PORTFOLIO, **kwargs))


class RiskIntelligenceUnitTests(unittest.TestCase):
    def test_initialisation_contract_and_navigation_are_explicitly_advisory(self) -> None:
        svc = service()
        status = svc.status(operator_authorized=True)
        contract = svc.contract()
        navigation = svc.navigation()
        self.assertEqual(status["phase"], "PI-22")
        self.assertEqual(status["mode"], "monitoring")
        self.assertFalse(status["safety"]["direct_financial_mutation"])
        self.assertFalse(status["safety"]["risk_decision_creation"])
        self.assertIn("NM-08", contract["authorities"]["risk_authority"])
        self.assertIn("NM-10", contract["authorities"]["paper_execution_authority"])
        self.assertFalse(contract["authorities"]["order_submission"])
        self.assertIsNone(contract["composite_score"])
        self.assertEqual(navigation["direct_financial_controls"], [])
        self.assertEqual(len(navigation["views"]), 7)

    def test_diversified_portfolio_analysis_records_every_required_area(self) -> None:
        result = analyze(service())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["portfolio"]["total_value_inr"], "100000.00")
        self.assertEqual(result["portfolio"]["available_cash_nc"], "19000.00")
        self.assertEqual(result["portfolio"]["pending_order_exposure_nc"], "500.00")
        self.assertEqual(result["nm08"]["authority"], "NM-08")
        self.assertFalse(result["nm08"]["pi22_creates_risk_decision"])
        self.assertFalse(result["financial_mutation"])
        self.assertFalse(result["order_submission"])
        for key in (
            "concentration",
            "correlation",
            "volatility",
            "drawdown",
            "liquidity",
            "regime",
            "warnings",
            "recommendations",
            "positions",
        ):
            self.assertIn(key, result)
        self.assertEqual(len(result["positions"]), 4)
        self.assertEqual(result["volatility"]["risk_level"], "normal")
        self.assertGreater(result["volatility"]["var"]["value"], 0)
        self.assertGreaterEqual(
            result["volatility"]["cvar"]["value"], result["volatility"]["var"]["value"]
        )
        self.assertEqual(result["positions"][0]["nm08_reference"], "nm08-position-1")

    def test_concentrated_portfolio_is_flagged_without_automatic_selling(self) -> None:
        fake = FakeSources()
        fake.detail["holdings"] = [
            _holding(AAA, value=90000, weight=0.90, sector="Technology"),
            _holding(CCC, value=10000, weight=0.10, sector="Energy", industry="Oil"),
        ]
        result = analyze(service(fake))
        self.assertEqual(result["concentration"]["risk_level"], "high")
        self.assertGreater(result["concentration"]["hhi"], 0.8)
        actions = {row["action"] for row in result["recommendations"]}
        self.assertIn("reduce_new_entries", actions)
        for row in result["recommendations"]:
            self.assertTrue(row["advisory_only"])
            self.assertEqual(row["order_submission"], False)

    def test_position_drawdown_volatility_and_forecast_degradation_are_explainable(self) -> None:
        fake = FakeSources()
        fake.forecast_direction = "down"
        fake.decline_last = 8
        result = analyze(service(fake))
        positions = {row["instrument_id"]: row for row in result["positions"]}
        self.assertIn(positions[AAA]["risk_level"], {"elevated", "high", "critical"})
        self.assertGreater(positions[AAA]["drawdown"]["current_drawdown_pct"], 0)
        self.assertEqual(positions[AAA]["forecast"]["direction"], "down")
        self.assertTrue(positions[AAA]["recommended_action"])

    def test_correlation_uses_exact_timestamp_alignment_and_reports_high_pairs(self) -> None:
        result = analyze(service())
        self.assertEqual(result["correlation"]["status"], "completed")
        pair = next(
            row
            for row in result["correlation"]["pairs"]
            if {row["instrument_a"], row["instrument_b"]} == {AAA, BBB}
        )
        self.assertAlmostEqual(pair["correlation"], 1.0, places=6)
        self.assertGreater(result["correlation"]["valid_pair_count"], 0)
        self.assertTrue(result["correlation"]["highly_correlated"])

    def test_misaligned_calendars_do_not_produce_correlations(self) -> None:
        fake = FakeSources()
        original = fake.bars
        fake.bars = lambda instrument_id, limit: (
            _bars(instrument_id, offset_days=365)[-limit:]
            if instrument_id == BBB
            else original(instrument_id, limit)
        )
        result = analyze(service(fake))
        pair = next(
            row
            for row in result["correlation"]["pairs"]
            if {row["instrument_a"], row["instrument_b"]} == {AAA, BBB}
        )
        self.assertFalse(pair["sufficient"])
        self.assertIsNone(pair["correlation"])

    def test_missing_bars_are_insufficient_not_zero_risk(self) -> None:
        fake = FakeSources()
        fake.missing_bars = {CCC}
        result = analyze(service(fake))
        self.assertEqual(result["data_status"], "completed_with_warnings")
        self.assertIn(CCC, result["evidence_errors"] or {CCC: "fixture"})
        position = next(row for row in result["positions"] if row["instrument_id"] == CCC)
        self.assertEqual(position["risk_level"], "insufficient_data")
        self.assertIsNone(position["volatility"]["var"]["value"])
        self.assertTrue(
            any(row["code"] == "historical_observations_missing" for row in result["warnings"])
        )
        self.assertEqual(result["liquidity"]["risk_level"], "elevated")
        self.assertEqual(result["liquidity"]["status"], "completed_with_warnings")
        self.assertEqual(result["liquidity"]["uncovered_weight"], 0.15)
        self.assertIn("liquidity_data_missing", {row["code"] for row in result["warnings"]})

    def test_stale_prices_are_reported_and_fail_closed(self) -> None:
        fake = FakeSources()
        fake.stale_prices = True
        fake.detail = fake._detail()
        result = analyze(service(fake))
        self.assertEqual(result["data_status"], "stale_data")
        self.assertIn("stale_market_data", {row["code"] for row in result["warnings"]})
        self.assertIn("no action should be inferred", " ".join(result["limitations"]).lower())

    def test_missing_market_evidence_cannot_produce_a_normal_headline(self) -> None:
        fake = FakeSources()
        instruments = [AAA, BBB, CCC, DDD, "XNSE:EEE", "XNSE:FFF"]
        fake.missing_bars = set(instruments)
        fake.detail["holdings"] = [
            _holding(
                instrument,
                value=10_000,
                weight=0.05,
                sector=sector,
                industry=sector,
            )
            for instrument, sector in zip(
                instruments,
                ("Technology", "Financials", "Energy", "Healthcare", "Industrials", "Utilities"),
                strict=True,
            )
        ]
        result = analyze(service(fake))
        self.assertEqual(result["concentration"]["risk_level"], "normal")
        self.assertEqual(result["volatility"]["status"], "insufficient_data")
        self.assertEqual(result["risk_level"], "insufficient_data")

    def test_liquidity_uses_volume_and_never_fabricates_depth(self) -> None:
        fake = FakeSources()
        fake.low_volume = True
        result = analyze(service(fake))
        self.assertEqual(result["liquidity"]["status"], "completed")
        self.assertIn(
            result["liquidity"]["liquidity_class"], {"low_liquidity", "very_low_liquidity"}
        )
        self.assertEqual(
            result["liquidity"]["market_depth"], "unavailable unless supplied by the provider"
        )
        self.assertTrue(
            all(row["market_depth"] == "quote_only" for row in result["liquidity"]["positions"])
        )

    def test_liquidity_rows_name_the_instrument_they_describe(self) -> None:
        fake = FakeSources()
        result = analyze(service(fake))
        positions = result["positions"]
        rows = result["liquidity"]["positions"]
        self.assertTrue(positions)
        self.assertEqual(len(rows), len(positions))
        self.assertEqual(
            [row.get("instrument_id") for row in rows],
            [position["instrument_id"] for position in positions],
        )
        self.assertTrue(all(row.get("instrument_id") for row in rows))
        worst = result["liquidity"]["worst_position"]
        self.assertIn(worst["instrument_id"], {row["instrument_id"] for row in rows})

    def test_regime_exposure_is_weighted_and_uncertainty_is_preserved(self) -> None:
        fake = FakeSources()
        result = analyze(service(fake))
        self.assertEqual(result["regime"]["primary"], "bull")
        self.assertAlmostEqual(result["regime"]["confidence"], 0.8, places=6)
        fake.regime = "uncertain"
        fake.regime_confidence = 0.2
        uncertain = analyze(service(fake))
        self.assertTrue(uncertain["regime"]["uncertain"])
        self.assertEqual(uncertain["regime"]["risk_level"], "elevated")

    def test_drawdown_tracks_authoritative_valuations(self) -> None:
        fake = FakeSources()
        fake.drawdown = True
        result = analyze(service(fake))
        self.assertEqual(result["drawdown"]["status"], "completed")
        self.assertGreater(result["drawdown"]["max_drawdown_pct"], 0)
        self.assertGreater(result["drawdown"]["max_drawdown_nc"], 0)
        self.assertIn(result["drawdown"]["risk_level"], {"elevated", "high", "critical"})

    def test_scenario_impact_is_isolated_and_labelled_hypothetical(self) -> None:
        svc = service()
        analyze(svc)
        result = asyncio.run(
            svc.run_scenario(
                PORTFOLIO,
                config_payload={"kind": "market_decline", "shock_pct": -10},
            )
        )
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["hypothetical"])
        self.assertFalse(result["is_prediction"])
        self.assertEqual(result["estimated_change_nc"], -8000.0)
        self.assertEqual(result["portfolio_value_before_nc"], 100000.0)
        self.assertEqual(result["estimated_portfolio_value_nc"], 92000.0)
        self.assertEqual(result["remaining_cash_nc"], 19000.0)
        self.assertEqual(
            sum(row["estimated_change_nc"] for row in result["position_contributions"]), -8000.0
        )
        self.assertFalse(result["financial_mutation"])

    def test_cash_depletion_scenario_reports_cash_not_position_loss(self) -> None:
        svc = service()
        analyze(svc)
        result = asyncio.run(
            svc.run_scenario(PORTFOLIO, config_payload={"kind": "cash_depletion", "shock_pct": 0})
        )
        self.assertEqual(result["cash_depletion_nc"], 19000.0)
        self.assertEqual(result["estimated_change_nc"], -19000.0)
        self.assertEqual(result["remaining_cash_nc"], 0.0)
        self.assertEqual(result["position_contributions"], [])

    def test_pending_order_and_liquidity_scenarios_are_explicit(self) -> None:
        svc = service()
        analyze(svc)
        pending = asyncio.run(
            svc.run_scenario(
                PORTFOLIO,
                config_payload={"kind": "multiple_pending_orders", "shock_pct": -10},
            )
        )
        self.assertEqual(pending["pending_order_loss_nc"], -50.0)
        self.assertEqual(pending["estimated_change_nc"], -50.0)
        self.assertEqual(pending["position_contributions"], [])
        self.assertEqual(pending["remaining_cash_nc"], 19000.0)
        liquidity = asyncio.run(
            svc.run_scenario(
                PORTFOLIO,
                config_payload={"kind": "liquidity_deterioration", "shock_pct": -10},
            )
        )
        self.assertGreater(liquidity["liquidity_cost_nc"], 0)
        self.assertLess(liquidity["estimated_change_nc"], -8000.0)

    def test_liquidity_scenario_fails_closed_on_missing_position_impact(self) -> None:
        fake = FakeSources()
        # CCC has no usable volume history, so its liquidity block carries no
        # estimated impact range. The server must not charge it zero cost.
        fake.missing_bars = {CCC}
        svc = service(fake)
        analyze(svc)
        liquidity = asyncio.run(
            svc.run_scenario(
                PORTFOLIO,
                config_payload={"kind": "liquidity_deterioration", "shock_pct": -10},
            )
        )
        self.assertEqual(liquidity["status"], "insufficient_data")
        self.assertEqual(
            liquidity["reason"],
            "scenario requires liquidity-impact evidence for every position",
        )
        self.assertIn(CCC, liquidity["missing_liquidity_evidence"])
        self.assertLess(liquidity["liquidity_cost_coverage_pct"], 100.0)
        # The covered positions are still costed; the scenario is refused
        # because the uncovered position would otherwise be free.
        self.assertGreater(liquidity["liquidity_cost_nc"], 0)
        combined = asyncio.run(
            svc.run_scenario(
                PORTFOLIO,
                config_payload={"kind": "combined", "shock_pct": -10},
            )
        )
        self.assertEqual(combined["status"], "insufficient_data")
        self.assertIn(CCC, combined["missing_liquidity_evidence"])

    def test_volatility_and_combined_scenarios_are_not_aliases(self) -> None:
        svc = service()
        analyze(svc)
        volatility = asyncio.run(
            svc.run_scenario(
                PORTFOLIO,
                config_payload={"kind": "volatility_expansion", "shock_pct": 50},
            )
        )
        self.assertEqual(volatility["status"], "completed")
        self.assertEqual(volatility["volatility_multiplier"], 1.5)
        self.assertLess(volatility["estimated_change_nc"], 0)
        correlated = asyncio.run(
            svc.run_scenario(
                PORTFOLIO,
                config_payload={"kind": "correlated_decline", "shock_pct": 0},
            )
        )
        self.assertEqual(correlated["volatility_multiplier"], 1.0)
        self.assertLess(correlated["estimated_change_nc"], 0)
        self.assertIn("perfect correlation", correlated["methodology"])
        combined = asyncio.run(
            svc.run_scenario(
                PORTFOLIO,
                config_payload={"kind": "combined", "shock_pct": -10},
            )
        )
        self.assertGreater(combined["liquidity_cost_nc"], 0)
        self.assertEqual(combined["liquidity_cost_coverage_pct"], 100.0)

    def test_scenario_cannot_be_run_against_a_different_portfolio(self) -> None:
        svc = service()
        analyze(svc)
        created = svc.create_scenario(
            {"portfolio_id": "pf-other", "kind": "market_decline", "shock_pct": -10},
            actor="operator",
        )
        with self.assertRaises(RiskIntelligenceNotFound):
            asyncio.run(
                svc.run_scenario(
                    PORTFOLIO,
                    scenario_id=str(created["scenario"]["scenario_id"]),
                )
            )

    def test_non_finite_stress_shock_is_refused(self) -> None:
        svc = service()
        analyze(svc)
        with self.assertRaises(RiskIntelligenceValidationError):
            asyncio.run(
                svc.run_stress(
                    PORTFOLIO,
                    mode="hypothetical",
                    shock_pct=float("nan"),
                )
            )

    def test_pending_exposure_above_available_cash_is_high_alert(self) -> None:
        fake = FakeSources()
        fake.pending_orders = lambda portfolio_id: {
            "orders": [
                {
                    "status": "open",
                    "side": "buy",
                    "remaining_quantity": "1000",
                    "reference_price": "50",
                }
            ]
        }
        result = analyze(service(fake))
        self.assertEqual(result["capital"]["pending_buy_notional_nc"], 50000.0)
        self.assertGreater(result["capital"]["pending_to_available_cash_pct"], 100.0)
        self.assertIn(
            "pending_orders_exceed_available_cash",
            {row["code"] for row in result["warnings"]},
        )

    def test_recovery_updates_the_persisted_failure_payload(self) -> None:
        svc = service()
        svc.store.save_stress(
            {
                "stress_id": "ristress_failed",
                "portfolio_id": PORTFOLIO,
                "status": "calculation_failed",
                "result_digest": "digest",
                "generated_at": NOW.isoformat(),
            }
        )
        recovered = asyncio.run(svc.recover(reason="operator review"))
        self.assertEqual(recovered["stress_runs_requiring_review"], ["ristress_failed"])
        stored = svc.stress_results(PORTFOLIO)["stress_results"][0]
        self.assertEqual(stored["status"], "requires_review")
        self.assertEqual(stored["recovered_at"], NOW.isoformat())

    def test_recent_valuation_window_is_chronological_and_current(self) -> None:
        store = PortfolioStore(":memory:")
        try:
            for index in range(5):
                store.save_valuation(
                    ValuationSnapshot(
                        snapshot_id=f"v{index}",
                        portfolio_id=PORTFOLIO,
                        at=NOW + timedelta(days=index),
                        cash=Decimal("0"),
                        invested=Decimal("100"),
                        total_value=Decimal(100 + index),
                        realized_pnl=Decimal("0"),
                        unrealized_pnl=Decimal("0"),
                        return_pct=Decimal("0"),
                        drawdown_pct=Decimal("0"),
                        cash_weight=Decimal("0"),
                        priced_positions=1,
                        stale_positions=0,
                        holdings=1,
                    )
                )
            rows = store.valuations(PORTFOLIO, limit=2, latest=True)
            self.assertEqual([row["total_value"] for row in rows], ["103.00", "104.00"])
        finally:
            store.close()

    def test_stress_modes_are_reproducible_and_record_assumptions(self) -> None:
        svc = service()
        analyze(svc)
        first = asyncio.run(svc.run_stress(PORTFOLIO, mode="volatility"))
        second = asyncio.run(svc.run_stress(PORTFOLIO, mode="volatility"))
        self.assertEqual(first["stress_id"], second["stress_id"])
        self.assertEqual(first["result_digest"], second["result_digest"])
        self.assertEqual(first["status"], "completed")
        self.assertTrue(first["assumptions"])
        self.assertIn("VaR", first["methodology"])
        historical = asyncio.run(svc.run_stress(PORTFOLIO, mode="historical"))
        self.assertEqual(historical["status"], "completed")
        self.assertIn("historical", historical["methodology"])
        combined = asyncio.run(svc.run_stress(PORTFOLIO, mode="combined"))
        self.assertEqual(combined["status"], "completed")
        self.assertGreater(combined["liquidity_cost_nc"], 0)

    def test_alert_deduplication_cooldown_and_status_transition(self) -> None:
        svc = service()
        first = analyze(svc)
        self.assertGreater(len(first["alerts"]), 0)
        second = analyze(svc, request_id="different-request")
        self.assertEqual(len(second["alerts"]), len(first["alerts"]))
        alert = second["alerts"][0]
        self.assertEqual(alert["occurrences"], 2)
        updated = asyncio.run(
            svc.update_alert(str(alert["alert_id"]), action="acknowledge", actor="operator")
        )
        self.assertEqual(updated["alert"]["status"], "acknowledged")
        self.assertFalse(updated["financial_mutation"])

    def test_protective_recommendations_are_advisory_and_reference_nm08(self) -> None:
        result = analyze(service())
        self.assertTrue(result["recommendations"])
        for row in result["recommendations"]:
            self.assertTrue(row["advisory_only"])
            self.assertFalse(row["order_submission"])
            self.assertEqual(row["workflow"][-1], "NM-10 paper execution")
            self.assertIn("NM-08 assessment", row["workflow"])

    def test_nm08_rejection_and_unavailability_are_not_overridden(self) -> None:
        fake = FakeSources()
        fake.nm08_portfolio = {"outcome": "rejected", "decision_id": "nm08-rejected"}
        fake.nm08_position = {}
        result = analyze(service(fake))
        self.assertEqual(result["nm08"]["outcome"], "rejected")
        self.assertEqual(result["nm08"]["reference"], "nm08-rejected")
        self.assertIsNone(result["positions"][0]["nm08_reference"])
        self.assertFalse(result["nm08"]["pi22_creates_risk_decision"])

    def test_scheduled_scan_is_disabled_in_disabled_mode(self) -> None:
        fake = FakeSources()
        fake.mode = "disabled"
        svc = service(fake)
        with self.assertRaises(Exception) as caught:
            analyze(svc, trigger="scheduled")
        self.assertEqual(getattr(caught.exception, "code", ""), "SCAN_DISABLED")

    def test_persistence_and_immutable_history_across_restart(self) -> None:
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "risk_intelligence.sqlite3")
            first = service(path=path)
            result = analyze(first)
            assessment_id = result["assessment_id"]
            first.close()
            second = service(path=path)
            overview = second.overview(PORTFOLIO)
            self.assertEqual(overview["assessment_id"], assessment_id)
            self.assertEqual(overview["snapshot_version"], result["snapshot_version"])
            self.assertEqual(second.history(PORTFOLIO)["total"], 1)
            second.close()

    def test_resource_limits_bound_positions_and_pairs(self) -> None:
        config = RiskThresholds(max_positions=2, max_correlation_pairs=1, max_alerts=2)
        svc = service()
        svc.update_config(config.to_dict(), actor="operator")
        result = analyze(svc)
        self.assertEqual(len(result["positions"]), 2)
        self.assertEqual(result["positions_omitted"], 2)
        self.assertLessEqual(result["correlation"]["valid_pair_count"], 1)
        self.assertLessEqual(len(result["alerts"]), 2)

    def test_configuration_validation_rejects_unknown_fields(self) -> None:
        svc = service()
        with self.assertRaises(RiskIntelligenceValidationError):
            svc.update_config({"made_up_threshold": 1})

    def test_no_analytical_service_method_can_mutate_or_execute(self) -> None:
        svc = service()
        contract = svc.contract()["authorities"]
        for key in (
            "risk_override",
            "risk_decision_creation",
            "order_submission",
            "portfolio_mutation",
            "cash_mutation",
            "ledger_write",
            "fill_mutation",
            "broker_connected",
            "live_trading",
        ):
            self.assertFalse(contract[key], key)
        self.assertTrue(contract["paper_only"])
        self.assertEqual(contract["currency"], "NC")
        self.assertEqual(contract["inr_equivalent"], "1 NC = 1 INR")


class ApiSurfaceTests(unittest.TestCase):
    def test_every_pi22_route_is_registered_and_non_rendering(self) -> None:
        expected = {
            ("GET", "/market/risk-intelligence/status"),
            ("GET", "/market/risk-intelligence/contracts"),
            ("GET", "/market/risk-intelligence/navigation"),
            ("GET", "/market/risk-intelligence/config"),
            ("PUT", "/market/risk-intelligence/config"),
            ("POST", "/market/risk-intelligence/analyze"),
            ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/overview"),
            ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/positions"),
            ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/concentration"),
            ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/correlation"),
            ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/volatility"),
            ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/drawdown"),
            ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/liquidity"),
            ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/regime"),
            ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/history"),
            ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/trend"),
            ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/recommendations"),
            ("GET", "/market/risk-intelligence/alerts"),
            ("POST", "/market/risk-intelligence/alerts/{alert_id}/action"),
            ("GET", "/market/risk-intelligence/scenarios"),
            ("POST", "/market/risk-intelligence/scenarios"),
            ("POST", "/market/risk-intelligence/portfolios/{portfolio_id}/scenarios/run"),
            ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/scenario-runs"),
            ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/stress-tests"),
            ("POST", "/market/risk-intelligence/portfolios/{portfolio_id}/stress-tests"),
            ("POST", "/market/risk-intelligence/recover"),
        }
        routes = {(row.method, row.path) for row in ApiSurface.default().routes}
        self.assertEqual(expected - routes, set())
        for route in expected:
            self.assertEqual(ROUTE_CONSUMERS[route], "no-render")

    def test_pi22_routes_contain_no_order_or_execution_path(self) -> None:
        rows = [
            row
            for row in ApiSurface.default().routes
            if row.path.startswith("/market/risk-intelligence")
        ]
        self.assertEqual(len(rows), 26)
        for row in rows:
            for forbidden in ("order", "fill", "execute", "ledger"):
                self.assertNotIn(forbidden, row.path)


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
                "NOVACONTROL_API_TOKEN": "pi22-test-token",
                "NOVACONTROL_AUTONOMY_OPERATOR_TOKEN": "pi22-operator-token",
            },
            clear=False,
        )
        cls._env.start()
        from novacontrol.api.app import create_app

        cls._client = TestClient(create_app(data_dir=cls._tmp.name))
        cls._client.__enter__()
        cls._headers = {"Authorization": "Bearer pi22-test-token"}
        cls._operator = {**cls._headers, "X-Autonomy-Operator-Token": "pi22-operator-token"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls._client.__exit__(None, None, None)
        cls._env.stop()
        cls._tmp.cleanup()

    def test_actual_application_opens_pi22_service(self) -> None:
        response = self._client.get("/market/risk-intelligence/status", headers=self._operator)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["phase"], "PI-22")
        self.assertTrue(body["initialised"])
        self.assertTrue(body["operator_authorized"])
        self.assertFalse(body["safety"]["direct_financial_mutation"])
        self.assertFalse(body["safety"]["direct_order_submission"])

    def test_pi22_reads_require_authentication(self) -> None:
        self.assertEqual(self._client.get("/market/risk-intelligence/status").status_code, 401)
        self.assertEqual(self._client.get("/market/risk-intelligence/contracts").status_code, 401)
        self.assertEqual(self._client.get("/market/risk-intelligence/alerts").status_code, 401)

    def test_analysis_requires_operator_permission(self) -> None:
        response = self._client.post(
            "/market/risk-intelligence/analyze",
            headers=self._headers,
            json={"portfolio_id": "missing"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["detail"]["error"], "autonomy_operator_forbidden")

    def test_unknown_portfolio_is_a_not_found_without_fabrication(self) -> None:
        response = self._client.post(
            "/market/risk-intelligence/analyze",
            headers=self._operator,
            json={"portfolio_id": "missing"},
        )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["detail"]["error"], "RISK_INTELLIGENCE_NOT_FOUND")

    def test_non_numeric_stress_shock_is_a_validation_error(self) -> None:
        response = self._client.post(
            "/market/risk-intelligence/portfolios/missing/stress-tests",
            headers=self._operator,
            json={"mode": "hypothetical", "shock_pct": "not-a-number"},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("numeric", response.json()["detail"])

    def test_contracts_config_and_navigation_are_readable(self) -> None:
        contracts = self._client.get("/market/risk-intelligence/contracts", headers=self._headers)
        self.assertEqual(contracts.status_code, 200, contracts.text)
        self.assertIn("NM-08", contracts.json()["authorities"]["risk_authority"])
        config = self._client.get("/market/risk-intelligence/config", headers=self._headers)
        self.assertEqual(config.status_code, 200, config.text)
        self.assertGreaterEqual(config.json()["version"], 1)
        navigation = self._client.get("/market/risk-intelligence/navigation", headers=self._headers)
        self.assertEqual(navigation.status_code, 200, navigation.text)
        self.assertEqual(navigation.json()["direct_financial_controls"], [])

    def test_config_update_requires_operator_and_never_mutates_finances(self) -> None:
        denied = self._client.put(
            "/market/risk-intelligence/config",
            headers=self._headers,
            json={"drawdown_warning_pct": "8"},
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        allowed = self._client.put(
            "/market/risk-intelligence/config",
            headers=self._operator,
            json={"drawdown_warning_pct": "8"},
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertFalse(allowed.json()["financial_mutation"])
        self.assertEqual(allowed.json()["config"]["drawdown_warning_pct"], "8")

    def test_analysis_on_a_real_created_portfolio_is_persisted_and_readable(self) -> None:
        wallet = self._client.post("/market/wallets", headers=self._headers, json={"owner": "pi22"})
        self.assertEqual(wallet.status_code, 200, wallet.text)
        wallet_id = wallet.json()["wallet"]["wallet_id"]
        funded = self._client.post(
            f"/market/wallets/{wallet_id}/deposit",
            headers=self._headers,
            json={"amount": "5000.00"},
        )
        self.assertEqual(funded.status_code, 200, funded.text)
        created = self._client.post(
            "/market/portfolios",
            headers=self._headers,
            json={"horizon": "long_term", "funding_wallet_id": wallet_id, "capital": "5000.00"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        portfolio_id = created.json()["portfolio"]["portfolio_id"]
        analyzed = self._client.post(
            "/market/risk-intelligence/analyze",
            headers=self._operator,
            json={"portfolio_id": portfolio_id, "horizon": "5d", "request_id": "api-pi22"},
        )
        self.assertEqual(analyzed.status_code, 200, analyzed.text)
        body = analyzed.json()
        self.assertEqual(body["phase"], "PI-22")
        self.assertFalse(body["financial_mutation"])
        self.assertEqual(body["portfolio_id"], portfolio_id)
        overview = self._client.get(
            f"/market/risk-intelligence/portfolios/{portfolio_id}/overview",
            headers=self._headers,
        )
        self.assertEqual(overview.status_code, 200, overview.text)
        self.assertEqual(overview.json()["assessment_id"], body["assessment_id"])
        history = self._client.get(
            f"/market/risk-intelligence/portfolios/{portfolio_id}/history",
            headers=self._headers,
        )
        self.assertEqual(history.status_code, 200, history.text)
        self.assertGreaterEqual(history.json()["total"], 1)

    def test_frontend_never_fills_a_number_for_absent_risk_evidence(self) -> None:
        root = Path(__file__).resolve().parents[1] / "web" / "src"
        page = (root / "pages" / "RiskIntelligence.tsx").read_text(encoding="utf-8")
        self.assertIn("function numberText(", page)
        for fallback_render in (
            "num(latest.concentration?.hhi, 6)",
            "num(latest.concentration?.effective_holdings, 3)",
            "num(latest.concentration?.hhi, 4)",
            "num(latest.liquidity?.weighted_days_to_liquidate, 4)",
            "num(latest.data_freshness?.age_hours, 2)",
        ):
            self.assertNotIn(fallback_render, page)

    def test_frontend_workspace_and_navigation_are_wired_without_financial_controls(self) -> None:
        root = Path(__file__).resolve().parents[1] / "web" / "src"
        page = (root / "pages" / "RiskIntelligence.tsx").read_text(encoding="utf-8")
        nav = (root / "nav.ts").read_text(encoding="utf-8")
        app = (root / "App.tsx").read_text(encoding="utf-8")
        self.assertIn('id: "riskintelligence"', nav)
        self.assertIn("RiskIntelligencePage", app)
        for label in (
            "Risk overview",
            "Position risk",
            "Concentration & correlation",
            "Volatility & drawdown",
            "Liquidity",
            "Scenarios & stress",
            "Alerts & recommendations",
        ):
            self.assertIn(label, page)
        for forbidden in (
            "/market/paper/orders",
            "/market/execution/order",
            "submitOrder",
            "applyProposal",
        ):
            self.assertNotIn(forbidden, page)


if __name__ == "__main__":
    unittest.main()
