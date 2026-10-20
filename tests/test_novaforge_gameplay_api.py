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
        self.assertFalse(capabilities["decision_pipeline"]["available"])
        self.assertFalse(capabilities["game_launch"]["available"])
        self.assertIn("action_policy", capabilities)
        self.assertEqual(body["policy"]["allowed_buttons"], ["left"])

    def test_unknown_session_is_a_404_on_every_new_route(self) -> None:
        for method, path, payload in (
            ("post", "/novaforge/sessions/nope/objective", {"kind": "window_title", "parameter": "x"}),
            ("post", "/novaforge/sessions/nope/actions/plan", {"kind": "key", "key": "w"}),
            ("post", "/novaforge/sessions/nope/actions/execute", {"proposal_id": "x", "digest": "y"}),
            ("post", "/novaforge/sessions/nope/verify", None),
            ("get", "/novaforge/sessions/nope/verification", None),
        ):
            with self.subTest(path=path):
                response = getattr(self._client, method)(
                    path, **({"json": payload} if payload is not None else {})
                )
                self.assertEqual(response.status_code, 404, response.text)


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
