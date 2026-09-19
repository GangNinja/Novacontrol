"""Brain mode switching and task deletion unit tests."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from novacontrol.brain import BrainRequest, NovaBrain
from novacontrol.brain.scratch import ScratchReasoningEngine
from novacontrol.integrations.llm import EchoLLMProvider, build_cloud_provider
from novacontrol.settings import SettingsManager
from novacontrol.settings.models import BRAIN_MODES
from novacontrol.tasks import TaskCenter, TaskRecordStatus


class _FakeLLM:
    """Stand-in for a real provider (e.g. Ollama)."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "fake-ollama"

    @property
    def model(self) -> str:
        return "llama-fake"

    async def complete(self, messages, **kwargs) -> str:  # noqa: ANN001, ANN003
        self.calls += 1
        return "model answer"


class BrainModeTests(unittest.IsolatedAsyncioTestCase):
    def _brain(self, provider: object | None = None) -> NovaBrain:
        brain = NovaBrain(completion_provider=provider or EchoLLMProvider())
        # Detach the lazy re-probe: mode switching alone is what's under test.
        brain._ollama_reprobe = None
        return brain

    async def test_scratch_mode_answers_locally_even_with_a_model(self) -> None:
        provider = _FakeLLM()
        brain = self._brain(provider)

        brain.set_mode("scratch")
        response = await brain.chat(BrainRequest(text="what time is it", context={}))

        self.assertEqual(provider.calls, 0, "scratch mode must not call the LLM")
        self.assertEqual(response.payload["brain_mode"], "scratch")
        self.assertEqual(brain.effective_mode, "scratch")

    async def test_llm_mode_restores_the_boot_provider_after_scratch(self) -> None:
        provider = _FakeLLM()
        brain = self._brain(provider)
        brain.set_mode("scratch")
        brain.set_mode("llm")

        self.assertIs(brain.completion_provider, provider)
        response = await brain.chat(BrainRequest(text="hello", context={}))

        self.assertEqual(provider.calls, 1)
        self.assertEqual(response.payload["brain_mode"], "llm")

    async def test_llm_mode_without_a_model_stays_on_scratch(self) -> None:
        brain = self._brain(EchoLLMProvider())
        brain.set_mode("llm")

        self.assertEqual(brain.effective_mode, "scratch")

    async def test_auto_mode_reports_llm_when_configured(self) -> None:
        brain = self._brain(_FakeLLM())

        self.assertEqual(brain.mode, "auto")
        self.assertEqual(brain.effective_mode, "llm")

    async def test_forced_scratch_blocks_the_lazy_ollama_reprobe(self) -> None:
        brain = NovaBrain(completion_provider=EchoLLMProvider())

        async def reprobe() -> object | None:
            return _FakeLLM()

        brain._ollama_reprobe = reprobe
        brain.set_mode("scratch")
        await brain._maybe_upgrade_provider()

        self.assertEqual(str(brain.completion_provider.name), "echo",
                         "re-probe must not override forced scratch mode")

    async def test_scratch_payload_still_labels_itself(self) -> None:
        engine = ScratchReasoningEngine()
        payload = engine.answer("what time is it", {})
        self.assertEqual(payload["brain_mode"], "scratch")

    def test_brain_modes_vocabulary(self) -> None:
        self.assertEqual(BRAIN_MODES, ("auto", "llm", "scratch", "cloud"))


class ChatCloudRoundTripTests(unittest.IsolatedAsyncioTestCase):
    """A real chat request against a fake OpenAI-compatible cloud endpoint.

    Spins a local HTTP server speaking /v1/chat/completions, installs a
    build_cloud_provider instance pointed at it, forces cloud mode, and asserts
    the brain actually completes through the cloud wire protocol.
    """

    def setUp(self) -> None:
        self.received: dict[str, Any] = {}

        recorded = self.received

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                recorded.update(
                    {
                        "path": self.path,
                        "body": json.loads(self.rfile.read(length).decode("utf-8")),
                        "auth": self.headers.get("Authorization", ""),
                    }
                )
                answer = json.dumps(
                    {"choices": [{"message": {"role": "assistant", "content": "cloud says hi"}}]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *args: object) -> None:  # silence the console
                return

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"

    def tearDown(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    async def test_chat_completes_through_the_cloud_provider(self) -> None:
        brain = NovaBrain(completion_provider=EchoLLMProvider())
        brain._ollama_reprobe = None
        cloud = build_cloud_provider(
            "openai",
            "sk-test-key",
            model="gpt-4o-mini-test",
            environ={"NOVACONTROL_LLM_BASE_URL": self.base_url},
        )
        self.assertIsNotNone(cloud)
        assert cloud is not None
        # Point the preset URL at the local fake for this test only.
        cloud.base_url = self.base_url
        brain.set_cloud_provider(cloud)
        brain.set_mode("cloud")  # the cloud brain runs only when it is chosen

        response = await brain.chat(BrainRequest(text="hello there", context={}))

        self.assertIn("cloud says hi", response.summary)
        # The wire call carried the preset chat path, bearer key, and model.
        self.assertEqual(self.received["path"], "/v1/chat/completions")
        self.assertEqual(self.received["auth"], "Bearer sk-test-key")
        self.assertEqual(self.received["body"]["model"], "gpt-4o-mini-test")


class TaskDeletionTests(unittest.TestCase):
    def test_delete_removes_one_task_and_raises_on_unknown(self) -> None:
        center = TaskCenter()
        task = center.create("Keep me")
        other = center.create("Delete me")

        deleted = center.delete(other.id)

        self.assertEqual(deleted.id, other.id)
        self.assertEqual([t.id for t in center.list()], [task.id])
        with self.assertRaises(KeyError):
            center.delete("nope")

    def test_clear_removes_every_task(self) -> None:
        center = TaskCenter()
        center.create("a")
        center.create("b")
        center.update(center.create("c").id, TaskRecordStatus.COMPLETED)

        self.assertEqual(center.clear(), 3)
        self.assertEqual(center.list(), ())
        self.assertEqual(center.clear(), 0)

    def test_deleted_tasks_do_not_round_trip(self) -> None:
        center = TaskCenter()
        keep, drop = center.create("keep"), center.create("drop")
        center.delete(drop.id)

        restored = TaskCenter.from_dict(center.to_dict())

        self.assertEqual([t.id for t in restored.list()], [keep.id])

    def test_clear_snapshot_returns_removed_and_empties(self) -> None:
        center = TaskCenter()
        a, b = center.create("a"), center.create("b")
        center.update(b.id, TaskRecordStatus.COMPLETED)

        snapshot = center.clear_snapshot()

        self.assertEqual([r.id for r in snapshot], [a.id, b.id])
        self.assertEqual(center.list(), ())
        # Clearing an empty center snapshots nothing.
        self.assertEqual(center.clear_snapshot(), [])

    def test_restore_puts_records_back_and_skips_existing_ids(self) -> None:
        center = TaskCenter()
        a, b = center.create("a"), center.create("b")
        snapshot = center.clear_snapshot()

        # While the snapshot was held, a NEW task appeared.
        fresh = center.create("fresh")
        restored = center.restore(snapshot)

        self.assertEqual(restored, 2)
        self.assertEqual({t.id for t in center.list()}, {a.id, b.id, fresh.id})
        # Restoring again (double-undo) resurrects nothing new.
        self.assertEqual(center.restore(snapshot), 0)

    def test_restored_records_round_trip(self) -> None:
        center = TaskCenter()
        center.create("a")
        snapshot = center.clear_snapshot()
        center.restore(snapshot)

        revived = TaskCenter.from_dict(center.to_dict())

        self.assertEqual([t.title for t in revived.list()], ["a"])


class ExploreFollowsBrainModeTests(unittest.IsolatedAsyncioTestCase):
    """Explore synthesis must mirror the Chat brain in EVERY mode: scratch
    means local templates (no provider), cloud means the cloud provider, and
    the mode switch must invalidate the report cache (no stale synthesis)."""

    def _app(self):
        import os
        import tempfile
        from unittest import mock as _mock

        from novacontrol.application import NovaControlApplication
        from novacontrol.integrations.llm import EchoLLMProvider

        tmp = tempfile.TemporaryDirectory()
        os.environ.setdefault("NOVACONTROL_DISABLE_OLLAMA", "1")
        with _mock.patch(
            "novacontrol.application.build_llm_provider_from_environment",
            return_value=EchoLLMProvider(),
        ):
            app = NovaControlApplication(data_dir=tmp.name)
        return app, tmp

    def _explore_provider(self, app):
        return getattr(app.explore.explainer._completion_provider, "name", None)

    async def test_explore_provider_mirrors_every_mode(self) -> None:
        app, tmp = self._app()
        try:
            # Boot with Echo (no model): effective scratch -> templates (None).
            self.assertIsNone(self._explore_provider(app))

            # A real model appears (lazy upgrade simulation): the reprobe swaps
            # the provider AND fires on_provider_upgrade -> _sync_explore_provider.
            class _Fake:
                name = "fake-local"
                model = "m"

                async def complete(self, messages, **kwargs):
                    return "ok"

            fake = _Fake()
            app.brain.completion_provider = fake
            app._sync_explore_provider()
            self.assertEqual(self._explore_provider(app), "fake-local")

            # Scratch -> llm must come back to the SAME model (the local slot
            # re-arms through the brain's set_local_provider swap path).
            app.brain.set_local_provider(fake)
            app.set_brain_mode("scratch")
            app.set_brain_mode("llm")
            self.assertEqual(self._explore_provider(app), "fake-local")

            # Scratch: local templates everywhere, Chat AND Explore.
            app.set_brain_mode("scratch")
            self.assertIsNone(self._explore_provider(app))

            # llm: the local model again.
            app.set_brain_mode("llm")
            self.assertEqual(self._explore_provider(app), "fake-local")

            # Configuring a cloud key does NOT move either surface onto it: the
            # local model keeps answering until Cloud is selected by hand.
            app.set_cloud_llm("openai", "sk-test-1234567890abcdefgh", model="gpt-4o-mini")
            self.assertEqual(self._explore_provider(app), "fake-local")

            # cloud SELECTED: the cloud provider takes over BOTH surfaces.
            app.set_brain_mode("cloud")
            self.assertEqual(self._explore_provider(app), "cloud:openai")

            # scratch with cloud configured: STILL templates — scratch wins.
            app.set_brain_mode("scratch")
            self.assertIsNone(self._explore_provider(app))

            # Back to cloud: Explore returns to the cloud voice.
            app.set_brain_mode("cloud")
            self.assertEqual(self._explore_provider(app), "cloud:openai")
        finally:
            tmp.cleanup()

    async def test_report_cache_is_keyed_per_synthesis_brain(self) -> None:
        """A mode switch must not serve a report synthesized by the OLD brain:
        the same request after switching is a fresh synthesis (fresh report id)."""
        from novacontrol.explore import ExploreRequest
        from conftest import FakeSearchProvider, FakeVideoProvider

        app, tmp = self._app()
        try:
            app.explore.search_provider = FakeSearchProvider()
            app.explore.video_provider = FakeVideoProvider()
            request = ExploreRequest("brain cache key topic")

            class _Fake:
                name = "fake-local"
                model = "m"

                async def complete(self, messages, **kwargs):
                    return "ok"

            app.brain.completion_provider = _Fake()
            app._sync_explore_provider()
            first = await app.explore.research(request)

            # Same request, same brain -> cached (same id).
            second = await app.explore.research(request)
            self.assertEqual(first.id, second.id)

            # Switch to scratch -> different synthesis brain -> fresh report.
            app.set_brain_mode("scratch")
            third = await app.explore.research(request)
            self.assertNotEqual(first.id, third.id, "mode switch must not serve the old brain's cached report")
        finally:
            tmp.cleanup()

    def _brain(self, provider: object | None = None) -> NovaBrain:
        brain = NovaBrain(completion_provider=provider or EchoLLMProvider())
        # Detach the lazy re-probe: mode switching alone is what's under test.
        brain._ollama_reprobe = None
        return brain


class BrainModeCloudTests(unittest.IsolatedAsyncioTestCase):
    """Cloud-slot behaviors split out of BrainModeTests (same helpers)."""

    def _brain(self, provider: object | None = None) -> NovaBrain:
        brain = NovaBrain(completion_provider=provider or EchoLLMProvider())
        brain._ollama_reprobe = None
        return brain

    async def test_installing_a_cloud_key_stays_local_until_cloud_is_selected(self) -> None:
        """Storing a key is not asking for it: a configured cloud LLM must sit
        unused until the user picks Cloud, and then survive mode detours."""
        provider = _FakeLLM()
        brain = self._brain(EchoLLMProvider())
        brain.set_cloud_provider(provider)

        # Installed, reported, and NOT in use: local mode is untouched.
        self.assertEqual(brain.mode, "auto")
        self.assertEqual(brain.cloud_provider_name, "fake-ollama")
        self.assertEqual(str(brain.completion_provider.name), "echo")

        # Selecting Cloud is what activates it.
        brain.set_mode("cloud")
        self.assertIs(brain.completion_provider, provider)

        # A scratch detour then "cloud" again re-arms the SAME cloud provider.
        brain.set_mode("scratch")
        self.assertEqual(brain.effective_mode, "scratch")
        brain.set_mode("cloud")
        self.assertIs(brain.completion_provider, provider)
        response = await brain.chat(BrainRequest(text="hello", context={}))
        self.assertEqual(provider.calls, 1)
        self.assertEqual(response.payload["brain_mode"], "llm")  # derived from the live provider

    async def test_cloud_mode_without_a_configured_cloud_falls_back_like_llm(self) -> None:
        brain = self._brain(EchoLLMProvider())
        brain.set_mode("cloud")
        self.assertEqual(brain.effective_mode, "scratch")

    def test_clearing_the_cloud_provider_returns_to_auto(self) -> None:
        provider = _FakeLLM()
        brain = self._brain(EchoLLMProvider())
        brain.set_cloud_provider(provider)
        brain.set_cloud_provider(None)
        self.assertEqual(brain.mode, "auto")
        self.assertEqual(brain.cloud_provider_name, "")
        self.assertEqual(str(brain.completion_provider.name), "echo")

    def test_settings_manager_persists_and_validates_brain_mode(self) -> None:
        manager = SettingsManager()
        manager.update(brain_mode="scratch")
        self.assertEqual(manager.settings.brain_mode, "scratch")
        restored = SettingsManager.from_dict(manager.to_dict())
        self.assertEqual(restored.settings.brain_mode, "scratch")
        manager.update(brain_mode="bogus")
        self.assertEqual(manager.settings.brain_mode, "auto")

if __name__ == "__main__":
    unittest.main()
