"""HTTP surface for the gameplay remediation: capabilities, actions, verification.

The routes are driven through the real ``create_app()`` and the real session
manager; only its desktop seams, the injector and the window probe are replaced,
so what is tested is the route -> manager -> Guardian -> runner -> verifier path
the panel uses.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

try:
    from fastapi.testclient import TestClient  # noqa: F401 - requires httpx
except Exception:  # pragma: no cover - httpx is an optional dev dependency
    TestClient = None  # type: ignore[assignment]

from test_novaforge import FakeDesktop, _mission
from test_novaforge_api import _ForgeApiFixture
from test_novaforge_gameplay import RecordingInput, geometry

from novacontrol.novaforge import NovaForgeManager


class _GameplayApiFixture(_ForgeApiFixture):
    """The real application with a recording injector and a fake window probe."""

    def _isolated_application(self, **kwargs: Any):
        app = super()._isolated_application(**kwargs)
        # Replace the manager again, now with the two remediation seams: an
        # injector (records instead of touching the desktop) and a probe the
        # verifier reads the window from.
        self.desktop = FakeDesktop()
        self.input = RecordingInput()
        self.probe_window: Any = geometry(title=self.desktop.window.title)

        async def probe(_handle: int) -> Any:
            return self.probe_window

        self.manager = NovaForgeManager(
            data_dir=Path(self._tmp.name),
            capture=self.desktop.capture,
            geometry=self.desktop.geometry,
            window_lines=self.desktop.window_lines,
            release_inputs=self.desktop.release,
            action_input=self.input,
            objective_probe=probe,
            permissions=app.risk,
            publish=app._novaforge_publish,
            capture_supported=True,
            frame_interval=0.05,
            pause_timeout=2.0,
            stop_timeout=4.0,
        )
        app.novaforge = self.manager
        return app

    def _validated_session(self, **overrides: Any) -> dict[str, Any]:
        session = self._start(**overrides)
        session_id = session["session_id"]
        self._wait(lambda: self.manager.get(session_id).target_validated)
        return session


class NovaForgeCapabilityApiTests(_GameplayApiFixture):
    def test_capabilities_report_the_wired_runtime_and_each_gap(self) -> None:
        body = self._client.get("/novaforge/capabilities").json()
        capabilities = body["capabilities"]
        self.assertTrue(capabilities["action_execution"]["available"])
        self.assertTrue(capabilities["objective_verification"]["available"])
        self.assertTrue(capabilities["guardian_enforcement"]["action_budget_enforced"])
        pipeline = capabilities["decision_pipeline"]
        self.assertTrue(pipeline["available"], pipeline.get("reason"))
        self.assertFalse(pipeline["policy"]["model_backed"])
        self.assertEqual(pipeline["policy"]["policy"], "HarnessReadyPolicy")
        self.assertTrue(pipeline["safety"]["guarded_actions"])
        self.assertFalse(capabilities["game_launch"]["available"])
        self.assertIn("action_policy", capabilities)
        self.assertEqual(body["policy"]["allowed_buttons"], ["left"])

    def test_the_installed_policies_are_listed_with_honest_provenance(self) -> None:
        """NF-10: a caller can see the candidate AND the baseline before choosing."""
        pipeline = self._client.get("/novaforge/capabilities").json()["capabilities"][
            "decision_pipeline"
        ]
        rows = {row["id"]: row for row in pipeline["policies"]}
        self.assertEqual(set(rows), {"ready", "stages", "fixed"})
        self.assertEqual(rows["stages"]["policy"], "HarnessStagesPolicy")
        self.assertEqual(rows["stages"]["version"], 1)
        self.assertFalse(rows["stages"]["model_backed"])
        self.assertIn("baseline", rows["fixed"]["kind"])
        self.assertEqual(rows["ready"]["policy"], "HarnessReadyPolicy")

    def test_unknown_session_is_a_404_on_every_new_route(self) -> None:
        for method, path, payload in (
            ("post", "/novaforge/sessions/nope/objective", {"kind": "window_title", "parameter": "x"}),
            ("post", "/novaforge/sessions/nope/actions/plan", {"kind": "key", "key": "w"}),
            ("post", "/novaforge/sessions/nope/actions/execute", {"proposal_id": "x", "digest": "y"}),
            ("post", "/novaforge/sessions/nope/verify", None),
            ("post", "/novaforge/sessions/nope/loop", {"max_actions": 1}),
            ("get", "/novaforge/sessions/nope/verification", None),
        ):
            with self.subTest(path=path):
                response = getattr(self._client, method)(
                    path, **({"json": payload} if payload is not None else {})
                )
                self.assertEqual(response.status_code, 404, response.text)


class _RetitlingInput(RecordingInput):
    """Records edges and retitles the fake harness when a key reaches it."""

    def __init__(self, harness: "_LoopApiFixture") -> None:
        super().__init__()
        self.harness = harness

    async def press(self, kind: str, name: str) -> None:
        await super().press(kind, name)
        self.harness.retitle()


class _LoopApiFixture(_GameplayApiFixture):
    """The real application with a window that reacts to injected input.

    The injector is installed BEFORE the manager builds its InputGuard, because
    the guard captures the bound press at construction. Patching ``press`` after
    the fact would leave the guard calling the old function — a test that looks
    connected and is not.
    """

    def _isolated_application(self, **kwargs: Any):
        app = super()._isolated_application(**kwargs)
        self.desktop = FakeDesktop()
        self.title: str | None = self.desktop.window.title
        self.ready_title = f"{self.title} - READY"
        self.input = _RetitlingInput(self)

        async def probe(_handle: int) -> Any:
            return geometry(title=self.title) if self.title is not None else None

        self.manager = NovaForgeManager(
            data_dir=Path(self._tmp.name),
            capture=self.desktop.capture,
            geometry=self.desktop.geometry,
            window_lines=self.desktop.window_lines,
            release_inputs=self.desktop.release,
            action_input=self.input,
            objective_probe=probe,
            permissions=app.risk,
            publish=app._novaforge_publish,
            capture_supported=True,
            frame_interval=0.05,
            pause_timeout=2.0,
            stop_timeout=4.0,
        )
        app.novaforge = self.manager
        return app

    def retitle(self) -> None:
        self.title = self.ready_title
        self.desktop.window = geometry(title=self.ready_title)


class NovaForgeClosedLoopApiTests(_LoopApiFixture):
    """The loop over HTTP: bounded, gated, and verified rather than scripted."""

    def _responding_harness(self) -> str:
        """A validated session with a READY objective and a reactive window."""
        session = self._validated_session()
        session_id = session["session_id"]
        configured = self._client.post(
            f"/novaforge/sessions/{session_id}/objective",
            json={"kind": "window_title", "parameter": "READY"},
        ).json()
        self.assertTrue(configured["supported"], configured)
        return session_id

    def test_the_loop_completes_only_when_the_window_confirms_it(self) -> None:
        session_id = self._responding_harness()
        response = self._client.post(
            f"/novaforge/sessions/{session_id}/loop",
            json={"max_actions": 2, "deadline_seconds": 20},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "completed", body["reason"])
        self.assertIn("READY", body["reason"])
        self.assertEqual(body["actions_executed"], 1)
        self.assertEqual(self.input.presses, ["key:space"])
        self.assertEqual(self.input.releases, ["key:space"])
        self.assertEqual(body["verification"]["outcome"], "verified_success")
        self.assertEqual(body["policy"], "HarnessReadyPolicy")
        self.assertEqual(body["steps"][0]["proposal"]["action"]["key"], "space")
        self.assertIs(body["steps"][0]["outcome_met"], True)
        self.assertEqual(body["limits"]["max_actions"], 2)

    def test_an_unknown_policy_is_a_422_and_never_falls_back(self) -> None:
        session = self._validated_session()
        response = self._client.post(
            f"/novaforge/sessions/{session['session_id']}/loop",
            json={"policy": "invented", "max_actions": 1},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("Unknown policy", response.json()["detail"])
        self.assertIn("stages", response.json()["detail"])
        self.assertEqual(self.input.presses, [], "an unknown policy ran anyway")

    def test_the_default_policy_is_the_nf08_rule(self) -> None:
        """Backward compatibility is explicit: no ``policy`` field means ready."""
        session_id = self._responding_harness()
        body = self._client.post(
            f"/novaforge/sessions/{session_id}/loop",
            json={"max_actions": 2, "deadline_seconds": 20},
        ).json()
        self.assertEqual(body["status"], "completed", body["reason"])
        self.assertEqual(body["policy"], "HarnessReadyPolicy")
        self.assertEqual(body["policy_version"], 1)
        self.assertFalse(body["model_backed"])

    def test_invalid_limits_are_a_422_with_the_reason(self) -> None:
        session = self._validated_session()
        for payload in (
            {"max_actions": 0},
            {"max_actions": 10_000},
            {"deadline_seconds": 0},
            {"deadline_seconds": "soon"},
        ):
            with self.subTest(payload=payload):
                response = self._client.post(
                    f"/novaforge/sessions/{session['session_id']}/loop", json=payload
                )
                self.assertEqual(response.status_code, 422, response.text)

    def test_a_finished_run_becomes_a_trial_with_its_own_aggregate(self) -> None:
        """NF-10: the panel's numbers come from the same rows the report uses."""
        session_id = self._responding_harness()
        body = self._client.post(
            f"/novaforge/sessions/{session_id}/loop",
            json={"max_actions": 2, "deadline_seconds": 20},
        ).json()
        self.assertEqual(body["status"], "completed", body["reason"])
        trial = body["trial"]
        self.assertEqual(trial["outcome"], "verified_success")
        self.assertEqual(trial["policy"], "HarnessReadyPolicy")
        self.assertEqual(trial["actions"], 1)
        # The ready harness states no stage, so no stage transition is claimed.
        self.assertFalse(trial["responded_to_transition"])
        summaries = body["policy_summaries"]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["trials"], 1)
        self.assertEqual(summaries[0]["success_rate"], 1.0)
        self.assertFalse(summaries[0]["model_backed_trials"])

        history = self._client.get("/novaforge/trials").json()
        self.assertEqual(len(history["trials"]), 1)
        self.assertEqual(history["trials"][0]["outcome"], "verified_success")
        self.assertEqual(history["summaries"][0]["policy"], "HarnessReadyPolicy")
        self.assertFalse(history["durable"])
        self.assertIn("not persisted", history["note"])

    def test_an_inconclusive_run_is_kept_in_the_history(self) -> None:
        """A run that ended without a verdict is visible, never counted as a win."""
        session = self._validated_session()  # no objective: the verifier cannot decide
        body = self._client.post(
            f"/novaforge/sessions/{session['session_id']}/loop", json={"max_actions": 1}
        ).json()
        self.assertEqual(body["status"], "stopped", body["reason"])
        self.assertEqual(body["trial"]["outcome"], "inconclusive")
        history = self._client.get("/novaforge/trials").json()
        self.assertEqual(history["trials"][-1]["outcome"], "inconclusive")
        self.assertEqual(history["summaries"][-1]["outcomes"]["inconclusive"], 1)

    def test_a_refused_run_stays_visible_in_the_history(self) -> None:
        from novacontrol.novaforge import LoopResult

        nova = self._nova
        assert nova is not None
        row = nova._record_novaforge_trial(
            LoopResult(
                status="refused",
                reason="Guardian denied the plan",
                policy="FixedKeyPolicy",
                policy_version=1,
                actions_refused=1,
            )
        )
        self.assertEqual(row["outcome"], "refused")
        history = self._client.get("/novaforge/trials").json()
        self.assertIn("refused", [trial["outcome"] for trial in history["trials"]])
        by_policy = {row["policy"]: row for row in history["summaries"]}
        self.assertEqual(by_policy["FixedKeyPolicy"]["actions_refused_total"], 1)

    def test_cancelling_a_loop_that_is_not_running_says_so(self) -> None:
        session = self._validated_session()
        response = self._client.post(
            f"/novaforge/sessions/{session['session_id']}/loop/cancel"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["cancelled"])
        self.assertIn("No closed loop is running", response.json()["reason"])

    def test_a_window_that_cannot_be_read_stops_the_loop_before_it_acts(self) -> None:
        session_id = self._responding_harness()
        self.title = None  # the target vanished between validation and observation
        body = self._client.post(
            f"/novaforge/sessions/{session_id}/loop", json={"max_actions": 1}
        ).json()
        self.assertEqual(body["status"], "unavailable", body["reason"])
        self.assertIn("probe returned nothing", body["reason"])
        self.assertEqual(self.input.presses, [])


class NovaForgeActionApiTests(_GameplayApiFixture):
    def test_objective_validation_is_a_422_with_the_reason(self) -> None:
        session = self._validated_session()
        response = self._client.post(
            f"/novaforge/sessions/{session['session_id']}/objective",
            json={"kind": "window_title"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("title text", response.text)

    def test_a_plan_sends_nothing_and_execution_with_its_digest_sends_once(self) -> None:
        session = self._validated_session()
        session_id = session["session_id"]
        configured = self._client.post(
            f"/novaforge/sessions/{session_id}/objective",
            json={"kind": "window_title", "parameter": "READY"},
        ).json()
        self.assertTrue(configured["supported"])

        plan = self._client.post(
            f"/novaforge/sessions/{session_id}/actions/plan", json={"kind": "key", "key": "w"}
        ).json()
        self.assertEqual(plan["status"], "proposed")
        self.assertEqual(self.input.presses, [], "planning sent input")

        self.probe_window = geometry(title=f"{self.desktop.window.title} - READY")
        result = self._client.post(
            f"/novaforge/sessions/{session_id}/actions/execute",
            json={"proposal_id": plan["proposal"]["proposal_id"], "digest": plan["proposal"]["digest"]},
        ).json()
        self.assertEqual(result["status"], "executed", result)
        self.assertEqual(self.input.presses, ["key:w"])
        self.assertEqual(result["verification"]["outcome"], "verified_success")

        verdict = self._client.get(f"/novaforge/sessions/{session_id}/verification").json()
        self.assertEqual(verdict["verification"]["outcome"], "verified_success")
        self.assertIs(verdict["verification"]["achieved"], True)
        self.assertEqual(verdict["actions"]["executed"], 1)

    def test_a_mismatched_digest_is_denied_over_http(self) -> None:
        session = self._validated_session()
        session_id = session["session_id"]
        plan = self._client.post(
            f"/novaforge/sessions/{session_id}/actions/plan", json={"kind": "key", "key": "w"}
        ).json()
        result = self._client.post(
            f"/novaforge/sessions/{session_id}/actions/execute",
            json={"proposal_id": plan["proposal"]["proposal_id"], "digest": "0" * 64},
        ).json()
        self.assertEqual(result["status"], "denied")
        self.assertEqual(self.input.presses, [])

    def test_an_unknown_proposal_is_a_422(self) -> None:
        session = self._validated_session()
        response = self._client.post(
            f"/novaforge/sessions/{session['session_id']}/actions/execute",
            json={"proposal_id": "act_nope", "digest": "abc"},
        )
        self.assertEqual(response.status_code, 422)

    def test_verify_without_an_objective_reports_unsupported(self) -> None:
        session = self._validated_session()
        verdict = self._client.post(f"/novaforge/sessions/{session['session_id']}/verify").json()
        self.assertEqual(verdict["outcome"], "unsupported")
        self.assertIsNone(verdict["achieved"])


if __name__ == "__main__":
    unittest.main()
