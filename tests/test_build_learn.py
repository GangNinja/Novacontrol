"""Learn (teach/recall) and Build (code plan) features, end to end.

The Learn tab used to only record improvement cycles — typing a fact never
became durable knowledge. The Build tab's /plan was a generic 3-step wrapper
with no coding knowledge. These tests pin the real capabilities:

  * ``teach_knowledge`` persists a fact (KNOWLEDGE namespace) with duplicate
    detection; ``list_knowledge`` retrieves it; chat ``remember this:`` stores
    through the same path and a topical question recalls it;
  * ``build_code_plan`` produces language-aware steps and an artifact name in
    every language, drafts code via the configured LLM when available, and
    degrades to the deterministic plan when it is not;
  * the HTTP surface: POST /knowledge/teach, GET /knowledge,
    POST /knowledge/recall, POST /plan/code (with the route-contract
    renderer fields renderLearning and renderBuild read first).
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
from novacontrol.brain.models import BrainRequest


def _isolated_app() -> tuple[NovaControlApplication, TemporaryDirectory]:
    tmp = TemporaryDirectory()
    return NovaControlApplication(data_dir=tmp.name), tmp


class TeachKnowledgeTests(unittest.IsolatedAsyncioTestCase):
    """teach -> list -> recall, over the real memory store."""

    async def test_teach_persists_and_lists(self) -> None:
        app, tmp = _isolated_app()
        try:
            taught = await app.teach_knowledge("the server room code is 4417")
            self.assertFalse(taught["duplicate"])
            self.assertEqual(taught["mode"], "knowledge_teach")
            self.assertIn("Learned", taught["message"])

            listed = await app.list_knowledge("server room")
            self.assertEqual(listed["count"], 1)
            self.assertEqual(listed["facts"][0]["text"], "the server room code is 4417")
        finally:
            tmp.cleanup()

    async def test_teach_detects_duplicates(self) -> None:
        app, tmp = _isolated_app()
        try:
            await app.teach_knowledge("my sister's birthday is June 3")
            again = await app.teach_knowledge("my sister's birthday is June 3")
            self.assertTrue(again["duplicate"])
            self.assertIn("already", again["message"].lower())
        finally:
            tmp.cleanup()

    async def test_teach_rejects_empty_fact(self) -> None:
        app, tmp = _isolated_app()
        try:
            with self.assertRaises(ValueError):
                await app.teach_knowledge("   ")
        finally:
            tmp.cleanup()

    async def test_chat_remember_this_stores_recallable_knowledge(self) -> None:
        app, tmp = _isolated_app()
        try:
            text = "remember this: the garage door code is 9915"
            status, payload = await app._handle_memory(BrainRequest(text=text), text)
            self.assertEqual(status, "memory")
            self.assertEqual(payload["mode"], "memory_store")
            self.assertIn("9915", payload["message"])

            question = "what is the garage door code"
            status, payload = await app._handle_memory(BrainRequest(text=question), question)
            self.assertEqual(payload["mode"], "memory_recall")
            self.assertIn("9915", payload["message"])
        finally:
            tmp.cleanup()

    async def test_memory_miss_reports_guidance_not_a_crash(self) -> None:
        app, tmp = _isolated_app()
        try:
            question = "what is a dolphin"
            status, payload = await app._handle_memory(BrainRequest(text=question), question)
            self.assertEqual(payload["mode"], "memory_recall")
            self.assertIn("message", payload)
            self.assertEqual(payload["facts"], [])
        finally:
            tmp.cleanup()


class BuildCodePlanTests(unittest.IsolatedAsyncioTestCase):
    """Language-aware coding plans, deterministic fallback + LLM draft path."""

    async def test_python_plan_has_language_aware_steps_and_artifact(self) -> None:
        app, tmp = _isolated_app()
        try:
            result = await app.build_code_plan("write a fibonacci generator", language="python")
            self.assertEqual(result["mode"], "code_plan")
            self.assertEqual(result["language"], "python")
            self.assertTrue(result["artifact"]["path"].endswith(".py"))
            titles = [s["title"] for s in result["plan"]["steps"]]
            self.assertIn("Implement in python", " ".join(titles))
            self.assertTrue(any("test_" in s["description"] for s in result["plan"]["steps"]))
            # No LLM configured -> a RUNNABLE scaffold, never an empty editor
            # (the empty artifact made the Build tab feel like nothing codes).
            self.assertNotEqual(result["artifact"]["content"], "")
            self.assertEqual(result["artifact"]["generated_by"], "scaffold")
            compile(result["artifact"]["content"], result["artifact"]["path"], "exec")  # must be valid python
            self.assertIn('"""', result["artifact"]["content"])
        finally:
            tmp.cleanup()

    async def test_language_aliases_and_extensions(self) -> None:
        app, tmp = _isolated_app()
        try:
            for lang, ext in (("py", ".py"), ("go", ".go"), ("rust", ".rs"), ("ts", ".ts"), ("bash", ".sh")):
                with self.subTest(lang=lang):
                    result = await app.build_code_plan("write a file watcher utility", language=lang)
                    self.assertEqual(result["language"], {"py": "python", "go": "go", "rust": "rust", "ts": "typescript", "bash": "shell"}[lang])
                    self.assertTrue(result["artifact"]["path"].endswith(ext))
        finally:
            tmp.cleanup()

    async def test_llm_draft_is_used_when_configured(self) -> None:
        app, tmp = _isolated_app()
        try:
            class _FakeCompletion:
                name = "fake-cloud"

                async def complete(self, messages: Any, **kwargs: Any) -> str:
                    return "def fibonacci(n):\n    ...\n"

            app.brain.completion_provider = _FakeCompletion()
            self.assertTrue(app.brain.model_configured)
            result = await app.build_code_plan("write a fibonacci generator", language="python")
            # The agent loop runs the draft; without a JS/py execution guarantee
            # for the fake (python IS installed, so the fake's non-sense code
            # executes and may fail), the artifact is agent-generated either way.
            self.assertIn(result["artifact"]["generated_by"], ("agent", "llm"))
            self.assertTrue(result["agent"]["steps"], "agent trace must be present")
        finally:
            tmp.cleanup()

    async def test_llm_failure_falls_back_to_deterministic_plan(self) -> None:
        app, tmp = _isolated_app()
        try:
            class _ExplodingCompletion:
                name = "fake-cloud"

                async def complete(self, messages: Any, **kwargs: Any) -> str:
                    raise RuntimeError("provider down")

            app.brain.completion_provider = _ExplodingCompletion()
            result = await app.build_code_plan("write a fibonacci generator", language="python")
            # Provider failure degrades to the runnable scaffold, never empty.
            self.assertEqual(result["artifact"]["generated_by"], "scaffold")
            self.assertNotEqual(result["artifact"]["content"], "")
            compile(result["artifact"]["content"], result["artifact"]["path"], "exec")
        finally:
            tmp.cleanup()

    async def test_empty_goal_rejected(self) -> None:
        app, tmp = _isolated_app()
        try:
            with self.assertRaises(ValueError):
                await app.build_code_plan("   ")
        finally:
            tmp.cleanup()


class LearnBuildApiTests(unittest.TestCase):
    """HTTP surface: teach/list/recall/code-plan over the real create_app."""

    def setUp(self) -> None:
        if TestClient is None:
            self.skipTest("httpx not installed")
        self._tmp = TemporaryDirectory()
        self._patchers = [
            mock.patch("novacontrol.api.app.NovaControlApplication", side_effect=self._isolated_application),
            mock.patch("novacontrol.application.LocalDesktopRunner", lambda *a, **k: None),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", lambda *a, **k: None),
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

    def _isolated_application(self, **kwargs: Any) -> NovaControlApplication:
        kwargs["data_dir"] = self._tmp.name
        app = NovaControlApplication(**kwargs)  # type: ignore[arg-type]
        self._nova = app
        return app

    def test_teach_list_recall_over_http(self) -> None:
        response = self._client.post("/knowledge/teach", json={"fact": "the office wifi password is correct-horse"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mode"], "knowledge_teach")
        self.assertFalse(body["duplicate"])

        listing = self._client.get("/knowledge", params={"query": "wifi"})
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["count"], 1)

        recall = self._client.post("/knowledge/recall", json={"query": "wifi password"})
        self.assertEqual(recall.status_code, 200)
        self.assertEqual(recall.json()["facts"][0]["text"], "the office wifi password is correct-horse")

    def test_teach_requires_a_fact(self) -> None:
        response = self._client.post("/knowledge/teach", json={"fact": "  "})
        self.assertEqual(response.status_code, 422)

    def test_code_plan_over_http(self) -> None:
        response = self._client.post("/plan/code", json={"goal": "write a csv merger", "language": "python"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mode"], "code_plan")
        self.assertEqual(body["language"], "python")
        self.assertTrue(body["artifact"]["path"].endswith(".py"))
        self.assertTrue(body["plan"]["steps"], "renderBuild reads plan.steps")

    def test_code_plan_requires_a_goal(self) -> None:
        response = self._client.post("/plan/code", json={"goal": ""})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
