"""HTTP-level tests for the NovaMarket NM-01 management surface.

The real ``create_app()`` factory is used with a temp data dir and no-op device
runners, exactly like the rest of the web suite. These tests prove the routes
drive the real subsystem — not a mock that answers successfully — including the
failure path where initialization is refused and the status surface has to tell
the truth about it.
"""

from __future__ import annotations

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
    from fastapi.testclient import TestClient  # requires httpx
except Exception:  # pragma: no cover - optional dev dependency
    TestClient = None  # type: ignore[assignment]

from novacontrol.api.app import create_app
from novacontrol.api.models import ApiSurface
from novacontrol.api.route_consumers import ROUTE_CONSUMERS
from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.novamarket.prediction.models import ForecastOutput
from novacontrol.novamarket.scheduling.models import ForecastOutcome

_MARKET_ROUTES = {
    # NM-12: Shadow Mode's non-executing surface.
    ("GET", "/market/shadow/status"),
    ("GET", "/market/shadow/contracts"),
    ("POST", "/market/shadow/sessions"),
    ("GET", "/market/shadow/sessions"),
    ("GET", "/market/shadow/sessions/{session_id}"),
    ("GET", "/market/shadow/sessions/{session_id}/history"),
    ("POST", "/market/shadow/sessions/{session_id}/start"),
    ("POST", "/market/shadow/sessions/{session_id}/pause"),
    ("POST", "/market/shadow/sessions/{session_id}/resume"),
    ("POST", "/market/shadow/sessions/{session_id}/stop"),
    ("POST", "/market/shadow/sessions/{session_id}/complete"),
    ("POST", "/market/shadow/sessions/{session_id}/cancel"),
    ("POST", "/market/shadow/sessions/{session_id}/cycle"),
    ("POST", "/market/shadow/sessions/{session_id}/track-outcomes"),
    ("GET", "/market/shadow/sessions/{session_id}/decisions"),
    ("GET", "/market/shadow/sessions/{session_id}/projections"),
    ("GET", "/market/shadow/sessions/{session_id}/outcomes"),
    ("GET", "/market/shadow/sessions/{session_id}/analytics"),
    ("GET", "/market/shadow/sessions/{session_id}/errors"),
    ("GET", "/market/shadow/sessions/{session_id}/health"),
    ("POST", "/market/shadow/sessions/{session_id}/compare"),
    ("GET", "/market/shadow/sessions/{session_id}/comparisons"),
    # NM-13: controlled adaptive learning.
    ("GET", "/market/learning/status"),
    ("GET", "/market/learning/contracts"),
    ("GET", "/market/learning/dashboard"),
    ("GET", "/market/learning/health"),
    ("POST", "/market/learning/evidence"),
    ("GET", "/market/learning/evidence"),
    ("GET", "/market/learning/evidence/{evidence_id}"),
    ("POST", "/market/learning/monitor"),
    ("GET", "/market/learning/alerts"),
    ("POST", "/market/learning/alerts/{alert_id}/acknowledge"),
    ("POST", "/market/learning/experiments"),
    ("GET", "/market/learning/experiments"),
    ("GET", "/market/learning/experiments/{experiment_id}"),
    ("POST", "/market/learning/experiments/{experiment_id}/start"),
    ("POST", "/market/learning/experiments/{experiment_id}/run"),
    ("POST", "/market/learning/experiments/{experiment_id}/cancel"),
    ("GET", "/market/learning/experiments/{experiment_id}/candidates"),
    ("GET", "/market/learning/candidates/{candidate_id}"),
    ("POST", "/market/learning/candidates/{candidate_id}/evaluate"),
    ("POST", "/market/learning/candidates/{candidate_id}/approve"),
    ("POST", "/market/learning/candidates/{candidate_id}/reject"),
    ("POST", "/market/learning/candidates/{candidate_id}/shadow-validate"),
    ("POST", "/market/learning/candidates/{candidate_id}/promote"),
    ("GET", "/market/learning/strategies/{strategy_id}/versions"),
    ("GET", "/market/learning/strategies/{strategy_id}/active-version"),
    ("POST", "/market/learning/strategies/{strategy_id}/rollback"),
    ("POST", "/market/execution/orders"),
    ("GET", "/market/execution/orders"),
    ("GET", "/market/execution/orders/{order_id}"),
    ("GET", "/market/execution/orders/{order_id}/fills"),
    ("GET", "/market/execution/orders/{order_id}/history"),
    ("POST", "/market/execution/orders/{order_id}/process"),
    ("POST", "/market/execution/orders/{order_id}/cancel"),
    ("GET", "/market/execution/fills"),
    ("GET", "/market/execution/status"),
    ("GET", "/market/execution/contracts"),
    ("GET", "/market/execution/fees"),
    ("GET", "/market/execution/slippage"),
    ("POST", "/market/execution/reconcile"),
    ("GET", "/market/execution/reconciliations"),
    ("GET", "/market/backtests/status"),
    ("GET", "/market/backtests/contracts"),
    ("POST", "/market/backtests/validate"),
    ("POST", "/market/backtests"),
    ("GET", "/market/backtests"),
    ("POST", "/market/backtests/compare"),
    ("GET", "/market/backtests/comparisons"),
    ("POST", "/market/backtests/{backtest_id}/walk-forward"),
    ("POST", "/market/backtests/{backtest_id}/start"),
    ("POST", "/market/backtests/{backtest_id}/cancel"),
    ("GET", "/market/backtests/{backtest_id}"),
    ("GET", "/market/backtests/{backtest_id}/results"),
    ("GET", "/market/backtests/{backtest_id}/metrics"),
    ("GET", "/market/backtests/{backtest_id}/trades"),
    ("GET", "/market/backtests/{backtest_id}/orders"),
    ("GET", "/market/backtests/{backtest_id}/risk-decisions"),
    ("GET", "/market/backtests/{backtest_id}/signals"),
    ("GET", "/market/backtests/{backtest_id}/data-quality"),
    ("GET", "/market/backtests/{backtest_id}/verify"),
    ("POST", "/market/backtests/{backtest_id}/sensitivity"),
    # PI-11: forecast evaluation over existing forecast stores.
    ("GET", "/market/evaluations/status"),
    ("GET", "/market/evaluations/contracts"),
    ("GET", "/market/evaluations"),
    ("POST", "/market/evaluations"),
    ("GET", "/market/evaluations/{evaluation_id}"),
    ("GET", "/market/evaluations/{evaluation_id}/verify"),
    ("POST", "/market/evaluations/{evaluation_id}/reproduce"),
    ("GET", "/market/strategies/catalog"),
    ("GET", "/market/strategies"),
    ("POST", "/market/strategies"),
    ("GET", "/market/strategies/status"),
    ("GET", "/market/strategies/contracts"),
    ("GET", "/market/multi-timeframe/status"),
    ("GET", "/market/multi-timeframe/analysis"),
    ("GET", "/market/strategies/proposals"),
    ("GET", "/market/strategies/proposals/{proposal_id}"),
    ("POST", "/market/strategies/proposals/{proposal_id}/forward"),
    ("GET", "/market/strategies/conflicts"),
    ("GET", "/market/strategies/signals"),
    ("GET", "/market/strategies/signals/{signal_id}"),
    ("GET", "/market/strategies/{strategy_id}"),
    ("POST", "/market/strategies/{strategy_id}/config"),
    ("GET", "/market/strategies/{strategy_id}/versions"),
    ("POST", "/market/strategies/{strategy_id}/activate"),
    ("POST", "/market/strategies/{strategy_id}/pause"),
    ("POST", "/market/strategies/{strategy_id}/suspend"),
    ("POST", "/market/strategies/{strategy_id}/retire"),
    ("POST", "/market/strategies/{strategy_id}/degrade"),
    ("POST", "/market/strategies/{strategy_id}/evaluate"),
    ("GET", "/market/strategies/{strategy_id}/signals"),
    ("GET", "/market/strategies/{strategy_id}/performance"),
    ("GET", "/market/strategies/{strategy_id}/lifecycle"),
    ("POST", "/market/strategies/run"),
    ("POST", "/market/risk/assess"),
    ("GET", "/market/risk/status"),
    ("GET", "/market/risk/contracts"),
    ("GET", "/market/risk/decisions"),
    ("GET", "/market/risk/decisions/{decision_id}"),
    ("POST", "/market/risk/decisions/{decision_id}/apply"),
    ("GET", "/market/risk/portfolio/{portfolio_id}"),
    ("GET", "/market/risk/portfolio/{portfolio_id}/positions"),
    ("GET", "/market/risk/position/{portfolio_id}/{instrument_id}"),
    ("GET", "/market/risk/exposure"),
    ("GET", "/market/risk/policies"),
    ("POST", "/market/risk/policies/{policy_id}"),
    ("GET", "/market/risk/alerts"),
    ("POST", "/market/risk/alerts/{alert_id}/acknowledge"),
    ("POST", "/market/risk/alerts/{alert_id}/resolve"),
    ("POST", "/market/risk/stress"),
    ("GET", "/market/risk/stress"),
    ("GET", "/market/risk/overrides"),
    ("POST", "/market/risk/overrides"),
    ("GET", "/market/status"),
    ("GET", "/market/overview"),
    ("GET", "/market/contracts"),
    ("GET", "/market/config"),
    ("POST", "/market/config"),
    ("POST", "/market/notifications/test"),
    ("POST", "/market/enable"),
    ("POST", "/market/disable"),
    ("GET", "/market/autonomy/contracts"),
    ("GET", "/market/autonomy/status"),
    ("GET", "/market/autonomy/health"),
    ("GET", "/market/autonomy/reliability"),
    ("POST", "/market/autonomy/recover"),
    ("POST", "/market/autonomy/start"),
    ("POST", "/market/autonomy/pause"),
    ("POST", "/market/autonomy/resume"),
    ("POST", "/market/autonomy/stop"),
    ("GET", "/market/workspace"),
    ("GET", "/market/data/status"),
    ("GET", "/market/data/providers"),
    ("GET", "/market/instruments"),
    ("POST", "/market/instruments"),
    ("POST", "/market/instruments/lookup"),
    ("POST", "/market/universe/sync"),
    ("POST", "/market/data/ingest"),
    ("GET", "/market/data/quote/{instrument_id}"),
    ("GET", "/market/data/history/{instrument_id}"),
    ("GET", "/market/data/bars/{instrument_id}"),
    ("GET", "/market/wallet/status"),
    ("GET", "/market/wallets"),
    ("POST", "/market/wallets"),
    ("POST", "/market/wallets/transfer"),
    ("GET", "/market/wallets/{wallet_id}"),
    ("GET", "/market/wallets/{wallet_id}/transactions"),
    ("GET", "/market/wallets/{wallet_id}/ledger"),
    ("POST", "/market/wallets/{wallet_id}/deposit"),
    ("POST", "/market/wallets/{wallet_id}/withdraw"),
    ("POST", "/market/wallets/{wallet_id}/status"),
    ("GET", "/market/wallets/{wallet_id}/reconcile"),
    ("POST", "/market/wallet/reconcile"),
    ("POST", "/market/wallet/reversals"),
    ("GET", "/market/events"),
    ("GET", "/market/events/sources"),
    ("POST", "/market/events/sources/{source_id}/enabled"),
    ("POST", "/market/events/refresh"),
    ("POST", "/market/events/expire"),
    ("GET", "/market/events/instrument/{instrument_id}"),
    ("GET", "/market/events/{event_id}"),
    ("GET", "/market/events/{event_id}/evidence"),
    ("GET", "/market/events/{event_id}/history"),
    ("GET", "/market/discovery/status"),
    ("GET", "/market/discovery/candidates"),
    ("GET", "/market/discovery/candidates/{instrument_id}"),
    ("POST", "/market/discovery/cycle"),
    ("POST", "/market/discovery/reevaluate"),
    ("POST", "/market/discovery/events/{event_id}"),
    ("POST", "/market/discovery/scheduler"),
    ("GET", "/market/discovery/history"),
    ("GET", "/market/watchlists"),
    ("POST", "/market/watchlists"),
    ("GET", "/market/watchlists/{watchlist_id}"),
    ("POST", "/market/watchlists/{watchlist_id}/members"),
    ("DELETE", "/market/watchlists/{watchlist_id}/members/{instrument_id}"),
    ("POST", "/market/watchlists/{watchlist_id}/rename"),
    ("GET", "/market/screener"),
    ("GET", "/market/orderblock"),
    ("GET", "/market/orderblock/{instrument_id}"),
    ("GET", "/market/tradingwatch"),
    ("GET", "/market/tradingwatch/watchlists"),
    ("GET", "/market/tradingwatch/replay/{instrument_id}"),
    ("POST", "/market/forecasts"),
    ("GET", "/market/forecasts"),
    ("GET", "/market/forecasts/status"),
    ("GET", "/market/forecasts/models"),
    ("GET", "/market/forecasts/performance"),
    ("GET", "/market/forecasts/calibration"),
    ("POST", "/market/forecasts/train"),
    ("POST", "/market/forecasts/evaluate"),
    ("POST", "/market/forecasts/models/{model_id}/promote"),
    ("GET", "/market/forecasts/{forecast_id}"),
    ("GET", "/market/forecasts/{forecast_id}/outcome"),
    ("GET", "/market/forecasts/{forecast_id}/explain"),
    # PI-09: durable forecast lifecycle scheduling.
    ("POST", "/market/forecasts/cycles"),
    ("GET", "/market/forecasts/cycles"),
    ("GET", "/market/forecasts/cycles/{cycle_id}"),
    ("GET", "/market/forecasts/scheduler"),
    ("POST", "/market/forecasts/scheduler/tick"),
    ("GET", "/market/forecasts/missed"),
    ("GET", "/market/forecasts/evaluations/pending"),
    ("GET", "/market/forecasts/accuracy"),
    # PI-10: swing and long-term horizons.
    ("POST", "/market/forecasts/horizons"),
    ("GET", "/market/forecasts/horizons/status"),
    ("GET", "/market/forecasts/horizons/performance"),
    ("GET", "/market/forecasts/horizons/{instrument_id}/context"),
    ("GET", "/market/forecasts/horizons/{instrument_id}"),
    ("POST", "/market/portfolios"),
    ("GET", "/market/portfolios"),
    ("GET", "/market/portfolios/status"),
    ("GET", "/market/portfolios/horizons"),
    ("GET", "/market/portfolios/{portfolio_id}"),
    ("GET", "/market/portfolios/{portfolio_id}/holdings"),
    ("GET", "/market/portfolios/{portfolio_id}/valuation"),
    ("GET", "/market/portfolios/{portfolio_id}/valuations"),
    ("GET", "/market/portfolios/{portfolio_id}/performance"),
    ("GET", "/market/portfolios/{portfolio_id}/decisions"),
    ("GET", "/market/portfolios/{portfolio_id}/proposals"),
    ("GET", "/market/portfolios/{portfolio_id}/lifecycle"),
    ("GET", "/market/portfolios/{portfolio_id}/reconcile"),
    ("POST", "/market/portfolios/{portfolio_id}/rebalance"),
    ("POST", "/market/portfolios/{portfolio_id}/apply"),
    ("POST", "/market/portfolios/{portfolio_id}/status"),
    ("POST", "/market/portfolios/{portfolio_id}/close"),
    # PI-13: isolated RL research (no production execution authority).
    ("GET", "/market/rl/status"),
    ("GET", "/market/rl/contracts"),
    ("GET", "/market/rl/health"),
    ("POST", "/market/rl/environments"),
    ("GET", "/market/rl/environments"),
    ("GET", "/market/rl/environments/{environment_id}"),
    ("POST", "/market/rl/environments/{environment_id}/episodes"),
    ("GET", "/market/rl/episodes"),
    ("GET", "/market/rl/episodes/{episode_id}"),
    ("POST", "/market/rl/episodes/{episode_id}/step"),
    ("POST", "/market/rl/episodes/{episode_id}/reset"),
    ("POST", "/market/rl/episodes/{episode_id}/cancel"),
    ("GET", "/market/rl/episodes/{episode_id}/transitions"),
    ("GET", "/market/rl/episodes/{episode_id}/experiences"),
    # PI-15: isolated training and experimental policy research.
    ("GET", "/market/training/status"),
    ("GET", "/market/training/contracts"),
    ("GET", "/market/training/health"),
    ("GET", "/market/training/configurations"),
    ("POST", "/market/training/configurations"),
    ("GET", "/market/training/configurations/{config_id}"),
    ("POST", "/market/training/validate"),
    ("GET", "/market/training/runs"),
    ("POST", "/market/training/runs"),
    ("GET", "/market/training/runs/{run_id}"),
    ("GET", "/market/training/runs/{run_id}/metrics"),
    ("GET", "/market/training/runs/{run_id}/checkpoints"),
    ("POST", "/market/training/runs/{run_id}/checkpoints"),
    ("POST", "/market/training/runs/{run_id}/pause"),
    ("POST", "/market/training/runs/{run_id}/resume"),
    ("POST", "/market/training/runs/{run_id}/cancel"),
    ("POST", "/market/training/runs/{run_id}/retry"),
    ("GET", "/market/training/checkpoints/{checkpoint_id}"),
    ("POST", "/market/training/checkpoints/{checkpoint_id}/resume"),
    ("POST", "/market/training/optimize"),
    ("GET", "/market/training/searches/{search_id}"),
    ("GET", "/market/training/evaluations"),
    # PI-16: advisory adaptive-intelligence surface.
    ("GET", "/market/adaptive/status"),
    ("GET", "/market/adaptive/contracts"),
    ("GET", "/market/adaptive/health"),
    ("GET", "/market/adaptive/dashboard"),
    ("POST", "/market/adaptive/configurations"),
    ("GET", "/market/adaptive/configurations"),
    ("GET", "/market/adaptive/configurations/{config_id}"),
    ("POST", "/market/adaptive/regime/analyze"),
    ("GET", "/market/adaptive/regime/latest"),
    ("POST", "/market/adaptive/regime/annotations"),
    ("GET", "/market/adaptive/regime/annotations"),
    ("POST", "/market/adaptive/regime/evaluations"),
    ("GET", "/market/adaptive/regime/evaluations"),
    ("POST", "/market/adaptive/experiences/query"),
    ("POST", "/market/adaptive/changes/detect"),
    ("GET", "/market/adaptive/changes"),
    ("POST", "/market/adaptive/drift/detect"),
    ("GET", "/market/adaptive/drift"),
    ("POST", "/market/adaptive/degradation/monitor"),
    ("GET", "/market/adaptive/degradation"),
    ("POST", "/market/adaptive/suitability/assess"),
    ("GET", "/market/adaptive/suitability"),
    ("POST", "/market/adaptive/recommendations"),
    ("GET", "/market/adaptive/recommendations"),
    ("POST", "/market/adaptive/recommendations/{recommendation_id}/acknowledge"),
    ("POST", "/market/adaptive/retraining"),
    ("GET", "/market/adaptive/retraining"),
    ("POST", "/market/adaptive/reassessments"),
    ("GET", "/market/adaptive/reassessments"),
    ("POST", "/market/adaptive/reassessments/{reassessment_id}/run"),
    ("GET", "/market/adaptive/reassessments/{reassessment_id}"),
    ("GET", "/market/training/policies"),
    ("GET", "/market/training/policies/{policy_id}"),
    ("POST", "/market/training/policies/{policy_id}/validate"),
    ("POST", "/market/training/policies/{policy_id}/test"),
    ("POST", "/market/training/policies/{policy_id}/archive"),
    ("POST", "/market/training/compare"),
    # PI-20: unified read-only prediction intelligence dashboard.
    ("GET", "/market/intelligence/status"),
    ("GET", "/market/intelligence/contracts"),
    ("GET", "/market/intelligence/navigation"),
    ("GET", "/market/intelligence/overview"),
    ("GET", "/market/intelligence/instruments/{instrument_id}"),
    ("GET", "/market/intelligence/forecasts"),
    ("GET", "/market/intelligence/compare"),
    ("GET", "/market/intelligence/horizons/{instrument_id}"),
    ("GET", "/market/intelligence/lifecycle"),
    ("GET", "/market/intelligence/performance"),
    ("GET", "/market/intelligence/regimes"),
    ("GET", "/market/intelligence/decisions"),
    ("GET", "/market/intelligence/decisions/{cycle_id}"),
    ("GET", "/market/intelligence/explain/{subject_id}"),
    ("GET", "/market/intelligence/attribution/{forecast_id}"),
    ("GET", "/market/intelligence/evidence/{subject_id}"),
    ("GET", "/market/intelligence/research"),
    ("GET", "/market/intelligence/portfolio"),
    ("GET", "/market/intelligence/policies"),
    ("GET", "/market/intelligence/replay/{cycle_id}"),
    ("GET", "/market/intelligence/filters"),
    ("PUT", "/market/intelligence/filters"),
}


@unittest.skipIf(TestClient is None, "fastapi.testclient requires httpx")
class _MarketApiCase(unittest.TestCase):
    """Temp data dir, no-op device runners, one real application per test."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._nova: NovaControlApplication | None = None
        self._patchers = [
            mock.patch(
                "novacontrol.api.app.NovaControlApplication",
                side_effect=self._isolated_application,
            ),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch(
                "novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner
            ),
        ]
        for patcher in self._patchers:
            patcher.start()
        self._client = TestClient(create_app())
        self._client.__enter__()

    def tearDown(self) -> None:
        self._client.__exit__(None, None, None)
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()

    def _isolated_application(self, **kwargs: Any) -> NovaControlApplication:
        kwargs["data_dir"] = self._tmp.name
        application = NovaControlApplication(**kwargs)
        self._nova = application
        return application

    def _status(self) -> dict[str, Any]:
        response = self._client.get("/market/status")
        self.assertEqual(response.status_code, 200)
        return response.json()


class NovaMarketStatusApiTests(_MarketApiCase):
    def test_status_reports_the_live_subsystem(self) -> None:
        body = self._status()

        self.assertEqual(body["subsystem"], "novamarket")
        self.assertEqual(body["phase"], "NM-14")
        self.assertEqual(body["contract_version"], "1.0.0")
        self.assertEqual(body["lifecycle"], "running")
        self.assertEqual(body["health"]["state"], "ok")
        self.assertTrue(body["operational"])
        self.assertGreaterEqual(len(body["transitions"]), 4)
        self.assertEqual(body["subscriptions"]["count"], 1)
        self.assertEqual(body["permissions"]["real_money_trading"], False)
        market_data = body["market_data"]
        self.assertTrue(market_data["initialised"])
        self.assertTrue(market_data["store"]["append_only"])
        self.assertEqual(market_data["unknown_providers"], [])

    def test_capabilities_are_reported_with_honest_availability(self) -> None:
        capabilities = {
            item["capability_id"]: item for item in self._status()["capabilities"]
        }
        self.assertEqual(
            set(capabilities),
            {
                "market.status",
                "market.configure",
                "market.contracts",
                "market.read",
                "market.data.history",
                "market.data.ingest",
                "market.data.providers",
                "market.instruments.manage",
                # NM-03: wallets and the deterministic ledger.
                "market.wallet.read",
                "market.wallet.move",
                "market.ledger.reconcile",
                "market.ledger.correct",
                # NM-04: evidence-backed market events.
                "market.events.read",
                "market.events.refresh",
                "market.events.sources",
                # NM-05: discovery and watchlists.
                "market.discovery.candidates",
                "market.discovery.run",
                "market.watchlist.read",
                "market.watchlist.manage",
                # NM-06: probabilistic forecasting.
                "market.forecast.read",
                "market.forecast.run",
                "market.forecast.train",
                "market.forecast.models",
                "market.risk.assess",
                "market.risk.read",
                "market.risk.policy",
                "market.risk.governance",
                "market.strategy.read",
                "market.strategy.manage",
                "market.strategy.evaluate",
                "market.portfolio.manage",
                "market.portfolio.read",
                "market.portfolio.rebalance",
                "market.execution.submit",
                "market.execution.read",
                "market.execution.reconcile",
                "market.backtest.read",
                "market.backtest.run",
                "market.backtest.compare",
                "market.shadow.read",
                "market.shadow.manage",
                "market.shadow.run",
                "market.shadow.compare",
                "market.learning.read",
                "market.learning.run",
                "market.learning.manage",
                "market.learning.approve",
                # PI-01: prediction contracts and immutable evidence.
                "market.prediction.read",
                "market.prediction.run",
                "market.prediction.models",
                "market.prediction.evidence",
                # PI-02: versioned historical datasets.
                "market.dataset.read",
                "market.dataset.build",
                "market.dataset.manage",
                # PI-03: the controlled low-latency gateway.
                "market.gateway.read",
                "market.gateway.intraday",
                "market.gateway.manage",
                # PI-04: features and market regimes.
                "market.feature.read",
                "market.feature.generate",
                "market.feature.manage",
            },
        )
        self.assertTrue(capabilities["market.status"]["available"])
        self.assertTrue(capabilities["market.data.providers"]["available"])
        # Providers are installed, but no retrieval has been verified in this
        # fresh data directory, so reads are not advertised as proven.
        self.assertFalse(capabilities["market.read"]["available"])
        self.assertIn("verified", capabilities["market.read"]["reason"])

    def test_no_live_or_broker_capability_is_advertised(self) -> None:
        ids = {item["capability_id"] for item in self._status()["capabilities"]}
        self.assertIn("market.execution.submit", ids)
        self.assertIn("market.backtest.run", ids)
        for forbidden in ("live", "broker"):
            self.assertFalse(
                any(forbidden in capability for capability in ids), forbidden
            )


class NovaMarketContractApiTests(_MarketApiCase):
    def test_contracts_describe_the_implemented_architecture(self) -> None:
        response = self._client.get("/market/contracts")
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertEqual(len(body["roadmap"]), 14)
        self.assertEqual(body["roadmap"][0]["phase"], "NM-01")
        self.assertEqual(body["roadmap"][1]["title"], "Market Data Reliability")
        self.assertEqual(len(body["domains"]), 12)
        self.assertEqual(len(body["horizons"]), 3)
        self.assertEqual(body["money"]["exchange_rate"], "1 NC = ₹1")
        self.assertEqual(body["permissions"]["real_money_trading"], False)

        self.assertEqual(len(body["events"]["implemented"]), 196)
        implemented = {spec["name"] for spec in body["events"]["implemented"]}
        self.assertIn("novamarket.data.available", implemented)
        self.assertIn("novamarket.data.freshness_changed", implemented)
        extension = {spec["name"] for spec in body["events"]["extension"]}
        self.assertNotIn("novamarket.data.available", extension)
        self.assertNotIn("novamarket.event.detected", extension)
        self.assertIn("novamarket.market_event.discovered", implemented)
        self.assertTrue(body["market_data"]["store"]["append_only"])
        self.assertIn("yahoo", body["market_data"]["enabled_providers"])
        for key, phase in {
            "strategy": "NM-09",
            "execution": "NM-10",
            "backtesting": "NM-11",
            "shadow": "NM-12",
            "learning": "NM-13",
            "audit": "NM-14",
        }.items():
            self.assertEqual(body[key]["phase"], phase, key)
        self.assertTrue(body["audit"]["implemented"])
        self.assertTrue(body["audit"]["local_only"])

        storage = {item["data"]: item for item in body["storage"]}
        self.assertTrue(storage["historical_prices"]["append_only"])
        self.assertTrue(storage["ledger_entries"]["append_only"])

    def test_paper_execution_is_granted_but_live_execution_is_not(self) -> None:
        matrix = {
            row["operation"]: row
            for row in self._client.get("/market/contracts").json()["permissions"]["matrix"]
        }
        self.assertTrue(matrix["market.status.read"]["granted"])
        self.assertTrue(matrix["market.config.manage"]["granted"])
        self.assertTrue(matrix["market.portfolio.simulate"]["granted"])
        self.assertTrue(matrix["market.trade.paper"]["granted"])
        for operation in (
            "market.config.sensitive.read",
            "market.trade.live",
        ):
            self.assertFalse(matrix[operation]["granted"], operation)
        self.assertEqual(matrix["market.trade.live"]["risk"], "critical")
        self.assertTrue(matrix["market.trade.live"]["destructive"])


class NovaMarketConfigApiTests(_MarketApiCase):
    def test_get_returns_the_configuration_and_schema_without_secrets(self) -> None:
        response = self._client.get("/market/config")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["valid"])
        self.assertEqual(body["config"]["mode"], "observe")
        fields = {row["field"] for row in body["schema"]}
        self.assertIn("credentials_ref", fields)
        self.assertNotIn("api_key", fields)
        self.assertNotIn("api_key", body["config"])

    def test_a_partial_update_changes_only_the_named_field(self) -> None:
        response = self._client.post(
            "/market/config", json={"stale_after_seconds": 1234}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["changed"], ["stale_after_seconds"])

        body = self._client.get("/market/config").json()
        self.assertEqual(body["config"]["stale_after_seconds"], 1234)
        self.assertEqual(body["config"]["mode"], "observe")
        self.assertEqual(body["config"]["resources"]["max_assets"], 50)

    def test_a_nested_partial_update_preserves_the_other_limits(self) -> None:
        self._client.post(
            "/market/config",
            json={"resources": {"max_assets": 10}},
        )
        config = self._client.get("/market/config").json()["config"]
        self.assertEqual(config["resources"]["max_assets"], 10)
        self.assertEqual(config["resources"]["max_quotes_per_asset"], 500)

    def test_enabled_false_disables_and_enabled_true_restores(self) -> None:
        disabled = self._client.post("/market/config", json={"enabled": False})
        self.assertEqual(disabled.status_code, 200)
        self.assertEqual(disabled.json()["status"]["lifecycle"], "disabled")
        self.assertEqual(self._status()["lifecycle"], "disabled")

        enabled = self._client.post("/market/config", json={"enabled": True})
        self.assertEqual(enabled.status_code, 200)
        self.assertEqual(enabled.json()["status"]["lifecycle"], "running")

    def test_invalid_configuration_is_422_with_the_reason(self) -> None:
        cases = (
            ({"mode": "live"}, "out of scope"),
            ({"mode": "paper"}, "NM-10"),
            ({"api_key": "x"}, "credentials_ref"),
            ({"not_a_field": 1}, "unknown configuration field"),
            ({"stale_after_seconds": 0}, "between 1 and 604800"),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                response = self._client.post("/market/config", json=payload)
                self.assertEqual(response.status_code, 422)
                self.assertIn(expected, response.json()["detail"])


class NovaMarketWorkspaceApiTests(_MarketApiCase):
    """NM-14: the workspace is real, live and integrated with the same state."""

    def test_the_workspace_page_is_served_without_fabricated_values(self) -> None:
        page = self._client.get("/market/workspace")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("NovaMarket", page.text)
        self.assertIn("Market Intelligence", page.text)
        self.assertIn("Adaptive Learning", page.text)
        self.assertIn("market-workspace.js", page.text)
        self.assertNotIn("__NC_ASSET_VERSION__", page.text)
        self.assertEqual(
            page.headers.get("cache-control"),
            "no-cache, no-store, max-age=0, must-revalidate",
        )

    def test_status_and_overview_survive_one_component_read_failure(self) -> None:
        assert self._nova is not None
        service = self._nova.novamarket

        def refuse() -> dict[str, Any]:
            raise RuntimeError("strategy store unavailable")

        service.strategy_status = refuse  # type: ignore[method-assign]
        status = self._client.get("/market/status")
        self.assertEqual(status.status_code, 200, status.text)
        body = status.json()
        self.assertFalse(body["strategy"]["initialised"])
        self.assertIn("strategy store unavailable", body["strategy"]["detail"])
        overview = self._client.get("/market/overview")
        self.assertEqual(overview.status_code, 200, overview.text)
        self.assertEqual(overview.json()["phase"], "NM-14")

    def test_activity_and_audit_survive_disabled_event_emission(self) -> None:
        assert self._nova is not None
        self._nova.novamarket._publisher.emit_enabled = False
        wallet_response = self._client.post(
            "/market/wallets", json={"owner": "quiet-mode-user"}
        )
        self.assertEqual(wallet_response.status_code, 200, wallet_response.text)
        wallet_id = wallet_response.json()["wallet"]["wallet_id"]
        deposit = self._client.post(
            f"/market/wallets/{wallet_id}/deposit", json={"amount": "10.00"}
        )
        self.assertEqual(deposit.status_code, 200, deposit.text)
        body = self._client.get("/market/overview").json()
        self.assertIn(
            "novamarket.wallet.deposit_completed",
            {event["type"] for event in body["activity"]},
        )
        self.assertTrue(body["audit"]["available"])
        self.assertGreaterEqual(body["audit"]["records"], 1)

    def test_overview_reflects_the_live_subsystem_and_wallet(self) -> None:
        wallet_response = self._client.post("/market/wallets", json={"owner": "overview-user"})
        self.assertEqual(wallet_response.status_code, 200, wallet_response.text)
        wallet_id = wallet_response.json()["wallet"]["wallet_id"]
        deposit = self._client.post(
            f"/market/wallets/{wallet_id}/deposit", json={"amount": "125.00"}
        )
        self.assertEqual(deposit.status_code, 200, deposit.text)

        response = self._client.get("/market/overview")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["phase"], "NM-14")
        self.assertEqual(body["summary"]["lifecycle"], "running")
        self.assertEqual(body["summary"]["wallet_balance_inr"], "125.00")
        self.assertFalse(body["safety"]["real_money_trading"])
        self.assertFalse(body["safety"]["broker_connected"])
        self.assertTrue(body["audit"]["available"])
        self.assertGreaterEqual(body["audit"]["records"], 1)
        event_types = {event["type"] for event in body["activity"]}
        self.assertIn("novamarket.wallet.deposit_completed", event_types)
        self.assertIn("wallets", body["recent"])

    def test_overview_reports_empty_portfolio_aggregates_honestly(self) -> None:
        body = self._client.get("/market/overview").json()
        portfolios = body["portfolios"]
        self.assertTrue(portfolios["available"])
        self.assertEqual(portfolios["count"], 0)
        self.assertEqual(portfolios["by_horizon"], [])
        self.assertEqual(body["summary"]["portfolios"], 0)
        self.assertEqual(body["summary"]["portfolio_value_inr"], "0.00")
        self.assertEqual(body["summary"]["portfolio_cash_inr"], "0.00")

    def test_overview_aggregates_portfolio_valuations_without_mixing_ledgers(self) -> None:
        wallet = self._client.post(
            "/market/wallets", json={"owner": "aggregate-user"}
        ).json()
        wallet_id = wallet["wallet"]["wallet_id"]
        deposited = self._client.post(
            f"/market/wallets/{wallet_id}/deposit", json={"amount": "5000.00"}
        )
        self.assertEqual(deposited.status_code, 200, deposited.text)
        created = self._client.post(
            "/market/portfolios",
            json={
                "horizon": "short_term",
                "funding_wallet_id": wallet_id,
                "capital": "2000.00",
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        portfolio_id = created.json()["portfolio"]["portfolio_id"]
        detail = self._client.get(f"/market/portfolios/{portfolio_id}").json()
        valuation = detail["valuation"]

        body = self._client.get("/market/overview").json()
        summary = body["summary"]
        aggregate = body["portfolios"]
        self.assertTrue(aggregate["available"])
        self.assertEqual(aggregate["count"], 1)
        self.assertEqual(summary["portfolios"], 1)
        # The aggregate is the portfolio's own valuation, not a re-derivation.
        self.assertEqual(summary["portfolio_value_inr"], valuation["total_value"])
        self.assertEqual(summary["portfolio_cash_inr"], valuation["cash"])
        self.assertEqual(aggregate["total_value_inr"], valuation["total_value"])
        self.assertEqual(aggregate["rows"][0]["portfolio_id"], portfolio_id)
        self.assertEqual(aggregate["rows"][0]["horizon"], "short_term")
        # The wallet balance is reported separately and is never the portfolio total.
        self.assertNotEqual(
            summary["wallet_balance_inr"], summary["portfolio_value_inr"]
        )
        horizons = {row["horizon"]: row for row in aggregate["by_horizon"]}
        self.assertEqual(horizons["short_term"]["count"], 1)
        self.assertEqual(
            horizons["short_term"]["total_value_inr"], valuation["total_value"]
        )


class MultiAgentApiTests(_MarketApiCase):
    """PI-17's HTTP surface drives the coordinator without financial mutation."""

    def test_cycle_is_advisory_and_leaves_financial_state_untouched(self) -> None:
        assert self._nova is not None
        wallet_before = self._nova.novamarket.wallet_status()
        portfolios_before = self._nova.novamarket.portfolio_status()

        status = self._client.get("/market/multiagent/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["phase"], "PI-17")
        self.assertTrue(status.json()["advisory_only"])
        self.assertFalse(status.json()["production_authority"])

        created = self._client.post(
            "/market/multiagent/cycles",
            json={
                "instrument_id": "XNSE:ALPHA",
                "exchange": "XNSE",
                "horizon": "5d",
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        cycle_id = created.json()["cycle"]["cycle_id"]

        run = self._client.post(f"/market/multiagent/cycles/{cycle_id}/run", json={})
        self.assertEqual(run.status_code, 200, run.text)
        body = run.json()
        self.assertIn(body["cycle"]["status"], {"completed", "degraded"})
        self.assertEqual(body["decision"]["action"], "no_action")
        self.assertIsNone(body["decision"]["confidence"])

        handoff = self._client.get(
            f"/market/multiagent/cycles/{cycle_id}/handoff"
        )
        self.assertEqual(handoff.status_code, 200, handoff.text)
        self.assertTrue(handoff.json()["advisory_only"])
        self.assertFalse(handoff.json()["production_authority"])
        self.assertFalse(handoff.json()["order_submitted"])

        evidence = self._client.get(
            f"/market/multiagent/cycles/{cycle_id}/evidence"
        )
        self.assertEqual(evidence.status_code, 200, evidence.text)
        self.assertGreaterEqual(evidence.json()["count"], 0)
        missing = self._client.get("/market/multiagent/cycles/not_a_cycle")
        self.assertEqual(missing.status_code, 404)

        self.assertEqual(self._nova.novamarket.wallet_status(), wallet_before)
        self.assertEqual(self._nova.novamarket.portfolio_status(), portfolios_before)


class NovaMarketMultiTimeframeApiTests(_MarketApiCase):
    def test_status_is_read_only_and_names_the_authorities(self) -> None:
        response = self._client.get("/market/multi-timeframe/status")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["authority"]["executes"])
        self.assertFalse(body["authority"]["approves"])
        self.assertEqual(body["timeframes"]["context"], "20m")
        self.assertEqual(body["config"]["minimum_confidence"], 0.55)

    def test_analysis_refuses_an_unknown_portfolio_without_a_500(self) -> None:
        response = self._client.get(
            "/market/multi-timeframe/analysis?portfolio_id=not_a_portfolio"
        )
        self.assertIn(response.status_code, {404, 409, 422})


class NovaMarketCorsTests(unittest.TestCase):
    """CORS is opt-in, exact and never a wildcard."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._patchers = [
            mock.patch(
                "novacontrol.api.app.NovaControlApplication",
                side_effect=self._build_application,
            ),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch(
                "novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner
            ),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()

    def _build_application(self, **kwargs: Any) -> NovaControlApplication:
        kwargs["data_dir"] = self._tmp.name
        return NovaControlApplication(**kwargs)

    def _client(self, origins: str | None) -> Any:
        environment = (
            {}
            if origins is None
            else {
                "NOVAMARKET_CORS_ORIGINS": origins,
                # An exposed CORS origin is a non-loopback deployment and now
                # requires the API credential before the app may start.
                "NOVACONTROL_API_TOKEN": "c" * 40,
            }
        )
        with mock.patch.dict(os.environ, environment, clear=False):
            if origins is None:
                os.environ.pop("NOVAMARKET_CORS_ORIGINS", None)
            return TestClient(
                create_app(), headers={"Authorization": f"Bearer {'c' * 40}"}
                if origins is not None
                else {}
            )

    def test_no_environment_variable_means_no_cross_origin_access(self) -> None:
        client = self._client(None)
        with client:
            response = client.get(
                "/market/overview", headers={"Origin": "https://market.example.com"}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("access-control-allow-origin", response.headers)

    def test_an_allowlisted_origin_receives_an_exact_preflight_answer(self) -> None:
        client = self._client("https://market.example.com")
        with client:
            response = client.options(
                "/market/overview",
                headers={
                    "Origin": "https://market.example.com",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "Authorization",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.headers.get("access-control-allow-origin"),
                "https://market.example.com",
            )
            self.assertIn("GET", response.headers.get("access-control-allow-methods", ""))
            self.assertNotEqual(
                response.headers.get("access-control-allow-origin"), "*"
            )

    def test_an_unlisted_origin_is_not_allowed(self) -> None:
        client = self._client("https://market.example.com")
        with client:
            response = client.get(
                "/market/overview", headers={"Origin": "https://untrusted.example"}
            )
            self.assertNotIn("access-control-allow-origin", response.headers)


class NovaMarketLifecycleApiTests(_MarketApiCase):
    def test_disable_withdraws_and_enable_restores(self) -> None:
        assert self._nova is not None
        self.assertIsNotNone(self._nova.capabilities.get("market.status"))

        response = self._client.post("/market/disable", json={"reason": "test"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["lifecycle"], "disabled")
        self.assertIsNone(self._nova.capabilities.get("market.status"))
        self.assertIsNone(self._nova.risk.declared_for("market.config.manage"))
        self.assertEqual(
            self._nova.event_bus.subscriber_count(
                "novamarket.configuration_requested"
            ),
            0,
        )

        restored = self._client.post("/market/enable", json={"reason": "test"})
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["lifecycle"], "running")
        self.assertIsNotNone(self._nova.capabilities.get("market.status"))

    def test_initialization_failure_is_500_and_status_reports_error(self) -> None:
        assert self._nova is not None
        self._client.post("/market/disable", json={})

        def refuse() -> None:
            raise RuntimeError("market data provider refused")

        self._nova.novamarket._validators = (refuse,)

        response = self._client.post("/market/enable", json={})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "Internal server error.")
        self.assertEqual(response.json()["error"], "internal_server_error")
        self.assertTrue(response.headers.get("x-request-id"))
        self.assertNotIn("market data provider refused", response.text)

        status = self._status()
        self.assertEqual(status["lifecycle"], "error")
        self.assertEqual(status["health"]["state"], "failing")
        self.assertIn("market data provider refused", status["last_error"])
        self.assertIsNone(self._nova.capabilities.get("market.status"))

        # The failure is recoverable once the dependency exists.
        self._nova.novamarket._validators = ()
        recovered = self._client.post("/market/enable", json={})
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.json()["lifecycle"], "running")

    def test_the_subsystem_survives_disable_enable_cycles_without_duplication(self) -> None:
        for _ in range(3):
            self.assertEqual(
                self._client.post("/market/disable", json={}).json()["lifecycle"],
                "disabled",
            )
            self.assertEqual(
                self._client.post("/market/enable", json={}).json()["lifecycle"],
                "running",
            )
        assert self._nova is not None
        self.assertEqual(
            self._nova.event_bus.subscriber_count(
                "novamarket.configuration_requested"
            ),
            1,
        )
        self.assertEqual(len(self._status()["capabilities"]), 60)


@unittest.skipIf(TestClient is None, "fastapi.testclient requires httpx")
class PredictionEvaluationApiTests(_MarketApiCase):
    """PI-11's evaluation surface persists and verifies explicit evidence."""

    @staticmethod
    def _record(
        index: int,
        predicted: str = "101",
        *,
        model_id: str = "fixture_model",
        ensemble_id: str = "",
    ) -> dict[str, Any]:
        generated = datetime(2026, 1, 5 + index, 10, 0, tzinfo=UTC)
        return {
            "forecast_id": f"api_fx_{index}",
            "source": "records",
            "origin": "synthetic_fixture",
            "instrument_id": "XNSE:ALPHA",
            "horizon": "5d",
            "generated_at": generated.isoformat(),
            "target_at": (generated + timedelta(days=5)).isoformat(),
            "reference_at": generated.isoformat(),
            "actual_observed_at": (generated + timedelta(days=5)).isoformat(),
            "reference_price": 100,
            "predicted_price": predicted,
            "actual_price": 100,
            "predicted_return": 0.01,
            "actual_return": 0.0,
            "predicted_direction": "up",
            "p_up": 0.6,
            "p_down": 0.3,
            "p_neutral": 0.1,
            "probability_status": "calibrated",
            "interval_lower": -0.05,
            "interval_upper": 0.08,
            "model_id": model_id,
            "ensemble_id": ensemble_id,
            "dataset_version": "fixture-ds-1",
            "feature_set_version": "fixture-fs-1",
        }

    def test_evaluation_create_verify_and_reproduce(self) -> None:
        payload = {
            "name": "HTTP fixture evaluation",
            "sources": ["records"],
            "record_origin": "synthetic_fixture",
            "as_of": datetime(2026, 2, 1, tzinfo=UTC).isoformat(),
            "min_samples": 2,
            "records": [self._record(0), self._record(1, "99")],
        }
        created = self._client.post("/market/evaluations", json=payload)
        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        self.assertEqual(body["samples"], 2)
        self.assertEqual(body["phase"], "PI-11")
        self.assertEqual(body["source_summary"]["counts"]["records"], 2)
        self.assertIn("price", body["metrics"])
        self.assertFalse(body["metrics"]["models"]["automatic_promotion"])

        listing = self._client.get("/market/evaluations")
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["total"], 1)
        status = self._client.get("/market/evaluations/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["evaluations"]["total"], 1)

        detail = self._client.get(f"/market/evaluations/{body['evaluation_id']}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["report_hash"], body["report_hash"])

        verify = self._client.get(
            f"/market/evaluations/{body['evaluation_id']}/verify"
        )
        self.assertEqual(verify.status_code, 200, verify.text)
        self.assertTrue(verify.json()["ok"])

        before_replay = self._client.get("/market/evaluations/status").json()
        replay = self._client.post(
            f"/market/evaluations/{body['evaluation_id']}/reproduce"
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["ok"], replay.text)
        self.assertTrue(replay.json()["same_data_digest"])
        after_replay = self._client.get("/market/evaluations/status").json()
        self.assertEqual(
            after_replay["evaluations"]["total"],
            before_replay["evaluations"]["total"],
        )

    def test_string_source_filter_is_normalized(self) -> None:
        created = self._client.post(
            "/market/evaluations",
            json={
                "sources": "records",
                "as_of": datetime(2026, 2, 1, tzinfo=UTC).isoformat(),
                "partial": 1,
                "records": [self._record(0), self._record(1, "99")],
            },
        )
        # Unknown config fields are still rejected before evaluation.
        self.assertEqual(created.status_code, 422, created.text)
        created = self._client.post(
            "/market/evaluations",
            json={
                "sources": "records",
                "as_of": datetime(2026, 2, 1, tzinfo=UTC).isoformat(),
                "records": [self._record(0), self._record(1, "99")],
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["samples"], 2)

    def test_model_and_ensemble_filters_are_persisted_and_applied(self) -> None:
        model_payload = {
            "sources": ["records"],
            "record_origin": "synthetic_fixture",
            "as_of": datetime(2026, 2, 1, tzinfo=UTC).isoformat(),
            "models": ["model_a"],
            "records": [
                self._record(0, model_id="model_a"),
                self._record(1, "99", model_id="model_b"),
            ],
        }
        created = self._client.post("/market/evaluations", json=model_payload)
        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        self.assertEqual(body["samples"], 1)
        self.assertEqual(body["config"]["models"], ["model_a"])
        self.assertEqual(body["records_sample"][0]["model_id"], "model_a")

        ensemble_payload = {
            "sources": ["records"],
            "record_origin": "synthetic_fixture",
            "as_of": datetime(2026, 2, 1, tzinfo=UTC).isoformat(),
            "models": ["ens_api"],
            "records": [
                self._record(0, model_id="", ensemble_id="ens_api"),
                self._record(1, "99", model_id="", ensemble_id="ens_other"),
            ],
        }
        created = self._client.post("/market/evaluations", json=ensemble_payload)
        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        self.assertEqual(body["samples"], 1)
        self.assertEqual(body["config"]["models"], ["ens_api"])
        self.assertEqual(body["records_sample"][0]["ensemble_id"], "ens_api")

    def test_invalid_configuration_is_422_and_unknown_is_404(self) -> None:
        bad = self._client.post(
            "/market/evaluations",
            json={"sources": ["records"], "unknown_field": 1},
        )
        self.assertEqual(bad.status_code, 422, bad.text)
        empty_sources = self._client.post(
            "/market/evaluations", json={"sources": []}
        )
        self.assertEqual(empty_sources.status_code, 422, empty_sources.text)
        missing = self._client.get("/market/evaluations/pev_missing")
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_status_contract_and_authority_are_explicit(self) -> None:
        status = self._client.get("/market/evaluations/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["authority"]["execution_authority"], "NM-10")
        contract = self._client.get("/market/evaluations/contracts")
        self.assertEqual(contract.status_code, 200, contract.text)
        self.assertEqual(contract.json()["phase"], "PI-11")
        self.assertTrue(contract.json()["authority"]["paper_only"])


class AdaptiveIntelligenceApiTests(_MarketApiCase):
    """The PI-16 API is real, authenticated and advisory-only."""

    def test_status_is_available_before_initialisation(self) -> None:
        response = self._client.get("/market/adaptive/status")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["phase"], "PI-16")
        self.assertFalse(body["production_authority"])
        self.assertFalse(body["activation_available"])

    def test_drift_detection_persists_evidence(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        reference = [
            {
                "timestamp": (start + timedelta(days=index)).isoformat(),
                "returns": 0.001,
                "features": {"rsi_14": 50.0},
            }
            for index in range(20)
        ]
        current = [
            {
                "timestamp": (start + timedelta(days=30 + index)).isoformat(),
                "returns": 0.03,
                "features": {"rsi_14": 80.0},
            }
            for index in range(20)
        ]
        response = self._client.post(
            "/market/adaptive/drift/detect",
            json={
                "scope": "XNSE:ALPHA",
                "kind": "data",
                "as_of": (start + timedelta(days=80)).isoformat(),
                "reference": reference,
                "current": current,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["drift"]["status"], "data_drift")
        listing = self._client.get("/market/adaptive/drift?limit=10")
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertGreaterEqual(listing.json()["total"], 1)

    def test_forbidden_automatic_promotion_is_422(self) -> None:
        response = self._client.post(
            "/market/adaptive/configurations",
            json={"automatic_promotion": True},
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_unknown_reassessment_is_404(self) -> None:
        response = self._client.get("/market/adaptive/reassessments/ada_missing")
        self.assertEqual(response.status_code, 404, response.text)

    def test_no_activation_route_exists(self) -> None:
        paths = {
            route.path
            for route in ApiSurface.default().routes
            if route.path.startswith("/market/adaptive/")
        }
        for forbidden in (
            "/market/adaptive/activate",
            "/market/adaptive/promote",
            "/market/adaptive/orders",
            "/market/adaptive/execute",
        ):
            self.assertNotIn(forbidden, paths)


class NovaMarketRouteContractTests(unittest.TestCase):
    """The management routes are declared exactly once in both registries."""

    def test_every_market_route_is_on_the_api_surface(self) -> None:
        routes = {(route.method, route.path) for route in ApiSurface.default().routes}
        self.assertEqual(routes & _MARKET_ROUTES, _MARKET_ROUTES)
        self.assertEqual(len(_MARKET_ROUTES), 341)

    def test_every_market_route_declares_a_non_render_consumer(self) -> None:
        for route in _MARKET_ROUTES:
            with self.subTest(route=route):
                self.assertEqual(ROUTE_CONSUMERS[route], "no-render")

    def test_no_live_execution_route_exists(self) -> None:
        paths = {
            route.path
            for route in ApiSurface.default().routes
            if route.path.startswith("/market/")
        }
        self.assertEqual(
            paths,
            {
                "/market/status",
                "/market/adaptive/status",
                "/market/adaptive/contracts",
                "/market/adaptive/health",
                "/market/adaptive/dashboard",
                "/market/adaptive/configurations",
                "/market/adaptive/configurations/{config_id}",
                "/market/adaptive/regime/analyze",
                "/market/adaptive/regime/latest",
                "/market/adaptive/regime/annotations",
                "/market/adaptive/regime/evaluations",
                "/market/adaptive/experiences/query",
                "/market/adaptive/changes/detect",
                "/market/adaptive/changes",
                "/market/adaptive/drift/detect",
                "/market/adaptive/drift",
                "/market/adaptive/degradation/monitor",
                "/market/adaptive/degradation",
                "/market/adaptive/suitability/assess",
                "/market/adaptive/suitability",
                "/market/adaptive/recommendations",
                "/market/adaptive/recommendations/{recommendation_id}/acknowledge",
                "/market/adaptive/retraining",
                "/market/adaptive/reassessments",
                "/market/adaptive/reassessments/{reassessment_id}/run",
                "/market/adaptive/reassessments/{reassessment_id}",
                "/market/multiagent/status",
                "/market/multiagent/contracts",
                "/market/multiagent/health",
                "/market/multiagent/agents",
                "/market/multiagent/agents/{agent_id}",
                "/market/multiagent/agents/{agent_id}/config",
                "/market/multiagent/cycles",
                "/market/multiagent/cycles/{cycle_id}",
                "/market/multiagent/cycles/{cycle_id}/agents",
                "/market/multiagent/cycles/{cycle_id}/run",
                "/market/multiagent/cycles/{cycle_id}/cancel",
                "/market/multiagent/cycles/{cycle_id}/evidence",
                "/market/multiagent/cycles/{cycle_id}/conflicts",
                "/market/multiagent/cycles/{cycle_id}/decision",
                "/market/multiagent/cycles/{cycle_id}/handoff",
                "/market/multiagent/history",
                "/market/multiagent/performance",
                "/market/multiagent/performance/{agent_id}",
                "/market/optimization/status",
                "/market/optimization/contracts",
                "/market/optimization/health",
                "/market/optimization/configs",
                "/market/optimization/configs/{config_id}",
                "/market/optimization/validate",
                "/market/optimization/runs",
                "/market/optimization/history",
                "/market/optimization/latest",
                "/market/optimization/runs/{run_id}",
                "/market/optimization/runs/{run_id}/allocations",
                "/market/optimization/runs/{run_id}/diversification",
                "/market/optimization/runs/{run_id}/risk",
                "/market/optimization/runs/{run_id}/liquidity",
                "/market/optimization/runs/{run_id}/rebalancing",
                "/market/optimization/runs/{run_id}/scenarios",
                "/market/optimization/runs/{run_id}/backtests",
                "/market/optimization/runs/{run_id}/handoff",
                "/market/optimization/runs/{run_id}/cancel",
                "/market/optimization/backtests",
                "/market/optimization/backtests/{backtest_id}",
                "/market/intelligence/status",
                "/market/intelligence/contracts",
                "/market/intelligence/navigation",
                "/market/intelligence/overview",
                "/market/intelligence/instruments/{instrument_id}",
                "/market/intelligence/forecasts",
                "/market/intelligence/compare",
                "/market/intelligence/horizons/{instrument_id}",
                "/market/intelligence/lifecycle",
                "/market/intelligence/performance",
                "/market/intelligence/regimes",
                "/market/intelligence/decisions",
                "/market/intelligence/decisions/{cycle_id}",
                "/market/intelligence/explain/{subject_id}",
                "/market/intelligence/attribution/{forecast_id}",
                "/market/intelligence/evidence/{subject_id}",
                "/market/intelligence/research",
                "/market/intelligence/portfolio",
                "/market/intelligence/policies",
                "/market/intelligence/replay/{cycle_id}",
                "/market/intelligence/filters",
                "/market/research/status",
                "/market/research/contracts",
                "/market/research/health",
                "/market/research/runs",
                "/market/research/runs/{run_id}",
                "/market/research/runs/{run_id}/run",
                "/market/research/runs/{run_id}/cancel",
                "/market/research/runs/{run_id}/refresh",
                "/market/research/runs/{run_id}/company",
                "/market/research/runs/{run_id}/financials",
                "/market/research/runs/{run_id}/events",
                "/market/research/runs/{run_id}/news",
                "/market/research/runs/{run_id}/sector",
                "/market/research/runs/{run_id}/peers",
                "/market/research/runs/{run_id}/valuation",
                "/market/research/runs/{run_id}/thesis",
                "/market/research/runs/{run_id}/evidence",
                "/market/research/runs/{run_id}/claims",
                "/market/research/runs/{run_id}/features",
                "/market/research/runs/{run_id}/handoff",
                "/market/research/history",
                "/market/research/latest",
                "/market/research/monitor",
                "/market/overview",
                "/market/contracts",
                "/market/config",
                "/market/notifications/test",
                "/market/universe/sync",
                "/market/enable",
                "/market/disable",
                "/market/workspace",
                "/market/data/status",
                "/market/data/providers",
                "/market/instruments",
                "/market/instruments/lookup",
                "/market/data/ingest",
                "/market/data/quote/{instrument_id}",
                "/market/data/history/{instrument_id}",
                "/market/data/bars/{instrument_id}",
                "/market/screener",
                "/market/orderblock",
                "/market/orderblock/{instrument_id}",
                "/market/tradingwatch",
                "/market/tradingwatch/watchlists",
                "/market/tradingwatch/replay/{instrument_id}",
                "/market/watchlists/{watchlist_id}/rename",
                "/market/wallet/status",
                "/market/wallets",
                "/market/wallets/transfer",
                "/market/wallets/{wallet_id}",
                "/market/wallets/{wallet_id}/transactions",
                "/market/wallets/{wallet_id}/ledger",
                "/market/wallets/{wallet_id}/deposit",
                "/market/wallets/{wallet_id}/withdraw",
                "/market/wallets/{wallet_id}/status",
                "/market/wallets/{wallet_id}/reconcile",
                "/market/wallet/reconcile",
                "/market/wallet/reversals",
                "/market/events",
                "/market/events/sources",
                "/market/events/sources/{source_id}/enabled",
                "/market/events/refresh",
                "/market/events/expire",
                "/market/events/instrument/{instrument_id}",
                "/market/events/{event_id}",
                "/market/events/{event_id}/evidence",
                "/market/events/{event_id}/history",
                "/market/discovery/status",
                "/market/discovery/candidates",
                "/market/discovery/candidates/{instrument_id}",
                "/market/discovery/cycle",
                "/market/discovery/reevaluate",
                "/market/discovery/events/{event_id}",
                "/market/discovery/scheduler",
                "/market/discovery/history",
                "/market/watchlists",
                "/market/watchlists/{watchlist_id}",
                "/market/watchlists/{watchlist_id}/members",
                "/market/watchlists/{watchlist_id}/members/{instrument_id}",
                "/market/forecasts",
                "/market/forecasts/status",
                "/market/forecasts/models",
                "/market/forecasts/performance",
                "/market/forecasts/calibration",
                "/market/forecasts/train",
                "/market/forecasts/evaluate",
                "/market/forecasts/models/{model_id}/promote",
                "/market/forecasts/{forecast_id}",
                "/market/forecasts/{forecast_id}/outcome",
                "/market/forecasts/{forecast_id}/explain",
                "/market/forecasts/cycles",
                "/market/forecasts/cycles/{cycle_id}",
                "/market/forecasts/scheduler",
                "/market/forecasts/scheduler/tick",
                "/market/forecasts/missed",
                "/market/forecasts/evaluations/pending",
                "/market/forecasts/accuracy",
                "/market/forecasts/horizons",
                "/market/forecasts/horizons/status",
                "/market/forecasts/horizons/performance",
                "/market/forecasts/horizons/{instrument_id}/context",
                "/market/forecasts/horizons/{instrument_id}",
                "/market/portfolios",
                "/market/portfolios/status",
                "/market/portfolios/horizons",
                "/market/portfolios/{portfolio_id}",
                "/market/portfolios/{portfolio_id}/holdings",
                "/market/portfolios/{portfolio_id}/valuation",
                "/market/portfolios/{portfolio_id}/valuations",
                "/market/portfolios/{portfolio_id}/performance",
                "/market/portfolios/{portfolio_id}/decisions",
                "/market/portfolios/{portfolio_id}/proposals",
                "/market/portfolios/{portfolio_id}/lifecycle",
                "/market/portfolios/{portfolio_id}/reconcile",
                "/market/portfolios/{portfolio_id}/rebalance",
                "/market/portfolios/{portfolio_id}/apply",
                "/market/portfolios/{portfolio_id}/status",
                "/market/portfolios/{portfolio_id}/close",
                "/market/risk/assess",
                "/market/risk/status",
                "/market/risk/contracts",
                "/market/risk/decisions",
                "/market/risk/decisions/{decision_id}",
                "/market/risk/decisions/{decision_id}/apply",
                "/market/risk/portfolio/{portfolio_id}",
                "/market/risk/portfolio/{portfolio_id}/positions",
                "/market/risk/position/{portfolio_id}/{instrument_id}",
                "/market/risk/exposure",
                "/market/risk/policies",
                "/market/risk/policies/{policy_id}",
                "/market/risk/alerts",
                "/market/risk/alerts/{alert_id}/acknowledge",
                "/market/risk/alerts/{alert_id}/resolve",
                "/market/risk/stress",
                "/market/risk/stress",
                "/market/risk/overrides",
                "/market/strategies/catalog",
                "/market/strategies",
                "/market/strategies",
                "/market/strategies/status",
                "/market/strategies/contracts",
                "/market/multi-timeframe/status",
                "/market/multi-timeframe/analysis",
                "/market/strategies/proposals",
                "/market/strategies/proposals/{proposal_id}",
                "/market/strategies/proposals/{proposal_id}/forward",
                "/market/strategies/conflicts",
                "/market/strategies/signals",
                "/market/strategies/signals/{signal_id}",
                "/market/strategies/{strategy_id}",
                "/market/strategies/{strategy_id}/config",
                "/market/strategies/{strategy_id}/versions",
                "/market/strategies/{strategy_id}/activate",
                "/market/strategies/{strategy_id}/pause",
                "/market/strategies/{strategy_id}/suspend",
                "/market/strategies/{strategy_id}/retire",
                "/market/strategies/{strategy_id}/degrade",
                "/market/strategies/{strategy_id}/evaluate",
                "/market/strategies/{strategy_id}/signals",
                "/market/strategies/{strategy_id}/performance",
                "/market/strategies/{strategy_id}/lifecycle",
                "/market/strategies/run",
                "/market/risk/overrides",
                "/market/execution/orders",
                "/market/execution/orders/{order_id}",
                "/market/execution/orders/{order_id}/fills",
                "/market/execution/orders/{order_id}/history",
                "/market/execution/orders/{order_id}/process",
                "/market/execution/orders/{order_id}/cancel",
                "/market/execution/fills",
                "/market/execution/status",
                "/market/execution/contracts",
                "/market/execution/fees",
                "/market/execution/slippage",
                "/market/execution/reconcile",
                "/market/execution/reconciliations",
                "/market/backtests/status",
                "/market/backtests/contracts",
                "/market/backtests/validate",
                "/market/backtests",
                "/market/backtests/compare",
                "/market/backtests/comparisons",
                "/market/backtests/{backtest_id}/walk-forward",
                "/market/backtests/{backtest_id}/start",
                "/market/backtests/{backtest_id}/cancel",
                "/market/backtests/{backtest_id}",
                "/market/backtests/{backtest_id}/results",
                "/market/backtests/{backtest_id}/metrics",
                "/market/backtests/{backtest_id}/trades",
                "/market/backtests/{backtest_id}/orders",
                "/market/backtests/{backtest_id}/risk-decisions",
                "/market/backtests/{backtest_id}/signals",
                "/market/backtests/{backtest_id}/data-quality",
                "/market/backtests/{backtest_id}/verify",
                "/market/backtests/{backtest_id}/sensitivity",
                "/market/evaluations/status",
                "/market/evaluations/contracts",
                "/market/evaluations",
                "/market/evaluations/{evaluation_id}",
                "/market/evaluations/{evaluation_id}/verify",
                "/market/evaluations/{evaluation_id}/reproduce",
                "/market/shadow/status",
                "/market/shadow/contracts",
                "/market/shadow/sessions",
                "/market/shadow/sessions/{session_id}",
                "/market/shadow/sessions/{session_id}/history",
                "/market/shadow/sessions/{session_id}/start",
                "/market/shadow/sessions/{session_id}/pause",
                "/market/shadow/sessions/{session_id}/resume",
                "/market/shadow/sessions/{session_id}/stop",
                "/market/shadow/sessions/{session_id}/complete",
                "/market/shadow/sessions/{session_id}/cancel",
                "/market/shadow/sessions/{session_id}/cycle",
                "/market/shadow/sessions/{session_id}/track-outcomes",
                "/market/shadow/sessions/{session_id}/decisions",
                "/market/shadow/sessions/{session_id}/projections",
                "/market/shadow/sessions/{session_id}/outcomes",
                "/market/shadow/sessions/{session_id}/analytics",
                "/market/shadow/sessions/{session_id}/errors",
                "/market/shadow/sessions/{session_id}/health",
                "/market/shadow/sessions/{session_id}/compare",
                "/market/shadow/sessions/{session_id}/comparisons",
                "/market/learning/status",
                "/market/learning/contracts",
                "/market/learning/dashboard",
                "/market/learning/health",
                "/market/learning/evidence",
                "/market/learning/evidence/{evidence_id}",
                "/market/learning/monitor",
                "/market/learning/alerts",
                "/market/learning/alerts/{alert_id}/acknowledge",
                "/market/learning/experiments",
                "/market/learning/experiments/{experiment_id}",
                "/market/learning/experiments/{experiment_id}/start",
                "/market/learning/experiments/{experiment_id}/run",
                "/market/learning/experiments/{experiment_id}/cancel",
                "/market/learning/experiments/{experiment_id}/candidates",
                "/market/learning/candidates/{candidate_id}",
                "/market/learning/candidates/{candidate_id}/evaluate",
                "/market/learning/candidates/{candidate_id}/approve",
                "/market/learning/candidates/{candidate_id}/reject",
                "/market/learning/candidates/{candidate_id}/shadow-validate",
                "/market/learning/candidates/{candidate_id}/promote",
                "/market/learning/strategies/{strategy_id}/versions",
                "/market/learning/strategies/{strategy_id}/active-version",
                "/market/learning/strategies/{strategy_id}/rollback",
                # AT-01: the runtime and safety shell (no order path).
                "/market/autonomy/contracts",
                "/market/autonomy/status",
                "/market/autonomy/health",
                "/market/autonomy/reliability",
                "/market/autonomy/recover",
                "/market/autonomy/start",
                "/market/autonomy/pause",
                "/market/autonomy/resume",
                "/market/autonomy/stop",
                # AT-02: verified session monitoring.
                "/market/autonomy/session",
                "/market/autonomy/monitoring",
                "/market/autonomy/monitoring/config",
                "/market/autonomy/monitoring/contracts",
                "/market/autonomy/monitoring/cycles",
                "/market/autonomy/monitoring/cycles/{cycle_id}",
                "/market/autonomy/monitoring/run",
                # AT-03: decision proposals over NM-08 risk.
                "/market/autonomy/decisions/contracts",
                "/market/autonomy/decisions/status",
                "/market/autonomy/decisions/runs",
                "/market/autonomy/decisions/runs/{run_id}",
                "/market/autonomy/decisions/signals",
                "/market/autonomy/decisions/proposals",
                "/market/autonomy/decisions/proposals/{proposal_id}",
                "/market/autonomy/decisions/predictions",
                "/market/autonomy/decisions/predictions/{decision_id}",
                "/market/autonomy/decisions/outcomes",
                "/market/autonomy/decisions/outcomes/run",
                "/market/autonomy/decisions/forecast-policy",
                "/market/autonomy/decisions/run",
                # PI-13: isolated RL research, not a production mutation path.
                "/market/rl/status",
                "/market/rl/contracts",
                "/market/rl/health",
                "/market/rl/environments",
                "/market/rl/environments/{environment_id}",
                "/market/rl/environments/{environment_id}/episodes",
                "/market/rl/episodes",
                "/market/rl/episodes/{episode_id}",
                "/market/rl/episodes/{episode_id}/step",
                "/market/rl/episodes/{episode_id}/reset",
                "/market/rl/episodes/{episode_id}/cancel",
                "/market/rl/episodes/{episode_id}/transitions",
                "/market/rl/episodes/{episode_id}/experiences",
                # PI-14: isolated reward and policy evaluation surfaces.
                "/market/rewards/status",
                "/market/rewards/contracts",
                "/market/rewards/health",
                "/market/rewards/baselines",
                "/market/rewards/configurations",
                "/market/rewards/configurations/{config_id}",
                "/market/rewards/episodes/{episode_id}/calculate",
                "/market/rewards/episodes/{episode_id}",
                "/market/rewards/results/{reward_id}",
                "/market/rewards/evaluations",
                "/market/rewards/evaluations/cancel",
                "/market/rewards/evaluations/{evaluation_id}",
                "/market/rewards/comparisons",
                "/market/rewards/stability",
                "/market/rewards/walk-forward",
                # PI-15: isolated training and experimental policy research.
                "/market/training/status",
                "/market/training/contracts",
                "/market/training/health",
                "/market/training/configurations",
                "/market/training/configurations/{config_id}",
                "/market/training/datasets",
                "/market/training/validate",
                "/market/training/runs",
                "/market/training/runs/{run_id}",
                "/market/training/runs/{run_id}/metrics",
                "/market/training/runs/{run_id}/checkpoints",
                "/market/training/runs/{run_id}/pause",
                "/market/training/runs/{run_id}/resume",
                "/market/training/runs/{run_id}/cancel",
                "/market/training/runs/{run_id}/retry",
                "/market/training/checkpoints/{checkpoint_id}",
                "/market/training/checkpoints/{checkpoint_id}/resume",
                "/market/training/optimize",
                "/market/training/searches/{search_id}",
                "/market/training/evaluations",
                "/market/training/policies",
                "/market/training/policies/{policy_id}",
                "/market/training/policies/{policy_id}/validate",
                "/market/training/policies/{policy_id}/test",
                "/market/training/policies/{policy_id}/archive",
                "/market/training/compare",
                # AT-04: paper execution through NM-10.
                "/market/autonomy/executions/contracts",
                "/market/autonomy/executions/status",
                "/market/autonomy/executions",
                "/market/autonomy/executions/failures",
                "/market/autonomy/executions/{execution_id}",
                "/market/autonomy/executions/{execution_id}/events",
                "/market/autonomy/executions/run",
                "/market/autonomy/executions/{execution_id}/reconcile",
                # AT-05: supervised protective exits.
                "/market/autonomy/exits/contracts",
                "/market/autonomy/exits/status",
                "/market/autonomy/exits/positions",
                "/market/autonomy/exits/configurations",
                "/market/autonomy/exits/configurations/{portfolio_id}/{instrument_id}",
                "/market/autonomy/exits/decisions",
                "/market/autonomy/exits/decisions/{decision_id}",
                "/market/autonomy/exits/decisions/{decision_id}/events",
                "/market/autonomy/exits/decisions/{decision_id}/reconcile",
                "/market/autonomy/exits/learning",
                "/market/autonomy/exits/run",
                # PI-21: advisory portfolio management, no direct order or
                # financial mutation route.
                "/market/portfolio-management/status",
                "/market/portfolio-management/contracts",
                "/market/portfolio-management/navigation",
                "/market/portfolio-management/config",
                "/market/portfolio-management/review",
                "/market/portfolio-management/cycles",
                "/market/portfolio-management/cycles/{cycle_id}",
                "/market/portfolio-management/cycles/{cycle_id}/events",
                "/market/portfolio-management/cycles/{cycle_id}/handoff",
                "/market/portfolio-management/cycles/{cycle_id}/reconcile",
                "/market/portfolio-management/recover",
                "/market/portfolio-management/portfolios/{portfolio_id}/snapshot",
                "/market/portfolio-management/portfolios/{portfolio_id}/positions",
                "/market/portfolio-management/portfolios/{portfolio_id}/opportunities",
                "/market/portfolio-management/portfolios/{portfolio_id}/decisions",
                "/market/portfolio-management/portfolios/{portfolio_id}/capital",
                "/market/portfolio-management/portfolios/{portfolio_id}/cash",
                "/market/portfolio-management/portfolios/{portfolio_id}/exposure",
                "/market/portfolio-management/portfolios/{portfolio_id}/rebalancing",
                "/market/portfolio-management/portfolios/{portfolio_id}/analytics",
                # PI-22: advisory risk intelligence, no direct order or
                # financial mutation route.
                "/market/risk-intelligence/status",
                "/market/risk-intelligence/contracts",
                "/market/risk-intelligence/navigation",
                "/market/risk-intelligence/config",
                "/market/risk-intelligence/analyze",
                "/market/risk-intelligence/portfolios/{portfolio_id}/overview",
                "/market/risk-intelligence/portfolios/{portfolio_id}/positions",
                "/market/risk-intelligence/portfolios/{portfolio_id}/concentration",
                "/market/risk-intelligence/portfolios/{portfolio_id}/correlation",
                "/market/risk-intelligence/portfolios/{portfolio_id}/volatility",
                "/market/risk-intelligence/portfolios/{portfolio_id}/drawdown",
                "/market/risk-intelligence/portfolios/{portfolio_id}/liquidity",
                "/market/risk-intelligence/portfolios/{portfolio_id}/regime",
                "/market/risk-intelligence/portfolios/{portfolio_id}/history",
                "/market/risk-intelligence/portfolios/{portfolio_id}/trend",
                "/market/risk-intelligence/portfolios/{portfolio_id}/recommendations",
                "/market/risk-intelligence/alerts",
                "/market/risk-intelligence/alerts/{alert_id}/action",
                "/market/risk-intelligence/scenarios",
                "/market/risk-intelligence/portfolios/{portfolio_id}/scenarios/run",
                "/market/risk-intelligence/portfolios/{portfolio_id}/scenario-runs",
                "/market/risk-intelligence/portfolios/{portfolio_id}/stress-tests",
                "/market/risk-intelligence/recover",
                # PI-23: advisory forecast optimization, not an order or model
                # promotion route.
                "/market/forecast-intelligence/status",
                "/market/forecast-intelligence/contracts",
                "/market/forecast-intelligence/navigation",
                "/market/forecast-intelligence/config",
                "/market/forecast-intelligence/registry",
                "/market/forecast-intelligence/history",
                "/market/forecast-intelligence/overview",
                "/market/forecast-intelligence/comparison/{instrument_id}",
                "/market/forecast-intelligence/horizons/{instrument_id}",
                "/market/forecast-intelligence/regimes/{instrument_id}",
                "/market/forecast-intelligence/instruments",
                "/market/forecast-intelligence/evaluate",
                "/market/forecast-intelligence/select",
                "/market/forecast-intelligence/calibration",
                "/market/forecast-intelligence/uncertainty",
                "/market/forecast-intelligence/disagreement",
                "/market/forecast-intelligence/consistency",
                "/market/forecast-intelligence/drift",
                "/market/forecast-intelligence/degradation",
                "/market/forecast-intelligence/weights",
                "/market/forecast-intelligence/reliability",
                "/market/forecast-intelligence/fallback",
                "/market/forecast-intelligence/analyze",
                "/market/forecast-intelligence/health",
                # PI-24: advisory event intelligence over NM-04 evidence, with
                # no forecast mutation, risk decision or order route.
                "/market/event-intelligence/status",
                "/market/event-intelligence/contracts",
                "/market/event-intelligence/navigation",
                "/market/event-intelligence/config",
                "/market/event-intelligence/events",
                "/market/event-intelligence/events/{event_id}",
                "/market/event-intelligence/timeline",
                "/market/event-intelligence/relevance",
                "/market/event-intelligence/impact",
                "/market/event-intelligence/warnings",
                "/market/event-intelligence/features",
                "/market/event-intelligence/forecast-context",
                "/market/event-intelligence/outcomes",
                "/market/event-intelligence/outcomes/study",
                "/market/event-intelligence/forecast-evaluation",
                "/market/event-intelligence/agents",
                "/market/event-intelligence/portfolio/{portfolio_id}/context",
                "/market/event-intelligence/sync",
                "/market/event-intelligence/ingest",
                "/market/event-intelligence/analyze",
                "/market/event-intelligence/overview",
                "/market/event-intelligence/history",
                "/market/event-intelligence/health",
            },
        )
        self.assertNotIn("/market/trade/live", paths)
        for path in paths:
            self.assertNotIn("broker", path)


@unittest.skipIf(TestClient is None, "fastapi.testclient requires httpx")
class NovaMarketShadowApiTests(_MarketApiCase):
    """The NM-12 HTTP surface drives a non-executing session end to end."""

    def test_shadow_lifecycle_over_http(self) -> None:
        import asyncio

        from novacontrol.novamarket.shadow import ShadowService

        now = datetime(2026, 1, 2, 10, 0, tzinfo=UTC)
        closes = (110, 108, 106, 104, 102, 100, 98, 96, 94, 92, 90, 80, 95, 110)

        class Selected:
            def __init__(self, price: object, at: datetime) -> None:
                self.price = Decimal(str(price))
                self.observed_at = at
                self.quality = "valid"
                self.freshness = "fresh"

        class Result:
            def __init__(self, price: object, at: datetime) -> None:
                self.selected = Selected(price, at)
                self.usable = True

        class Bar:
            def __init__(self, close: object, at: datetime) -> None:
                self.close = Decimal(str(close))
                self.high = self.close + 1
                self.low = self.close - 1
                self.volume = Decimal("10000")
                self.observed_at = at
                self.partial = False

        class Page:
            def __init__(self, items: list[Bar]) -> None:
                self.items = items

        class Market:
            def latest(self, instrument_id: str) -> Result:
                return Result(closes[-1], now)

            def bars(
                self, instrument_id: str, limit: int = 60, ascending: bool = True
            ) -> Page:
                return Page(
                    [
                        Bar(
                            close,
                            now - timedelta(days=len(closes) - 1 - index),
                        )
                        for index, close in enumerate(closes)
                    ]
                )

        policy = {
            "max_volatility_pct": "1000",
            "min_cash_fraction": "0",
            "max_invested_fraction": "1",
            "max_position_weight": "1",
            "max_sector_weight": "1",
            "min_liquidity_value": "0",
            "max_turnover": "2",
            "max_drawdown_pct": "100",
            "max_data_age_hours": 1000,
            "max_state_age_hours": 1000,
            "min_observations": 2,
            "max_aggregate_sector_weight": "1",
        }
        service = ShadowService(
            Path(self._tmp.name) / "shadow_api.sqlite3",
            market_data=Market(),
            risk_policy_loader=lambda version: policy,
            clock=lambda: now,
        )
        try:
            assert self._nova is not None
            self._nova.novamarket._shadow_service = service
            wallet_before = self._nova.novamarket.wallet_status()
            portfolio_before = self._nova.novamarket.portfolio_status()

            status = self._client.get("/market/shadow/status")
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["phase"], "NM-12")
            self.assertFalse(status.json()["execution_connected"])

            created = self._client.post(
                "/market/shadow/sessions",
                json={
                    "name": "api shadow",
                    "instruments": ["XNSE:ALPHA"],
                    "strategies": [
                        {
                            "strategy_id": "trend",
                            "catalog_id": "trend_sma",
                            "version": "v1",
                            "config": {"fast_period": 2, "slow_period": 3},
                        }
                    ],
                    "baseline_snapshot": {
                        "positions": [],
                        "cash": "100000",
                        "total_value": "100000",
                    },
                    "auto_monitor": False,
                },
            )
            self.assertEqual(created.status_code, 200, created.text)
            session_id = created.json()["session"]["session_id"]
            self.assertEqual(created.json()["session"]["status"], "created")

            started = self._client.post(
                f"/market/shadow/sessions/{session_id}/start"
            )
            self.assertEqual(started.status_code, 200, started.text)
            self.assertEqual(started.json()["session"]["status"], "running")

            bars = [
                {
                    "observed_at": (
                        now - timedelta(days=len(closes) - 1 - index)
                    ).isoformat(),
                    "close": str(close),
                    "high": str(close + 1),
                    "low": str(close - 1),
                    "volume": "10000",
                }
                for index, close in enumerate(closes)
            ]
            cycle = self._client.post(
                f"/market/shadow/sessions/{session_id}/cycle",
                json={
                    "observations": {
                        "XNSE:ALPHA": {
                            "price": "110",
                            "observed_at": now.isoformat(),
                            "bars": bars,
                        }
                    }
                },
            )
            self.assertEqual(cycle.status_code, 200, cycle.text)
            self.assertEqual(cycle.json()["counts"]["decisions"], 1)
            self.assertEqual(cycle.json()["counts"]["projections"], 1)

            decisions = self._client.get(
                f"/market/shadow/sessions/{session_id}/decisions"
            )
            self.assertEqual(decisions.status_code, 200, decisions.text)
            self.assertEqual(decisions.json()["total"], 1)
            self.assertEqual(decisions.json()["decisions"][0]["signal_type"], "buy")

            projections = self._client.get(
                f"/market/shadow/sessions/{session_id}/projections"
            )
            self.assertEqual(projections.status_code, 200, projections.text)
            self.assertIsNone(
                projections.json()["projections"][0]["execution_order_id"]
            )

            analytics = self._client.get(
                f"/market/shadow/sessions/{session_id}/analytics"
            )
            self.assertEqual(analytics.status_code, 200, analytics.text)
            self.assertTrue(analytics.json()["hypothetical"])

            health = self._client.get(
                f"/market/shadow/sessions/{session_id}/health"
            )
            self.assertEqual(health.status_code, 200, health.text)
            self.assertEqual(health.json()["status"], "running")

            comparison = self._client.post(
                f"/market/shadow/sessions/{session_id}/compare",
                json={"reference": {"signals": {"buy": 0}}},
            )
            self.assertEqual(comparison.status_code, 200, comparison.text)
            self.assertTrue(comparison.json()["comparison"]["compatible"])

            for action in ("pause", "resume", "stop"):
                response = self._client.post(
                    f"/market/shadow/sessions/{session_id}/{action}"
                )
                self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                self._client.post(
                    f"/market/shadow/sessions/{session_id}/pause"
                ).status_code,
                409,
            )
            self.assertEqual(
                self._client.get("/market/shadow/sessions/not_a_session").status_code,
                404,
            )

            assert self._nova is not None
            self.assertEqual(self._nova.novamarket.wallet_status(), wallet_before)
            self.assertEqual(
                self._nova.novamarket.portfolio_status(), portfolio_before
            )
        finally:
            asyncio.run(service.close())


@unittest.skipIf(TestClient is None, "fastapi.testclient requires httpx")
class NovaMarketLearningApiTests(_MarketApiCase):
    """The NM-13 surface records provenance and refuses unauthorised promotion."""

    def test_learning_lifecycle_over_http(self) -> None:
        now = datetime.now(UTC)
        created = self._client.post(
            "/market/strategies",
            json={
                "catalog_id": "trend_sma",
                "name": "learning api strategy",
                "config": {"fast_period": 3, "slow_period": 8},
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        strategy = created.json()["strategy"]
        strategy_id = strategy["strategy_id"]

        status = self._client.get("/market/learning/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["phase"], "NM-13")
        self.assertFalse(status.json()["execution_connected"])
        self.assertFalse(status.json()["executes_trades"])

        evidence = {
            "kind": "backtest",
            "source_ref": f"bt_{strategy_id}",
            "strategy_id": strategy_id,
            "strategy_version": strategy["version"],
            "catalog_id": "trend_sma",
            "config": strategy["config"],
            "window_start": (now - timedelta(days=30)).isoformat(),
            "window_end": (now - timedelta(days=2)).isoformat(),
            "observations": 60,
            "metrics": {"total_return": 0.04, "sharpe": 1.1},
            "in_sample": False,
            "out_of_sample": True,
            "instruments": ["XNSE:ALPHA"],
            "regimes": ["bull_low_volatility"],
        }
        recorded = self._client.post("/market/learning/evidence", json=evidence)
        self.assertEqual(recorded.status_code, 200, recorded.text)
        evidence_id = recorded.json()["evidence"]["evidence_id"]
        self.assertEqual(recorded.json()["evidence"]["status"], "accepted")
        self.assertFalse(recorded.json()["evidence"]["is_live_performance"])

        duplicate = self._client.post("/market/learning/evidence", json=evidence)
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(
            duplicate.json()["evidence"]["evidence_id"], evidence_id
        )

        listing = self._client.get(
            "/market/learning/evidence", params={"strategy_id": strategy_id}
        )
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["total"], 1)
        detail = self._client.get(f"/market/learning/evidence/{evidence_id}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["evidence"]["kind"], "backtest")

        monitor = self._client.post(
            "/market/learning/monitor",
            json={"strategy_id": strategy_id, "window_days": 30},
        )
        self.assertEqual(monitor.status_code, 200, monitor.text)
        self.assertEqual(len(monitor.json()["snapshots"]), 3)
        snapshots = {row["kind"]: row for row in monitor.json()["snapshots"]}
        self.assertTrue(snapshots["backtest"]["metrics"]["available"])
        self.assertFalse(snapshots["paper"]["metrics"]["available"])
        self.assertFalse(snapshots["shadow"]["metrics"]["available"])

        experiment = self._client.post(
            "/market/learning/experiments",
            json={
                "strategy_id": strategy_id,
                "instruments": ["XNSE:ALPHA"],
                "train_start": (now - timedelta(days=170)).isoformat(),
                "train_end": (now - timedelta(days=60)).isoformat(),
                "validation_start": (now - timedelta(days=60)).isoformat(),
                "validation_end": (now - timedelta(days=2)).isoformat(),
                "evidence_refs": [evidence_id],
                "method": "grid",
                "candidate_configs": [
                    {**strategy["config"], "fast_period": 4, "slow_period": 10}
                ],
                "approval_policy": {"mode": "manual"},
            },
        )
        self.assertEqual(experiment.status_code, 200, experiment.text)
        experiment_id = experiment.json()["experiment"]["experiment_id"]
        self.assertEqual(experiment.json()["experiment"]["status"], "created")
        self.assertEqual(
            experiment.json()["experiment"]["evidence_refs"], [evidence_id]
        )

        candidates = self._client.get(
            f"/market/learning/experiments/{experiment_id}/candidates"
        )
        self.assertEqual(candidates.status_code, 200, candidates.text)
        self.assertEqual(candidates.json()["total"], 1)
        candidate_id = candidates.json()["candidates"][0]["candidate_id"]
        self.assertEqual(
            candidates.json()["candidates"][0]["status"], "generated"
        )

        for action in ("approve", "shadow-validate", "promote"):
            refused = self._client.post(
                f"/market/learning/candidates/{candidate_id}/{action}", json={}
            )
            self.assertEqual(refused.status_code, 409, (action, refused.text))

        cancelled = self._client.post(
            f"/market/learning/experiments/{experiment_id}/cancel",
            json={"reason": "api lifecycle test"},
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["experiment"]["status"], "cancelled")
        self.assertEqual(
            self._client.post(
                f"/market/learning/experiments/{experiment_id}/run"
            ).status_code,
            409,
        )

        versions = self._client.get(
            f"/market/learning/strategies/{strategy_id}/versions"
        )
        self.assertEqual(versions.status_code, 200, versions.text)
        self.assertEqual(versions.json()["versions"], [])
        missing = self._client.get("/market/learning/candidates/not_a_candidate")
        self.assertEqual(missing.status_code, 404)

        assert self._nova is not None
        self.assertFalse(self._nova.novamarket._learning().status()["execution_connected"])


if __name__ == "__main__":
    unittest.main()


class MarketEventApiTests(_MarketApiCase):
    """The nine NM-04 routes, driven through the real application."""

    def test_search_returns_an_empty_page_before_anything_is_ingested(self) -> None:
        response = self._client.get("/market/events")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["events"], [])
        self.assertEqual(body["limit"], 50)

    def test_sources_report_the_india_news_feeds_enabled(self) -> None:
        body = self._client.get("/market/events/sources").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["total"], 6)
        rows = {row["source_id"]: row for row in body["sources"]}
        self.assertEqual(
            set(rows),
            {
                "rbi_press",
                "fed_press",
                "bls_latest",
                "sec_edgar_8k",
                "et_markets",
                "bs_markets",
            },
        )
        self.assertEqual(
            {source_id for source_id, row in rows.items() if row["enabled"]},
            {"et_markets", "bs_markets"},
        )
        self.assertTrue(all(row["verified_live"] is False for row in body["sources"]))

    def test_enablement_round_trips_through_the_store(self) -> None:
        response = self._client.post(
            "/market/events/sources/rbi_press/enabled", json={"enabled": True}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["enabled"])
        rows = {
            row["source_id"]: row
            for row in self._client.get("/market/events/sources").json()["sources"]
        }
        self.assertTrue(rows["rbi_press"]["enabled"])

    def test_an_unknown_source_is_a_typed_404(self) -> None:
        disabled = self._client.post(
            "/market/events/sources/not_a_feed/enabled", json={"enabled": True}
        )
        self.assertEqual(disabled.status_code, 404)
        refreshed = self._client.post(
            "/market/events/refresh", json={"source_id": "not_a_feed"}
        )
        self.assertEqual(refreshed.status_code, 404)

    def test_an_unknown_event_is_a_404_on_every_read(self) -> None:
        self.assertEqual(self._client.get("/market/events/evt_missing").status_code, 404)
        self.assertEqual(
            self._client.get("/market/events/evt_missing/evidence").status_code, 404
        )
        self.assertEqual(
            self._client.get("/market/events/evt_missing/history").status_code, 404
        )

    def test_an_invalid_filter_is_a_422(self) -> None:
        self.assertEqual(self._client.get("/market/events?limit=0").status_code, 422)
        self.assertEqual(
            self._client.get("/market/events?category=weather").status_code, 422
        )
        self.assertEqual(
            self._client.get("/market/events?since=yesterday").status_code, 422
        )

    def test_expiring_an_empty_radar_reports_zero(self) -> None:
        response = self._client.post("/market/events/expire")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["expired"], [])

    def test_instrument_events_for_an_unknown_instrument_is_empty(self) -> None:
        response = self._client.get("/market/events/instrument/XNSE:NONE")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 0)


class DiscoveryApiTests(_MarketApiCase):
    """The thirteen NM-05 routes, driven through the real application."""

    def test_status_reports_the_discovery_engine(self) -> None:
        body = self._client.get("/market/discovery/status").json()
        self.assertTrue(body["initialised"])
        self.assertEqual(body["phase"], "NM-05")
        self.assertIn("universe", body)
        self.assertIn("policy", body)
        self.assertFalse(body["semantics"]["trading"] if "semantics" in body else False)

    def test_four_default_watchlists_exist(self) -> None:
        body = self._client.get("/market/watchlists").json()
        self.assertEqual(body["total"], 4)
        self.assertEqual(
            {row["kind"] for row in body["watchlists"]},
            {"discovery", "activity", "event_driven", "liquidity"},
        )

    def test_an_empty_cycle_is_reported_honestly(self) -> None:
        response = self._client.post("/market/discovery/cycle", json={})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["scanned"], 0)
        self.assertEqual(body["evaluated"], 0)
        self.assertEqual(body["status"], "ok")

    def test_candidates_are_empty_before_ingestion(self) -> None:
        self.assertEqual(
            self._client.get("/market/discovery/candidates").json()["total"], 0
        )
        self.assertEqual(
            self._client.get("/market/discovery/candidates/XNSE:NONE").status_code,
            404,
        )

    def test_a_watchlist_can_be_created_and_read(self) -> None:
        created = self._client.post(
            "/market/watchlists", json={"name": "API list", "kind": "custom"}
        )
        self.assertEqual(created.status_code, 200)
        watchlist_id = created.json()["watchlist_id"]
        body = self._client.get(f"/market/watchlists/{watchlist_id}").json()
        self.assertEqual(body["name"], "API list")
        self.assertEqual(body["total"], 0)

    def test_unknown_members_and_kinds_are_typed_errors(self) -> None:
        watchlist_id = self._client.get("/market/watchlists").json()["watchlists"][0][
            "watchlist_id"
        ]
        added = self._client.post(
            f"/market/watchlists/{watchlist_id}/members",
            json={"instrument_id": "XNSE:NONE"},
        )
        self.assertEqual(added.status_code, 404)
        self.assertEqual(
            self._client.delete(
                f"/market/watchlists/{watchlist_id}/members/XNSE:NONE"
            ).status_code,
            404,
        )
        self.assertEqual(
            self._client.post(
                "/market/watchlists", json={"name": "x", "kind": "weather"}
            ).status_code,
            422,
        )

    def test_the_scheduler_starts_and_stops(self) -> None:
        started = self._client.post(
            "/market/discovery/scheduler",
            json={"action": "start", "interval_seconds": 30},
        )
        self.assertEqual(started.status_code, 200)
        self.assertTrue(started.json()["running"])
        stopped = self._client.post(
            "/market/discovery/scheduler", json={"action": "stop"}
        )
        self.assertEqual(stopped.status_code, 200)
        self.assertFalse(stopped.json()["running"])

    def test_an_invalid_scheduler_action_is_422(self) -> None:
        response = self._client.post(
            "/market/discovery/scheduler", json={"action": "later"}
        )
        self.assertEqual(response.status_code, 422)

    def test_history_is_readable_before_any_membership(self) -> None:
        body = self._client.get("/market/discovery/history").json()
        self.assertEqual(body["total"], 0)

class ForecastApiTests(_MarketApiCase):
    """The eleven NM-06 routes, driven through the real application."""

    def test_status_reports_horizons_and_the_read_only_contract(self) -> None:
        body = self._client.get("/market/forecasts/status").json()
        self.assertTrue(body["initialised"])
        self.assertEqual(
            body["horizons"],
            {
                "next_session": 1,
                "few_sessions": 3,
                "one_week": 5,
                "one_month": 21,
                "three_months": 63,
            },
        )
        status = self._status()["forecasting"]
        self.assertTrue(status["initialised"])
        self.assertEqual(status["policy"]["directional_threshold_pct"], 0.5)
        contract = self._client.get("/market/contracts").json()["forecasting"]
        self.assertTrue(contract["implemented"])
        self.assertIn("immutable", " ".join(contract["guarantees"]))

    def test_an_unknown_horizon_is_a_422(self) -> None:
        response = self._client.post(
            "/market/forecasts",
            json={"instrument_id": "XNSE:ALPHA", "horizon": "decade"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("unknown horizon", response.json()["detail"])

    def test_empty_reads_are_honest(self) -> None:
        self.assertEqual(self._client.get("/market/forecasts").json()["total"], 0)
        self.assertEqual(
            self._client.get("/market/forecasts/models").json()["models"], []
        )
        self.assertEqual(
            self._client.get("/market/forecasts/performance").json()["live"]["samples"],
            0,
        )
        calibration = self._client.get(
            "/market/forecasts/calibration",
            params={"model_id": "random_walk", "horizon": "next_session"},
        ).json()
        self.assertEqual(calibration["status"], "not_calibrated")

    def test_an_unknown_horizon_on_reads_is_a_422(self) -> None:
        for path in (
            "/market/forecasts",
            "/market/forecasts/models",
            "/market/forecasts/performance",
        ):
            with self.subTest(path=path):
                response = self._client.get(
                    path, params={"horizon": "decade"}
                )
                self.assertEqual(response.status_code, 422)
                self.assertIn("unknown horizon", response.json()["detail"])

    def test_calibration_requires_a_model_id(self) -> None:
        response = self._client.get(
            "/market/forecasts/calibration",
            params={"horizon": "next_session"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("model_id", response.json()["detail"])

    def test_a_forecast_for_an_unknown_instrument_returns_404(self) -> None:
        response = self._client.post(
            "/market/forecasts",
            json={"instrument_id": "XNSE:MISSING", "horizon": "next_session"},
        )
        self.assertEqual(response.status_code, 404)

    def test_pfc_detail_and_outcome_read_the_lifecycle_store(self) -> None:
        """PI-09 ``pfc_`` reads come from PI-01, not the PI-08 forecast store."""
        assert self._nova is not None
        store = self._nova.novamarket.prediction_service().store
        now = datetime.now(UTC).replace(microsecond=0)

        def forecast(
            forecast_id: str, lifecycle_id: str, generated: datetime, target: datetime
        ) -> ForecastOutput:
            return ForecastOutput(
                forecast_id=forecast_id,
                lifecycle_id=lifecycle_id,
                lifecycle_stage="t_minus_1",
                instrument_id="XNSE:API09",
                exchange="XNSE",
                currency="INR",
                reference_price=Decimal("100"),
                reference_at=generated,
                predicted_price=Decimal("101"),
                predicted_return=0.01,
                direction="up",
                lower_return=-0.05,
                upper_return=0.08,
                lower_price=Decimal("95"),
                upper_price=Decimal("108"),
                uncertainty="medium",
                uncertainty_width=0.13,
                p_up=None,
                p_down=None,
                p_neutral=None,
                probability_status="unavailable",
                calibration_status="uncalibrated",
                confidence=None,
                ensemble_id="ens_api09",
                model_id="ensemble",
                model_version="pi08-api09",
                member_versions=("xgboost@v1",),
                forecast_horizon="1d",
                target_at=target,
                generated_at=generated,
                data_observed_at=generated - timedelta(minutes=1),
                data_received_at=generated - timedelta(minutes=1),
                data_resolution="1d",
                data_freshness="fresh",
                data_quality="valid",
                data_snapshot_id=f"snap_{forecast_id}",
                validity="valid",
                validity_conditions=(),
                disagreement=0.0,
                digest="c" * 64,
                feature_set_version="api09-features",
                dataset_version="api09-dataset",
            )

        pending_forecast = forecast(
            "pfc_api09_pending",
            "lc_api09_pending",
            now,
            now + timedelta(hours=1),
        )
        evaluated_forecast = forecast(
            "pfc_api09_evaluated",
            "lc_api09_evaluated",
            now - timedelta(hours=2),
            now - timedelta(hours=1),
        )
        self.assertTrue(store.save_forecast(pending_forecast))
        self.assertTrue(store.save_forecast(evaluated_forecast))

        detail = self._client.get("/market/forecasts/pfc_api09_pending")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["forecast_id"], "pfc_api09_pending")
        self.assertEqual(detail.json()["lifecycle_id"], "lc_api09_pending")
        self.assertEqual(
            detail.json()["forecast_target_timestamp"],
            pending_forecast.target_at.isoformat(),
        )

        pending = self._client.get("/market/forecasts/pfc_api09_pending/outcome")
        self.assertEqual(pending.status_code, 200)
        self.assertEqual(pending.json()["status"], "pending")
        self.assertEqual(pending.json()["cycle_id"], "lc_api09_pending")
        self.assertEqual(
            pending.json()["target_at"], pending_forecast.target_at.isoformat()
        )

        self.assertTrue(
            store.save_outcome(
                ForecastOutcome(
                    outcome_id="pout_api09_evaluated",
                    cycle_id=evaluated_forecast.lifecycle_id,
                    forecast_id=evaluated_forecast.forecast_id,
                    stage="t_minus_1",
                    target_at=evaluated_forecast.target_at,
                    actual_observed_at=evaluated_forecast.target_at
                    + timedelta(minutes=1),
                    evaluated_at=evaluated_forecast.target_at + timedelta(minutes=2),
                    observation_delay_seconds=60.0,
                    actual_price=Decimal("102"),
                    actual_return=0.02,
                    predicted_price=Decimal("101"),
                    predicted_return=0.01,
                    predicted_direction="up",
                    actual_direction="up",
                    direction_correct=True,
                    price_error=Decimal("-1"),
                    absolute_price_error=Decimal("1"),
                    percentage_price_error=0.01,
                    return_error=-0.01,
                    absolute_return_error=0.01,
                    inside_interval=True,
                    interval_lower=-0.05,
                    interval_upper=0.08,
                    interval_width=0.13,
                )
            )
        )

        evaluated = self._client.get("/market/forecasts/pfc_api09_evaluated/outcome")
        self.assertEqual(evaluated.status_code, 200)
        self.assertEqual(evaluated.json()["status"], "evaluated")
        self.assertEqual(evaluated.json()["cycle_id"], "lc_api09_evaluated")
        self.assertEqual(evaluated.json()["forecast_id"], "pfc_api09_evaluated")
        self.assertEqual(evaluated.json()["actual_price"], "102")
        self.assertTrue(evaluated.json()["direction_correct"])

        evaluated_detail = self._client.get("/market/forecasts/pfc_api09_evaluated")
        self.assertEqual(evaluated_detail.status_code, 200)
        self.assertEqual(evaluated_detail.json()["lifecycle_id"], "lc_api09_evaluated")
        self.assertEqual(
            evaluated_detail.json()["forecast_target_timestamp"],
            evaluated_forecast.target_at.isoformat(),
        )

        for suffix in ("", "/outcome"):
            with self.subTest(suffix=suffix):
                unknown = self._client.get(f"/market/forecasts/pfc_api09_missing{suffix}")
                self.assertEqual(unknown.status_code, 404)


_BACKTEST_NOW = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)


def _backtest_bars() -> list[Any]:
    rows = []
    for index in range(30):
        at = _BACKTEST_NOW + timedelta(days=index)
        base = Decimal("100") - Decimal(index) if index < 15 else Decimal("85") + Decimal(index - 15)
        rows.append(
            SimpleNamespace(
                observed_at=at,
                retrieved_at=at,
                open=base,
                high=base + 1,
                low=base - 1,
                close=base,
                volume=Decimal("10000"),
                provider="fixture",
                quality=SimpleNamespace(value="valid"),
                partial=False,
                provider_metadata={"adjusted": False},
            )
        )
    return rows


class _BacktestMarketData:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def bars(self, instrument_id, *, interval, start, end, limit, offset, ascending):
        rows = self._rows
        return SimpleNamespace(
            items=rows[offset : offset + limit], total=len(rows)
        )


def _backtest_payload(name: str = "http fixture") -> dict[str, Any]:
    return {
        "name": name,
        "strategy_catalog_id": "trend_sma",
        "strategy_version": "nm09-trend_sma-1.0",
        "strategy_config": {"fast_period": 3, "slow_period": 5, "horizon_days": 5},
        "instruments": ["XNSE:ALPHA"],
        "start": _BACKTEST_NOW.isoformat(),
        "end": (_BACKTEST_NOW + timedelta(days=40)).isoformat(),
        "initial_capital": "100000",
        "execution_config": {
            "participation_rate": "1",
            "spread_bps": "0",
            "impact_coefficient_bps": "0",
            "volatility_weight": "0",
            "brokerage_bps": "0",
            "exchange_bps": "0",
            "sebi_bps": "0",
            "stamp_duty_bps": "0",
            "gst_percent": "0",
            "require_fresh_data": False,
        },
    }


@unittest.skipIf(TestClient is None, "fastapi.testclient requires httpx")
class NovaMarketBacktestApiTests(_MarketApiCase):
    """The NM-11 HTTP surface drives a real persisted run end to end."""

    def test_backtest_lifecycle_over_http(self) -> None:
        from novacontrol.novamarket.backtesting import BacktestingService

        service = BacktestingService(
            Path(self._tmp.name) / "backtests.sqlite3",
            market_data=_BacktestMarketData(_backtest_bars()),
            clock=lambda: _BACKTEST_NOW,
        )
        try:
            assert self._nova is not None
            self._nova.novamarket._backtest_service = service

            created = self._client.post("/market/backtests", json=_backtest_payload())
            self.assertEqual(created.status_code, 200, created.text)
            backtest_id = created.json()["backtest_id"]
            self.assertEqual(created.json()["status"], "created")

            started = self._client.post(f"/market/backtests/{backtest_id}/start")
            self.assertEqual(started.status_code, 200, started.text)
            self.assertEqual(started.json()["status"], "completed")

            results = self._client.get(f"/market/backtests/{backtest_id}/results")
            self.assertEqual(results.status_code, 200, results.text)
            self.assertTrue(results.json()["metrics"]["available"])

            trades = self._client.get(f"/market/backtests/{backtest_id}/trades")
            self.assertEqual(trades.status_code, 200, trades.text)
            self.assertTrue(trades.json()["trades"])

            verify = self._client.get(f"/market/backtests/{backtest_id}/verify")
            self.assertEqual(verify.status_code, 200, verify.text)
            self.assertTrue(verify.json()["ok"])

            listing = self._client.get("/market/backtests").json()
            self.assertEqual(listing["backtests"][0]["backtest_id"], backtest_id)

            comparisons = self._client.get("/market/backtests/comparisons")
            self.assertEqual(comparisons.status_code, 200, comparisons.text)
            self.assertEqual(comparisons.json()["total"], 0)

            walk = self._client.post(
                f"/market/backtests/{backtest_id}/walk-forward",
                json={"train_bars": 8, "test_bars": 5},
            )
            self.assertEqual(walk.status_code, 200, walk.text)
            self.assertGreaterEqual(walk.json()["count"], 1)
            self.assertTrue(walk.json()["persisted"])

            status = self._client.get("/market/backtests/status").json()
            # The main run plus every walk-forward fold is a completed run.
            self.assertGreaterEqual(status["completed"], 1)
            self.assertTrue(status["isolated"])
        finally:
            service.close()

    def test_unknown_and_incomplete_backtests_are_reported_honestly(self) -> None:
        from novacontrol.novamarket.backtesting import BacktestingService

        service = BacktestingService(
            Path(self._tmp.name) / "backtests2.sqlite3",
            market_data=_BacktestMarketData(_backtest_bars()),
            clock=lambda: _BACKTEST_NOW,
        )
        try:
            assert self._nova is not None
            self._nova.novamarket._backtest_service = service
            missing = self._client.get("/market/backtests/bt_missing")
            self.assertEqual(missing.status_code, 404, missing.text)

            created = self._client.post(
                "/market/backtests", json=_backtest_payload("pending fixture")
            ).json()
            pending = self._client.get(
                f"/market/backtests/{created['backtest_id']}/results"
            )
            self.assertEqual(pending.status_code, 422, pending.text)
            self.assertIn("has not completed", pending.json()["detail"])
        finally:
            service.close()
