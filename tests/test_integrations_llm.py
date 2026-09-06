"""Tests for LLM provider resolution: Ollama auto-detection, URL shape, fallback."""

from __future__ import annotations

import unittest
from unittest import mock

from novacontrol.integrations import (
    EchoLLMProvider,
    OpenAICompatibleLLMProvider,
    build_llm_provider_from_environment,
    build_ollama_provider,
    make_ollama_reprobe,
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


class OllamaReprobeTests(unittest.IsolatedAsyncioTestCase):
    """The lazy chat-time probe: starting Ollama after the server upgrades chat
    and Explore without a restart. Misses are rate-limited; a hit caches."""

    async def test_disabled_env_returns_probe_that_always_misses(self) -> None:
        for env in (
            {"NOVACONTROL_DISABLE_OLLAMA": "1"},
            {"NOVACONTROL_ENABLE_EXTERNAL_LLM": "1", "NOVACONTROL_LLM_MODEL": "m"},
        ):
            with (
                self.subTest(env=env),
                mock.patch(
                    "novacontrol.integrations.llm.detect_ollama",
                    side_effect=AssertionError("no network allowed"),
                ),
            ):
                reprobe = make_ollama_reprobe(env)
                self.assertIsNone(await reprobe())

    @staticmethod
    def _reprobe_state(reprobe: object) -> dict:
        """Reach into the closure's state dict ({provider, last_miss}) for tests."""
        cell = next(c for c in reprobe.__closure__ if isinstance(c.cell_contents, dict))  # type: ignore[union-attr]
        return cell.cell_contents  # type: ignore[no-any-return]

    async def test_miss_is_rate_limited_then_hit_upgrades_and_caches(self) -> None:
        detect_calls = {"n": 0}

        def flaky_detect(url: str) -> dict | None:
            detect_calls["n"] += 1
            return {"url": url, "models": ["llama3.2:3b"]} if detect_calls["n"] >= 2 else None

        reprobe = make_ollama_reprobe({})
        state = self._reprobe_state(reprobe)
        with mock.patch("novacontrol.integrations.llm.detect_ollama", side_effect=flaky_detect):
            self.assertIsNone(await reprobe())  # miss #1 (arms the rate limit)
            self.assertIsNone(await reprobe())  # inside window: skipped, no network
            self.assertEqual(detect_calls["n"], 1)

            state["last_miss"] = 0.0  # open the window
            upgraded = await reprobe()  # detect call #2: Ollama "appeared" -> hit

        self.assertIsInstance(upgraded, OpenAICompatibleLLMProvider)
        assert isinstance(upgraded, OpenAICompatibleLLMProvider)
        self.assertEqual(upgraded.name, "ollama")
        self.assertEqual(upgraded.model, "llama3.2:3b")

        # After a hit: same object forever, zero network.
        with mock.patch(
            "novacontrol.integrations.llm.detect_ollama",
            side_effect=AssertionError("no network after a hit"),
        ):
            again = await reprobe()
        self.assertIs(again, upgraded)



if __name__ == "__main__":
    unittest.main()
