"""HTTP surface for NovaForge NF-05, driven through the real create_app().

The routes run against the REAL session manager, the REAL NF-04 benchmark lab
and the REAL NF-05 evaluation lab, with only the desktop seam replaced. No game
is launched and no gameplay input exists.
"""

from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

try:
    from fastapi.testclient import TestClient  # noqa: F401 - requires httpx
except Exception:  # pragma: no cover - httpx is an optional dev dependency
    TestClient = None  # type: ignore[assignment]

from test_web_api import _IsolatedApiTestCase
from test_novaforge import FakeDesktop

from novacontrol.api.app import create_app
from novacontrol.novaforge import (
    EVALUATIONS_NAMESPACE,
    NovaForgeBenchmarkLab,
    NovaForgeEvaluationLab,
    NovaForgeManager,
)
from novacontrol.persistence.json_store import JsonStateStore


class _EvaluationApiFixture(_IsolatedApiTestCase):
    def _isolated_application(self, **kwargs: Any):
        app = super()._isolated_application(**kwargs)
        self.desktop = FakeDesktop()
        self.store = JsonStateStore(self._tmp.name)
        self.manager = NovaForgeManager(
            data_dir=Path(self._tmp.name),
            capture=self.desktop.capture,
            geometry=self.desktop.geometry,
            window_lines=self.desktop.window_lines,
            release_inputs=self.desktop.release,
            permissions=app.risk,
            publish=app._novaforge_publish,
            capture_supported=True,
            frame_interval=0.05,
            pause_timeout=2.0,
            stop_timeout=4.0,
        )
        app.novaforge = self.manager
        self.benchmark = NovaForgeBenchmarkLab(
            manager=self.manager,
            store=self.store,
            publish=app._novaforge_publish,
            poll_interval=0.02,
        )
        app.novaforge_benchmarks = self.benchmark
        self.lab = NovaForgeEvaluationLab(
            benchmark=self.benchmark,
            store=self.store,
            publish=app._novaforge_publish,
        )
        app.novaforge_evaluations = self.lab
        return app

    def _request(self, **overrides: Any) -> dict[str, Any]:
        payload = {
            "game_id": "disposable-window",
            "scenario_id": "observation-baseline",
            "target_title": "NC FORGE TEST",
            "duration_seconds": 1,
            "frame_interval_seconds": 0.05,
            "confirm": True,
        }
        payload.update(overrides)
        return payload

    def _get_ok(self, path: str) -> dict[str, Any]:
        response = self._client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _post_ok(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(path, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _wait_terminal(self, evaluation_id: str, timeout: float = 15.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self._get_ok(f"/novaforge/evaluations/{evaluation_id}")
            if record["status"] != "running":
                return record
            time.sleep(0.03)
        self.fail(f"evaluation {evaluation_id} did not finish in time")


class EvaluationRegistryApiTests(_EvaluationApiFixture):
    def test_the_registry_separates_compatibility_from_control(self) -> None:
        body = self._get_ok("/novaforge/games")
        self.assertEqual(body["controllable_count"], 0)
        self.assertEqual(body["observation_only_count"], 2)
        self.assertEqual(body["runnable_count"], 2)
        by_id = {item["game_id"]: item for item in body["games"]}
        self.assertEqual(by_id["disposable-window"]["status"], "OBSERVATION_ONLY")
        self.assertEqual(by_id["gameplay-objective"]["status"], "UNSUPPORTED")
        self.assertFalse(by_id["disposable-window"]["input"]["available"])
        self.assertFalse(by_id["disposable-window"]["launch"]["available"])

    def test_one_environment_and_an_unknown_one(self) -> None:
        body = self._get_ok("/novaforge/games/disposable-window")
        self.assertEqual(body["game_id"], "disposable-window")
        self.assertTrue(body["scenarios"])
        self.assertEqual(body["validation"], [])
        self.assertTrue(body["limitations"])
        self.assertEqual(
            self._client.get("/novaforge/games/no-such-game").status_code, 404
        )
        self.assertEqual(
            self._client.get("/novaforge/games/disposable-window?version=9").status_code,
            404,
        )


class EvaluationPreflightApiTests(_EvaluationApiFixture):
    def test_preflight_reports_the_checks_and_starts_nothing(self) -> None:
        body = self._post_ok("/novaforge/evaluations/preflight", self._request())
        self.assertTrue(body["ready"], body["blocking"])
        self.assertEqual(body["identity"]["handle"], self.desktop.window.handle)
        self.assertGreaterEqual(len(body["checks"]), 10)
        self.assertFalse(body["baseline"]["available"])
        self.assertIn("starts nothing", body["note"])
        self.assertIsNone(self.manager.active())
        self.assertEqual(self._get_ok("/novaforge/evaluations")["count"], 0)

    def test_preflight_reports_a_refusal_without_raising(self) -> None:
        body = self._post_ok(
            "/novaforge/evaluations/preflight", self._request(game_id="no-such-game")
        )
        self.assertFalse(body["ready"])
        self.assertTrue(body["errors"])

    def test_the_baseline_route_is_not_shadowed_by_the_detail_route(self) -> None:
        body = self._get_ok(
            "/novaforge/evaluations/baseline?game=disposable-window&scenario=observation-baseline"
        )
        self.assertFalse(body["available"])
        self.assertIn("first completed evaluation", body["reason"])
        self.assertEqual(
            self._client.get(
                "/novaforge/evaluations/baseline?game=nope"
            ).status_code,
            404,
        )


class EvaluationRunApiTests(_EvaluationApiFixture):
    def test_a_confirmed_evaluation_completes_without_claiming_an_objective(self) -> None:
        started = self._post_ok("/novaforge/evaluations", self._request())
        self.assertEqual(started["status"], "running")
        finished = self._wait_terminal(started["id"])
        self.assertEqual(finished["status"], "completed", finished["reason"])
        self.assertTrue(finished["valid"])
        self.assertFalse(finished["objective"]["available"])
        self.assertFalse(finished["objective"]["verified"])
        self.assertIsNone(finished["objective"]["achieved"])
        self.assertEqual(finished["baseline"]["role"], "baseline")
        self.assertTrue(finished["benchmark_run_id"])
        self.assertTrue(finished["session_id"])

        history = self._get_ok("/novaforge/evaluations")
        self.assertEqual(history["count"], 1)
        self.assertEqual(history["evaluations"][0]["id"], started["id"])

        report = self._get_ok(f"/novaforge/evaluations/{started['id']}/report")
        self.assertEqual(report["evaluation_id"], started["id"])
        self.assertFalse(report["objective_verified"])
        self.assertFalse(report["real_gameplay_control_exercised"])
        self.assertEqual(report["compatibility"]["status"], "OBSERVATION_ONLY")
        self.assertTrue(report["evidence"]["timeline"])
        self.assertTrue(report["limitations"])

    def test_start_requires_confirmation(self) -> None:
        response = self._client.post(
            "/novaforge/evaluations", json=self._request(confirm=False)
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("explicitly confirmed", response.json()["detail"])
        self.assertEqual(self._get_ok("/novaforge/evaluations")["count"], 0)

    def test_unsupported_start_is_refused_and_retained(self) -> None:
        response = self._client.post(
            "/novaforge/evaluations", json=self._request(game_id="gameplay-objective")
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("decision pipeline", response.json()["detail"])
        history = self._get_ok("/novaforge/evaluations")
        self.assertEqual(history["count"], 1)
        self.assertEqual(history["evaluations"][0]["status"], "unsupported")
        self.assertEqual(self.benchmark.history()["count"], 0)

    def test_a_duplicate_start_is_a_conflict_and_cancel_is_idempotent(self) -> None:
        first = self._post_ok(
            "/novaforge/evaluations",
            self._request(duration_seconds=10, frame_interval_seconds=0.1),
        )
        second = self._client.post("/novaforge/evaluations", json=self._request())
        self.assertEqual(second.status_code, 409)
        self.assertIn("still active", second.json()["detail"])

        cancelled = self._post_ok(
            f"/novaforge/evaluations/{first['id']}/cancel", {}
        )
        self.assertFalse(cancelled["already_stopped"])
        self.assertEqual(cancelled["evaluation"]["status"], "aborted")
        self.assertFalse(cancelled["evaluation"]["valid"])
        again = self._post_ok(f"/novaforge/evaluations/{first['id']}/cancel", {})
        self.assertTrue(again["already_stopped"])

    def test_unknown_evaluation_routes_are_404(self) -> None:
        self.assertEqual(self._client.get("/novaforge/evaluations/nope").status_code, 404)
        self.assertEqual(
            self._client.post("/novaforge/evaluations/nope/cancel", json={}).status_code, 404
        )
        self.assertEqual(
            self._client.get("/novaforge/evaluations/nope/report").status_code, 404
        )

    def test_a_baseline_comparison_is_not_claimed_as_a_win(self) -> None:
        first = self._post_ok("/novaforge/evaluations", self._request())
        self._wait_terminal(first["id"])
        second = self._post_ok("/novaforge/evaluations", self._request())
        finished = self._wait_terminal(second["id"])
        self.assertEqual(finished["baseline"]["evaluation_id"], first["id"])
        comparison = finished["comparison"]
        self.assertIsNotNone(comparison)
        assert comparison is not None
        self.assertTrue(comparison["compatible"], comparison["reasons"])
        self.assertFalse(comparison["verdict"]["established"])
        baseline = self._get_ok(
            "/novaforge/evaluations/baseline?game=disposable-window&scenario=observation-baseline"
        )
        self.assertTrue(baseline["available"])
        self.assertEqual(baseline["evaluation_id"], first["id"])


class EvaluationAuthTests(_EvaluationApiFixture):
    def test_a_report_for_a_partial_stored_record_is_200_not_500(self) -> None:
        """A stored record missing evidence/limitations must not break the route."""
        record = self._post_ok("/novaforge/evaluations", self._request())
        self._wait_terminal(record["id"])
        payload = self.store.read(EVALUATIONS_NAMESPACE)
        for row in payload["evaluations"]:
            row.pop("evidence", None)
            row.pop("limitations", None)
        self.store.write(EVALUATIONS_NAMESPACE, payload)
        # A process restart reads that partial record back; the route must still
        # answer with a report rather than a KeyError turned into a 500.
        assert self._nova is not None
        self._nova.novaforge_evaluations = NovaForgeEvaluationLab(
            benchmark=self.benchmark, store=JsonStateStore(self._tmp.name)
        )
        report = self._get_ok(f"/novaforge/evaluations/{record['id']}/report")
        self.assertEqual(report["evaluation_id"], record["id"])
        self.assertEqual(report["evidence"], {})
        self.assertTrue(report["limitations"])
        self.assertIsInstance(report["missing_evidence"], list)
        self.assertFalse(report["objective_verified"])
        self.assertFalse(report["real_gameplay_control_exercised"])

    def test_routes_require_the_configured_token(self) -> None:
        with mock.patch.dict(os.environ, {"NOVACONTROL_API_TOKEN": "s3cret"}):
            client = TestClient(create_app())
            client.__enter__()
            try:
                unauthenticated = client.get("/novaforge/games")
                self.assertEqual(unauthenticated.status_code, 401)
                missing_on_start = client.post(
                    "/novaforge/evaluations", json=self._request()
                )
                self.assertEqual(missing_on_start.status_code, 401)
                authenticated = client.get(
                    "/novaforge/games",
                    headers={"Authorization": "Bearer s3cret"},
                )
                self.assertEqual(authenticated.status_code, 200)
            finally:
                client.__exit__(None, None, None)
