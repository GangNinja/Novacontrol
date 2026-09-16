"""Tests for LLM provider resolution: Ollama auto-detection, URL shape, fallback."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

import json

from novacontrol.integrations import (
    AnthropicMessagesProvider,
    EchoLLMProvider,
    OpenAICompatibleLLMProvider,
    build_cloud_provider,
    build_llm_provider_from_environment,
    build_ollama_provider,
    build_vision_provider,
    make_ollama_reprobe,
    ollama_models,
)
from novacontrol.integrations import llm as llm_module
from novacontrol.integrations.llm import cloud_llm_presets, get_cloud_preset

OLLAMA_URL = "http://127.0.0.1:11434"


class OllamaDetectionTests(unittest.TestCase):

    def setUp(self) -> None:
        # The module-level detection cache leaks between tests; every test here
        # starts from a cold cache so refresh semantics are observable.
        self._original_cache = llm_module._ollama_cache
        llm_module._ollama_cache = None

    def tearDown(self) -> None:
        llm_module._ollama_cache = self._original_cache

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

    def test_explicit_model_pins_the_provider(self) -> None:
        """The brain model picker's core contract: the picked model WINS over
        the auto-pick preference list."""
        info = {"url": OLLAMA_URL, "models": ["llama3.2:3b", "qwen3:30b"]}
        with mock.patch("novacontrol.integrations.llm.detect_ollama", return_value=info):
            provider = build_ollama_provider(model="qwen3:30b")
        assert provider is not None
        self.assertEqual(provider.model, "qwen3:30b")

    def test_detect_ollama_caches_until_refresh(self) -> None:
        """Boot + reprobe reuse the snapshot; the model picker passes refresh
        so models pulled after boot appear."""
        pulled = False

        def fake_urlopen(request: object, timeout: float) -> object:
            nonlocal pulled
            models = ["llama3.2:3b", "qwen3:30b"] if pulled else ["llama3.2:3b"]
            body = json.dumps({"models": [{"name": m} for m in models]}).encode("utf-8")
            return contextlib.closing(io.BytesIO(body))

        with mock.patch("novacontrol.integrations.llm.urlopen", side_effect=fake_urlopen) as probe:
            # First call probes the network and caches.
            self.assertEqual(ollama_models(refresh=False), ["llama3.2:3b"])
            self.assertEqual(probe.call_count, 1)
            # Cached: a second non-refresh call does not re-probe.
            self.assertEqual(ollama_models(refresh=False), ["llama3.2:3b"])
            self.assertEqual(probe.call_count, 1)
            # Refresh bypasses the cache and sees the pulled model.
            pulled = True
            self.assertEqual(ollama_models(), ["llama3.2:3b", "qwen3:30b"])
            self.assertEqual(probe.call_count, 2)

    def test_ollama_models_empty_when_unreachable(self) -> None:
        with mock.patch("novacontrol.integrations.llm.detect_ollama", return_value=None):
            self.assertEqual(ollama_models(), [])


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

    async def test_pinned_model_survives_lazy_ollama_startup(self) -> None:
        """make_ollama_reprobe(model=...) pins the UPGRADED provider too — the
        picker's choice must survive Ollama starting after the server booted."""
        info = {"url": OLLAMA_URL, "models": ["llama3.2:3b", "qwen3:30b"]}
        with mock.patch("novacontrol.integrations.llm.detect_ollama", return_value=info):
            reprobe = make_ollama_reprobe({}, model="qwen3:30b")
            upgraded = await reprobe()
        assert isinstance(upgraded, OpenAICompatibleLLMProvider)
        self.assertEqual(upgraded.model, "qwen3:30b")


class AnthropicMessagesProviderTests(unittest.IsolatedAsyncioTestCase):
    """Claude's native /v1/messages wire format — the whole reason this class
    exists. Every test captures the actual request a completion sends and pins
    it to Anthropic's spec: x-api-key auth, anthropic-version header, a
    top-level system field, REQUIRED max_tokens, and content[0].text parsing."""

    def setUp(self) -> None:
        self.captured: dict = {}

        def fake_transport(url: str, headers: dict[str, str], payload: dict) -> dict:
            self.captured = {"url": url, "headers": headers, "payload": payload}
            return {"content": [{"type": "text", "text": "claude says hi"}]}

        self.provider = AnthropicMessagesProvider(
            name="cloud:claude", api_key="sk-ant-test-123", model="claude-sonnet-4-5",
            transport=fake_transport,
        )

    async def test_request_hits_native_endpoint_with_spec_headers(self) -> None:
        answer = await self.provider.complete([{"role": "user", "content": "hi"}])

        self.assertEqual(answer, "claude says hi")
        self.assertEqual(self.captured["url"], "https://api.anthropic.com/v1/messages")
        headers = self.captured["headers"]
        self.assertEqual(headers["x-api-key"], "sk-ant-test-123")  # NOT Bearer
        self.assertNotIn("Authorization", headers)
        self.assertEqual(headers["anthropic-version"], "2023-06-01")

    async def test_system_prompt_hoists_to_top_level_and_max_tokens_required(self) -> None:
        await self.provider.complete([
            {"role": "system", "content": "You are NovaControl."},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "continue"},
        ])

        payload = self.captured["payload"]
        self.assertEqual(payload["system"], "You are NovaControl.")  # NOT a message
        self.assertEqual(
            [m["role"] for m in payload["messages"]],
            ["user", "assistant", "user"],  # system stripped from the message list
        )
        self.assertIn("max_tokens", payload)  # REQUIRED by /v1/messages
        self.assertEqual(payload["model"], "claude-sonnet-4-5")

    async def test_explicit_system_kwarg_wins_and_kwargs_pass_through(self) -> None:
        await self.provider.complete(
            [{"role": "system", "content": "message-level"}, {"role": "user", "content": "hi"}],
            system="kwarg-level",
            temperature=0.2,
        )

        payload = self.captured["payload"]
        self.assertEqual(payload["system"], "kwarg-level")
        self.assertEqual(payload["temperature"], 0.2)
        self.assertNotIn("system", [m.get("role") for m in payload["messages"]])

    async def test_multi_block_and_empty_responses_degrade_honestly(self) -> None:
        self.provider.transport = lambda *_: {"content": [  # type: ignore[misc]
            {"type": "text", "text": "part1 "},
            {"type": "tool_use", "id": "x"},  # non-text block: skipped
            {"type": "text", "text": "part2"},
        ]}
        self.assertEqual(await self.provider.complete([{"role": "user", "content": "hi"}]), "part1 part2")

        self.provider.transport = lambda *_: {"content": []}  # type: ignore[misc]
        self.assertEqual(await self.provider.complete([{"role": "user", "content": "hi"}]), "")


class ClaudePresetTests(unittest.TestCase):
    """The claude preset routes through the NATIVE class, and its key hint
    follows the ANTHROPIC_API_KEY env name."""

    def test_claude_preset_listed_with_env_hint(self) -> None:
        presets = {row["id"]: row for row in cloud_llm_presets()}
        self.assertIn("claude", presets)
        self.assertEqual(presets["claude"]["key_hint"], "ANTHROPIC_API_KEY")
        self.assertIn("claude-sonnet-4-5", presets["claude"]["models"])
        # No secrets or endpoints in the picker metadata.
        self.assertNotIn("base_url", json.dumps(presets))

    def test_build_cloud_provider_dispatches_claude_to_native_class(self) -> None:
        provider = build_cloud_provider("claude", "sk-ant-key")
        self.assertIsInstance(provider, AnthropicMessagesProvider)
        assert isinstance(provider, AnthropicMessagesProvider)
        self.assertEqual(provider.name, "cloud:claude")
        self.assertEqual(provider.model, "claude-sonnet-4-5")

        # An explicit model from the UI wins over the preset default.
        pinned = build_cloud_provider("claude", "sk-ant-key", model="claude-opus-4-1")
        assert isinstance(pinned, AnthropicMessagesProvider)
        self.assertEqual(pinned.model, "claude-opus-4-1")

    def test_other_presets_still_build_openai_compatible(self) -> None:
        provider = build_cloud_provider("openai", "sk-test")
        self.assertIsInstance(provider, OpenAICompatibleLLMProvider)
        self.assertNotIsInstance(provider, AnthropicMessagesProvider)

    def test_claude_is_not_a_vision_preset_yet(self) -> None:
        """Deliberate: /v1/messages images use base64 source blocks, not the
        image_url shape the vision layer sends."""
        provider, reason = build_vision_provider("claude", "sk-ant-key")
        self.assertIsNone(provider)
        self.assertIn("vision", reason.lower())


class TestConnectionPingTests(unittest.IsolatedAsyncioTestCase):
    """The Settings "Test Connection" flow: ping with the PASTED key before
    saving. Pins the ping for BOTH provider classes (OpenAI-compatible and
    Claude native) at the transport level — the request each one sends."""

    async def test_ping_openai_compatible_provider(self) -> None:
        captured: dict = {}

        def transport(url: str, headers: dict, payload: dict) -> dict:
            captured.update({"url": url, "auth": headers.get("Authorization"), "max_tokens": payload.get("max_tokens")})
            return {"choices": [{"message": {"content": "ok"}}]}

        provider = build_cloud_provider("openai", "sk-ping-test", model="gpt-4o-mini")
        assert isinstance(provider, OpenAICompatibleLLMProvider)
        provider.transport = transport
        answer = await provider.complete([{"role": "user", "content": "Reply with the single word: ok"}], max_tokens=8)

        self.assertEqual(answer, "ok")
        self.assertEqual(captured["auth"], "Bearer sk-ping-test")
        self.assertEqual(captured["max_tokens"], 8)

    async def test_ping_claude_native_provider(self) -> None:
        captured: dict = {}

        def transport(url: str, headers: dict, payload: dict) -> dict:
            captured.update({"url": url, "key": headers.get("x-api-key"), "max_tokens": payload.get("max_tokens")})
            return {"content": [{"type": "text", "text": "ok"}]}

        provider = build_cloud_provider("claude", "sk-ant-ping-test", model="claude-sonnet-4-5")
        assert isinstance(provider, AnthropicMessagesProvider)
        provider.transport = transport
        answer = await provider.complete([{"role": "user", "content": "Reply with the single word: ok"}], max_tokens=8)

        self.assertEqual(answer, "ok")
        self.assertEqual(captured["key"], "sk-ant-ping-test")
        self.assertEqual(captured["max_tokens"], 8)


class ProviderTelemetryTests(unittest.IsolatedAsyncioTestCase):
    """Lifetime cloud-LLM telemetry for the System panel: cumulative token
    usage from each API's usage block (OpenAI `usage.*_tokens`, Claude
    `usage.input/output_tokens`) and the last transport failure verbatim."""

    async def test_openai_provider_accumulates_usage_across_calls(self) -> None:
        provider = OpenAICompatibleLLMProvider(
            name="test", base_url="http://x", api_key="k", model="m",
            transport=lambda *_: {"choices": [{"message": {"content": "ok"}}],
                                  "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}},
        )
        await provider.complete([{"role": "user", "content": "a"}])
        await provider.complete([{"role": "user", "content": "b"}])

        self.assertEqual(provider.usage["requests"], 2)
        self.assertEqual(provider.usage["prompt_tokens"], 24)
        self.assertEqual(provider.usage["completion_tokens"], 14)
        self.assertEqual(provider.usage["total_tokens"], 38)
        self.assertEqual(provider.last_error, "")

    async def test_openai_provider_captures_transport_failure_as_last_error(self) -> None:
        def boom(*_args: object) -> dict[str, object]:
            raise RuntimeError("connection refused")

        provider = OpenAICompatibleLLMProvider(
            name="test", base_url="http://x", api_key="k", model="m", transport=boom,
        )
        with self.assertRaises(RuntimeError):
            await provider.complete([{"role": "user", "content": "a"}])

        self.assertEqual(provider.last_error, "RuntimeError: connection refused")
        self.assertEqual(provider.usage["requests"], 0)

    async def test_claude_provider_accumulates_anthropic_usage_fields(self) -> None:
        provider = AnthropicMessagesProvider(
            name="claude", api_key="k", model="m",
            transport=lambda *_: {"content": [{"type": "text", "text": "ok"}],
                                  "usage": {"input_tokens": 30, "output_tokens": 5}},
        )
        await provider.complete([{"role": "user", "content": "a"}])

        self.assertEqual(provider.usage["requests"], 1)
        self.assertEqual(provider.usage["prompt_tokens"], 30)
        self.assertEqual(provider.usage["completion_tokens"], 5)
        self.assertEqual(provider.usage["total_tokens"], 35)
        self.assertEqual(provider.last_error, "")

    async def test_claude_provider_captures_transport_failure_as_last_error(self) -> None:
        def boom(*_args: object) -> dict[str, object]:
            raise RuntimeError("HTTP Error 401: Unauthorized")

        provider = AnthropicMessagesProvider(name="claude", api_key="k", model="m", transport=boom)
        with self.assertRaises(RuntimeError):
            await provider.complete([{"role": "user", "content": "a"}])

        self.assertEqual(provider.last_error, "RuntimeError: HTTP Error 401: Unauthorized")
        self.assertEqual(provider.usage["requests"], 0)


if __name__ == "__main__":
    unittest.main()
