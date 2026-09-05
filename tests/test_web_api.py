"""HTTP-level tests for the FastAPI surface using TestClient.

These tests exercise the real create_app() factory end to end: plan a device
command (desktop or browser), execute it with the server-minted approval token,
and verify the API rejects missing / unknown / expired / replayed tokens with
403. The desktop and browser runners are swapped for no-op runners and the app
data dir is a temp dir, so nothing touches the real desktop, a real browser, or
the ./data state store.
"""

from __future__ import annotations

import re
import time
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

try:
    from fastapi.testclient import TestClient  # requires httpx
except Exception:  # pragma: no cover - httpx is an optional dev dependency
    TestClient = None  # type: ignore[assignment]

from novacontrol.api.app import create_app
from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner

# Each device kind plans and executes through the same /command/* endpoints; the
# intent router dispatches desktop commands to the desktop controller and browser
# commands (navigate, fill forms) to the browser controller.
DEVICE_COMMANDS = [
    ("open calculator", "desktop_automation"),
    ("navigate to example.com", "browser_automation"),
]


@unittest.skipIf(TestClient is None, "fastapi.testclient.TestClient requires httpx (pip install httpx)")
class CommandExecutionApiTests(unittest.TestCase):
    """plan -> execute with a real token; 403 on missing/unknown/expired/replayed."""

    def setUp(self) -> None:
        if TestClient is None:
            self.skipTest("httpx not installed")
        self._tmp = TemporaryDirectory()
        self._nova: NovaControlApplication | None = None
        self._patchers = [
            mock.patch("novacontrol.api.app.NovaControlApplication", side_effect=self._isolated_application),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
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


if __name__ == "__main__":
    unittest.main()
