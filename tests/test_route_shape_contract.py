"""HTTP-level shape assertions for the route-contract registry.

The static contract tests (BackendFrontendContractTests in test_web_platform.py)
pin WHICH renderer owns each route's body by reading JS source text. This suite
pins the runtime half: for every declared renderer, the REAL HTTP response (via
TestClient against the real create_app) must contain the exact fields that
renderer reads FIRST — the fields that decide which branch renders.

If a handler response loses a first-read field (a rename, a dropped key), the
renderer silently falls to a fallback branch; these tests turn that drift into
a loud failure naming the field, the route, and the renderer.

First-read fields, transcribed from the renderers themselves:

  renderExplore/tryRenderResearch  overview | key_points | sources | videos
  renderBuild                      plan.steps | actions
  renderCommand                    summary; steps; approval.{approved,token};
                                   command; status; bridge
  renderWorkflow                   summary; approval; preview.changes
  renderLearning                   message | training_scope; iterations
  renderHealth                     checks (checks[].{ok,level}); ready | ok
  renderSettingsResult             iterates keys; loadSettings reads
                                   approval_mode/detailed_explanations/
                                   include_videos_in_explore/auto_approve_run
  renderPhoneStatus                state; adapter; available; next_steps
  renderVisionClickResult          status; label; output.{located_by,
                                   coordinates}; verification.changed
  renderChatResult (general)       route; summary
  renderGeneric                    commands | textForSpeech(data)
"""

from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

try:
    from fastapi.testclient import TestClient  # requires httpx
except Exception:  # pragma: no cover - httpx is an optional dev dependency
    TestClient = None  # type: ignore[assignment]

from novacontrol.api.app import create_app
from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.phone.models import PhoneBridgeState, PhoneBridgeStatus, PhoneDevice


class _FakePhoneRunner:
    """Test double for a paired ADB bridge (same shape as test_web_api's)."""

    def __init__(self, *, available: bool) -> None:
        self.available = available

    def status(self) -> PhoneBridgeStatus:
        devices = (PhoneDevice(id="emulator-5554", state="device", model="Pixel 7"),) if self.available else ()
        return PhoneBridgeStatus(
            available=self.available,
            state=PhoneBridgeState.DEVICE_CONNECTED if self.available else PhoneBridgeState.NO_DEVICE,
            adapter="fake",
            devices=devices,
        )


def _isolated_application(**kwargs: Any) -> NovaControlApplication:
    """Force a temp data dir so create_app never touches ./data, and keep the
    instance so tests can swap runners (the phone paired-device case)."""
    kwargs["data_dir"] = _DATA_DIR
    app = NovaControlApplication(**kwargs)
    _APP_STACK.append(app)
    return app


_DATA_DIR = ""
_APP_STACK: list[NovaControlApplication] = []


@unittest.skipIf(TestClient is None, "httpx not installed")
class RendererShapeContractTests(unittest.TestCase):
    """Every declared renderer's first-read fields exist in the real response."""

    def setUp(self) -> None:
        global _DATA_DIR
        self._tmp = TemporaryDirectory()
        _DATA_DIR = self._tmp.name
        patchers = [
            mock.patch("novacontrol.api.app.NovaControlApplication", side_effect=_isolated_application),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self._client = TestClient(create_app())
        self._client.__enter__()
        self.addCleanup(self._client.__exit__, None, None, None)
        self.addCleanup(_APP_STACK.clear)

    # -- helpers ------------------------------------------------------------

    def _post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(path, json=json)
        self.assertEqual(response.status_code, 200, f"{path} -> {response.status_code}: {response.text[:300]}")
        return response.json()

    def _get(self, path: str) -> dict[str, Any]:
        response = self._client.get(path)
        self.assertEqual(response.status_code, 200, f"{path} -> {response.status_code}: {response.text[:300]}")
        return response.json()

    def _assert_fields(self, payload: dict[str, Any], fields: list[str], *, route: str, renderer: str) -> None:
        for field in fields:
            with self.subTest(route=route, renderer=renderer, field=field):
                self.assertIn(field, payload, (
                    f"{renderer} reads '{field}' first from {route}'s response, "
                    f"but the response lacks it: {sorted(payload)[:12]}"
                ))

    # -- each declared renderer's first-read fields, over real HTTP ---------

    def test_render_explore_fields_in_post_explore(self) -> None:
        """tryRenderResearch branches on overview/key_points/sources/videos."""
        payload = self._post("/explore", {"topic": "route contract probe topic", "include_videos": False})
        self._assert_fields(
            payload, ["overview", "key_points", "sources"], route="POST /explore", renderer="renderExplore",
        )

    def test_render_command_fields_in_command_plan_and_execute(self) -> None:
        """renderCommand reads summary, steps, approval, command, status."""
        plan = self._post("/command/plan", {"command": "open notepad", "correlation_id": "shape-probe"})
        self._assert_fields(
            plan, ["summary", "steps", "approval", "command", "status", "workflow"],
            route="POST /command/plan", renderer="renderCommand",
        )
        # Approve And Run's preconditions live INSIDE approval.
        for field in ("approved", "token", "required"):
            with self.subTest(route="POST /command/plan", renderer="renderCommand", field=f"approval.{field}"):
                self.assertIn(field, plan["approval"])
        executed = self._post(
            "/command/execute", {"command": "open notepad", "approval_token": plan["approval"]["token"]}
        )
        self._assert_fields(
            executed, ["summary", "status", "approval"], route="POST /command/execute", renderer="renderCommand",
        )

    def test_render_command_fields_in_desktop_plan(self) -> None:
        plan = self._post("/desktop/plan", {"command": "open notepad"})
        self._assert_fields(
            plan, ["summary", "steps", "approval", "command", "status"],
            route="POST /desktop/plan", renderer="renderCommand",
        )

    def test_render_command_fields_in_phone_plan_bridge_blocked(self) -> None:
        """No device: status is waiting_for_phone_bridge and `bridge` carries
        the state/adapter/available/next_steps renderPhoneStatus reads.

        The default (real) runner is hermetically replaced: when a physical
        device is attached, the honest answer is waiting_for_approval, and
        this test pins the no-device rendering specifically."""
        _APP_STACK[-1].phone.runner = _FakePhoneRunner(available=False)
        plan = self._post("/phone/plan", {"command": "open whatsapp on my phone"})
        self._assert_fields(
            plan, ["summary", "command", "status", "bridge"],
            route="POST /phone/plan", renderer="renderCommand",
        )
        self.assertEqual(plan["status"], "waiting_for_phone_bridge")
        self._assert_fields(
            plan["bridge"], ["state", "adapter", "available", "next_steps"],
            route="POST /phone/plan (bridge)", renderer="renderPhoneStatus",
        )

    def test_render_command_fields_in_phone_plan_with_device(self) -> None:
        """A paired device mints a token: renderCommand's Approve precondition."""
        runner = _FakePhoneRunner(available=True)
        _APP_STACK[-1].phone.runner = runner
        plan = self._post("/phone/plan", {"command": "open whatsapp on my phone"})
        self.assertEqual(plan["status"], "waiting_for_approval")
        self._assert_fields(
            plan, ["summary", "command", "status", "approval"],
            route="POST /phone/plan (paired)", renderer="renderCommand",
        )

    def test_render_phone_status_fields_in_phone_connect(self) -> None:
        status = self._post("/phone/connect", {})
        self._assert_fields(
            status, ["state", "adapter", "available", "next_steps"],
            route="POST /phone/connect", renderer="renderPhoneStatus",
        )

    def test_render_build_fields_in_post_plan(self) -> None:
        """renderBuild branches on plan.steps; plan.name feeds the summary."""
        payload = self._post("/plan", {"goal": "write the report then review it"})
        self._assert_fields(payload, ["plan"], route="POST /plan", renderer="renderBuild")
        # renderBuild's summary reads name || goal — Plan.to_dict carries goal.
        self._assert_fields(
            payload["plan"], ["steps", "goal"], route="POST /plan (plan)", renderer="renderBuild",
        )
        self.assertTrue(payload["plan"]["steps"], "a multi-clause goal must produce steps")

    def test_render_workflow_fields_in_improve_workflow(self) -> None:
        payload = self._post("/improve/workflow", {"goal": "improve error handling"})
        self._assert_fields(
            payload, ["summary", "status", "approval", "goal"],
            route="POST /improve/workflow", renderer="renderWorkflow",
        )

    def test_render_workflow_fields_in_improve_preview(self) -> None:
        """rememberPreview needs preview.id; renderWorkflow renders changes."""
        payload = self._post("/improve/preview", {"goal": "improve error handling"})
        self._assert_fields(
            payload, ["summary", "status", "approval", "preview"],
            route="POST /improve/preview", renderer="renderWorkflow",
        )
        self._assert_fields(
            payload["preview"], ["id", "changes"], route="POST /improve/preview (preview)", renderer="renderWorkflow",
        )

    def test_render_workflow_fields_in_improve_approve(self) -> None:
        """Approve round-trips a preview id and returns the applied workflow."""
        preview = self._post("/improve/preview", {"goal": "improve error handling"})
        payload = self._post(
            "/improve/approve", {"goal": "improve error handling", "preview_id": preview["preview"]["id"]}
        )
        self._assert_fields(
            payload, ["summary", "status", "approval"],
            route="POST /improve/approve", renderer="renderWorkflow",
        )

    def test_render_learning_fields_in_learn_and_train(self) -> None:
        learn = self._post("/learn", {"goal": "learn route shapes", "feedback": "none"})
        self._assert_fields(
            learn, ["message", "mode"], route="POST /learn", renderer="renderLearning",
        )
        trained = self._post("/train", {"goal": "learn route shapes", "iterations": 2})
        self._assert_fields(
            trained, ["training_scope", "iterations", "mode"], route="POST /train", renderer="renderLearning",
        )
        # renderLearning's iteration cards read these three keys.
        self.assertTrue(trained["iterations"])
        for field in ("iteration", "action_count", "finding_count", "memory_key"):
            with self.subTest(route="POST /train (iterations[])", renderer="renderLearning", field=field):
                self.assertIn(field, trained["iterations"][0])

    def test_render_health_fields_in_system_health_and_harden(self) -> None:
        for path in ("/system/health", "/system/harden"):
            payload = self._get(path)
            self._assert_fields(
                payload, ["checks", "ready" if path.endswith("harden") else "ok"],
                route=f"GET {path}", renderer="renderHealth",
            )
            self.assertTrue(payload["checks"], f"{path} must report at least one check")
            # The renderer handles BOTH check dialects: health checks carry
            # level/message, hardening checks carry ok/detail. Each check must
            # satisfy one side of each ?? / || pair.
            check = payload["checks"][0]
            for field in ("name",):
                with self.subTest(route=f"GET {path} (checks[])", renderer="renderHealth", field=field):
                    self.assertIn(field, check)
            with self.subTest(route=f"GET {path} (checks[])", renderer="renderHealth", field="ok|level"):
                self.assertTrue("ok" in check or "level" in check, f"check needs ok or level: {sorted(check)}")
            with self.subTest(route=f"GET {path} (checks[])", renderer="renderHealth", field="detail|message"):
                self.assertTrue("detail" in check or "message" in check)

    def test_render_settings_result_fields_in_settings_roundtrip(self) -> None:
        """POST returns the saved settings; GET feeds loadSettings' four reads."""
        saved = self._post(
            "/settings",
            {
                "approval_mode": "ask",
                "detailed_explanations": True,
                "include_videos_in_explore": True,
                "auto_approve_run": False,
            },
        )
        self._assert_fields(
            saved,
            ["approval_mode", "detailed_explanations", "include_videos_in_explore", "auto_approve_run"],
            route="POST /settings", renderer="renderSettingsResult",
        )
        loaded = self._get("/settings")
        self._assert_fields(
            loaded,
            ["approval_mode", "detailed_explanations", "include_videos_in_explore", "auto_approve_run"],
            route="GET /settings (loadSettings)", renderer="loadSettings",
        )

    def test_render_vision_click_fields_via_noop_runner(self) -> None:
        """The noop runner returns the action-shaped body renderVisionClickResult
        reads: status/label/output/verification (with honest changed=None)."""
        payload = self._post("/vision/click", {"label": "File"})
        self._assert_fields(
            payload, ["action", "label", "status", "output", "verification"],
            route="POST /vision/click", renderer="renderVisionClickResult",
        )
        self.assertIn("changed", payload["verification"])
        self.assertEqual(payload["action"], "guided_click")

    def test_render_chat_result_general_fields_in_ask(self) -> None:
        """The /ask envelope carries route/summary; the general branch renders
        the summary as the lead, so it must be present and non-empty."""
        payload = self._post("/ask", {"request": "what is a black hole"})
        self._assert_fields(
            payload, ["route", "intent", "summary"], route="POST /ask", renderer="renderChatResult",
        )
        self.assertTrue(str(payload["summary"]).strip(), "the general branch renders summary as the lead")

    def test_render_generic_fields_in_status(self) -> None:
        """/status (renderGeneric consumer) must at least render its fallback
        textForSpeech card — any dict satisfies it; pin the route answers."""
        payload = self._get("/status")
        self.assertIsInstance(payload, dict)
        self.assertTrue(payload, "GET /status must return a body for renderGeneric")

    def test_desktop_execute_round_trip_keeps_render_command_shape(self) -> None:
        """The full plan→execute loop keeps renderCommand's executed shape."""
        plan = self._post("/desktop/plan", {"command": "open notepad"})
        executed = self._post(
            "/desktop/execute", {"command": "open notepad", "approval_token": plan["approval"]["token"]}
        )
        self._assert_fields(
            executed, ["summary", "status", "approval"], route="POST /desktop/execute", renderer="renderCommand",
        )
        self.assertEqual(executed["status"], "executed")


if __name__ == "__main__":
    unittest.main()
