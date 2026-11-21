"""NM-07 acceptance tests: three-horizon portfolios over the existing engines.

Every test drives the real ``PortfolioEngine`` with the real NM-03 wallet/ledger,
and replaces only the market-data, discovery, forecast and radar surfaces — the
same seams the application wires at boot. Nothing here talks to a broker, a
network or a real price feed.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from novacontrol.novamarket.contracts import (
    PortfolioStateError,
    PortfolioValidationError,
)
from novacontrol.novamarket.portfolio import (
    PortfolioEngine,
    PortfolioStore,
    allocate,
    assess_candidates,
    policy_for,
)
from novacontrol.novamarket.portfolio.models import ProposalStatus
from novacontrol.novamarket.wallet.engine import WalletEngine
from novacontrol.novamarket.wallet.store import WalletStore

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
INSTRUMENTS = ("XNSE:ALPHA", "XNSE:BETA", "XNSE:GAMMA")
ALL_HORIZONS = (
    "next_session",
    "few_sessions",
    "one_week",
    "one_month",
    "three_months",
)


class _Market:
    """Timestamped prices, one observation per instrument."""

    def __init__(self) -> None:
        self.prices = {instrument: Decimal("100.00") for instrument in INSTRUMENTS}
        self.observed_at = NOW
        self.provider = "nse"
        self.quote_present = True
        self.bar_prices: list[Decimal] = []

    def latest(self, instrument_id: str) -> Any:
        if instrument_id not in self.prices:
            raise LookupError(instrument_id)
        if not self.quote_present:
            return SimpleNamespace(selected=None)
        return SimpleNamespace(
            selected=SimpleNamespace(
                price=str(self.prices[instrument_id]),
                observed_at=self.observed_at,
                provider=self.provider,
            )
        )

    def bars(self, instrument_id: str, limit: int = 60, ascending: bool = True) -> Any:
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    close=str(price),
                    observed_at=self.observed_at,
                    provider=self.provider,
                )
                for price in self.bar_prices[-limit:]
            ]
        )

    def get_instrument(self, instrument_id: str) -> Any:
        return SimpleNamespace(
            to_dict=lambda: {
                "instrument_id": instrument_id,
                "symbol": instrument_id.split(":")[-1],
                "name": instrument_id,
                "sector": "Technology",
                "asset_class": "equity",
                "exchange": "XNSE",
            }
        )


class _Discovery:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = [
            self._row("XNSE:ALPHA", "Technology", 82.0),
            self._row("XNSE:BETA", "Automotive", 74.0),
        ]

    @staticmethod
    def _row(instrument_id: str, sector: str, score: float) -> dict[str, Any]:
        return {
            "instrument_id": instrument_id,
            "symbol": instrument_id.split(":")[-1],
            "name": instrument_id,
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


class _Forecasts:
    def __init__(self) -> None:
        self.age = timedelta(0)
        self.state = "active"
        self.include_models = True

    def forecasts(self, instrument_id: str = "", limit: int = 100) -> dict[str, Any]:
        rows = []
        for symbol in INSTRUMENTS:
            for horizon in ALL_HORIZONS:
                rows.append(
                    {
                        "forecast_id": f"fc_{symbol[-5:]}_{horizon}",
                        "instrument_id": symbol,
                        "horizon": horizon,
                        "predicted_return": "0.02",
                        "lower_return": "-0.01",
                        "upper_return": "0.05",
                        "direction": "up",
                        "probability_up": 0.62,
                        "confidence": 0.7,
                        "uncertainty": "medium",
                        "model_id": "m1",
                        "created_at": (NOW - self.age).isoformat(),
                        "expires_at": (NOW + timedelta(days=7)).isoformat(),
                    }
                )
        if instrument_id:
            rows = [row for row in rows if row["instrument_id"] == instrument_id]
        return {"forecasts": rows[:limit]}

    def models(self) -> dict[str, Any]:
        if not self.include_models:
            return {"models": []}
        return {
            "models": [
                {
                    "model_id": "m1",
                    "instrument_id": symbol,
                    "horizon": horizon,
                    "state": self.state,
                }
                for symbol in INSTRUMENTS
                for horizon in ALL_HORIZONS
            ]
        }


class _Radar:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def instrument_events(self, instrument_id: str, limit: int = 20) -> dict[str, Any]:
        return {
            "events": [
                dict(event)
                for event in self.events
                if event.get("instrument_id") == instrument_id
            ][:limit]
        }


class Harness(unittest.IsolatedAsyncioTestCase):
    """One funded wallet, one store and one engine per test."""

    funding = "40000.00"

    async def asyncSetUp(self) -> None:
        self.wallet_store = WalletStore(":memory:")
        self.addCleanup(self.wallet_store.close)
        self.wallets = WalletEngine(self.wallet_store)
        self.funding_wallet = (
            await self.wallets.create_wallet("house")
        )["wallet"]["wallet_id"]
        await self.wallets.deposit(self.funding_wallet, self.funding)
        self.store = PortfolioStore(":memory:")
        self.addCleanup(self.store.close)
        self.now = NOW
        self.market = _Market()
        self.discovery = _Discovery()
        self.forecasts = _Forecasts()
        self.radar = _Radar()
        self.published: list[str] = []
        self.engine = PortfolioEngine(
            self.store,
            self.wallets,
            market_data=self.market,
            discovery=self.discovery,
            forecasts=self.forecasts,
            radar=self.radar,
            publisher=self._publish,
            clock=lambda: self.now,
        )

    async def _publish(self, name: str, payload: dict[str, Any]) -> None:
        self.published.append(name)

    async def make(self, horizon: str = "short_term", capital: str = "10000.00") -> str:
        created = await self.engine.create(
            horizon, funding_wallet_id=self.funding_wallet, capital=capital
        )
        return created["portfolio"]["portfolio_id"]

    def funding_balance(self) -> str:
        return str(self.wallets.wallet(self.funding_wallet)["wallet"]["balance"]["total"])

    def portfolio_wallet(self, portfolio_id: str) -> str:
        return self.store.portfolio(portfolio_id).wallet_id  # type: ignore[union-attr]


class CreationTests(Harness):
    async def test_each_horizon_creates_a_funded_active_portfolio(self) -> None:
        for horizon, capital in (
            ("short_term", "10000.00"),
            ("medium_term", "12000.00"),
            ("long_term", "15000.00"),
        ):
            result = await self.engine.create(
                horizon, funding_wallet_id=self.funding_wallet, capital=capital
            )
            self.assertEqual(result["portfolio"]["status"], "active")
            self.assertEqual(result["portfolio"]["horizon"], horizon)
            self.assertFalse(result["replayed"])
        self.assertEqual(self.funding_balance(), "3000.00")

    async def test_repeated_creation_replays_the_same_portfolio(self) -> None:
        first = await self.engine.create(
            "short_term", funding_wallet_id=self.funding_wallet, capital="10000.00"
        )
        again = await self.engine.create(
            "short_term", funding_wallet_id=self.funding_wallet, capital="10000.00"
        )
        self.assertTrue(again["replayed"])
        self.assertEqual(first["portfolio"]["portfolio_id"], again["portfolio"]["portfolio_id"])
        self.assertEqual(self.funding_balance(), "30000.00")

    async def test_a_second_create_with_a_different_capital_is_refused(self) -> None:
        await self.make("short_term", "10000.00")
        with self.assertRaises(PortfolioValidationError):
            await self.engine.create(
                "short_term",
                funding_wallet_id=self.funding_wallet,
                capital="12000.00",
            )
        self.assertEqual(self.funding_balance(), "30000.00")
        self.assertEqual(self.engine.status()["portfolios"], 1)

    async def test_insufficient_capital_is_refused_and_leaves_no_portfolio(self) -> None:
        with self.assertRaises(PortfolioValidationError):
            await self.engine.create(
                "long_term", funding_wallet_id=self.funding_wallet, capital="40001.00"
            )
        self.assertEqual(self.engine.status()["portfolios"], 0)
        self.assertEqual(self.funding_balance(), "40000.00")

    async def test_two_portfolios_cannot_commit_the_same_capital(self) -> None:
        await self.make("short_term", "30000.00")
        with self.assertRaises(PortfolioValidationError):
            await self.engine.create(
                "long_term", funding_wallet_id=self.funding_wallet, capital="30000.00"
            )
        self.assertEqual(self.funding_balance(), "10000.00")

    async def test_an_unknown_horizon_is_refused(self) -> None:
        with self.assertRaises(PortfolioValidationError):
            await self.engine.create(
                "intraday", funding_wallet_id=self.funding_wallet, capital="100.00"
            )

    async def test_a_missing_funding_wallet_is_refused(self) -> None:
        with self.assertRaises(PortfolioValidationError):
            await self.engine.create(
                "short_term", funding_wallet_id="", capital="100.00"
            )


class AllocationTests(Harness):
    async def test_opening_allocation_buys_and_respects_the_position_cap(self) -> None:
        portfolio_id = await self.make()
        holdings = self.engine.holdings(portfolio_id)["holdings"]
        self.assertEqual(len(holdings), 2)
        policy = policy_for("short_term")
        for holding in holdings:
            self.assertLessEqual(
                Decimal(holding["weight"]), policy.max_position_weight
            )

    async def test_positions_record_the_sector_that_sizing_used(self) -> None:
        portfolio_id = await self.make()
        sectors = {
            row["instrument_id"]: row["sector"]
            for row in self.engine.holdings(portfolio_id)["holdings"]
        }
        self.assertEqual(sectors["XNSE:ALPHA"], "Technology")
        self.assertEqual(sectors["XNSE:BETA"], "Automotive")

    async def test_the_cash_reserve_survives_the_opening_allocation(self) -> None:
        portfolio_id = await self.make()
        valuation = self.engine.valuation(portfolio_id)["valuation"]
        policy = policy_for("short_term")
        self.assertGreaterEqual(
            Decimal(valuation["cash_weight"]), policy.min_cash_fraction
        )
        self.assertEqual(valuation["total_value"], "10000.00")

    async def test_sizing_never_exceeds_the_investable_fraction(self) -> None:
        portfolio_id = await self.make("medium_term", "12000.00")
        valuation = self.engine.valuation(portfolio_id)["valuation"]
        invested = Decimal(valuation["invested"])
        self.assertLessEqual(
            invested / Decimal("12000.00"), policy_for("medium_term").investable_fraction
        )

    async def test_allocate_keeps_weights_within_the_investable_fraction(self) -> None:
        rows = self.discovery.rows
        selected, _rejected = assess_candidates(
            rows,
            self._forecast_index(rows),
            self.engine._model_states(),
            policy_for("short_term"),
            NOW,
        )
        plan = allocate(selected, policy_for("short_term"))
        self.assertLessEqual(sum(plan.weights.values()), plan.investable)
        self.assertFalse(any(weight < policy_for("short_term").min_position_weight for weight in plan.weights.values()))

    def _forecast_index(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        index: dict[str, Any] = {}
        for row in rows:
            instrument_id = row["instrument_id"]
            index[instrument_id] = {
                forecast["horizon"]: forecast
                for forecast in self.forecasts.forecasts(instrument_id)["forecasts"]
            }
        return index


class SelectionGateTests(Harness):
    async def test_a_stale_forecast_is_rejected(self) -> None:
        self.forecasts.age = timedelta(days=30)
        portfolio_id = await self.make()
        self.assertEqual(self.engine.holdings(portfolio_id)["holdings"], [])
        self.assertEqual(self.engine.valuation(portfolio_id)["valuation"]["cash"], "10000.00")

    async def test_a_degraded_model_is_excluded_by_the_short_horizon(self) -> None:
        self.forecasts.state = "degraded"
        portfolio_id = await self.make("short_term")
        self.assertEqual(self.engine.holdings(portfolio_id)["holdings"], [])

    async def test_the_medium_horizon_accepts_a_degraded_model(self) -> None:
        self.forecasts.state = "degraded"
        portfolio_id = await self.make("medium_term", "12000.00")
        self.assertGreaterEqual(len(self.engine.holdings(portfolio_id)["holdings"]), 1)

    async def test_a_model_without_a_measured_state_is_rejected(self) -> None:
        self.forecasts.include_models = False
        portfolio_id = await self.make("medium_term", "12000.00")
        self.assertEqual(self.engine.holdings(portfolio_id)["holdings"], [])

    async def test_a_low_discovery_score_is_rejected(self) -> None:
        self.discovery.rows = [_Discovery._row("XNSE:GAMMA", "Energy", 10.0)]
        portfolio_id = await self.make("long_term", "15000.00")
        self.assertEqual(self.engine.holdings(portfolio_id)["holdings"], [])

    async def test_confirmed_event_pressure_excludes_only_the_short_horizon(self) -> None:
        self.radar.events = [
            {
                "instrument_id": "XNSE:ALPHA",
                "verification": "officially_confirmed",
                "importance": "high",
            }
        ]
        short_id = await self.make("short_term")
        held = {row["instrument_id"] for row in self.engine.holdings(short_id)["holdings"]}
        self.assertNotIn("XNSE:ALPHA", held)
        medium_id = await self.make("medium_term", "12000.00")
        medium_held = {
            row["instrument_id"] for row in self.engine.holdings(medium_id)["holdings"]
        }
        self.assertIn("XNSE:ALPHA", medium_held)


class ValuationTests(Harness):
    async def test_holdings_and_valuation_agree_on_one_price(self) -> None:
        """Both reads must use the newest observable price, not the fill price.

        O-1 from the PI-01…PI-24 audit: `/holdings` read the stored position
        (still at its fill price) while `/valuation` refreshed it first, so the
        same position was reported at two different prices in one read
        sequence.
        """

        portfolio_id = await self.make()
        self.market.prices["XNSE:ALPHA"] = Decimal("110.00")
        holdings = self.engine.holdings(portfolio_id)["holdings"]
        moved = {row["instrument_id"]: row for row in holdings}["XNSE:ALPHA"]
        self.assertEqual(moved["last_price"], "110.00")

        valuation = (await self.engine.valuate(portfolio_id))["valuation"]
        holdings = self.engine.holdings(portfolio_id)["holdings"]
        priced = [row for row in holdings if row["instrument_id"] == "XNSE:ALPHA"][0]
        self.assertEqual(priced["last_price"], "110.00")
        # One price per position, so the two endpoints' totals agree exactly.
        self.assertEqual(
            sum(Decimal(row["market_value"]) for row in holdings),
            Decimal(valuation["invested"]),
        )

    async def test_a_price_rise_is_reported_as_unrealized_profit(self) -> None:
        portfolio_id = await self.make()
        self.market.prices["XNSE:ALPHA"] = Decimal("110.00")
        await self.engine.valuate(portfolio_id)
        valuation = self.engine.valuation(portfolio_id)["valuation"]
        self.assertEqual(valuation["unrealized_pnl"], "250.00")
        self.assertEqual(valuation["total_value"], "10250.00")

    async def test_a_stale_price_is_marked_not_hidden(self) -> None:
        portfolio_id = await self.make()
        self.market.observed_at = NOW - timedelta(days=10)
        await self.engine.valuate(portfolio_id)
        valuation = self.engine.valuation(portfolio_id)["valuation"]
        self.assertEqual(valuation["stale_positions"], 2)
        self.assertTrue(
            any(row["stale"] for row in self.engine.holdings(portfolio_id)["holdings"])
        )

    async def test_valuations_accumulate_append_only(self) -> None:
        portfolio_id = await self.make()
        before = len(self.store.valuations(portfolio_id))
        self.now = NOW + timedelta(hours=1)
        await self.engine.valuate(portfolio_id)
        self.assertEqual(len(self.store.valuations(portfolio_id)), before + 1)

    async def test_performance_ratios_wait_for_enough_observations(self) -> None:
        portfolio_id = await self.make()
        await self.engine.valuate(portfolio_id)
        body = self.engine.performance(portfolio_id)
        metrics = body["metrics"]
        self.assertLess(metrics.get("observations", 0), 20)
        self.assertIsNone(metrics.get("sharpe", None))


class RebalanceTests(Harness):
    async def test_a_new_candidate_produces_a_validated_proposal_and_apply_buys_it(self) -> None:
        portfolio_id = await self.make()
        self.discovery.rows.append(_Discovery._row("XNSE:GAMMA", "Energy", 88.0))
        proposed = await self.engine.propose(portfolio_id, force=True)
        self.assertIsNotNone(proposed["proposal"])
        proposal = proposed["proposal"]
        self.assertEqual(proposal["status"], "validated")
        applied = await self.engine.apply(proposal["proposal_id"])
        self.assertTrue(applied["ok"])
        held = {
            row["instrument_id"] for row in self.engine.holdings(portfolio_id)["holdings"]
        }
        self.assertIn("XNSE:GAMMA", held)

    async def test_the_cooldown_skips_an_immediate_rebalance(self) -> None:
        portfolio_id = await self.make()
        skipped = await self.engine.propose(portfolio_id)
        self.assertEqual(skipped["skipped"], "cooldown")
        self.assertIsNone(skipped["proposal"])

    async def test_an_expired_proposal_is_refused_and_marked(self) -> None:
        portfolio_id = await self.make()
        self.discovery.rows.append(_Discovery._row("XNSE:GAMMA", "Energy", 88.0))
        proposal_id = (await self.engine.propose(portfolio_id, force=True))["proposal"][
            "proposal_id"
        ]
        with self.assertRaises(PortfolioStateError):
            await self.engine.apply(proposal_id, at=NOW + timedelta(days=30))
        self.assertEqual(self.store.proposal(proposal_id).status.value, "expired")

    async def test_a_failed_proposal_cannot_be_applied(self) -> None:
        portfolio_id = await self.make()
        self.discovery.rows.append(_Discovery._row("XNSE:GAMMA", "Energy", 88.0))
        proposal_id = (await self.engine.propose(portfolio_id, force=True))["proposal"][
            "proposal_id"
        ]
        self.store.set_proposal_status(proposal_id, ProposalStatus.FAILED)
        with self.assertRaises(PortfolioStateError):
            await self.engine.apply(proposal_id)

    async def test_applying_the_same_proposal_twice_is_idempotent(self) -> None:
        portfolio_id = await self.make()
        self.discovery.rows.append(_Discovery._row("XNSE:GAMMA", "Energy", 88.0))
        proposal_id = (await self.engine.propose(portfolio_id, force=True))["proposal"][
            "proposal_id"
        ]
        first = await self.engine.apply(proposal_id)
        second = await self.engine.apply(proposal_id)
        self.assertFalse(first.get("replayed", False))
        self.assertTrue(second["replayed"])

    async def test_no_candidate_leaves_existing_holdings_untouched(self) -> None:
        portfolio_id = await self.make()
        held_before = self.engine.holdings(portfolio_id)["holdings"]
        self.discovery.rows = []
        skipped = await self.engine.propose(portfolio_id, force=True)
        self.assertEqual(skipped["skipped"], "no_candidates")
        self.assertEqual(self.engine.holdings(portfolio_id)["holdings"], held_before)


class LifecycleTests(Harness):
    async def test_pause_and_resume_move_between_states(self) -> None:
        portfolio_id = await self.make()
        paused = await self.engine.set_status(portfolio_id, "pause", reason="review")
        self.assertEqual(paused["portfolio"]["status"], "paused")
        resumed = await self.engine.set_status(portfolio_id, "resume")
        self.assertEqual(resumed["portfolio"]["status"], "active")

    async def test_an_illegal_transition_is_refused(self) -> None:
        portfolio_id = await self.make()
        await self.engine.close(portfolio_id, reason="done")
        with self.assertRaises(PortfolioStateError):
            await self.engine.set_status(portfolio_id, "pause")

    async def test_close_returns_the_original_capital_to_the_funder(self) -> None:
        portfolio_id = await self.make()
        await self.engine.close(portfolio_id, reason="return the capital")
        self.assertEqual(self.funding_balance(), "40000.00")

    async def test_close_ends_the_book_and_returns_the_capital(self) -> None:
        portfolio_id = await self.make()
        closed = await self.engine.close(portfolio_id, reason="acceptance")
        self.assertEqual(closed["portfolio"]["status"], "closed")
        self.assertEqual(self.engine.status()["open_positions"], 0)
        self.assertEqual(
            self.wallets.wallet(self.portfolio_wallet(portfolio_id))["wallet"]["balance"][
                "total"
            ],
            "0.00",
        )


class LedgerTests(Harness):
    async def test_capital_is_conserved_across_every_wallet(self) -> None:
        short_id = await self.make("short_term", "10000.00")
        long_id = await self.make("long_term", "15000.00")
        total = Decimal(self.funding_balance())
        for portfolio_id in (short_id, long_id):
            wallet_id = self.portfolio_wallet(portfolio_id)
            total += Decimal(
                self.wallets.wallet(wallet_id)["wallet"]["balance"]["total"]
            )
        clearing = self.wallets.wallet("pfw_market_clearing")["wallet"]["balance"]["total"]
        total += Decimal(clearing)
        self.assertEqual(total, Decimal("40000.00"))

    async def test_reconcile_finds_no_discrepancy(self) -> None:
        portfolio_id = await self.make()
        report = self.engine.reconcile(portfolio_id)
        self.assertTrue(report["ok"])
        self.assertEqual(report["discrepancies"], [])

    async def test_the_wallet_ledger_reconciles_after_trading(self) -> None:
        await self.make()
        self.assertTrue((await self.wallets.reconcile())["ok"])


class PersistenceTests(Harness):
    async def test_creation_records_a_decision(self) -> None:
        portfolio_id = await self.make()
        kinds = [row["kind"] for row in self.engine.decisions(portfolio_id)["decisions"]]
        self.assertIn("create", kinds)
        self.assertIn("rebalance_applied", kinds)

    async def test_position_history_records_the_opening_buys(self) -> None:
        portfolio_id = await self.make()
        history = self.store.position_history(portfolio_id)
        self.assertGreaterEqual(len(history), 2)
        self.assertTrue(all(row["kind"] in {"buy", "open"} for row in history))

    async def test_state_survives_a_new_engine_over_the_same_store(self) -> None:
        portfolio_id = await self.make()
        reopened = PortfolioEngine(self.store, self.wallets, clock=lambda: NOW)
        self.assertEqual(
            reopened.detail(portfolio_id)["portfolio"]["portfolio_id"], portfolio_id
        )
        self.assertEqual(len(reopened.holdings(portfolio_id)["holdings"]), 2)


class ContractTests(Harness):
    async def test_the_contract_declares_simulation_only(self) -> None:
        contract = self.engine.contract()
        self.assertFalse(contract["executes_orders"])
        self.assertEqual(contract["phase"], "NM-07")

    async def test_status_exposes_all_three_horizons(self) -> None:
        body = self.engine.status()
        self.assertEqual(
            set(body["horizons"]), {"short_term", "medium_term", "long_term"}
        )
        self.assertEqual(body["portfolios"], 0)

    async def test_all_three_engines_are_stated_independently(self) -> None:
        policies = {row["horizon"]: row for row in self.engine.status()["horizons"].values()}
        self.assertNotEqual(policies["short_term"]["weighting"], policies["medium_term"]["weighting"])
        self.assertNotEqual(policies["medium_term"]["weighting"], policies["long_term"]["weighting"])
        self.assertNotEqual(
            policies["short_term"]["max_position_weight"],
            policies["long_term"]["max_position_weight"],
        )



try:  # pragma: no cover - optional dependency
    from fastapi.testclient import TestClient as _TestClient
except Exception:  # pragma: no cover
    _TestClient = None


@unittest.skipIf(_TestClient is None, "fastapi.testclient requires httpx")
class PortfolioApiTests(unittest.TestCase):
    """The portfolio routes through the real application and a temp data dir."""

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
            mock.patch(
                "novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner
            ),
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

    def test_status_reports_three_horizons(self) -> None:
        body = self.client.get("/market/portfolios/status").json()
        self.assertTrue(body["initialised"])
        self.assertEqual(len(body["horizons"]), 3)

    def test_the_list_starts_empty(self) -> None:
        body = self.client.get("/market/portfolios").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["total"], 0)

    def test_an_unknown_portfolio_is_a_404(self) -> None:
        response = self.client.get("/market/portfolios/pf_missing")
        self.assertEqual(response.status_code, 404)

    def test_creation_with_an_unknown_funding_wallet_is_a_422(self) -> None:
        response = self.client.post(
            "/market/portfolios",
            json={
                "horizon": "short_term",
                "funding_wallet_id": "wal_missing",
                "capital": "100.00",
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_a_funded_wallet_creates_a_portfolio_end_to_end(self) -> None:
        wallet = self.client.post("/market/wallets", json={"owner": "house"}).json()
        wallet_id = wallet["wallet"]["wallet_id"]
        deposited = self.client.post(
            f"/market/wallets/{wallet_id}/deposit", json={"amount": "5000.00"}
        )
        self.assertEqual(deposited.status_code, 200)
        created = self.client.post(
            "/market/portfolios",
            json={
                "horizon": "short_term",
                "funding_wallet_id": wallet_id,
                "capital": "2000.00",
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        self.assertEqual(body["portfolio"]["status"], "active")
        self.assertEqual(body["cash"], "2000.00")
        # Funding moved 2,000 out of the user's wallet. The portfolio's own
        # funding wallet must never be counted back into the user's balance.
        wallet_status = self.client.get("/market/wallet/status").json()
        self.assertEqual(wallet_status["summary"]["user_balance"], "3000.00")
        listed = self.client.get("/market/portfolios").json()
        self.assertEqual(listed["total"], 1)
        detail = self.client.get(
            f"/market/portfolios/{body['portfolio']['portfolio_id']}"
        ).json()
        self.assertEqual(detail["valuation"]["total_value"], "2000.00")

    def test_rebalance_on_an_unknown_portfolio_is_a_404(self) -> None:
        response = self.client.post(
            "/market/portfolios/pf_missing/rebalance", json={}
        )
        self.assertEqual(response.status_code, 404)


class PriceProvenanceTests(Harness):
    """A fixture-priced position says so; it never reads as live-priced."""

    async def test_fixture_priced_positions_are_named_in_the_book(self) -> None:
        portfolio_id = await self.make()
        live = self.engine.holdings(portfolio_id)
        self.assertTrue(live["holdings"])
        for row in live["holdings"]:
            self.assertEqual(row["price_provider"], "nse")
            self.assertEqual(row["price_from"], "quote")
            self.assertFalse(row["fixture_priced"])
        self.assertEqual(live["provenance"], {"fixture_priced": [], "note": ""})

        self.market.provider = "fixture"
        detail = self.engine.detail(portfolio_id)
        instruments = [row["instrument_id"] for row in detail["holdings"]]
        for row in detail["holdings"]:
            self.assertEqual(row["price_provider"], "fixture")
            self.assertEqual(row["price_from"], "quote")
            self.assertTrue(row["fixture_priced"])
        self.assertEqual(detail["provenance"]["fixture_priced"], instruments)
        self.assertIn("fixture data", detail["provenance"]["note"])
        self.assertIn(instruments[0], detail["provenance"]["note"])

        # A bar-only price is reported as a bar, and its provider decides the flag.
        self.market.quote_present = False
        self.market.bar_prices = [Decimal("100.00")]
        bar_only = self.engine.holdings(portfolio_id)
        for row in bar_only["holdings"]:
            self.assertEqual(row["price_from"], "bar")
            self.assertTrue(row["fixture_priced"])

        # A live provider clears the provenance: it follows the price actually used.
        self.market.provider = "NSE"
        cleared = self.engine.detail(portfolio_id)
        self.assertEqual(cleared["provenance"], {"fixture_priced": [], "note": ""})
        self.assertTrue(all(not row["fixture_priced"] for row in cleared["holdings"]))
