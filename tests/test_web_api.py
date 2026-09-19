"""HTTP-level tests for the FastAPI surface using TestClient.

These tests exercise the real create_app() factory end to end: plan a device
command (desktop or browser), execute it with the server-minted approval token,
and verify the API rejects missing / unknown / expired / replayed tokens with
403. The desktop and browser runners are swapped for no-op runners and the app
data dir is a temp dir, so nothing touches the real desktop, a real browser, or
the ./data state store.
"""

from __future__ import annotations

import json
import re
import time
import unittest
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

try:
    from fastapi.testclient import TestClient  # requires httpx
except Exception:  # pragma: no cover - httpx is an optional dev dependency
    TestClient = None  # type: ignore[assignment]

from conftest import FakeSearchProvider, FakeVideoProvider
from novacontrol.api.app import create_app
from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.integrations.llm import EchoLLMProvider
from novacontrol.phone.models import PhoneBridgeState, PhoneBridgeStatus, PhoneDevice

# Each device kind plans and executes through the same /command/* endpoints; the
# intent router dispatches desktop commands to the desktop controller and browser
# commands (navigate, fill forms) to the browser controller.
DEVICE_COMMANDS = [
    ("open calculator", "desktop_automation"),
    ("navigate to example.com", "browser_automation"),
    ("search the web for quantum computing", "browser_automation"),
]

# Phone commands use the dedicated /phone/* endpoints (plus /command/* once routed).
PHONE_COMMAND = "open whatsapp on my phone"


class FakePhoneRunner:
    """Test double for a real ADB bridge: reports a paired device and records runs."""

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.calls: list[dict] = []

    def status(self) -> PhoneBridgeStatus:
        devices = (
            (PhoneDevice(id="emulator-5554", state="device", model="Pixel 7"),)
            if self.available
            else ()
        )
        return PhoneBridgeStatus(
            available=self.available,
            state=(
                PhoneBridgeState.DEVICE_CONNECTED if self.available else PhoneBridgeState.NO_DEVICE
            ),
            adapter="fake",
            devices=devices,
        )

    async def run(self, action) -> dict:
        self.calls.append(action.to_dict())
        return {
            "adapter": "fake",
            "exit_code": 0,
            "stdout": "launched",
            "stderr": "",
        }


@unittest.skipIf(TestClient is None, "fastapi.testclient.TestClient requires httpx (pip install httpx)")
class _IsolatedApiTestCase(unittest.TestCase):
    """Shared fixture: real create_app() factory, temp data dir, no-op device runners.

    Subclasses add behavior-specific patchers via _extra_patchers(); the TestClient
    lifecycle and the isolated application instance (kept in self._nova) are inherited.
    """

    def _extra_patchers(self) -> list[Any]:
        return []

    def setUp(self) -> None:
        if TestClient is None:
            self.skipTest("httpx not installed")
        self._tmp = TemporaryDirectory()
        self._nova: NovaControlApplication | None = None
        self._patchers = [
            mock.patch("novacontrol.api.app.NovaControlApplication", side_effect=self._isolated_application),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
            *self._extra_patchers(),
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

    def _isolated_application(self, **kwargs: object) -> NovaControlApplication:
        """Force a temp data dir so create_app never touches ./data, and keep the instance."""
        kwargs["data_dir"] = self._tmp.name
        app = NovaControlApplication(**kwargs)  # type: ignore[arg-type]
        self._nova = app
        return app


class CommandExecutionApiTests(_IsolatedApiTestCase):
    """plan -> execute with a real token; 403 on missing/unknown/expired/replayed."""

    def _plan(self, command: str, expected_route: str) -> dict:
        response = self._client.post("/command/plan", json={"command": command})
        self.assertEqual(response.status_code, 200)
        plan = response.json()
        self.assertEqual(plan["route"], expected_route)
        self.assertEqual(plan["status"], "waiting_for_approval")
        self.assertTrue(plan["approval"]["token"], "plan must mint an approval token")
        return plan

    def test_plan_then_execute_with_real_token(self) -> None:
        for command, expected_route in DEVICE_COMMANDS:
            with self.subTest(command=command):
                token = self._plan(command, expected_route)["approval"]["token"]

                response = self._client.post(
                    "/command/execute", json={"command": command, "approval_token": token}
                )

                self.assertEqual(response.status_code, 200)
                executed = response.json()
                self.assertEqual(executed["status"], "executed")
                self.assertTrue(executed["approval"]["approved"])
                self.assertGreaterEqual(len(executed["execution_results"]), 1)

    def test_cross_family_token_executes_with_minting_family(self) -> None:
        """A token minted by /desktop/plan must run under the DESKTOP executor.

        The inline Approve And Run button always posts /command/execute, which
        re-dispatches by brain intent. For a browser-intent phrase planned via
        the desktop endpoint, the intent dispatch hands the token to the browser
        executor and the stored desktop workflow (e.g. an execute_script action)
        explodes on rebuild ('execute_script' is not a valid BrowserActionType).
        The plan's family stamp now overrides the intent dispatch.
        """
        response = self._client.post("/desktop/plan", json={"command": "open the browser and search for cats"})
        self.assertEqual(response.status_code, 200)
        plan = response.json()
        self.assertEqual(plan["route"], "desktop_automation")
        self.assertEqual(plan["family"], "desktop")
        token = plan["approval"]["token"]

        # /command/execute re-dispatches by brain intent (browser for this phrase);
        # the minting family (desktop) must win.
        response = self._client.post(
            "/command/execute", json={"command": "open the browser and search for cats", "approval_token": token}
        )
        self.assertEqual(response.status_code, 200)
        executed = response.json()
        self.assertEqual(executed["route"], "desktop_automation")
        self.assertEqual(executed["status"], "executed")
        self.assertGreaterEqual(len(executed["execution_results"]), 1)

    def test_execute_requires_token(self) -> None:
        for command, expected_route in DEVICE_COMMANDS:
            with self.subTest(command=command):
                response = self._client.post("/command/execute", json={"command": command})

                self.assertEqual(response.status_code, 403)
                self.assertIn("token", response.json()["detail"].lower())

    def test_execute_rejects_unknown_token(self) -> None:
        for command, _ in DEVICE_COMMANDS:
            with self.subTest(command=command):
                response = self._client.post(
                    "/command/execute", json={"command": command, "approval_token": "forged-token"}
                )

                self.assertEqual(response.status_code, 403)
                self.assertIn("unknown", response.json()["detail"].lower())

    def test_execute_rejects_expired_token(self) -> None:
        for command, expected_route in DEVICE_COMMANDS:
            with self.subTest(command=command):
                token = self._plan(command, expected_route)["approval"]["token"]
                assert self._nova is not None
                self._nova._pending_approvals[token]["expires_at"] = time.time() - 60  # age it past the TTL

                response = self._client.post(
                    "/command/execute", json={"command": command, "approval_token": token}
                )

                self.assertEqual(response.status_code, 403)
                self.assertIn("expired", response.json()["detail"].lower())

    def test_token_is_single_use(self) -> None:
        for command, expected_route in DEVICE_COMMANDS:
            with self.subTest(command=command):
                token = self._plan(command, expected_route)["approval"]["token"]
                first = self._client.post(
                    "/command/execute", json={"command": command, "approval_token": token}
                )
                self.assertEqual(first.status_code, 200)

                replay = self._client.post(
                    "/command/execute", json={"command": command, "approval_token": token}
                )
                self.assertEqual(replay.status_code, 403)

    # --- Phone endpoints -------------------------------------------------

    def _phone_runner(self, *, available: bool) -> FakePhoneRunner:
        """Swap in a deterministic bridge (real ADB state varies per host)."""
        assert self._nova is not None
        runner = FakePhoneRunner(available=available)
        self._nova.phone.runner = runner
        return runner

    def test_phone_execute_returns_pairing_plan_while_no_device_is_paired(self) -> None:
        """Without a phone bridge, execute is a pairing-plan stub: 200, nothing runs, no token."""
        self._phone_runner(available=False)
        for path in ("/phone/plan", "/phone/execute"):
            with self.subTest(path=path):
                response = self._client.post(path, json={"command": PHONE_COMMAND})
                self.assertEqual(response.status_code, 200)
                plan = response.json()
                self.assertEqual(plan["route"], "phone_control")
                self.assertEqual(plan["status"], "waiting_for_phone_bridge")
                self.assertEqual(plan["target"], "whatsapp")
                self.assertTrue(plan["approval"]["required"])
                self.assertFalse(plan["approval"]["approved"])
                self.assertNotIn(
                    "token", plan["approval"], "nothing to approve while no device is paired"
                )
                self.assertFalse(plan["bridge"]["available"])
                self.assertEqual(len(plan["workflow"]["actions"]), 1)

    def test_phone_execute_requires_token_once_a_device_is_available(self) -> None:
        """A paired device means real execution is possible: missing/forged tokens must 403."""
        self._phone_runner(available=True)
        for token in (None, "forged-token"):
            with self.subTest(token=token):
                payload: dict = {"command": PHONE_COMMAND}
                if token:
                    payload["approval_token"] = token
                response = self._client.post("/phone/execute", json=payload)
                self.assertEqual(response.status_code, 403)
                self.assertIn("token", response.json()["detail"].lower())

    def test_phone_plan_then_execute_with_real_token(self) -> None:
        """Plan mints a token once paired; execute with it really runs the action on the phone."""
        runner = self._phone_runner(available=True)

        plan = self._client.post("/phone/plan", json={"command": PHONE_COMMAND}).json()
        self.assertEqual(plan["status"], "waiting_for_approval")
        token = plan["approval"]["token"]
        self.assertTrue(token, "paired-phone plan must mint an approval token")

        response = self._client.post(
            "/phone/execute", json={"command": PHONE_COMMAND, "approval_token": token}
        )
        self.assertEqual(response.status_code, 200)
        executed = response.json()
        self.assertEqual(executed["status"], "executed")
        self.assertTrue(executed["approval"]["approved"])
        self.assertEqual(len(executed["execution_results"]), 1)
        self.assertEqual(executed["execution_results"][0]["status"], "completed")
        self.assertEqual(len(runner.calls), 1, "the paired phone must really have run the action")

    def _phone_plan_token(self) -> str:
        """Plan the phone command (paired device) and return the minted token."""
        plan = self._client.post("/phone/plan", json={"command": PHONE_COMMAND}).json()
        return plan["approval"]["token"]

    def test_phone_token_is_single_use_and_expires(self) -> None:
        self._phone_runner(available=True)

        token = self._phone_plan_token()
        payload = {"command": PHONE_COMMAND, "approval_token": token}
        first = self._client.post("/phone/execute", json=payload)
        self.assertEqual(first.status_code, 200)
        replay = self._client.post("/phone/execute", json=payload)
        self.assertEqual(replay.status_code, 403)

        token = self._phone_plan_token()
        assert self._nova is not None
        self._nova._pending_approvals[token]["expires_at"] = time.time() - 60  # age it past the TTL
        response = self._client.post(
            "/phone/execute", json={"command": PHONE_COMMAND, "approval_token": token}
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("expired", response.json()["detail"].lower())

    def test_command_execute_routes_phone_through_the_same_gate(self) -> None:
        """The unified /command/execute endpoint honors the same phone pairing/gate behavior."""
        self._phone_runner(available=False)
        pairing = self._client.post("/command/execute", json={"command": PHONE_COMMAND})
        self.assertEqual(pairing.status_code, 200)
        self.assertEqual(pairing.json()["status"], "waiting_for_phone_bridge")

        self._phone_runner(available=True)
        response = self._client.post("/command/execute", json={"command": PHONE_COMMAND})
        self.assertEqual(response.status_code, 403)
        self.assertIn("token", response.json()["detail"].lower())

    def test_status_reports_brain_provider_and_model(self) -> None:
        """The System panel's AI Brain card reads /status's brain block: provider + model."""
        brain = self._client.get("/status").json()["app"]["brain"]
        self.assertEqual(brain["provider"], "scratch")  # Echo LLM forced by the fixture
        self.assertEqual(brain["model"], "")
        self.assertFalse(brain["model_configured"])

    def test_static_html_css_js_are_never_cached(self) -> None:
        """UI edits must show on the next load: no HTML/CSS/JS response may be storable.

        Covers the whole static surface — the index route, top-level assets, the
        js/ subdirectory modules, and images — and pins the exact header value, so
        a refactor that downgrades any asset (or a new asset type that skips the
        headers) fails here rather than resurrecting the cached old UI.
        """
        expected = "no-cache, no-store, max-age=0, must-revalidate"
        paths = (
            "/",
            "/static/index.html",
            "/static/styles.css",
            "/static/app.js",
            "/static/js/dom.js",
            "/static/js/state.js",
            "/static/js/render-utils.js",
            "/static/js/render-explore.js",
            "/static/js/render-panels.js",
            "/static/js/voice.js",
            "/static/js/effects.js",
            "/static/nova-mark.svg",
        )
        for path in paths:
            with self.subTest(path=path):
                response = self._client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.headers.get("cache-control", ""),
                    expected,
                    f"{path} must be non-storable, got a different Cache-Control",
                )
                self.assertEqual(response.headers.get("pragma"), "no-cache")
                self.assertEqual(response.headers.get("expires"), "0")

    def test_index_serves_injected_asset_version(self) -> None:
        """Served index.html replaces the marker with one shared content-derived token."""
        body = self._client.get("/").text
        self.assertNotIn("__NC_ASSET_VERSION__", body, "marker leaked un-replaced into served HTML")
        tokens = set(re.findall(r"[?&]v=([0-9a-f]+)", body))
        self.assertTrue(tokens, "no versioned asset URLs found in served index.html")
        self.assertEqual(len(tokens), 1, f"assets must share one injected version, got: {tokens}")

    def test_index_serves_visible_ui_version_stamp(self) -> None:
        """The sidebar footer shows the same content-derived build id (shortened)."""
        body = self._client.get("/").text
        self.assertNotIn("__NC_UI_VERSION__", body, "UI version marker leaked un-replaced into served HTML")
        match = re.search(r"UI v([0-9a-f]{8})", body)
        self.assertIsNotNone(match, "served HTML must contain a visible 'UI v<8-hex>' build stamp")
        tokens = set(re.findall(r"[?&]v=([0-9a-f]+)", body))
        self.assertEqual(len(tokens), 1)
        (asset_version,) = tokens
        self.assertEqual(match.group(1), asset_version[:8], "UI stamp must be the injected asset version, shortened")

    def test_health_reports_the_same_build_id_the_page_shows(self) -> None:
        """`/health` and the sidebar stamp must agree, or staleness can't be detected.

        The page compares its own stamp against /health to notice that it is
        rendering CSS/JS from before an edit. Both sides have to come from the
        same content-derived hash: if they diverge the check either never fires
        (a stale page looks current) or fires forever (a current page is told to
        reload).
        """
        body = self._client.get("/").text
        match = re.search(r"UI v([0-9a-f]{8})", body)
        self.assertIsNotNone(match, "served HTML must carry a visible 'UI v<8-hex>' stamp")

        health = self._client.get("/health")
        self.assertEqual(health.status_code, 200)
        payload = health.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["ui_version"], "/health must report the build id it is serving")
        self.assertEqual(
            payload["ui_version"][:8],
            match.group(1),
            "/health build id must match the build id stamped into the page",
        )

    def test_health_build_id_changes_when_a_ui_file_changes(self) -> None:
        """The build id has to move when the UI does — that is the whole mechanism."""
        from pathlib import Path

        from novacontrol.api.app import _static_asset_version

        static_dir = Path("src/novacontrol/web/static")
        before = _static_asset_version(static_dir)
        probe = static_dir / "js" / "_probe_stale_build.js"
        probe.write_text("// probe\n", encoding="utf-8")
        try:
            self.assertNotEqual(before, _static_asset_version(static_dir))
        finally:
            probe.unlink()
        self.assertEqual(before, _static_asset_version(static_dir))


class AskAndExploreApiTests(_IsolatedApiTestCase):
    """The flattened {route, intent, summary, data} envelope and the flat /explore
    report, pinned over real HTTP.

    Determinism without a network: the Echo LLM is forced at boot (no Ollama probe,
    scratch answers locally, and try_llm_synthesis skips echo providers), and the
    conftest fake search/video providers are swapped onto the explore service so
    both /ask and /explore never touch the internet.
    """

    def _extra_patchers(self) -> list[Any]:
        # build_llm_provider_from_environment probes localhost:11434 at app boot;
        # forcing EchoLLMProvider keeps the suite deterministic on any host
        # (NOVACONTROL_DISABLE_OLLAMA is set process-wide in conftest, which also
        # disables the chat-time lazy re-probe).
        return [
            mock.patch(
                "novacontrol.application.build_llm_provider_from_environment",
                return_value=EchoLLMProvider(),
            ),
        ]

    def setUp(self) -> None:
        super().setUp()
        assert self._nova is not None
        self._nova.explore.search_provider = FakeSearchProvider()
        self._nova.explore.video_provider = FakeVideoProvider()

    def test_ask_returns_flattened_envelope_for_scratch_math(self) -> None:
        response = self._client.post("/ask", json={"request": "what is 15 times 3"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(sorted(body.keys()), ["data", "intent", "route", "summary"])
        self.assertEqual(body["route"], "chat")
        self.assertEqual(body["intent"], "chat")
        self.assertIn("45", body["summary"])
        self.assertEqual(body["summary"], body["data"]["message"])
        self.assertNotIn("payload", body, "envelope must stay flat; no payload key again")

    def test_ask_routes_research_phrasing_to_explore_and_carries_a_report(self) -> None:
        response = self._client.post(
            "/ask", json={"request": "Compare React, Vue, and Svelte for building a dashboard"}
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(sorted(body.keys()), ["data", "intent", "route", "summary"])
        self.assertEqual(body["route"], "explore")
        self.assertEqual(body["intent"], "explore")
        data = body["data"]
        self.assertEqual(data["topic"], "React, Vue, and Svelte")
        self.assertEqual(len(data["sources"]), 2)
        self.assertEqual(
            [section["title"] for section in data["sections"]],
            ["Quick Comparison", "How To Decide"],
        )
        self.assertTrue(data["overview"] and data["answer"])

    def test_explore_returns_the_flat_report_directly(self) -> None:
        response = self._client.post(
            "/explore",
            json={
                "topic": "How do I set up a home composting system?",
                "depth": "deep",
                "include_videos": True,
                "max_sources": 6,
                "max_videos": 5,
            },
        )

        self.assertEqual(response.status_code, 200)
        report = response.json()
        # Flat body: the report itself, no envelope, no payload nesting.
        self.assertIn("topic", report)
        self.assertIn("overview", report)
        self.assertNotIn("payload", report)
        self.assertNotIn("route", report)
        self.assertEqual(report["topic"], "home composting system")
        self.assertEqual(len(report["sources"]), 2)
        self.assertEqual(len(report["videos"]), 1)
        self.assertEqual(report["videos"][0]["channel"], "Example Channel")
        self.assertTrue(report["answer"], "report must carry a synthesized answer")

    def test_ask_envelope_never_leaks_a_payload_key(self) -> None:
        """The old payload-wrapped shapes must stay gone over the wire."""
        for request in (
            "what is 15 times 3",
            "hello there",
            "Compare React, Vue, and Svelte for building a dashboard",
        ):
            with self.subTest(request=request):
                body = self._client.post("/ask", json={"request": request}).json()
                self.assertIsInstance(body, dict)
                self.assertEqual(
                    {"route", "intent", "summary", "data"}, set(body.keys())
                )


class BrainModeAndTaskApiTests(_IsolatedApiTestCase):
    """The brain-mode switch endpoints and the task delete/clear endpoints."""

    def _extra_patchers(self) -> list[Any]:
        # Same determinism patcher as AskAndExploreApiTests: force the Echo LLM
        # at boot so the mode tests have a known starting provider.
        return [
            mock.patch(
                "novacontrol.application.build_llm_provider_from_environment",
                return_value=EchoLLMProvider(),
            ),
        ]

    def test_brain_mode_round_trip_and_scratch_reporting(self) -> None:
        # Boot is Echo (no model), so effective auto == scratch.
        status = self._client.get("/brain/mode")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["mode"], "auto")
        self.assertEqual(status.json()["effective_mode"], "scratch")

        switched = self._client.post("/brain/mode", json={"mode": "scratch"})
        self.assertEqual(switched.status_code, 200)
        self.assertEqual(switched.json()["mode"], "scratch")

        # The mode persists into /status and the settings store.
        app_status = self._client.get("/status").json()["app"]["brain"]
        self.assertEqual(app_status["mode"], "scratch")
        self.assertEqual(self._nova.settings.settings.brain_mode, "scratch")

        restored = self._client.post("/brain/mode", json={"mode": "auto"})
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["mode"], "auto")

    def test_brain_mode_rejects_unknown_values(self) -> None:
        response = self._client.post("/brain/mode", json={"mode": "turbo"})
        self.assertEqual(response.status_code, 422)

    def test_brain_cloud_presets_are_listed_without_secrets(self) -> None:
        response = self._client.get("/brain/cloud/presets")
        self.assertEqual(response.status_code, 200)
        presets = response.json()["presets"]
        self.assertTrue(any(row["id"] == "openai" for row in presets))
        self.assertTrue(any(row["id"] == "claude" for row in presets), "Claude must be a listed cloud option")
        for row in presets:
            self.assertNotIn("base_url", row, "preset metadata stays lean, no endpoints")
            self.assertNotIn("api_key", json.dumps(row))

    def test_brain_cloud_claude_connects_and_reports(self) -> None:
        """Claude connects through the SAME /brain/cloud surface; the status
        carries its label and model, the key never travels back."""
        installed = self._client.post(
            "/brain/cloud",
            json={"provider": "claude", "api_key": "sk-ant-test-1234567890abcdefgh", "model": "claude-opus-4-1"},
        )
        self.assertEqual(installed.status_code, 200)
        body = installed.json()
        # Storing a key never switches the brain: the mode the user is running
        # (auto/local by default) survives, and Cloud is a deliberate choice.
        self.assertEqual(body["mode"], "auto")
        self.assertEqual(body["cloud"]["provider"], "claude")
        self.assertEqual(body["cloud"]["model"], "claude-opus-4-1")
        self.assertNotIn("sk-ant-test-1234567890abcdefgh", json.dumps(body))

        # The stored config re-arms at boot (same namespace as other clouds).
        stored = self._nova._cloud_llm_store.read("cloud_llm")
        self.assertEqual(stored["provider"], "claude")
        self.assertEqual(self._nova.brain.cloud_provider_name, "cloud:claude")

    def test_brain_cloud_roundtrip_and_key_redaction(self) -> None:
        # Invalid provider and empty key are both rejected before anything stores.
        bad = self._client.post("/brain/cloud", json={"provider": "nope", "api_key": "k"})
        self.assertEqual(bad.status_code, 422)
        no_key = self._client.post("/brain/cloud", json={"provider": "openai", "api_key": "   "})
        self.assertEqual(no_key.status_code, 422)

        installed = self._client.post(
            "/brain/cloud", json={"provider": "openai", "api_key": "sk-test-1234567890abcdefgh"}
        )
        self.assertEqual(installed.status_code, 200)
        body = installed.json()
        self.assertEqual(body["mode"], "auto")
        cloud = body["cloud"]
        self.assertTrue(cloud["configured"])
        self.assertEqual(cloud["provider"], "openai")
        # The key must NEVER travel back to any client — redacted tail only.
        self.assertNotIn("sk-test-1234567890abcdefgh", json.dumps(body))
        self.assertTrue(cloud["api_key_hint"].endswith("efgh"))

        # The config (key included) persists locally and re-arms at boot.
        stored = self._nova._cloud_llm_store.read("cloud_llm")
        self.assertEqual(stored["provider"], "openai")
        self.assertEqual(stored["api_key"], "sk-test-1234567890abcdefgh")

        cleared = self._client.post("/brain/cloud/clear")
        self.assertEqual(cleared.status_code, 200)
        self.assertFalse(cleared.json()["cloud"]["configured"])
        self.assertEqual(cleared.json()["mode"], "auto")
        self.assertEqual(self._nova._cloud_llm_store.read("cloud_llm"), {})

    def test_brain_cloud_test_pings_without_saving(self) -> None:
        """The test-connection endpoint validates the pasted key with a real
        provider construction + completion, but NOTHING may persist: the
        stored config stays untouched, the brain stays untouched, and the
        pasted key never survives the request."""
        from unittest.mock import AsyncMock

        # A working ping: the app's test path builds the provider through
        # build_cloud_provider, so patch that to a fake that answers.
        class _FakeProvider:
            name = "cloud:openai"
            model = "gpt-4o-mini"

            async def complete(self, messages, **kwargs):
                return "ok"

        with mock.patch(
            "novacontrol.application.build_cloud_provider", return_value=_FakeProvider()
        ) as builder:
            ok = self._client.post(
                "/brain/cloud/test",
                json={"provider": "openai", "api_key": "sk-paste-1234567890abcdefgh", "model": "gpt-4o-mini"},
            )
        self.assertEqual(ok.status_code, 200)
        body = ok.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["sample"], "ok")
        self.assertFalse(body["saved"], "a test ping must never claim it saved")
        builder.assert_called_once_with("openai", "sk-paste-1234567890abcdefgh", model="gpt-4o-mini")

        # Nothing persisted, nothing activated.
        self.assertEqual(self._nova._cloud_llm_store.read("cloud_llm"), {})
        self.assertFalse(self._nova.brain.cloud_provider_name)
        self.assertNotIn("sk-paste-1234567890abcdefgh", json.dumps(self._client.get("/brain/mode").json()))

        # A failing ping surfaces the provider's rejection as a 422 detail.
        class _ExplodingProvider:
            name = "cloud:openai"
            model = "gpt-4o-mini"

            async def complete(self, messages, **kwargs):
                raise RuntimeError("401 invalid_api_key")

        with mock.patch(
            "novacontrol.application.build_cloud_provider", return_value=_ExplodingProvider()
        ):
            bad = self._client.post(
                "/brain/cloud/test",
                json={"provider": "openai", "api_key": "sk-bad-1234567890abcdefgh"},
            )
        self.assertEqual(bad.status_code, 422)
        self.assertIn("401", bad.json()["detail"])
        self.assertEqual(self._nova._cloud_llm_store.read("cloud_llm"), {}, "failed ping must not save either")

        # Validation parity with the real connect: unknown provider + empty key.
        self.assertEqual(self._client.post("/brain/cloud/test", json={"provider": "nope", "api_key": "k"}).status_code, 422)
        self.assertEqual(self._client.post("/brain/cloud/test", json={"provider": "openai", "api_key": "  "}).status_code, 422)

    def test_vertex_format_key_is_rejected_for_gemini(self) -> None:
        """The motivating mixup: a Google Cloud/Vertex token pasted into the
        Gemini (AI Studio) provider is rejected at save AND test time with a
        message that names the mistake and the fix — and nothing is stored."""
        vertex_key = "AQ.Ab8RLIrN2fTq_example_vertex_service_token_9SVA"
        for path in ("/brain/cloud", "/brain/cloud/test"):
            response = self._client.post(path, json={"provider": "gemini", "api_key": vertex_key})
            self.assertEqual(response.status_code, 422, path)
            detail = response.json()["detail"]
            self.assertIn("AI Studio", detail)
            self.assertIn("Vertex", detail)
            self.assertIn("aistudio.google.com/apikey", detail)
        # Rejected at the gate: nothing persisted, brain untouched.
        self.assertEqual(self._nova._cloud_llm_store.read("cloud_llm"), {})
        self.assertFalse(self._nova.brain.cloud_provider_name)

    def test_chat_clear_wipes_server_conversation(self) -> None:
        self._client.post("/ask", json={"request": "hello there"})
        self.assertGreater(self._nova.brain.conversation.turn_count, 0)

        cleared = self._client.post("/chat/clear")

        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json()["status"], "cleared")
        self.assertEqual(self._nova.brain.conversation.turn_count, 0)
        # The shared transcript is wiped with the LLM memory (one /ask adds
        # BOTH sides of the turn: user question + assistant answer).
        self.assertEqual(cleared.json()["transcript_dropped"], 2)
        self.assertEqual(self._client.get("/chat/history").json()["turns"], [])

    def test_chat_history_is_shared_and_survives_restart(self) -> None:
        """The thread lives on the server: /ask records it, another 'browser'
        (a fresh client with no storage) reads the same turns, and a rebuilt
        app instance re-loads the persisted thread."""
        self._client.post("/ask", json={"request": "shared question"})

        turns = self._client.get("/chat/history").json()["turns"]
        self.assertEqual([t["role"] for t in turns], ["user", "assistant"])
        self.assertEqual(turns[0]["text"], "shared question")

        # A different client sees the SAME thread (the old per-browser
        # localStorage behavior gave this second browser an empty panel).
        other_browser = self._client.get("/chat/history")
        self.assertEqual(other_browser.json(), turns and {"turns": turns})

        # Persistence: a rebuilt app (same data dir) re-loads the thread.
        rebuilt = NovaControlApplication(data_dir=self._nova.data_dir)
        try:
            self.assertEqual(
                [t["text"] for t in rebuilt.chat_transcript.turns()],
                [t["text"] for t in turns],
            )
        finally:
            rebuilt.stop() if hasattr(rebuilt, "stop") else None

    def test_chat_history_append_and_idempotent_migration(self) -> None:
        # A client-reported turn (voice input, CLI, …) lands in the thread.
        recorded = self._client.post(
            "/chat/history", json={"role": "user", "text": "from voice", "route": ""}
        )
        self.assertEqual(recorded.status_code, 200)
        self.assertTrue(recorded.json()["recorded"])

        # One-time migration: the browser's old localStorage thread merges in.
        local = [
            {"role": "user", "text": "old local q", "route": "", "at": 111},
            {"role": "assistant", "text": "old local a", "route": "chat", "at": 112},
        ]
        migrated = self._client.post("/chat/history", json={"action": "migrate", "turns": local})
        self.assertEqual(migrated.status_code, 200)
        self.assertEqual(migrated.json()["imported"], 2)

        # Re-running the same migration imports NOTHING (idempotent).
        again = self._client.post("/chat/history", json={"action": "migrate", "turns": local})
        self.assertEqual(again.json()["imported"], 0)

        # Turns already on the server are never duplicated by a migration.
        self._client.post("/ask", json={"request": "already here"})
        server_turns = self._client.get("/chat/history").json()["turns"]
        signature = lambda t: (t["role"], t["text"], t["at"])
        pre_clear = [signature(t) for t in self._client.get("/chat/history").json()["turns"]]
        self._client.post("/chat/history", json={"action": "migrate", "turns": pre_clear})
        post = self._client.get("/chat/history").json()["turns"]
        self.assertEqual(len(post), len(server_turns))

        # Invalid roles are rejected at the append path.
        bad = self._client.post("/chat/history", json={"role": "system", "text": "nope"})
        self.assertEqual(bad.status_code, 200)
        self.assertFalse(bad.json()["recorded"])

    def test_local_model_picker_listing_and_pinning(self) -> None:
        # No Ollama reachable in tests: the listing is empty but well-formed,
        # and reports the persisted pick.
        listed = self._client.get("/brain/ollama/models")
        self.assertEqual(listed.status_code, 200)
        body = listed.json()
        self.assertEqual(body["available"], [])
        self.assertEqual(body["picked"], "")
        self.assertIn("active_model", body)

        # Pinning a model with no Ollama running persists the pick (it re-arms
        # via the lazy re-probe when Ollama starts) and returns brain status.
        pinned = self._client.post("/brain/local/model", json={"model": "qwen3:30b"})
        self.assertEqual(pinned.status_code, 200)
        stored = self._nova._cloud_llm_store.read("cloud_llm")
        self.assertEqual(stored["local_model"], "qwen3:30b")
        again = self._client.get("/brain/ollama/models").json()
        self.assertEqual(again["picked"], "qwen3:30b")

        # Empty body clears the pick back to auto.
        cleared = self._client.post("/brain/local/model", json={"model": ""})
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(self._nova._cloud_llm_store.read("cloud_llm")["local_model"], "")

    def test_local_model_pin_survives_reboot(self) -> None:
        """The picked model persists and pins a rebuilt app's local provider."""
        self._nova._cloud_llm_store.write("cloud_llm", {"local_model": "qwen3:30b"})
        data_dir = self._nova.data_dir

        rebuilt = NovaControlApplication(data_dir=data_dir)
        try:
            # Boot found no Ollama (no network in tests) so the local slot stays
            # Echo, but the lazy re-probe must carry the pinned model.
            self.assertIsNotNone(rebuilt._ollama_reprobe)
            self.assertEqual(
                rebuilt._cloud_llm_store.read("cloud_llm").get("local_model"), "qwen3:30b"
            )
        finally:
            rebuilt.stop() if hasattr(rebuilt, "stop") else None

    def test_task_delete_and_clear_over_http(self) -> None:
        # A real /ask creates a tracked task record.
        self._client.post("/ask", json={"request": "what is 2+2"})
        tasks = self._client.get("/tasks").json()["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertIn("id", tasks[0])
        self.assertIn("title", tasks[0])
        self.assertNotIn("result", tasks[0], "status listing must stay a trimmed view")

        deleted = self._client.post("/tasks/delete", json={"id": tasks[0]["id"]})
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["status"], "deleted")
        self.assertEqual(self._client.get("/tasks").json()["tasks"], [])

        missing = self._client.post("/tasks/delete", json={"id": tasks[0]["id"]})
        self.assertEqual(missing.status_code, 404)

        cleared = self._client.post("/tasks/clear")
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json()["deleted"], 0)

    def test_task_clear_undo_round_trip(self) -> None:
        # Two real tasks, then a wipe that must come back.
        self._client.post("/ask", json={"request": "undo me one"})
        self._client.post("/ask", json={"request": "undo me two"})
        before = self._client.get("/tasks").json()["tasks"]
        self.assertEqual(len(before), 2)

        cleared = self._client.post("/tasks/clear")
        self.assertEqual(cleared.status_code, 200)
        body = cleared.json()
        self.assertEqual(body["deleted"], 2)
        self.assertEqual(self._client.get("/tasks").json()["tasks"], [])

        token = body["undo"]["token"]
        undone = self._client.post("/tasks/clear/undo", json={"token": token})
        self.assertEqual(undone.status_code, 200)
        self.assertEqual(undone.json()["restored"], 2)
        after = self._client.get("/tasks").json()["tasks"]
        self.assertEqual({t["id"] for t in after}, {t["id"] for t in before})

        # One-shot: replaying the same token is rejected.
        replay = self._client.post("/tasks/clear/undo", json={"token": token})
        self.assertEqual(replay.status_code, 409)

    def test_task_clear_undo_rejects_unknown_token(self) -> None:
        response = self._client.post("/tasks/clear/undo", json={"token": "nope"})
        self.assertEqual(response.status_code, 409)

    def test_task_clear_of_empty_list_has_no_undo(self) -> None:
        cleared = self._client.post("/tasks/clear")
        self.assertEqual(cleared.status_code, 200)
        self.assertNotIn("undo", cleared.json())


if __name__ == "__main__":
    unittest.main()


# ── Vision + bugs + auto-approve API (JARVIS batch) ─────────────────────────


class VisionAndBugsApiTests(_IsolatedApiTestCase):
    def test_vision_status_reports_model_and_bugs(self) -> None:
        response = self._client.get("/vision/status")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("vision_model", payload)
        self.assertIn("open_bugs", payload)

    def test_bug_recorded_by_vision_failure_is_listed_and_fixable(self) -> None:
        # A guided click against the Noop runner fails (no screenshot); the
        # failure must surface in /bugs with what/where/when.
        click = self._client.post("/vision/click", json={"label": "File"})
        self.assertEqual(click.status_code, 200)
        bugs = self._client.get("/bugs").json()
        self.assertGreaterEqual(len(bugs["bugs"]), 1)
        entry = bugs["bugs"][0]
        self.assertIn("what", entry)
        self.assertIn("where", entry)
        self.assertIn("when", entry)
        fixed = self._client.post(f"/bugs/{entry['id']}/fix")
        self.assertEqual(fixed.status_code, 200)
        self.assertEqual(fixed.json()["status"], "fixed")

    def test_auto_approve_run_setting_round_trips(self) -> None:
        saved = self._client.post("/settings", json={"auto_approve_run": True})
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()["auto_approve_run"])
        self.assertTrue(self._nova.settings.settings.auto_approve_run)
        # Persisted: a fresh read reflects it too.
        current = self._client.get("/settings").json()
        self.assertTrue(current["auto_approve_run"])
