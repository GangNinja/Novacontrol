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
    AutonomyValidationError,
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

    def test_resume_without_a_mode_restores_the_mode_that_was_paused(self) -> None:
        runtime = self._runtime()
        try:
            runtime.start("autonomous", acknowledge_paper_only=True)
            paused = runtime.pause()
            self.assertEqual(paused["desired_mode"], "autonomous")
            # No mode is supplied: the durable pre-pause mode is the target, and
            # autonomous still requires its explicit paper-only acknowledgement.
            resumed = runtime.resume(acknowledge_paper_only=True)
            self.assertEqual(resumed["mode"], "autonomous")
            self.assertEqual(resumed["desired_mode"], "autonomous")
            self.assertTrue(resumed["paper_only"])
            self.assertFalse(resumed["live_orders_possible"])

            runtime.pause()
            with self.assertRaises(AutonomyValidationError):
                runtime.resume()
            still_autonomous = runtime.resume(acknowledge_paper_only=True)
            self.assertEqual(still_autonomous["mode"], "autonomous")
            self.assertEqual(still_autonomous["desired_mode"], "autonomous")
        finally:
            runtime.close()

    def test_autonomous_requires_acknowledgement_and_enables_only_paper_trading(self) -> None:
        runtime = self._runtime()
        with self.assertRaises(Exception):
            runtime.start("autonomous")
        started = runtime.start("autonomous", acknowledge_paper_only=True)
        self.assertTrue(started["trading_implemented"])
        self.assertTrue(started["orders_possible"])
        self.assertFalse(started["live_orders_possible"])
        self.assertEqual(started["release_gate"], "open")
        self.assertTrue(started["paper_only"])
        self.assertFalse(started["live_trading"])
        self.assertFalse(started["broker_connected"])
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
            # Stage C R-02 adds the append-only control audit additively.
            self.assertEqual(meta["value"], "5")
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
                    "autonomy_control_events",
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
        self.assertTrue(TRADING_IMPLEMENTED)


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
        self.assertTrue(body["trading_implemented"])
        self.assertTrue(body["orders_possible"])
        self.assertFalse(body["live_orders_possible"])

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
        # Omitting the mode restores the paused mode rather than silently
        # downgrading (or escalating) it.
        self.client.post("/market/autonomy/pause", headers=self.operator, json={})
        restored = self.client.post("/market/autonomy/resume", headers=self.operator, json={})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["mode"], "monitoring")
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

    def test_contract_exposes_paper_execution_and_no_live_route(self) -> None:
        contracts = self.client.get("/market/autonomy/contracts", headers=self.auth)
        self.assertEqual(contracts.status_code, 200, contracts.text)
        body = contracts.json()
        self.assertTrue(body["trading_implemented"])
        self.assertTrue(body["orders_possible"])
        self.assertFalse(body["live_orders_possible"])
        self.assertEqual(body["release_gate"], "open")
        self.assertTrue(body["paper_only"])
        self.assertFalse(body["live_trading"])
        self.assertFalse(body["broker_connected"])
        for path in body["routes"]:
            self.assertNotIn("live", path["path"])
            self.assertNotIn("broker", path["path"])

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


class LoopbackAdminKeyTests(unittest.TestCase):
    """The loopback admin key opens every local door and nothing else.

    The local Vite proxy sends one loopback-only credential so a browser tab
    never prompts; it must act as the ordinary API credential and as the
    autonomy-operator credential.  The same key must be worthless from any
    non-loopback client, where only the configured tokens count.  This also
    pins that the operator-token separation survives: an ordinary credential
    still cannot start the runtime.
    """

    _ADMIN = "loopback-admin-key"
    _API = "api-secret"
    _OPERATOR = "operator-secret"

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self._env = mock.patch.dict(
            os.environ,
            {
                "NOVACONTROL_API_TOKEN": self._API,
                "NOVACONTROL_AUTONOMY_OPERATOR_TOKEN": self._OPERATOR,
                "NOVACONTROL_ADMIN_TOKEN": self._ADMIN,
            },
            clear=False,
        )
        self._env.start()
        from novacontrol.api.app import create_app

        self.app = create_app(data_dir=self._tmp.name)
        self.loopback = TestClient(self.app, client=("127.0.0.1", 51100))
        self.remote = TestClient(self.app, client=("10.1.2.3", 51100))

    def tearDown(self) -> None:
        self.loopback.close()
        self.remote.close()
        self._env.stop()
        self._tmp.cleanup()

    def _admin_headers(self) -> dict[str, str]:
        # Exactly what the local Vite proxy sends: both credential headers.
        return {
            "Authorization": f"Bearer {self._ADMIN}",
            "X-Autonomy-Operator-Token": self._ADMIN,
        }

    def test_loopback_admin_key_authorizes_ordinary_and_operator_routes(self) -> None:
        ordinary = self.loopback.get("/status", headers={"Authorization": f"Bearer {self._ADMIN}"})
        self.assertEqual(ordinary.status_code, 200, ordinary.text)

        status = self.loopback.get("/market/autonomy/status", headers=self._admin_headers())
        self.assertEqual(status.status_code, 200, status.text)
        self.assertTrue(status.json()["operator_authorized"])
        self.assertTrue(status.json()["operator_authorization_configured"])

        started = self.loopback.post(
            "/market/autonomy/start",
            headers=self._admin_headers(),
            json={"mode": "monitoring", "reason": "loopback admin key"},
        )
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["mode"], "monitoring")
        self.assertTrue(started.json()["operator_authorized"])

        stopped = self.loopback.post(
            "/market/autonomy/stop", headers=self._admin_headers(), json={}
        )
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.assertEqual(stopped.json()["mode"], "disabled")

    def test_the_same_key_is_rejected_from_a_non_loopback_client(self) -> None:
        ordinary = self.remote.get("/status", headers={"Authorization": f"Bearer {self._ADMIN}"})
        self.assertEqual(ordinary.status_code, 401, ordinary.text)

        status = self.remote.get("/market/autonomy/status", headers=self._admin_headers())
        self.assertEqual(status.status_code, 401, status.text)
        self.assertNotIn("operator_authorized", status.json())

        started = self.remote.post(
            "/market/autonomy/start",
            headers=self._admin_headers(),
            json={"mode": "monitoring"},
        )
        self.assertEqual(started.status_code, 401, started.text)
        refused_status = self.remote.get(
            "/market/autonomy/status", headers={"Authorization": f"Bearer {self._API}"}
        )
        self.assertEqual(refused_status.json()["mode"], "disabled")

        # The configured tokens still work from a remote address, so the
        # refusal above is about the admin key and not about the client.
        configured = self.remote.get("/status", headers={"Authorization": f"Bearer {self._API}"})
        self.assertEqual(configured.status_code, 200, configured.text)
        configured_operator = self.remote.get(
            "/market/autonomy/status",
            headers={
                "Authorization": f"Bearer {self._API}",
                "X-Autonomy-Operator-Token": self._OPERATOR,
            },
        )
        self.assertEqual(configured_operator.status_code, 200, configured_operator.text)
        self.assertTrue(configured_operator.json()["operator_authorized"])

    def test_ordinary_auth_still_cannot_control_the_runtime(self) -> None:
        refused = self.loopback.post(
            "/market/autonomy/start",
            headers={"Authorization": f"Bearer {self._API}"},
            json={"mode": "monitoring"},
        )
        self.assertEqual(refused.status_code, 403, refused.text)
        status = self.loopback.get(
            "/market/autonomy/status", headers={"Authorization": f"Bearer {self._API}"}
        )
        self.assertFalse(status.json()["operator_authorized"])
        self.assertEqual(status.json()["mode"], "disabled")

        # The admin key is not the operator token either: a deployment that
        # never configures it cannot be steered with a guess.
        wrong_key = self.loopback.post(
            "/market/autonomy/start",
            headers={
                "Authorization": f"Bearer {self._API}",
                "X-Autonomy-Operator-Token": "not-the-admin-key",
            },
            json={"mode": "monitoring"},
        )
        self.assertEqual(wrong_key.status_code, 403, wrong_key.text)


class OptInAdminKeyTests(unittest.TestCase):
    """The predictable local key is opt-in, never a built-in default.

    A deployment that configures ``NOVACONTROL_API_TOKEN`` but never exports
    ``NOVACONTROL_ADMIN_TOKEN`` must refuse the old launcher default from
    loopback: an unset admin token disables that credential entirely, so a
    backend reached through a loopback reverse proxy cannot inherit a guessable
    admin key.
    """

    _API = "api-secret"
    _OPERATOR = "operator-secret"
    _OLD_DEFAULT = "narasimha-novacontrol"

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self._env = mock.patch.dict(
            os.environ,
            {
                "NOVACONTROL_API_TOKEN": self._API,
                "NOVACONTROL_AUTONOMY_OPERATOR_TOKEN": self._OPERATOR,
            },
            clear=False,
        )
        self._env.start()
        # patch.dict snapshots the environment, so popping here is restored by
        # tearDown and the surrounding suite keeps its own admin token.
        os.environ.pop("NOVACONTROL_ADMIN_TOKEN", None)
        from novacontrol.api.app import create_app

        self.app = create_app(data_dir=self._tmp.name)
        self.loopback = TestClient(self.app, client=("127.0.0.1", 51100))

    def tearDown(self) -> None:
        self.loopback.close()
        self._env.stop()
        self._tmp.cleanup()

    def _old_key_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._OLD_DEFAULT}",
            "X-Autonomy-Operator-Token": self._OLD_DEFAULT,
        }

    def test_an_unset_admin_token_refuses_the_old_default_from_loopback(self) -> None:
        ordinary = self.loopback.get(
            "/status", headers={"Authorization": f"Bearer {self._OLD_DEFAULT}"}
        )
        self.assertEqual(ordinary.status_code, 401, ordinary.text)

        status = self.loopback.get(
            "/market/autonomy/status", headers=self._old_key_headers()
        )
        self.assertEqual(status.status_code, 401, status.text)

        started = self.loopback.post(
            "/market/autonomy/start",
            headers=self._old_key_headers(),
            json={"mode": "monitoring"},
        )
        self.assertEqual(started.status_code, 401, started.text)

        # Nothing ran: the configured credentials still see a disabled runtime.
        configured = self.loopback.get(
            "/market/autonomy/status",
            headers={
                "Authorization": f"Bearer {self._API}",
                "X-Autonomy-Operator-Token": self._OPERATOR,
            },
        )
        self.assertEqual(configured.status_code, 200, configured.text)
        self.assertEqual(configured.json()["mode"], "disabled")
        # The deployment bearer token still works from loopback, so the refusal
        # above is about the old admin key and not about loopback access.
        self.assertEqual(
            self.loopback.get(
                "/status", headers={"Authorization": f"Bearer {self._API}"}
            ).status_code,
            200,
        )


if __name__ == "__main__":
    unittest.main()
