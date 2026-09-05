"""Tests for LLM provider resolution: Ollama auto-detection, URL shape, fallback."""

from __future__ import annotations

import unittest
from unittest import mock

from novacontrol.integrations import (
    EchoLLMProvider,
    OpenAICompatibleLLMProvider,
    build_llm_provider_from_environment,
    build_ollama_provider,
)

OLLAMA_URL = "http://127.0.0.1:11434"


class OllamaDetectionTests(unittest.TestCase):

    def test_detected_ollama_builds_openai_compatible_provider(self) -> None:
        info = {"url": OLLAMA_URL, "models": ["llama3.2:3b", "other:big"]}
        with mock.patch("novacontrol.integrations.llm.detect_ollama", return_value=info):
            provider = build_ollama_provider()

        self.assertIsInstance(provider, OpenAICompatibleLLMProvider)
        self.assertEqual(provider.name, "ollama")
        self.assertEqual(provider.model, "llama3.2:3b")  # preferred small model wins

    def test_undetected_ollama_builds_nothing(self) -> None:
        with mock.patch("novacontrol.integrations.llm.detect_ollama", return_value=None):
            self.assertIsNone(build_ollama_provider())


class OllamaCompletionTests(unittest.IsolatedAsyncioTestCase):
    """The regression that made a detected Ollama unusable: the request URL had a
    duplicated /v1 segment (provider appends /v1/chat/completions to a base URL
    that already ended in /v1), so every completion hit a 404."""

    async def test_completion_hits_single_v1_endpoint(self) -> None:
        requested: dict[str, object] = {}

        def fake_transport(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
            requested["url"] = url
            return {"choices": [{"message": {"content": "hello from ollama"}}]}

        info = {"url": OLLAMA_URL, "models": ["llama3.2:3b"]}
        with mock.patch("novacontrol.integrations.llm.detect_ollama", return_value=info):
            provider = build_ollama_provider()
        assert provider is not None
        provider.transport = fake_transport

        answer = await provider.complete([{"role": "user", "content": "hi"}])

        self.assertEqual(requested["url"], f"{OLLAMA_URL}/v1/chat/completions")
        self.assertNotIn("/v1/v1/", str(requested["url"]))
        self.assertEqual(answer, "hello from ollama")

    async def test_empty_choices_return_empty_answer(self) -> None:
        provider = OpenAICompatibleLLMProvider(
            name="ollama", base_url=OLLAMA_URL, api_key="ollama", model="m",
            transport=lambda *_: {"choices": []},
        )
        self.assertEqual(await provider.complete([{"role": "user", "content": "hi"}]), "")


class ProviderResolutionTests(unittest.TestCase):

    def test_ollama_wins_when_detected_without_env_config(self) -> None:
        info = {"url": OLLAMA_URL, "models": ["llama3.2:3b"]}
        with mock.patch("novacontrol.integrations.llm.detect_ollama", return_value=info):
            provider = build_llm_provider_from_environment({})
        self.assertEqual(provider.name, "ollama")

    def test_falls_back_to_echo_templates_when_offline(self) -> None:
        with mock.patch("novacontrol.integrations.llm.detect_ollama", return_value=None):
            provider = build_llm_provider_from_environment({})
        self.assertIsInstance(provider, EchoLLMProvider)

    def test_disable_ollama_skips_detection_entirely(self) -> None:
        with mock.patch(
            "novacontrol.integrations.llm.detect_ollama",
            side_effect=AssertionError("detection must be skipped when disabled"),
        ):
            provider = build_llm_provider_from_environment({"NOVACONTROL_DISABLE_OLLAMA": "1"})
        self.assertIsInstance(provider, EchoLLMProvider)


if __name__ == "__main__":
    unittest.main()
