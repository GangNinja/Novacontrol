"""HTTP surface for NovaForge NF-04, driven through the real create_app().

The routes run against the REAL session manager with only its desktop seams
replaced, so the path under test is route -> lab -> manager -> session record
-> persisted benchmark run. No game is launched and no gameplay input exists.
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
from novacontrol.novaforge import NovaForgeBenchmarkLab, NovaForgeManager


class _BenchmarkApiFixture(_IsolatedApiTestCase):
    def _isolated_application(self, **kwargs: Any):
        app = super()._isolated_application(**kwargs)
        self.desktop = FakeDesktop()
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
        # The lab built in Application.__init__ points at the manager it was
        # constructed with; replacing the manager means replacing its consumer.
        app.novaforge_benchmarks = NovaForgeBenchmarkLab(
            manager=self.manager,
            publish=app._novaforge_publish,
            poll_interval=0.02,
        )
        self.lab = app.novaforge_benchmarks
        return app

    def _request(self, **overrides: Any) -> dict[str, Any]:
        payload = {
            "scenario_id": "observation-baseline",
            "target_title": "NC FORGE TEST",
            "duration_seconds": 1,
            "frame_interval_seconds": 0.05,
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

    def _wait_terminal(self, run_id: str, timeout: float = 12.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self._get_ok(f"/novaforge/benchmarks/runs/{run_id}")
            if run["status"] != "running":
                return run
            time.sleep(0.03)
        self.fail(f"benchmark run {run_id} did not finish in time")


class BenchmarkCatalogApiTests(_BenchmarkApiFixture):
    def test_catalog_lists_supported_and_unsupported_scenarios(self) -> None:
        body = self._get_ok("/novaforge/benchmarks/scenarios")
        by_id = {item["scenario_id"]: item for item in body["scenarios"]}
        self.assertEqual(body["supported_count"], 4)
        self.assertEqual(body["unsupported_count"], 1)
        self.assertTrue(by_id["observation-baseline"]["supported"])
        self.assertTrue(by_id["controlled-objective"]["supported"])
        self.assertTrue(by_id["controlled-objective"]["objective_required"])
        self.assertFalse(by_id["gameplay-objective"]["supported"])
        self.assertTrue(body["metric_catalog"])

    def test_scenario_detail_and_unknown_id(self) -> None:
        body = self._get_ok("/novaforge/benchmarks/scenarios/observation-baseline")
        self.assertEqual(body["scenario_id"], "observation-baseline")
        self.assertEqual(body["version"], 1)
        self.assertTrue(body["metric_definitions"])
        self.assertTrue(body["unsupported_metrics"])

        missing = self._client.get("/novaforge/benchmarks/scenarios/no-such-scenario")
        self.assertEqual(missing.status_code, 404)
        wrong_version = self._client.get("/novaforge/benchmarks/scenarios/observation-baseline?version=9")
        self.assertEqual(wrong_version.status_code, 404)

    def test_preflight_reports_the_live_window_and_starts_nothing(self) -> None:
        body = self._post_ok("/novaforge/benchmarks/preflight", self._request())
        self.assertTrue(body["valid"], body["errors"])
        self.assertTrue(body["runnable"])
        self.assertEqual(body["target"]["width"], 1000)
        self.assertEqual(body["config"]["model_id"], "")
        self.assertEqual(self._get_ok("/novaforge/benchmarks/runs")["count"], 0)

    def test_preflight_rejects_an_unknown_window(self) -> None:
        body = self._post_ok(
            "/novaforge/benchmarks/preflight", self._request(target_title="No Such Window")
        )
        self.assertFalse(body["valid"])
        self.assertTrue(any("No open window matches" in item for item in body["errors"]))
        self.assertIsNone(body["target"])

    def test_preflight_reports_an_unsupported_scenario(self) -> None:
        body = self._post_ok(
            "/novaforge/benchmarks/preflight",
            {"scenario_id": "gameplay-objective", "target_title": "NC FORGE TEST"},
        )
        self.assertFalse(body["valid"])
        self.assertIsNone(body["config"])
        self.assertTrue(any("decision pipeline" in item for item in body["errors"]))

    def test_start_rejects_invalid_configuration_with_422(self) -> None:
        response = self._client.post(
            "/novaforge/benchmarks/runs",
            json=self._request(
                scenario_id="capture-latency",
                duration_seconds=2,
                frame_interval_seconds=0.25,
            ),
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("at least 20 frames", response.json()["detail"])
        self.assertEqual(self._get_ok("/novaforge/benchmarks/runs")["count"], 0)

    def test_start_rejects_an_unsupported_scenario_with_422(self) -> None:
        response = self._client.post(
            "/novaforge/benchmarks/runs",
            json={"scenario_id": "gameplay-objective", "target_title": "NC FORGE TEST"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("decision pipeline", response.json()["detail"])
        self.assertEqual(self._get_ok("/novaforge/benchmarks/runs")["count"], 0)

    def test_unknown_run_and_cancel_are_404(self) -> None:
        self.assertEqual(self._client.get("/novaforge/benchmarks/runs/nope").status_code, 404)
        self.assertEqual(
            self._client.post("/novaforge/benchmarks/runs/nope/cancel").status_code, 404
        )

    def test_compare_and_aggregate_unknown_ids(self) -> None:
        self.assertEqual(
            self._client.get("/novaforge/benchmarks/compare?left=nope&right=also").status_code,
            404,
        )
        self.assertEqual(
            self._client.get("/novaforge/benchmarks/aggregate?scenario=nope").status_code, 404
        )


class BenchmarkRunApiTests(_BenchmarkApiFixture):
    def test_a_run_completes_and_records_evidence(self) -> None:
        run = self._post_ok("/novaforge/benchmarks/runs", self._request())
        self.assertEqual(run["status"], "running")
        finished = self._wait_terminal(run["id"])
        self.assertEqual(finished["status"], "completed", finished["reason"])
        self.assertTrue(finished["valid"])
        self.assertGreater(finished["metrics"]["frames"]["value"], 0)
        self.assertIsNone(finished["objective"]["achieved"])
        self.assertFalse(finished["objective"]["configured"])
        self.assertFalse(finished["objective"]["decided"])
        self.assertEqual(finished["objective"]["outcome"], "unsupported")
        self.assertTrue(finished["evidence"]["frames"])
        self.assertIn(
            f"/novaforge/sessions/{finished['session_id']}/timeline",
            finished["evidence"]["timeline_endpoint"],
        )

        history = self._get_ok("/novaforge/benchmarks/runs")
        self.assertEqual(history["count"], 1)
        self.assertEqual(history["runs"][0]["id"], run["id"])
        self.assertIsNone(history["active_run_id"])

    def test_duplicate_start_is_a_conflict_and_cancel_is_idempotent(self) -> None:
        first = self._post_ok(
            "/novaforge/benchmarks/runs",
            self._request(duration_seconds=10, frame_interval_seconds=0.1),
        )
        second = self._client.post("/novaforge/benchmarks/runs", json=self._request())
        self.assertEqual(second.status_code, 409)
        self.assertIn("still active", second.json()["detail"])

        cancelled = self._post_ok(
            f"/novaforge/benchmarks/runs/{first['id']}/cancel", {}
        )
        self.assertFalse(cancelled["already_stopped"])
        self.assertEqual(cancelled["run"]["status"], "aborted")
        self.assertFalse(cancelled["run"]["valid"])

        again = self._post_ok(f"/novaforge/benchmarks/runs/{first['id']}/cancel", {})
        self.assertTrue(again["already_stopped"])

    def test_aggregate_excludes_an_aborted_attempt(self) -> None:
        completed = self._post_ok("/novaforge/benchmarks/runs", self._request())
        self._wait_terminal(completed["id"])

        aborted = self._post_ok(
            "/novaforge/benchmarks/runs",
            self._request(duration_seconds=10, frame_interval_seconds=0.1),
        )
        cancelled = self._post_ok(f"/novaforge/benchmarks/runs/{aborted['id']}/cancel", {})
        self.assertEqual(cancelled["run"]["status"], "aborted")

        aggregate = self._get_ok("/novaforge/benchmarks/aggregate?scenario=observation-baseline")
        self.assertEqual(aggregate["attempts"], 2)
        self.assertEqual(aggregate["sample_size"], 1)
        self.assertEqual(aggregate["excluded_count"], 1)
        self.assertIsNone(aggregate["metrics"]["frames"]["stdev"])
        self.assertFalse(aggregate["objective_verified"])

    def test_two_compatible_runs_compare_without_claiming_superiority(self) -> None:
        first = self._post_ok("/novaforge/benchmarks/runs", self._request())
        self._wait_terminal(first["id"])
        second = self._post_ok("/novaforge/benchmarks/runs", self._request())
        self._wait_terminal(second["id"])

        comparison = self._get_ok(
            f"/novaforge/benchmarks/compare?left={first['id']}&right={second['id']}"
        )
        self.assertTrue(comparison["compatible"], comparison["reasons"])
        self.assertEqual(comparison["sample"], {"left": 1, "right": 1})
        self.assertFalse(comparison["verdict"]["established"])
        self.assertIn("no model", comparison["verdict"]["reason"].lower())
        frames = next(row for row in comparison["metrics"] if row["key"] == "frames")
        self.assertIn("left", frames)
        self.assertIn("right", frames)

        self_compare = self._client.get(
            f"/novaforge/benchmarks/compare?left={first['id']}&right={first['id']}"
        )
        self.assertEqual(self_compare.status_code, 422)

    def test_cancelled_run_detail_is_honest(self) -> None:
        run = self._post_ok(
            "/novaforge/benchmarks/runs",
            self._request(duration_seconds=10, frame_interval_seconds=0.1),
        )
        self._post_ok(f"/novaforge/benchmarks/runs/{run['id']}/cancel", {})
        detail = self._get_ok(f"/novaforge/benchmarks/runs/{run['id']}")
        self.assertEqual(detail["status"], "aborted")
        self.assertFalse(detail["valid"])
        self.assertIn("Cancelled by the operator", detail["reason"])
        self.assertFalse(detail["objective"]["data_complete"])
        self.assertIsNone(detail["objective"]["achieved"])


class BenchmarkAuthTests(_BenchmarkApiFixture):
    def test_routes_require_the_configured_token(self) -> None:
        with mock.patch.dict(os.environ, {"NOVACONTROL_API_TOKEN": "s3cret"}):
            client = TestClient(create_app())
            client.__enter__()
            try:
                unauthenticated = client.get("/novaforge/benchmarks/scenarios")
                self.assertEqual(unauthenticated.status_code, 401)
                missing_on_start = client.post(
                    "/novaforge/benchmarks/runs", json=self._request()
                )
                self.assertEqual(missing_on_start.status_code, 401)
                authenticated = client.get(
                    "/novaforge/benchmarks/scenarios",
                    headers={"Authorization": "Bearer s3cret"},
                )
                self.assertEqual(authenticated.status_code, 200)
            finally:
                client.__exit__(None, None, None)
