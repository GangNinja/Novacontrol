"""Docker smoke checklist as HTTP-level tests.

The container smoke build (see .freebuff/docker-smoke.ps1) proves the image
starts and serves /health; this module proves the same six application-level
behaviors the checklist covers — chat math, chat-to-research routing, the
/explore pipeline, command planning, health, and empty-input handling — over
real HTTP against the real create_app() factory, so CI gets the same
confidence without Docker or a browser.

Hermetic by construction: temp data dir, no-op device runners, Echo LLM forced
at boot, and fake search/video providers on the explore service — no internet,
no desktop, no adb.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

from conftest import FakeSearchProvider, FakeVideoProvider
from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner

try:
    from fastapi.testclient import TestClient  # requires httpx
except Exception:  # pragma: no cover - httpx is an optional dev dependency
    TestClient = None  # type: ignore[assignment]

import unittest
from tempfile import TemporaryDirectory

from novacontrol.api.app import create_app
from novacontrol.application import NovaControlApplication
from novacontrol.integrations.llm import EchoLLMProvider


@unittest.skipIf(TestClient is None, "fastapi.testclient.TestClient requires httpx (pip install httpx)")
class DockerSmokeHttpTests(unittest.TestCase):
    """The six-point smoke checklist, runnable anywhere CI runs pytest."""

    def setUp(self) -> None:
        if TestClient is None:
            self.skipTest("httpx not installed")
        self._tmp = TemporaryDirectory()
        patchers = [
            mock.patch(
                "novacontrol.api.app.NovaControlApplication",
                side_effect=self._isolated_application,
            ),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
            mock.patch(
                "novacontrol.application.build_llm_provider_from_environment",
                return_value=EchoLLMProvider(),
            ),
        ]
        for patcher in patchers:
            patcher.start()
        self._patchers = patchers
        self._client = TestClient(create_app())
        self._client.__enter__()
        assert self._nova is not None
        self._nova.explore.search_provider = FakeSearchProvider()
        self._nova.explore.video_provider = FakeVideoProvider()

    def tearDown(self) -> None:
        self._client.__exit__(None, None, None)
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()

    def _isolated_application(self, **kwargs: object) -> NovaControlApplication:
        kwargs["data_dir"] = self._tmp.name
        app = NovaControlApplication(**kwargs)  # type: ignore[arg-type]
        self._nova = app
        return app

    # 1. Health: the container's HEALTHCHECK target answers before anything else.
    def test_health_endpoint_responds(self) -> None:
        response = self._client.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body.get("ok", body.get("status") in ("ok", "healthy")))

    # 2. Chat math: scratch brain answers arithmetic locally.
    def test_chat_math_returns_local_answer(self) -> None:
        response = self._client.post("/ask", json={"request": "what is 15 times 3"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["route"], "chat")
        self.assertIn("45", body["summary"])

    # 3. Chat-to-research: research phrasing upgrades to the explore pipeline.
    def test_chat_research_question_routes_to_explore(self) -> None:
        response = self._client.post(
            "/ask", json={"request": "Compare React, Vue, and Svelte for building a dashboard"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["route"], "explore")
        self.assertEqual(body["intent"], "explore")
        data = body["data"]
        self.assertTrue(data.get("sources"), "research answer must carry sources")

    # 4. Explore: the dedicated endpoint returns the flat sourced report.
    def test_explore_returns_flat_report(self) -> None:
        response = self._client.post(
            "/explore",
            json={"topic": "How do I set up a home composting system?", "include_videos": True},
        )
        self.assertEqual(response.status_code, 200)
        report = response.json()
        self.assertIn("topic", report)
        self.assertIn("overview", report)
        self.assertNotIn("payload", report)
        self.assertTrue(report["sources"])

    # 5. Command plan: J.A.R.V.I.S planning mints a single-use approval token.
    def test_command_plan_mints_approval(self) -> None:
        response = self._client.post("/command/plan", json={"command": "open calculator"})
        self.assertEqual(response.status_code, 200)
        plan = response.json()
        self.assertEqual(plan["route"], "desktop_automation")
        self.assertEqual(plan["status"], "waiting_for_approval")
        self.assertTrue(plan["approval"]["token"])

    # 6. Bad input: a clean 422 at the edge — never a 500, never a crash.
    def test_missing_and_blank_input_rejected_with_422(self) -> None:
        # Missing required keys are field-level 422s, not KeyError-500s.
        for path in ("/ask", "/explore", "/command/plan"):
            with self.subTest(path=path, case="missing"):
                response = self._client.post(path, json={})
                self.assertEqual(response.status_code, 422)
                detail = response.json()["detail"]
                self.assertTrue(detail, "missing key must carry a field-level error")
        # Empty / whitespace-only input is rejected before reaching a pipeline.
        for path, field in (("/ask", "request"), ("/explore", "topic"), ("/command/plan", "command")):
            for bad in ("", "   "):
                with self.subTest(path=path, case=repr(bad)):
                    response = self._client.post(path, json={field: bad})
                    self.assertEqual(response.status_code, 422)
                    self.assertIn("must not be empty", response.json()["detail"][0]["msg"])

    def test_smoke_checklist_runs_in_order(self) -> None:
        """The whole checklist in one pass, mirroring the container smoke order."""
        self.assertEqual(self._client.get("/health").status_code, 200)
        self.assertIn("45", self._client.post("/ask", json={"request": "what is 15 times 3"}).json()["summary"])
        research = self._client.post(
            "/ask", json={"request": "Compare React, Vue, and Svelte for building a dashboard"}
        ).json()
        self.assertEqual(research["route"], "explore")
        explore = self._client.post("/explore", json={"topic": "home composting system"}).json()
        self.assertIn("overview", explore)
        plan = self._client.post("/command/plan", json={"command": "open calculator"}).json()
        self.assertEqual(plan["status"], "waiting_for_approval")
        self.assertEqual(self._client.post("/ask", json={"request": ""}).status_code, 422)

if __name__ == "__main__":
    unittest.main()
