"""AT-01 focused tests: runtime modes, durable lease and operator permission.

These tests deliberately do not construct an execution, portfolio, wallet or
strategy engine.  AT-01 must be provably unable to reach a financial mutation.
"""

from __future__ import annotations

import ast
import os
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

try:
    from fastapi.testclient import TestClient  # requires httpx
except Exception:  # pragma: no cover - optional dev dependency
    TestClient = None  # type: ignore[assignment]

from novacontrol.api.models import ApiSurface
from novacontrol.api.route_consumers import ROUTE_CONSUMERS
from novacontrol.novamarket.autonomy import (
    AutonomyLeaseError,
    AutonomyMode,
    AutonomyRuntime,
    AutonomyStateError,
    PAPER_ONLY,
    TRADING_IMPLEMENTED,
)
from novacontrol.novamarket.autonomy.store import AutonomyStore

_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "novacontrol" / "novamarket" / "autonomy"


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 10, 3, 45, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class AutonomyRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "novamarket.sqlite3"
        self.clock = _Clock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _runtime(self, owner: str = "owner-a", lease_seconds: int = 60) -> AutonomyRuntime:
        return AutonomyRuntime(
            self.path,
            clock=self.clock,
            owner_id=owner,
            lease_seconds=lease_seconds,
        )

    def test_boot_is_disabled_and_restart_never_resumes_autonomy(self) -> None:
        first = self._runtime("owner-a")
        self.assertEqual(first.status()["mode"], "disabled")
        self.assertEqual(first.status()["runtime_state"], "stopped")
        first.start("autonomous", acknowledge_paper_only=True)
        self.assertEqual(first.status()["mode"], "autonomous")

        # A second process opening the same durable store must not inherit the
        # mode while the first lease is live; it reports the conflict and stays
        # unable to start a duplicate runtime.
        second = self._runtime("owner-b")
        seen = second.status()
        self.assertEqual(seen["mode"], "disabled")
        self.assertEqual(seen["runtime_state"], "degraded")
        self.assertTrue(seen["lease"]["conflict"])
        self.assertTrue(seen["recovery_required"])
        with self.assertRaises(AutonomyLeaseError):
            second.start("monitoring")

        first.stop()
        third = self._runtime("owner-c")
        restarted = third.status()
        self.assertEqual(restarted["mode"], "disabled")
        self.assertEqual(restarted["runtime_state"], "stopped")
        self.assertFalse(restarted["recovery_required"])
        third.close()
        second.close()
        first.close()

    def test_mode_transitions_persist_and_invalid_transitions_are_refused(self) -> None:
        runtime = self._runtime()
        with self.assertRaises(AutonomyStateError):
            runtime.pause()
        started = runtime.start("monitoring")
        self.assertEqual(started["mode"], "monitoring")
        self.assertTrue(started["lease"]["held"])

        paused = runtime.pause()
        self.assertEqual(paused["mode"], "paused")
        self.assertTrue(paused["lease"]["held"])
        with self.assertRaises(AutonomyStateError):
            runtime.start("monitoring")
        with self.assertRaises(AutonomyStateError):
            runtime.resume("disabled")

        resumed = runtime.resume("autonomous", acknowledge_paper_only=True)
        self.assertEqual(resumed["mode"], "autonomous")
        with self.assertRaises(AutonomyStateError):
            runtime.start("paused")

        runtime.stop()
        self.assertEqual(runtime.status()["mode"], "disabled")
        self.assertTrue(runtime.status()["lease"]["valid"] is False)
        runtime.close()

    def test_autonomous_requires_acknowledgement_and_has_no_trading_path(self) -> None:
        runtime = self._runtime()
        with self.assertRaises(Exception):
            runtime.start("autonomous")
        started = runtime.start("autonomous", acknowledge_paper_only=True)
        self.assertFalse(started["trading_implemented"])
        self.assertFalse(started["orders_possible"])
        self.assertEqual(started["release_gate"], "closed")
        self.assertTrue(started["paper_only"])
        self.assertFalse(started["live_trading"])
        runtime.close()

    def test_lease_renewal_expiry_and_explicit_recovery(self) -> None:
        runtime = self._runtime(lease_seconds=30)
        runtime.start("monitoring")
        self.assertTrue(runtime.heartbeat())
        self.clock.advance(20)
        self.assertEqual(runtime.status()["runtime_state"], "running")
        self.assertTrue(runtime.heartbeat())
        self.clock.advance(61)
        degraded = runtime.status()
        self.assertEqual(degraded["runtime_state"], "degraded")
        self.assertFalse(degraded["lease"]["valid"])
        self.assertFalse(runtime.heartbeat())
        recovered = runtime.recover()
        self.assertEqual(recovered["mode"], "disabled")
        self.assertEqual(recovered["runtime_state"], "stopped")
        self.assertFalse(recovered["recovery_required"])
        self.assertEqual(runtime.start("monitoring")["mode"], "monitoring")
        runtime.close()

    def test_recovery_with_a_live_lease_releases_it_and_allows_restart(self) -> None:
        runtime = self._runtime()
        runtime.start("monitoring")
        recovered = runtime.recover()
        self.assertEqual(recovered["mode"], "disabled")
        self.assertEqual(recovered["runtime_state"], "stopped")
        self.assertFalse(recovered["lease"]["held"])
        self.assertFalse(recovered["lease"]["valid"])
        self.assertEqual(runtime.start("monitoring")["mode"], "monitoring")
        runtime.close()

    def test_one_cycle_at_a_time_and_expired_cycle_recovery(self) -> None:
        runtime = self._runtime(lease_seconds=30)
        runtime.start("monitoring")
        first = runtime.claim_cycle("cycle-1", "score:1")
        self.assertEqual(first["status"], "running")
        with self.assertRaises(AutonomyLeaseError):
            runtime.claim_cycle("cycle-2", "score:2")
        runtime.finish_cycle("cycle-1")
        self.assertIsNone(runtime.status()["active_cycle"])

        runtime.claim_cycle("cycle-3", "score:3")
        self.clock.advance(31)
        self.assertEqual(runtime.status()["runtime_state"], "degraded")
        self.assertEqual(runtime.status()["active_cycle"]["cycle_id"], "cycle-3")
        runtime.recover()
        self.assertIsNone(runtime.status()["active_cycle"])
        self.assertEqual(runtime.status()["mode"], "disabled")
        runtime.close()

    def test_store_schema_is_versioned_and_has_no_decision_or_exit_table(self) -> None:
        store = AutonomyStore(self.path)
        try:
            meta = store._conn.execute("SELECT value FROM autonomy_meta WHERE key = 'schema_version'").fetchone()
            # AT-02 adds the monitoring tables additively; the AT-01 tables stay.
            self.assertEqual(meta["value"], "3")
            tables = {
                row["name"]
                for row in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertEqual(
                tables,
                {
                    "autonomy_meta",
                    "autonomy_config",
                    "autonomy_runtime",
                    "autonomy_cycles",
                    "autonomy_monitoring_config",
                    "autonomy_monitoring_state",
                    "autonomy_monitoring_cycles",
                    "autonomy_monitoring_candidates",
                },
            )
        finally:
            store.close()

    def test_autonomy_package_contains_no_financial_engine_reference(self) -> None:
        # AT-02 deliberately composes the NM-05 discovery engine, so discovery
        # is no longer forbidden; every financial engine root still is.
        forbidden_import_roots = {
            "execution",
            "portfolio",
            "strategy",
            "risk",
            "wallet",
            "learning",
        }
        forbidden_names = {
            "PortfolioEngine",
            "ExecutionEngine",
            "WalletEngine",
            "StrategyEngine",
            "RiskEngine",
        }
        for path in _PACKAGE.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    parts = node.module.split(".")
                    self.assertFalse(
                        any(part in forbidden_import_roots for part in parts),
                        f"{path.name} imports financial module {node.module}",
                    )
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, forbidden_names, f"{path.name} references {node.id}")
                if isinstance(node, ast.Attribute) and node.attr == "apply":
                    self.fail(f"{path.name} references an apply() financial mutation")

    def test_paper_only_constants(self) -> None:
        self.assertTrue(PAPER_ONLY)
        self.assertFalse(TRADING_IMPLEMENTED)


@unittest.skipIf(TestClient is None, "fastapi.testclient requires httpx")
class AutonomyApiTests(unittest.TestCase):
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

    def test_ordinary_auth_cannot_control_and_state_is_unchanged(self) -> None:
        before = self.client.get("/market/autonomy/status", headers=self.auth)
        self.assertEqual(before.status_code, 200, before.text)
        self.assertFalse(before.json()["operator_authorized"])
        blocked = self.client.post(
            "/market/autonomy/start",
            headers=self.auth,
            json={"mode": "monitoring"},
        )
        self.assertEqual(blocked.status_code, 403, blocked.text)
        after = self.client.get("/market/autonomy/status", headers=self.auth)
        self.assertEqual(after.json()["mode"], "disabled")
        self.assertFalse(after.json()["lease"]["held"])

    def test_unauthenticated_requests_are_rejected(self) -> None:
        for method, path in (
            ("get", "/market/autonomy/status"),
            ("post", "/market/autonomy/start"),
            ("post", "/market/autonomy/stop"),
        ):
            response = getattr(self.client, method)(path)
            self.assertEqual(response.status_code, 401, response.text)

    def test_operator_can_control_and_reads_server_authoritative_state(self) -> None:
        started = self.client.post(
            "/market/autonomy/start",
            headers=self.operator,
            json={"mode": "monitoring", "reason": "test"},
        )
        self.assertEqual(started.status_code, 200, started.text)
        body = started.json()
        self.assertEqual(body["mode"], "monitoring")
        self.assertEqual(body["runtime_state"], "running")
        self.assertTrue(body["operator_authorized"])
        self.assertFalse(body["trading_implemented"])
        self.assertFalse(body["orders_possible"])

        status = self.client.get("/market/autonomy/status", headers=self.operator)
        self.assertEqual(status.json()["mode"], "monitoring")
        self.assertTrue(status.json()["operator_authorized"])
        self.assertIn("lease", status.json())

        paused = self.client.post("/market/autonomy/pause", headers=self.operator, json={})
        self.assertEqual(paused.status_code, 200, paused.text)
        self.assertEqual(paused.json()["mode"], "paused")
        resumed = self.client.post(
            "/market/autonomy/resume",
            headers=self.operator,
            json={"mode": "monitoring"},
        )
        self.assertEqual(resumed.status_code, 200, resumed.text)
        stopped = self.client.post("/market/autonomy/stop", headers=self.operator, json={})
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.assertEqual(stopped.json()["mode"], "disabled")

    def test_running_lease_is_renewed_without_a_market_cycle(self) -> None:
        from novacontrol.novamarket.service import NovaMarketService

        with mock.patch.object(
            NovaMarketService,
            "_autonomy_heartbeat_interval_seconds",
            return_value=0.02,
        ):
            with TestClient(self.app) as client:
                started = client.post(
                    "/market/autonomy/start",
                    headers=self.operator,
                    json={"mode": "monitoring", "reason": "heartbeat test"},
                )
                self.assertEqual(started.status_code, 200, started.text)
                first = datetime.fromisoformat(started.json()["lease"]["expires_at"])
                time.sleep(0.2)
                status = client.get(
                    "/market/autonomy/status", headers=self.operator
                )
                self.assertEqual(status.status_code, 200, status.text)
                self.assertEqual(status.json()["runtime_state"], "running")
                renewed = datetime.fromisoformat(status.json()["lease"]["expires_at"])
                self.assertGreater(renewed, first)

    def test_contract_exposes_no_trading_or_order_route(self) -> None:
        contracts = self.client.get("/market/autonomy/contracts", headers=self.auth)
        self.assertEqual(contracts.status_code, 200, contracts.text)
        body = contracts.json()
        self.assertFalse(body["trading_implemented"])
        self.assertFalse(body["orders_possible"])
        self.assertEqual(body["release_gate"], "closed")
        self.assertTrue(body["paper_only"])
        self.assertFalse(body["live_trading"])
        for path in body["routes"]:
            self.assertNotIn("order", path["path"])
            self.assertNotIn("scan", path["path"])
            self.assertNotIn("execute", path["path"])

    def test_operator_token_is_distinct_and_fails_closed_when_unset(self) -> None:
        self._env.stop()
        with mock.patch.dict(os.environ, {"NOVACONTROL_API_TOKEN": "api-only"}, clear=False):
            os.environ.pop("NOVACONTROL_AUTONOMY_OPERATOR_TOKEN", None)
            from novacontrol.api.app import create_app

            tmp = TemporaryDirectory(ignore_cleanup_errors=True)
            try:
                app = create_app(data_dir=tmp.name)
                client = TestClient(app)
                status = client.get("/market/autonomy/status", headers={"Authorization": "Bearer api-only"})
                self.assertEqual(status.status_code, 200, status.text)
                self.assertFalse(status.json()["operator_authorization_configured"])
                refused = client.post(
                    "/market/autonomy/start",
                    headers={"Authorization": "Bearer api-only"},
                    json={"mode": "monitoring"},
                )
                self.assertEqual(refused.status_code, 403, refused.text)
                client.close()
            finally:
                tmp.cleanup()
        self._env.start()

    def test_every_autonomy_route_has_a_consumer_contract(self) -> None:
        expected = {
            ("GET", "/market/autonomy/contracts"),
            ("GET", "/market/autonomy/status"),
            ("GET", "/market/autonomy/health"),
            ("POST", "/market/autonomy/start"),
            ("POST", "/market/autonomy/pause"),
            ("POST", "/market/autonomy/resume"),
            ("POST", "/market/autonomy/stop"),
        }
        self.assertTrue(expected.issubset(ROUTE_CONSUMERS))
        surface = {(route.method, route.path) for route in ApiSurface.default().routes}
        self.assertTrue(expected.issubset(surface))


if __name__ == "__main__":
    unittest.main()
