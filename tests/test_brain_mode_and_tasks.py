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

    async def test_cloud_provider_activates_and_survives_mode_detours(self) -> None:
        provider = _FakeLLM()
        brain = self._brain(EchoLLMProvider())
        brain.set_cloud_provider(provider)

        self.assertEqual(brain.mode, "cloud")
        self.assertIs(brain.completion_provider, provider)
        self.assertEqual(brain.cloud_provider_name, "fake-ollama")

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


if __name__ == "__main__":
    unittest.main()
