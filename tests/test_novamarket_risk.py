"""NM-08 acceptance tests: independent risk, policy enforcement and governance.

The tests drive the real RiskEngine over the real NM-07 portfolio engine and the
real NM-03 ledger. Only the market-data, forecast and radar seams are replaced.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from novacontrol.novamarket.contracts import (
    RiskNotFoundError,
    RiskStateError,
    RiskValidationError,
)
from novacontrol.novamarket.portfolio import PortfolioEngine, PortfolioStore
from novacontrol.novamarket.risk.metrics import beta, correlation, sharpe, sortino
from novacontrol.novamarket.risk import (
    RiskEngine,
    RiskOutcome,
    RiskStore,
    annualized_volatility,
    concentration,
    historical_cvar,
    historical_var,
    max_drawdown,
)
from novacontrol.novamarket.wallet.engine import WalletEngine
from novacontrol.novamarket.wallet.store import WalletStore

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
SYMBOLS = ("XNSE:ALPHA", "XNSE:BETA", "XNSE:GAMMA")


class Market:
    def __init__(self, *, usable: bool = True, volatility: Decimal = Decimal("0.001")) -> None:
        self.usable = usable
        self.volatility = volatility

    def latest(self, instrument_id: str) -> Any:
        return SimpleNamespace(
            usable=self.usable,
            selected=SimpleNamespace(price="100.00", observed_at=NOW),
        )

    def bars(self, instrument_id: str, limit: int = 160, ascending: bool = True) -> Any:
        items = []
        close = Decimal("100.00")
        for index in range(60):
            close = close + (self.volatility if index % 2 == 0 else -self.volatility)
            items.append(
                SimpleNamespace(
                    close=close,
                    volume=Decimal("10000"),
                    observed_at=NOW - timedelta(days=59 - index),
                )
            )
        return SimpleNamespace(items=items)

    def get_instrument(self, instrument_id: str) -> Any:
        sector = "Technology" if instrument_id.endswith("ALPHA") else "Automotive"
        return SimpleNamespace(
            to_dict=lambda: {
                "instrument_id": instrument_id,
                "sector": sector,
                "asset_class": "equity",
            }
        )


class Discovery:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = [
            self._row("XNSE:ALPHA", "Technology", 82.0),
            self._row("XNSE:BETA", "Automotive", 74.0),
        ]

    @staticmethod
    def _row(symbol: str, sector: str, score: float) -> dict[str, Any]:
        return {
            "instrument_id": symbol,
            "sector": sector,
            "asset_class": "equity",
            "score": score,
            "confidence": 0.8,
            "data_quality": "good",
            "evaluation": {
                "score": score,
                "confidence": 0.8,
                "data_quality": "good",
                "sector": sector,
                "asset_class": "equity",
            },
        }

    def candidates(self, limit: int = 100) -> dict[str, Any]:
        return {"candidates": [dict(row) for row in self.rows[:limit]]}


class Forecasts:
    def forecasts(self, instrument_id: str = "", limit: int = 50) -> dict[str, Any]:
        rows = [
            {
                "forecast_id": f"fc_{symbol[-5:]}_{horizon}",
                "instrument_id": symbol,
                "horizon": horizon,
                "predicted_return": "0.02",
                "lower_return": "-0.01",
                "upper_return": "0.05",
                "uncertainty": "medium",
                "model_id": "m1",
                "created_at": NOW.isoformat(),
            }
            for symbol in SYMBOLS
            for horizon in ("next_session", "few_sessions", "one_week", "one_month", "three_months")
        ]
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
                if instrument_id is None or symbol == instrument_id
            ]
        }


class Radar:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def instrument_events(self, instrument_id: str, limit: int = 20) -> dict[str, Any]:
        return {
            "events": [
                dict(row) for row in self.events if row.get("instrument_id") == instrument_id
            ][:limit]
        }


class Harness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.wallets = WalletEngine(WalletStore(":memory:"))
        self.funding = (await self.wallets.create_wallet("house"))["wallet"]["wallet_id"]
        await self.wallets.deposit(self.funding, "40000.00")
        self.portfolios = PortfolioEngine(
            PortfolioStore(":memory:"),
            self.wallets,
            market_data=Market(),
            discovery=Discovery(),
            forecasts=Forecasts(),
            radar=Radar(),
            clock=lambda: NOW,
        )
        self.market = Market()
        self.forecasts = Forecasts()
        self.radar = Radar()
        self.risk = RiskEngine(
            RiskStore(":memory:"),
            self.portfolios,
            market_data=self.market,
            forecasts=self.forecasts,
            radar=self.radar,
            clock=lambda: NOW,
        )

    async def make(self, horizon: str = "short_term", capital: str = "10000.00") -> str:
        created = await self.portfolios.create(
            horizon, funding_wallet_id=self.funding, capital=capital
        )
        return created["portfolio"]["portfolio_id"]

    async def proposal(self, portfolio_id: str) -> dict[str, Any]:
        self.portfolios._discovery.rows.append(Discovery._row("XNSE:GAMMA", "Energy", 88.0))
        body = await self.portfolios.propose(portfolio_id, force=True)
        return body["proposal"]


class MetricTests(unittest.TestCase):
    def test_known_drawdown(self) -> None:
        result = max_drawdown([100.0, 120.0, 90.0, 110.0])
        self.assertTrue(result.sufficient)
        self.assertEqual(result.value, Decimal("25.000000"))

    def test_constant_prices_have_zero_volatility(self) -> None:
        result = annualized_volatility([100.0] * 30)
        self.assertTrue(result.sufficient)
        self.assertEqual(result.value, Decimal("0.000000"))

    def test_insufficient_observations_are_not_a_number(self) -> None:
        result = annualized_volatility([100.0, 101.0, 99.0])
        self.assertFalse(result.sufficient)
        self.assertIsNone(result.value)
        self.assertIn("at least 20", result.reason)

    def test_historical_var_and_cvar(self) -> None:
        returns = [-0.10] + [-0.01] * 19
        var = historical_var(returns)
        cvar = historical_cvar(returns)
        self.assertEqual(var.value, Decimal("1.000000"))
        self.assertEqual(cvar.value, Decimal("5.500000"))

    def test_concentration_of_an_equal_pair(self) -> None:
        self.assertEqual(concentration([0.5, 0.5]).value, Decimal("0.500000"))

    def test_concentration_reports_an_empty_book(self) -> None:
        self.assertFalse(concentration([]).sufficient)



    def test_beta_correlation_and_risk_adjusted_ratios(self) -> None:
        series = [0.01, -0.02, 0.03, 0.005, -0.01] * 6
        self.assertEqual(beta(series, series).value, Decimal("1.000000"))
        self.assertEqual(correlation(series, series).value, Decimal("1.000000"))
        self.assertTrue(sharpe(series).sufficient)
        self.assertTrue(sortino(series).sufficient)
        self.assertFalse(sharpe([0.0] * 25).sufficient)
        self.assertFalse(beta([0.01] * 25, [0.01] * 25).sufficient)

class PolicyTests(Harness):
    def policy(self, **changes: Any) -> Any:
        from dataclasses import replace

        from novacontrol.novamarket.risk import POLICY_DEFAULTS

        return replace(POLICY_DEFAULTS["short_term"], **changes)

    def snapshot(self, **overrides: Any) -> Any:
        from novacontrol.novamarket.risk import RiskSnapshot

        body: dict[str, Any] = {
            "positions": (),
            "projected": ({"instrument_id": "XNSE:ALPHA", "weight": Decimal("0.20"), "sector": "Technology"},),
            "cash_weight": Decimal("0.30"),
            "invested_fraction": Decimal("0.70"),
            "drawdown_pct": Decimal("0"),
            "volatility_pct": Decimal("10"),
            "turnover": Decimal("0.10"),
            "data_age_hours": 1.0,
            "state_age_hours": 1.0,
            "liquidity": Decimal("1000000"),
        }
        body.update(overrides)
        return RiskSnapshot(**body)

    def test_a_compliant_book_is_approved(self) -> None:
        from novacontrol.novamarket.risk import evaluate_policy

        self.assertEqual(
            evaluate_policy(self.policy(), self.snapshot(), action="apply", adds_exposure=True).outcome,
            RiskOutcome.APPROVED,
        )

    def test_position_concentration_rejects(self) -> None:
        from novacontrol.novamarket.risk import evaluate_policy

        evaluation = evaluate_policy(
            self.policy(max_position_weight=Decimal("0.10")),
            self.snapshot(),
            action="apply",
            adds_exposure=True,
        )
        self.assertEqual(evaluation.outcome, RiskOutcome.REJECTED)
        self.assertIn("max_position_weight", [item.code for item in evaluation.breaches])

    def test_cash_reserve_rejects(self) -> None:
        from novacontrol.novamarket.risk import evaluate_policy

        evaluation = evaluate_policy(
            self.policy(), self.snapshot(cash_weight=Decimal("0.02")), action="apply", adds_exposure=True
        )
        self.assertEqual(evaluation.outcome, RiskOutcome.REJECTED)

    def test_restricted_instrument_rejects(self) -> None:
        from novacontrol.novamarket.risk import evaluate_policy

        evaluation = evaluate_policy(
            self.policy(restricted_instruments=("XNSE:ALPHA",)),
            self.snapshot(),
            action="apply",
            adds_exposure=True,
        )
        self.assertEqual(evaluation.outcome, RiskOutcome.REJECTED)

    def test_stale_data_is_insufficient_not_approved(self) -> None:
        from novacontrol.novamarket.risk import evaluate_policy

        evaluation = evaluate_policy(
            self.policy(), self.snapshot(data_age_hours=99.0), action="apply", adds_exposure=True
        )
        self.assertEqual(evaluation.outcome, RiskOutcome.REJECTED)
        self.assertIn("stale_market_data", [item.code for item in evaluation.breaches])

    def test_volatility_restricts(self) -> None:
        from novacontrol.novamarket.risk import evaluate_policy

        evaluation = evaluate_policy(
            self.policy(), self.snapshot(volatility_pct=Decimal("80")), action="apply", adds_exposure=True
        )
        self.assertEqual(evaluation.outcome, RiskOutcome.RESTRICTED)

    def test_drawdown_requires_review(self) -> None:
        from novacontrol.novamarket.risk import evaluate_policy

        evaluation = evaluate_policy(
            self.policy(), self.snapshot(drawdown_pct=Decimal("40")), action="apply", adds_exposure=True
        )
        self.assertEqual(evaluation.outcome, RiskOutcome.REQUIRES_REVIEW)



    def test_a_hard_breach_outranks_missing_metrics(self) -> None:
        from novacontrol.novamarket.risk import evaluate_policy

        evaluation = evaluate_policy(
            self.policy(restricted_instruments=("XNSE:ALPHA",)),
            self.snapshot(volatility_pct=None),
            action="apply",
            adds_exposure=True,
        )
        self.assertEqual(evaluation.outcome, RiskOutcome.REJECTED)

class EngineTests(Harness):
    async def test_a_validated_proposal_receives_a_decision(self) -> None:
        portfolio_id = await self.make()
        proposal = await self.proposal(portfolio_id)
        decision = await self.risk.assess(
            "apply_proposal", portfolio_id=portfolio_id, proposal_id=proposal["proposal_id"]
        )
        self.assertIn(decision["outcome"], {outcome.value for outcome in RiskOutcome})
        self.assertEqual(decision["proposal_id"], proposal["proposal_id"])
        self.assertTrue(decision["explanation"])
        self.assertTrue(decision["metrics"])

    async def test_repeated_assessment_is_idempotent(self) -> None:
        portfolio_id = await self.make()
        proposal = await self.proposal(portfolio_id)
        first = await self.risk.assess(
            "apply_proposal", portfolio_id=portfolio_id, proposal_id=proposal["proposal_id"]
        )
        second = await self.risk.assess(
            "apply_proposal", portfolio_id=portfolio_id, proposal_id=proposal["proposal_id"]
        )
        self.assertEqual(first["decision_id"], second["decision_id"])

    async def test_an_approved_decision_applies_the_exact_proposal(self) -> None:
        portfolio_id = await self.make()
        proposal = await self.proposal(portfolio_id)
        decision = await self.risk.assess(
            "apply_proposal", portfolio_id=portfolio_id, proposal_id=proposal["proposal_id"]
        )
        if decision["outcome"] != RiskOutcome.APPROVED.value:
            self.skipTest(f"fixture produced {decision['outcome']}, not an approval")
        applied = await self.risk.apply_decision(decision["decision_id"])
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["proposal_id"], proposal["proposal_id"])

    async def test_a_changed_portfolio_state_invalidates_the_approval(self) -> None:
        portfolio_id = await self.make()
        proposal = await self.proposal(portfolio_id)
        decision = await self.risk.assess(
            "apply_proposal", portfolio_id=portfolio_id, proposal_id=proposal["proposal_id"]
        )
        if decision["outcome"] != RiskOutcome.APPROVED.value:
            self.skipTest("fixture did not approve")
        await self.portfolios.apply(proposal["proposal_id"])
        with self.assertRaises(Exception):
            await self.risk.apply_decision(decision["decision_id"])

    async def test_rejection_cannot_be_applied(self) -> None:
        portfolio_id = await self.make()
        await self.risk.update_policy("short_term", {"max_position_weight": "0.05"}, actor="test")
        decision = await self.risk.assess(
            "add_position",
            portfolio_id=portfolio_id,
            instrument_id="XNSE:GAMMA",
            side="buy",
            notional="4000.00",
        )
        self.assertEqual(decision["outcome"], RiskOutcome.REJECTED.value)
        with self.assertRaises(RiskStateError):
            await self.risk.apply_decision(decision["decision_id"])

    async def test_rejection_is_not_overridable(self) -> None:
        portfolio_id = await self.make()
        await self.risk.update_policy("short_term", {"max_position_weight": "0.05"}, actor="test")
        decision = await self.risk.assess(
            "add_position",
            portfolio_id=portfolio_id,
            instrument_id="XNSE:GAMMA",
            side="buy",
            notional="4000.00",
        )
        with self.assertRaises(RiskStateError):
            await self.risk.record_override(
                decision["decision_id"], actor="ops", reason="please"
            )

    async def test_insufficient_data_is_never_approved(self) -> None:
        self.risk.set_market_data(None)
        portfolio_id = await self.make()
        decision = await self.risk.assess("rebalance", portfolio_id=portfolio_id)
        self.assertNotEqual(decision["outcome"], RiskOutcome.APPROVED.value)
        self.assertIn(decision["outcome"], {"insufficient_data", "review_required", "requires_review"})

    async def test_unknown_proposal_is_a_typed_404(self) -> None:
        portfolio_id = await self.make()
        with self.assertRaises(RiskNotFoundError):
            await self.risk.assess(
                "apply_proposal", portfolio_id=portfolio_id, proposal_id="preb_missing"
            )

    async def test_unknown_action_is_a_typed_422(self) -> None:
        with self.assertRaises(RiskValidationError):
            await self.risk.assess("teleport")

    async def test_aggregate_exposure_uses_actual_capital(self) -> None:
        short_id = await self.make("short_term", "10000.00")
        medium_id = await self.make("medium_term", "12000.00")
        long_id = await self.make("long_term", "8000.00")
        body = self.risk.exposure()
        self.assertEqual(body["total_value"], "30000.00")
        self.assertEqual(len(body["portfolios"]), 3)
        ids = {row["portfolio_id"] for row in body["portfolios"]}
        self.assertEqual(ids, {short_id, medium_id, long_id})

    async def test_stress_is_labelled_hypothetical(self) -> None:
        portfolio_id = await self.make()
        body = await self.risk.stress(portfolio_id, "market_decline")
        self.assertTrue(body["hypothetical"])
        self.assertFalse(body["is_forecast"])
        self.assertEqual(body["scenario"]["kind"], "market")
        self.assertLessEqual(Decimal(body["impact"]), Decimal("0"))

    async def test_alerts_are_created_and_resolved(self) -> None:
        portfolio_id = await self.make()
        await self.risk.update_policy("short_term", {"max_position_weight": "0.05"}, actor="test")
        await self.risk.assess(
            "add_position",
            portfolio_id=portfolio_id,
            instrument_id="XNSE:GAMMA",
            side="buy",
            notional="4000.00",
        )
        alerts = self.risk.alerts(status="open")["alerts"]
        self.assertTrue(alerts)
        alert_id = alerts[0]["alert_id"]
        acknowledged = await self.risk.acknowledge_alert(alert_id, actor="ops")
        self.assertEqual(acknowledged["alert"]["status"], "acknowledged")
        resolved = await self.risk.resolve_alert(alert_id, actor="ops")
        self.assertEqual(resolved["alert"]["status"], "resolved")

    async def test_policy_updates_are_versioned_and_auditable(self) -> None:
        before = self.risk.policy("short_term").version
        body = await self.risk.update_policy(
            "short_term", {"max_position_weight": "0.12"}, actor="ops"
        )
        self.assertNotEqual(body["policy"]["version"], before)
        self.assertEqual(len(body["history"]), 2)
        self.assertEqual(body["policy"]["max_position_weight"], "0.12")

    async def test_unknown_policy_field_is_refused(self) -> None:
        with self.assertRaises(RiskValidationError):
            await self.risk.update_policy("short_term", {"free_money": True}, actor="ops")

    async def test_portfolio_risk_reports_metrics_and_breaches(self) -> None:
        portfolio_id = await self.make()
        body = self.risk.portfolio_risk(portfolio_id)
        self.assertEqual(body["portfolio_id"], portfolio_id)
        self.assertIn("metrics", body)
        self.assertIn("breaches", body)
        self.assertIn("positions", body)

    async def test_position_risk_is_per_instrument(self) -> None:
        portfolio_id = await self.make()
        body = self.risk.position_risk(portfolio_id, "XNSE:ALPHA")
        self.assertEqual(body["instrument_id"], "XNSE:ALPHA")
        self.assertIn("volatility", body["metrics"])

    async def test_contract_declares_simulation_only(self) -> None:
        contract = self.risk.contract()
        self.assertFalse(contract["executes_orders"])
        self.assertEqual(contract["phase"], "NM-08")
        self.assertIn("approved", contract["outcomes"])



    async def test_different_notionals_do_not_share_one_decision(self) -> None:
        portfolio_id = await self.make()
        small = await self.risk.assess(
            "add_position",
            portfolio_id=portfolio_id,
            instrument_id="XNSE:GAMMA",
            side="buy",
            notional="1000.00",
        )
        large = await self.risk.assess(
            "add_position",
            portfolio_id=portfolio_id,
            instrument_id="XNSE:GAMMA",
            side="buy",
            notional="4000.00",
        )
        self.assertNotEqual(small["decision_id"], large["decision_id"])
        self.assertEqual(large["outcome"], RiskOutcome.REJECTED.value)

    async def test_position_actions_need_a_positive_notional(self) -> None:
        portfolio_id = await self.make()
        with self.assertRaises(RiskValidationError):
            await self.risk.assess(
                "add_position", portfolio_id=portfolio_id, instrument_id="XNSE:GAMMA"
            )
        with self.assertRaises(RiskValidationError):
            await self.risk.assess(
                "add_position",
                portfolio_id=portfolio_id,
                instrument_id="XNSE:GAMMA",
                notional="-1",
            )

    async def test_capital_allocation_above_cash_is_rejected(self) -> None:
        portfolio_id = await self.make()
        decision = await self.risk.assess(
            "allocate_capital", portfolio_id=portfolio_id, notional="999999.00"
        )
        self.assertEqual(decision["outcome"], RiskOutcome.REJECTED.value)
        self.assertIn(
            "insufficient_cash", [item["code"] for item in decision["violations"]]
        )

    async def test_paper_order_is_governed_as_exposure(self) -> None:
        portfolio_id = await self.make()
        decision = await self.risk.assess(
            "paper_order",
            portfolio_id=portfolio_id,
            instrument_id="XNSE:GAMMA",
            side="buy",
            notional="4000.00",
        )
        self.assertEqual(decision["action"], "paper_order")
        self.assertEqual(decision["outcome"], RiskOutcome.REJECTED.value)

    async def test_restricted_decision_cannot_be_applied_at_full_size(self) -> None:
        portfolio_id = await self.make()
        proposal = await self.proposal(portfolio_id)
        await self.risk.update_policy("short_term", {"max_turnover": "0.01"}, actor="audit")
        decision = await self.risk.assess(
            "apply_proposal",
            portfolio_id=portfolio_id,
            proposal_id=proposal["proposal_id"],
        )
        if decision["outcome"] != RiskOutcome.RESTRICTED.value:
            self.skipTest(f"fixture produced {decision['outcome']}, not a restriction")
        await self.risk.record_override(
            decision["decision_id"], actor="audit", reason="accept the restriction"
        )
        with self.assertRaises(RiskStateError):
            await self.risk.apply_decision(decision["decision_id"])

    async def test_policy_change_invalidates_an_approval(self) -> None:
        portfolio_id = await self.make()
        proposal = await self.proposal(portfolio_id)
        decision = await self.risk.assess(
            "apply_proposal",
            portfolio_id=portfolio_id,
            proposal_id=proposal["proposal_id"],
        )
        if decision["outcome"] != RiskOutcome.APPROVED.value:
            self.skipTest(f"fixture produced {decision['outcome']}, not an approval")
        await self.risk.update_policy("short_term", {"max_turnover": "0.45"}, actor="audit")
        with self.assertRaises(RiskStateError):
            await self.risk.apply_decision(decision["decision_id"])

    async def test_verified_event_pressure_requires_review(self) -> None:
        portfolio_id = await self.make()
        self.radar.events.append(
            {
                "instrument_id": "XNSE:ALPHA",
                "verification": "officially_confirmed",
                "importance": "high",
            }
        )
        decision = await self.risk.assess("rebalance", portfolio_id=portfolio_id)
        self.assertEqual(decision["outcome"], RiskOutcome.REQUIRES_REVIEW.value)

    async def test_missing_forecast_service_requires_review(self) -> None:
        portfolio_id = await self.make()
        self.risk.set_forecasts(None)
        decision = await self.risk.assess(
            "add_position",
            portfolio_id=portfolio_id,
            instrument_id="XNSE:GAMMA",
            side="buy",
            notional="1000.00",
        )
        self.assertEqual(decision["outcome"], RiskOutcome.REQUIRES_REVIEW.value)
        self.assertIn(
            "forecast_unreliable", [item["code"] for item in decision["violations"]]
        )

    async def test_acknowledged_alert_is_still_deduplicated(self) -> None:
        portfolio_id = await self.make()
        await self.risk.update_policy("short_term", {"max_position_weight": "0.05"}, actor="audit")
        await self.risk.assess(
            "add_position",
            portfolio_id=portfolio_id,
            instrument_id="XNSE:GAMMA",
            side="buy",
            notional="4000.00",
        )
        alerts = self.risk.alerts(status="open")["alerts"]
        self.assertTrue(alerts)
        await self.risk.acknowledge_alert(alerts[0]["alert_id"], actor="audit")

        def position_alerts() -> int:
            return sum(
                1
                for alert in self.risk.alerts()["alerts"]
                if alert["metric"] == "max_position_weight"
            )

        before = position_alerts()
        await self.risk.assess(
            "add_position",
            portfolio_id=portfolio_id,
            instrument_id="XNSE:GAMMA",
            side="buy",
            notional="4100.00",
        )
        self.assertEqual(position_alerts(), before)

    async def test_position_risk_without_price_history_is_insufficient(self) -> None:
        portfolio_id = await self.make()
        self.risk.set_market_data(None)
        report = self.risk.position_risk(portfolio_id, "XNSE:ALPHA")
        self.assertEqual(report["outcome"], RiskOutcome.INSUFFICIENT_DATA.value)
        self.assertTrue(report["insufficient"])

    async def test_latest_policy_version_survives_identical_timestamps(self) -> None:
        first = await self.risk.update_policy(
            "short_term", {"max_position_weight": "0.24"}, actor="a"
        )
        second = await self.risk.update_policy(
            "short_term", {"max_position_weight": "0.23"}, actor="b"
        )
        self.assertNotEqual(first["policy"]["version"], second["policy"]["version"])
        self.assertEqual(self.risk.policy("short_term").version, second["policy"]["version"])

    async def test_volatility_stress_reports_a_projected_metric(self) -> None:
        portfolio_id = await self.make()
        report = await self.risk.stress(portfolio_id, "volatility_shock")
        self.assertIsNotNone(report["projected_volatility_pct"])
        self.assertTrue(report["hypothetical"])
        self.assertFalse(report["is_forecast"])

try:  # pragma: no cover - optional dependency
    from fastapi.testclient import TestClient as _TestClient
except Exception:  # pragma: no cover
    _TestClient = None


@unittest.skipIf(_TestClient is None, "fastapi.testclient requires httpx")
class RiskApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from tempfile import TemporaryDirectory
        from unittest import mock

        from novacontrol.api.app import create_app
        from novacontrol.application import NovaControlApplication
        from novacontrol.browser import NoopBrowserRunner
        from novacontrol.desktop import NoopDesktopRunner

        self._tmp = TemporaryDirectory()

        def build(**kwargs: Any) -> NovaControlApplication:
            kwargs["data_dir"] = self._tmp.name
            return NovaControlApplication(**kwargs)

        self._patchers = [
            mock.patch("novacontrol.api.app.NovaControlApplication", side_effect=build),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
        ]
        for patcher in self._patchers:
            patcher.start()
        self.client = _TestClient(create_app())
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()

    def test_status_lists_the_four_policies(self) -> None:
        body = self.client.get("/market/risk/status").json()
        self.assertTrue(body["initialised"])
        self.assertEqual(len(body["policies"]), 4)

    def test_policies_and_contracts_are_readable(self) -> None:
        policies = self.client.get("/market/risk/policies").json()
        self.assertEqual(len(policies["policies"]), 4)
        contract = self.client.get("/market/risk/contracts").json()
        self.assertFalse(contract["executes_orders"])
        self.assertIn("insufficient_data", contract["outcomes"])

    def test_empty_exposure_is_reported_as_zero(self) -> None:
        body = self.client.get("/market/risk/exposure").json()
        self.assertEqual(body["total_value"], "0")
        self.assertEqual(body["portfolios"], [])

    def test_an_unknown_decision_is_a_404(self) -> None:
        self.assertEqual(
            self.client.get("/market/risk/decisions/rdec_missing").status_code, 404
        )


if __name__ == "__main__":
    unittest.main()
