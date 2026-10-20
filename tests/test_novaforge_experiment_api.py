"""HTTP surface for NovaForge NF-06, driven through the real create_app().

The routes run against the REAL session manager, the REAL NF-04 benchmark lab,
the REAL NF-05 evaluation lab and the REAL NF-06 Coach / experiment lab, with
only the desktop seam replaced. No game is launched and no gameplay input
exists, so nothing here can claim a gameplay improvement.
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from typing import Any

try:
    from fastapi.testclient import TestClient  # noqa: F401 - requires httpx
except Exception:  # pragma: no cover - httpx is an optional dev dependency
    TestClient = None  # type: ignore[assignment]

from test_web_api import _IsolatedApiTestCase
from test_novaforge import FakeDesktop

from novacontrol.api.app import create_app
from novacontrol.novaforge import (
    NovaForgeBenchmarkLab,
    NovaForgeCoach,
    NovaForgeEvaluationLab,
    NovaForgeExperimentLab,
    NovaForgeManager,
)
from novacontrol.persistence.json_store import JsonStateStore

TARGET = "NC FORGE TEST"


class _ExperimentApiFixture(_IsolatedApiTestCase):
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
        self.evaluations = NovaForgeEvaluationLab(
            benchmark=self.benchmark,
            store=self.store,
            publish=app._novaforge_publish,
        )
        app.novaforge_evaluations = self.evaluations
        self.coach = NovaForgeCoach(
            manager=self.manager,
            benchmark=self.benchmark,
            evaluations=self.evaluations,
            store=self.store,
            publish=app._novaforge_publish,
        )
        app.novaforge_coach = self.coach
        self.experiments = NovaForgeExperimentLab(
            evaluations=self.evaluations,
            store=self.store,
            publish=app._novaforge_publish,
            permission=lambda: {
                "checked": True,
                "allowed": True,
                "source": "fixture",
                "reason": "The fixture permission layer allows observation.",
            },
        )
        app.novaforge_experiments = self.experiments
        return app

    def _request(self, **overrides: Any) -> dict[str, Any]:
        payload = {
            "game_id": "disposable-window",
            "scenario_id": "observation-baseline",
            "target_title": TARGET,
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

    def _post_ok(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._client.post(path, json=payload if payload is not None else {})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _wait_evaluation(self, evaluation_id: str, timeout: float = 20.0) -> dict[str, Any]:
        # Waited through the lab, not the poll loop: the HTTP surface allows 120
        # requests a minute and a fast poll would spend that budget on waiting.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.evaluations.get(evaluation_id)
            if record is not None and record["status"] != "running":
                return record
            time.sleep(0.03)
        self.fail(f"evaluation {evaluation_id} did not finish in time")

    def _evaluate(self, **overrides: Any) -> dict[str, Any]:
        started = self._post_ok("/novaforge/evaluations", self._request(**overrides))
        return self._wait_evaluation(str(started["id"]))

    def _wait_experiment(self, experiment_id: str, timeout: float = 60.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.experiments.get(experiment_id)
            if record is not None and record["status"] not in {"running", "approved"}:
                return record
            time.sleep(0.05)
        self.fail(f"experiment {experiment_id} did not finish in time")


class CoachApiTests(_ExperimentApiFixture):
    def test_the_capability_card_names_its_vocabularies_and_limits(self) -> None:
        body = self._get_ok("/novaforge/coach")
        self.assertEqual(body["name"], "Forge Coach")
        self.assertTrue(body["analysis"]["read_only"])
        self.assertFalse(body["analysis"]["caches"])
        self.assertEqual(
            body["analysis"]["finding_kinds"],
            ["observed", "derived", "hypothesis", "unknown"],
        )
        self.assertIn("proposed", body["recommendations"]["statuses"])
        self.assertTrue(body["limits"])
        self.assertIn("no objective verifier", " ".join(body["limits"]).lower())

    def test_an_analysis_of_a_recorded_evaluation_is_read_only_and_cited(self) -> None:
        evaluation = self._evaluate()
        before = self._get_ok(f"/novaforge/evaluations/{evaluation['id']}")
        body = self._post_ok(
            "/novaforge/coach/analyse", {"evaluation_id": evaluation["id"]}
        )
        self.assertTrue(body["read_only"])
        # The pinned window identity travels with the subject; a record that
        # holds it must never be reported as "not recorded".
        window = self.desktop.window
        assert window is not None
        self.assertEqual(body["subject"]["target"]["title"], window.title)
        self.assertEqual(body["subject"]["target"]["handle"], window.handle)
        self.assertTrue(body["findings"])
        for finding in body["findings"]:
            self.assertTrue(finding["evidence"])
            self.assertIn("alternatives", finding)
        self.assertFalse(body["earliest_recoverable_error"]["causation_claimed"])
        # Recomputing cannot modify the record it read.
        after = self._get_ok(f"/novaforge/evaluations/{evaluation['id']}")
        self.assertEqual(before, after)

    def test_an_empty_subject_is_refused_and_an_unknown_one_is_not_found(self) -> None:
        refused = self._client.post("/novaforge/coach/analyse", json={})
        self.assertEqual(refused.status_code, 422, refused.text)
        missing = self._client.post(
            "/novaforge/coach/analyse", json={"session_id": "no-such-session"}
        )
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_a_recommendation_decision_is_persisted_operator_state(self) -> None:
        evaluation = self._evaluate()
        analysis = self._post_ok(
            "/novaforge/coach/analyse", {"evaluation_id": evaluation["id"]}
        )
        recommendation = analysis["recommendations"][0]
        decided = self._post_ok(
            f"/novaforge/coach/recommendations/{recommendation['recommendation_id']}",
            {"status": "approved", "note": "worth a repeat"},
        )
        self.assertEqual(decided["status"], "approved")
        self.assertEqual(decided["persistence"]["state"], "verified")

        listed = self._get_ok("/novaforge/coach/recommendations?status=approved")
        self.assertEqual(listed["count"], 1)
        self.assertEqual(
            listed["recommendations"][0]["recommendation_id"],
            recommendation["recommendation_id"],
        )
        self.assertIn("not evidence", listed["note"])

        unknown = self._client.post(
            "/novaforge/coach/recommendations/not-an-id", json={"status": "approved"}
        )
        self.assertEqual(unknown.status_code, 404, unknown.text)
        bad_status = self._client.post(
            "/novaforge/coach/recommendations/rec-000000000000", json={"status": "vibes"}
        )
        self.assertEqual(bad_status.status_code, 422, bad_status.text)


class ExperimentApiTests(_ExperimentApiFixture):
    def _proposal(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "game_id": "disposable-window",
            "scenario_id": "observation-baseline",
            "target_title": TARGET,
            "kind": "repeat",
            "variable": "none",
            "primary_metric": "frames",
            "repeat_count": 1,
        }
        payload.update(overrides)
        return payload

    def test_an_unsupported_spec_is_retained_blocked_instead_of_an_error_page(self) -> None:
        blocked = self._post_ok(
            "/novaforge/experiments",
            self._proposal(
                kind="configuration_change",
                variable="duration_seconds",
                change={"duration_seconds": 2, "frame_interval_seconds": 0.1},
            ),
        )
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("exactly the declared variable", " ".join(blocked["errors"]))
        # A blocked proposal cannot be approved, and the refusal is a 422 with reasons.
        refused = self._client.post(f"/novaforge/experiments/{blocked['id']}/approve", json={})
        self.assertEqual(refused.status_code, 422, refused.text)

    def test_propose_approve_run_then_read_the_results(self) -> None:
        proposal = self._post_ok("/novaforge/experiments", self._proposal())
        self.assertEqual(proposal["status"], "proposed", proposal["errors"])
        experiment_id = str(proposal["id"])
        self.assertEqual(self._get_ok(f"/novaforge/experiments/{experiment_id}")["status"], "proposed")

        approved = self._post_ok(f"/novaforge/experiments/{experiment_id}/approve", {})
        self.assertTrue(approved["approval"])

        self._post_ok(f"/novaforge/experiments/{experiment_id}/run")
        self._wait_experiment(experiment_id)
        finished = self._get_ok(f"/novaforge/experiments/{experiment_id}")
        self.assertEqual(finished["status"], "completed", finished["reason"])
        results = self._get_ok(f"/novaforge/experiments/{experiment_id}/results")
        self.assertTrue(results["available"])
        self.assertEqual(results["results"]["verdict"]["label"], "inconclusive")
        self.assertFalse(results["results"]["verdict"]["improvement_claimed"])
        self.assertFalse(results["results"]["verdict"]["gameplay_claimed"])

    def test_a_run_without_an_approval_is_refused_and_unknown_ids_are_not_found(self) -> None:
        proposal = self._post_ok("/novaforge/experiments", self._proposal())
        refused = self._client.post(f"/novaforge/experiments/{proposal['id']}/run")
        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertIn("approv", refused.json()["detail"].lower())

        self.assertEqual(
            self._client.get("/novaforge/experiments/no-such-experiment").status_code, 404
        )
        self.assertEqual(
            self._client.get("/novaforge/experiments/no-such-experiment/results").status_code,
            404,
        )
        # A known experiment that has NOT finished answers "not available yet"
        # (200) rather than pretending an empty comparison is a result.
        pending = self._get_ok(f"/novaforge/experiments/{proposal['id']}/results")
        self.assertFalse(pending["available"])
        self.assertIn("not finished", pending["reason"])

    def test_a_recommendation_proposal_is_bound_to_its_finding(self) -> None:
        self.desktop.fail = True
        evaluation = self._evaluate(duration_seconds=2)
        analysis = self._post_ok(
            "/novaforge/coach/analyse", {"evaluation_id": evaluation["id"]}
        )
        recommendation = next(
            item for item in analysis["recommendations"] if item["category"] == "capture_failure"
        )
        proposal = self._post_ok(
            "/novaforge/experiments",
            {
                "recommendation_id": recommendation["recommendation_id"],
                "evaluation_id": evaluation["id"],
            },
        )
        self.assertEqual(proposal["status"], "proposed", proposal["errors"])
        spec = proposal["spec"]
        self.assertEqual(spec["subject"]["finding_id"], recommendation["finding_id"])
        self.assertEqual(spec["subject"]["recommendation_id"], recommendation["recommendation_id"])
        self.assertEqual(spec["scenario_id"], "capture-latency")
        self.assertEqual(spec["primary_metric"], "capture_failures")

        missing = self._client.post(
            "/novaforge/experiments",
            json={
                "recommendation_id": "rec-000000000000",
                "evaluation_id": evaluation["id"],
            },
        )
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_the_history_lists_retained_experiments_with_their_state(self) -> None:
        first = self._post_ok("/novaforge/experiments", self._proposal())
        self._post_ok(f"/novaforge/experiments/{first['id']}/reject", {"note": "not now"})
        body = self._get_ok("/novaforge/experiments")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["experiments"][0]["status"], "rejected")
        self.assertEqual(body["experiments"][0]["reason"], "not now")
        self.assertFalse(body["experiments"][0]["approved"])
        filtered = self._get_ok("/novaforge/experiments?status=proposed")
        self.assertEqual(filtered["count"], 0)
        self.assertEqual(
            self._client.get("/novaforge/experiments?status=vibes").status_code, 422
        )
