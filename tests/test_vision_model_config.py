"""Multimodal vision-model configuration: build, wire, locate, persist.

The vision layer used to be OCR-only. A configured multimodal model (local
Ollama or a cloud provider) locates elements SEMANTICALLY — the screenshot is
embedded in an OpenAI-format multimodal message, the model answers with a JSON
0-1000-grid point, and the runner clicks it. These tests pin:

  - build_vision_provider: ollama probing/validation, cloud presets, the
    key requirement, and the (None, reason) contract on failure;
  - the application config surface: set/clear/status + hot-swap into the
    runner, the VisionController, and the agentcore perception engine;
  - boot restore from the persisted (gitignored) state namespace;
  - /vision/model + /vision/model/clear over real HTTP (422 on bad config);
  - the locate contract: JSON 0-1000 grid parsing (incl. prose-wrapped JSON),
    honest None, and llm_vision-wins-over-OCR priority;
  - describe_screen routes through the model, and has_vision_model stays
    False for the Echo fallback (which only echoes its own prompt back).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from test_web_api import _IsolatedApiTestCase

from novacontrol.application import NovaControlApplication
from novacontrol.desktop.vision_guide import _parse_llm_point, locate_element
from novacontrol.integrations.llm import EchoLLMProvider, build_vision_provider


def _ollama_info(models: list[str]) -> dict[str, Any]:
    return {"url": "http://127.0.0.1:11434", "models": models}


class BuildVisionProviderTests(unittest.TestCase):
    """The builder: ollama probing, cloud presets, (None, reason) contract."""

    def test_ollama_reachable_picks_the_vision_model(self) -> None:
        with mock.patch(
            "novacontrol.integrations.llm.detect_ollama",
            return_value=_ollama_info(["llama3.2:latest", "llava:13b"]),
        ):
            provider, reason = build_vision_provider("ollama", "")

        self.assertEqual(reason, "")
        self.assertIsNotNone(provider)
        self.assertEqual(provider.model, "llava:13b")
        self.assertEqual(provider.name, "vision:ollama")

    def test_ollama_explicit_model_must_be_vision_capable(self) -> None:
        with mock.patch(
            "novacontrol.integrations.llm.detect_ollama",
            return_value=_ollama_info(["llama3.2:latest"]),
        ):
            provider, reason = build_vision_provider("ollama", "", model="llama3.2:latest")
        self.assertIsNone(provider)
        self.assertIn("not a vision model", reason)

    def test_ollama_unreachable_returns_reason(self) -> None:
        with mock.patch(
            "novacontrol.integrations.llm.detect_ollama",
            return_value=None,
        ):
            provider, reason = build_vision_provider("ollama", "")
        self.assertIsNone(provider)
        self.assertIn("not reachable", reason)

    def test_ollama_without_any_vision_model(self) -> None:
        with mock.patch(
            "novacontrol.integrations.llm.detect_ollama",
            return_value=_ollama_info(["llama3.2:latest", "qwen2.5:7b"]),
        ):
            provider, reason = build_vision_provider("ollama", "")
        self.assertIsNone(provider)
        self.assertIn("No vision model found", reason)

    def test_cloud_preset_builds_provider_with_the_key(self) -> None:
        provider, reason = build_vision_provider("openai", "sk-test-123")
        self.assertEqual(reason, "")
        self.assertEqual(provider.name, "vision:openai")
        self.assertEqual(provider.api_key, "sk-test-123")
        self.assertTrue(provider.model)

    def test_cloud_preset_requires_a_key(self) -> None:
        provider, reason = build_vision_provider("openai", "  ")
        self.assertIsNone(provider)
        self.assertIn("API key is required", reason)

    def test_unknown_provider_rejected(self) -> None:
        provider, reason = build_vision_provider("anthropic-direct", "k")
        self.assertIsNone(provider)
        self.assertIn("Unknown vision model provider", reason)

    def test_text_only_preset_rejected(self) -> None:
        # A preset that exists but has no vision-capable models registered
        # must be refused (Mistral's current models take no image content).
        provider, reason = build_vision_provider("mistral", "k")
        self.assertIsNone(provider)
        self.assertIn("no vision-capable models", reason)


class _ScriptedProvider:
    """Minimal multimodal provider double returning scripted answers."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.calls: list[list[dict[str, Any]]] = []

    @property
    def name(self) -> str:
        return "vision:test"

    async def complete(self, messages: Any, **kwargs: Any) -> str:
        self.calls.append(messages)
        return self._answers.pop(0) if self._answers else ""


class ParseLlmPointTests(unittest.TestCase):
    """The JSON 0-1000 grid contract (plus the legacy region fallback)."""

    def test_pure_json_maps_grid_to_pixels(self) -> None:
        self.assertEqual(_parse_llm_point('{"found": true, "x": 500, "y": 250}', 1000, 500), (500, 125))

    def test_json_wrapped_in_prose(self) -> None:
        answer = 'Sure! {"found": true, "x": 100, "y": 800} hope that helps'
        self.assertEqual(_parse_llm_point(answer, 1000, 1000), (100, 800))

    def test_found_false_is_none(self) -> None:
        self.assertIsNone(_parse_llm_point('{"found": false}', 1000, 1000))

    def test_out_of_grid_coordinates_rejected(self) -> None:
        self.assertIsNone(_parse_llm_point('{"found": true, "x": 1500, "y": 5}', 1000, 1000))

    def test_non_json_region_fallback(self) -> None:
        point = _parse_llm_point("middle-right", 1000, 500)
        self.assertIsNotNone(point)
        self.assertGreater(point[0], 500)

    def test_empty_answer_is_none(self) -> None:
        self.assertIsNone(_parse_llm_point("", 1000, 1000))


async def _async_return(value: Any) -> Any:
    return value


def _async_stub(value: Any) -> Any:
    """An async FUNCTION returning value — production code calls then awaits it."""

    async def stub(*args: Any, **kwargs: Any) -> Any:
        return value

    return stub


def _async_none(*args: Any, **kwargs: Any) -> None:
    return None


class LocatePriorityTests(unittest.IsolatedAsyncioTestCase):
    """llm_vision wins over OCR; honest None when the model cannot find it."""

    def setUp(self) -> None:
        from PIL import Image

        self.tmp = tempfile.TemporaryDirectory()
        self.png = str(Path(self.tmp.name) / "screen.png")
        Image.new("RGB", (640, 480), (245, 245, 245)).save(self.png)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_llm_point_wins_over_ocr(self) -> None:
        provider = _ScriptedProvider(['{"found": true, "x": 250, "y": 750}'])
        # OCR would match "Library" at a DIFFERENT point — the LLM answer must win.
        ocr_words = [{"text": "Library", "line": 0, "x": 10, "y": 10, "w": 40, "h": 10}]
        with mock.patch(
            "novacontrol.desktop.vision_guide._ocr_words", new=_async_stub(ocr_words)
        ):
            result = await locate_element(self.png, "Library", llm_provider=provider)

        self.assertIsNotNone(result)
        x, y, how = result
        self.assertEqual(how, "llm_vision")
        self.assertEqual((x, y), (160, 360))  # (250/1000*640, 750/1000*480)

    async def test_model_missing_label_falls_back_to_ocr(self) -> None:
        provider = _ScriptedProvider(['{"found": false}'])
        ocr_words = [{"text": "Library", "line": 0, "x": 100, "y": 50, "w": 60, "h": 20}]
        with mock.patch(
            "novacontrol.desktop.vision_guide._ocr_words", new=_async_stub(ocr_words)
        ):
            result = await locate_element(self.png, "Library", llm_provider=provider)
        self.assertIsNotNone(result)
        self.assertEqual(result[2], "ocr")

    async def test_no_provider_uses_ocr(self) -> None:
        ocr_words = [{"text": "Library", "line": 0, "x": 100, "y": 50, "w": 60, "h": 20}]
        with mock.patch(
            "novacontrol.desktop.vision_guide._ocr_words", new=_async_stub(ocr_words)
        ):
            result = await locate_element(self.png, "Library", llm_provider=None)
        self.assertIsNotNone(result)
        self.assertEqual(result[2], "ocr")

    async def test_multimodal_message_shape(self) -> None:
        """The screenshot MUST travel inside message content (OpenAI format)."""
        provider = _ScriptedProvider(['{"found": true, "x": 500, "y": 500}'])
        await locate_element(self.png, "File", llm_provider=provider)
        messages = provider.calls[0]
        content = messages[-1]["content"]
        self.assertIsInstance(content, list)
        types = [part.get("type") for part in content]
        self.assertIn("text", types)
        self.assertIn("image_url", types)


class ConfigSurfaceTests(unittest.TestCase):
    """set/clear/status + hot-swap into every consumer + boot restore."""

    def _app(self) -> NovaControlApplication:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return NovaControlApplication(data_dir=tmp.name)

    def test_set_cloud_model_hot_swaps_into_all_consumers(self) -> None:
        app = self._app()
        status = app.set_vision_llm("openai", "sk-live-key-987654321")
        self.assertTrue(status["configured"])
        self.assertEqual(status["provider"], "openai")
        self.assertEqual(status["api_key_hint"], "…4321")
        # Hot-swapped into the runner, the VisionController, and perception.
        self.assertIsNotNone(app.desktop.runner.vision_provider)
        self.assertTrue(app.vision.has_vision_model)
        self.assertIsNotNone(app.agentic.perception._provider)
        # The stored key never leaks through the status surface.
        self.assertNotIn("sk-live-key-987654321", str(status))

    def test_set_ollama_model(self) -> None:
        app = self._app()
        with mock.patch(
            "novacontrol.integrations.llm.detect_ollama",
            return_value=_ollama_info(["llava:13b"]),
        ):
            status = app.set_vision_llm("ollama", "")
        self.assertTrue(status["configured"])
        self.assertEqual(status["provider"], "ollama")
        self.assertEqual(status["model"], "llava:13b")
        self.assertTrue(app.vision.has_vision_model)

    def test_clear_returns_to_ocr_only(self) -> None:
        app = self._app()
        app.set_vision_llm("openai", "sk-clear-me")
        status = app.clear_vision_llm()
        self.assertFalse(status["configured"])
        self.assertIsNone(app.desktop.runner.vision_provider)
        self.assertFalse(app.vision.has_vision_model)
        self.assertIsNone(app.agentic.perception._provider)

    def test_bad_config_raises_valueerror_with_reason(self) -> None:
        app = self._app()
        with self.assertRaises(ValueError) as caught:
            app.set_vision_llm("openai", "")
        self.assertIn("API key is required", str(caught.exception))

    def test_boot_restore_reapplies_the_persisted_model(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = NovaControlApplication(data_dir=tmp.name)
        app.set_vision_llm("openai", "sk-persisted-1")
        # A fresh application over the same data dir restores the provider.
        app2 = NovaControlApplication(data_dir=tmp.name)
        self.assertTrue(app2.vision_llm_status()["configured"])
        self.assertTrue(app2.vision.has_vision_model)


class VisionModelEndpointTests(_IsolatedApiTestCase):
    """POST /vision/model + /vision/model/clear over real HTTP."""

    def test_set_then_status_then_clear(self) -> None:
        response = self._client.post(
            "/vision/model",
            json={"provider": "openai", "api_key": "sk-http-1", "model": "gpt-4o-mini"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["configured"])

        status = self._client.get("/vision/status").json()
        self.assertTrue(status["vision_llm"]["configured"])
        self.assertTrue(status["vision_model"])
        self.assertNotIn("sk-http-1", str(status))

        cleared = self._client.post("/vision/model/clear")
        self.assertEqual(cleared.status_code, 200)
        self.assertFalse(cleared.json()["configured"])

    def test_missing_key_is_422(self) -> None:
        response = self._client.post("/vision/model", json={"provider": "openai", "api_key": ""})
        self.assertEqual(response.status_code, 422)
        self.assertIn("API key", response.json()["detail"])

    def test_unknown_provider_is_422(self) -> None:
        response = self._client.post("/vision/model", json={"provider": "nope", "api_key": "k"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("Unknown vision model provider", response.json()["detail"])


class HasVisionModelGateTests(unittest.TestCase):
    """The Echo fallback must never count as a vision model."""

    def test_echo_provider_is_not_a_vision_model(self) -> None:
        from novacontrol.core.buglog import BugLog
        from novacontrol.desktop.controller import DesktopAutomationController
        from novacontrol.desktop.vision import VisionController

        vision = VisionController(
            DesktopAutomationController(), bug_log=BugLog("unused-echo-bugs.json"), llm_provider=EchoLLMProvider()
        )
        self.assertFalse(vision.has_vision_model)

    def test_real_provider_is_a_vision_model(self) -> None:
        from novacontrol.core.buglog import BugLog
        from novacontrol.desktop.controller import DesktopAutomationController
        from novacontrol.desktop.vision import VisionController

        vision = VisionController(
            DesktopAutomationController(), bug_log=BugLog("unused-real-bugs.json"), llm_provider=_ScriptedProvider([])
        )
        self.assertTrue(vision.has_vision_model)


class DescribeSemanticTests(unittest.IsolatedAsyncioTestCase):
    """describe_screen routes through the model when one is configured."""

    async def test_describe_uses_the_model_not_the_window_probe(self) -> None:
        from novacontrol.core.buglog import BugLog
        from novacontrol.desktop.controller import DesktopAutomationController
        from novacontrol.desktop.models import DesktopActionResult, DesktopActionStatus
        from novacontrol.desktop.vision import VisionController
        from novacontrol.vision.models import ScreenUnderstanding
        from novacontrol.vision.multimodal import MultimodalVisionProcessor

        provider = _ScriptedProvider(["A settings window with a sidebar and a Save button."])
        vision = VisionController(
            DesktopAutomationController(), bug_log=BugLog("unused-describe-bugs.json"), llm_provider=provider
        )
        completed = DesktopActionResult(action_id="a1", status=DesktopActionStatus.COMPLETED, output={})
        with (
            mock.patch.object(
                DesktopAutomationController, "execute_action", new=_async_stub(completed)
            ),
            mock.patch.object(
                MultimodalVisionProcessor,
                "understand_screen",
                new=_async_stub(ScreenUnderstanding(summary="A settings window with a sidebar and a Save button.")),
            ),
        ):
            report = await vision.describe_screen()

        self.assertEqual(report["summary"]["source"], "vision_model")
        self.assertIn("settings window", report["message"])


if __name__ == "__main__":
    unittest.main()
