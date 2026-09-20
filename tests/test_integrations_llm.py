"""Tests for LLM provider resolution: Ollama auto-detection, URL shape, fallback."""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest import mock
from urllib.error import URLError

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
    validate_cloud_key,
)
from novacontrol.integrations import llm as llm_module
from novacontrol.integrations.llm import (
    OllamaMemoryPlan,
    cloud_llm_presets,
    ensure_exclusive_ollama_model,
    get_cloud_preset,
    is_ollama_vision_model,
    ollama_keep_alive,
    ollama_loaded_models,
    ollama_memory_plan,
    ollama_unload_on_switch,
    provider_supports_vision,
    unload_ollama_model,
)

OLLAMA_URL = "http://127.0.0.1:11434"
_GIB = 1024 ** 3


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


class ValidateCloudKeyTests(unittest.TestCase):
    """Paste-time key-format gate: impossible keys never reach the wire.

    The motivating case: a Google Cloud/Vertex service token (AQ.…) pasted
    into the Gemini (AI Studio) provider 404s mysteriously — the format gate
    now names the mixup at save/test time instead.
    """

    def test_vertex_token_in_gemini_is_rejected_with_mixup_message(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_cloud_key("gemini", "AQ.Ab8RLIrN2fTq_example_service_token_9SVA")
        message = str(ctx.exception)
        self.assertIn("AI Studio", message)
        self.assertIn("Vertex", message)
        self.assertIn("aistudio.google.com/apikey", message)

    def test_oauth_token_in_gemini_is_also_caught(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_cloud_key("gemini", "ya29.a0AfH6SMB_example_oauth_token")
        self.assertIn("AI Studio", str(ctx.exception))

    def test_wellformed_ai_studio_key_passes(self) -> None:
        validate_cloud_key("gemini", "AIza" + "A1_-" * 9)  # 39 chars, AIza charset

    def test_openai_sk_key_passes_and_garbage_is_rejected(self) -> None:
        validate_cloud_key("openai", "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA123456")
        with self.assertRaises(ValueError) as ctx:
            validate_cloud_key("openai", "gsk_totally_not_openai_1234567890")
        self.assertIn("sk-", str(ctx.exception))

    def test_claude_sk_ant_key_passes_and_bare_sk_is_rejected(self) -> None:
        validate_cloud_key("claude", "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAA")
        with self.assertRaises(ValueError) as ctx:
            validate_cloud_key("claude", "sk-plain-openai-style-1234567890")
        self.assertIn("sk-ant-", str(ctx.exception))

    def test_groq_and_deepseek_shapes(self) -> None:
        validate_cloud_key("groq", "gsk_" + "a" * 24)
        with self.assertRaises(ValueError):
            validate_cloud_key("groq", "sk-not-groq-12345678901234567890")
        validate_cloud_key("deepseek", "sk-" + "0123456789abcdef" * 2)
        with self.assertRaises(ValueError):
            validate_cloud_key("deepseek", "gsk_" + "a" * 24)  # Groq key, not DeepSeek

    def test_unvalidated_providers_pass_anything(self) -> None:
        # openrouter/mistral formats are not reliably distinctive: no gate.
        for provider in ("openrouter", "mistral"):
            validate_cloud_key(provider, "totally-unrecognized-shape")

    def test_empty_and_whitespace_keys_pass_through(self) -> None:
        # Empty is the caller's required-field check, not a format problem.
        for key in ("", "   "):
            validate_cloud_key("gemini", key)


class _FakeOllamaServer:
    """Server double for the lifecycle endpoints.

    Tracks which models are "resident" so a test can assert that a switch
    really evicted the other one, and records every call for inspection.
    ``sizes`` is the model roster with each footprint in bytes: /api/ps reports
    the resident subset, /api/tags every pulled model.
    """

    def __init__(self, loaded: list[str], *, sizes: dict[str, int] | None = None) -> None:
        self.loaded = list(loaded)
        self.sizes = dict(sizes or {})
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, request: object, timeout: float = 0) -> object:
        data = getattr(request, "data", None)
        payload: dict[str, object] = json.loads(data.decode("utf-8")) if data else {}
        path = str(getattr(request, "full_url", "")).replace(OLLAMA_URL, "")
        self.calls.append((path, payload))
        if path == "/api/ps":
            body = json.dumps({
                "models": [
                    {"name": name, "size": self.sizes.get(name, 0)} for name in self.loaded
                ]
            }).encode("utf-8")
        elif path == "/api/tags":
            body = json.dumps({
                "models": [{"name": name, "size": size} for name, size in self.sizes.items()]
            }).encode("utf-8")
        elif path == "/api/generate":
            # keep_alive 0 unloads; any truthy value (re)loads with that window.
            self.loaded = [name for name in self.loaded if name != payload.get("model")]
            if payload.get("keep_alive"):
                self.loaded.append(str(payload["model"]))
            body = b'{"done": true}'
        else:  # pragma: no cover - a wrong endpoint is a test failure
            raise AssertionError(f"unexpected lifecycle endpoint: {path}")
        return contextlib.closing(io.BytesIO(body))


class OllamaLifecycleTests(unittest.TestCase):
    """One local model resident at a time: evict before switching.

    A machine that cannot hold a chat brain and a vision brain at once should
    pay the reload deliberately, at a known instant, instead of letting memory
    pressure decide mid-answer.
    """

    def test_loaded_models_reads_the_ps_roster(self) -> None:
        server = _FakeOllamaServer(["qwen3:8b"])
        with mock.patch("novacontrol.integrations.llm.urlopen", server):
            self.assertEqual(ollama_loaded_models(), ["qwen3:8b"])

    def test_loaded_models_is_none_when_unreachable(self) -> None:
        # None (cannot ask) must be distinct from [] (nothing resident): only a
        # real answer may drive an eviction.
        with mock.patch("novacontrol.integrations.llm.urlopen", side_effect=URLError("refused")):
            self.assertIsNone(ollama_loaded_models())

    def test_unload_posts_keep_alive_zero_to_the_native_api(self) -> None:
        server = _FakeOllamaServer(["qwen3:8b"])
        with mock.patch("novacontrol.integrations.llm.urlopen", server):
            self.assertTrue(unload_ollama_model("qwen3:8b"))
        self.assertEqual(server.calls, [("/api/generate", {"model": "qwen3:8b", "keep_alive": 0})])
        self.assertEqual(server.loaded, [])

    def test_ensure_exclusive_evicts_only_the_other_model(self) -> None:
        server = _FakeOllamaServer(["qwen3:8b", "qwen3-vl:4b"])
        with mock.patch("novacontrol.integrations.llm.urlopen", server):
            evicted = ensure_exclusive_ollama_model("qwen3-vl:4b")
        self.assertEqual(evicted, ["qwen3:8b"])
        self.assertEqual(server.loaded, ["qwen3-vl:4b"])

    def test_ensure_exclusive_treats_latest_and_bare_name_as_one_model(self) -> None:
        server = _FakeOllamaServer(["qwen3:latest"])
        with mock.patch("novacontrol.integrations.llm.urlopen", server):
            self.assertEqual(ensure_exclusive_ollama_model("qwen3"), [])
        self.assertEqual(server.loaded, ["qwen3:latest"])

    def test_ensure_exclusive_is_a_noop_when_nothing_is_resident(self) -> None:
        server = _FakeOllamaServer([])
        with mock.patch("novacontrol.integrations.llm.urlopen", server):
            self.assertEqual(ensure_exclusive_ollama_model("qwen3:8b"), [])
        # Nothing resident means nothing to unload — no /api/generate at all.
        self.assertEqual([call for call in server.calls if call[0] == "/api/generate"], [])

    def test_ensure_exclusive_never_raises_when_the_server_is_down(self) -> None:
        with mock.patch("novacontrol.integrations.llm.urlopen", side_effect=URLError("down")):
            self.assertEqual(ensure_exclusive_ollama_model("qwen3:8b"), [])

    def test_configured_keep_alive_is_armed_on_a_switch(self) -> None:
        server = _FakeOllamaServer(["qwen3:8b"])
        with mock.patch("novacontrol.integrations.llm.urlopen", server):
            ensure_exclusive_ollama_model("qwen3-vl:4b", keep_alive="30m")
        self.assertIn(("/api/generate", {"model": "qwen3-vl:4b", "keep_alive": "30m"}), server.calls)
        self.assertEqual(server.loaded, ["qwen3-vl:4b"])

    def test_keep_alive_is_left_to_ollama_unless_configured(self) -> None:
        # The default must change nothing: no env var, no arming call.
        self.assertEqual(ollama_keep_alive(), "")
        server = _FakeOllamaServer(["qwen3:8b"])
        with mock.patch("novacontrol.integrations.llm.urlopen", server):
            ensure_exclusive_ollama_model("qwen3-vl:4b")
        # Exactly one /api/generate call, and it is the unload — an unset
        # keep_alive must never produce an arming call of its own.
        self.assertEqual(
            [payload for path, payload in server.calls if path == "/api/generate"],
            [{"model": "qwen3:8b", "keep_alive": 0}],
        )

    def test_unload_on_switch_can_be_disabled(self) -> None:
        with mock.patch.dict(os.environ, {"NOVACONTROL_OLLAMA_UNLOAD_ON_SWITCH": "0"}):
            self.assertFalse(ollama_unload_on_switch())
        with mock.patch.dict(os.environ, {"NOVACONTROL_OLLAMA_UNLOAD_ON_SWITCH": ""}):
            self.assertTrue(ollama_unload_on_switch())


class OllamaMemoryPlanTests(unittest.TestCase):
    """Measure memory BEFORE loading, and unload what must go first.

    A 16 GB machine cannot hold a 5.9 GB chat model and a 3.3 GB vision model
    alongside the desktop. Two separate reasons to evict are pinned here: never
    co-resident by policy, and not-enough-memory whatever the policy says.
    """

    def _plan(
        self,
        model: str,
        loaded: list[str],
        *,
        sizes: dict[str, int] | None = None,
        available: int | None = 20 * _GIB,
        exclusive: bool = True,
    ) -> OllamaMemoryPlan:
        server = _FakeOllamaServer(loaded, sizes=sizes)
        with (
            mock.patch("novacontrol.integrations.llm.urlopen", server),
            mock.patch(
                "novacontrol.integrations.llm.ollama_available_memory_bytes", return_value=available
            ),
        ):
            return ollama_memory_plan(model, OLLAMA_URL, exclusive=exclusive)

    def test_exclusive_evicts_even_with_memory_to_spare(self) -> None:
        # The guarantee is policy, not memory pressure: with a roomy machine
        # both models would FIT, and they still must not both be resident.
        plan = self._plan(
            "qwen3-vl:4b",
            ["qwen3:8b"],
            sizes={"qwen3:8b": 59 * _GIB // 10, "qwen3-vl:4b": 33 * _GIB // 10},
            available=20 * _GIB,
            exclusive=True,
        )
        self.assertEqual(plan.evict, ("qwen3:8b",))
        self.assertEqual(plan.resident_models, ("qwen3:8b",))
        self.assertTrue(plan.fits_after_evict)

    def test_memory_alone_forces_eviction_when_exclusivity_is_off(self) -> None:
        # With the policy switch off, a model that does not fit still unloads
        # the one in its way — the safety floor is not optional.
        plan = self._plan(
            "qwen3-vl:4b",
            ["qwen3:8b"],
            sizes={"qwen3:8b": 59 * _GIB // 10, "qwen3-vl:4b": 33 * _GIB // 10},
            available=_GIB // 2,  # 0.5 GB free: nowhere near enough
            exclusive=False,
        )
        self.assertEqual(plan.evict, ("qwen3:8b",))
        self.assertTrue(plan.fits_after_evict)  # 0.5 + 5.9 >= 3.3 + headroom

    def test_no_eviction_when_the_model_fits_with_exclusivity_off(self) -> None:
        plan = self._plan(
            "qwen3-vl:4b",
            ["qwen3:8b"],
            sizes={"qwen3:8b": 59 * _GIB // 10, "qwen3-vl:4b": 1 * _GIB},
            available=20 * _GIB,
            exclusive=False,
        )
        self.assertEqual(plan.evict, ())
        self.assertEqual(plan.resident_models, ("qwen3:8b",))  # still reported

    def test_an_impossible_load_is_reported_instead_of_attempted_silently(self) -> None:
        # A small resident model cannot free room for a big incoming one.
        plan = self._plan(
            "qwen3:8b",
            ["moondream:latest"],
            sizes={"moondream:latest": _GIB // 3, "qwen3:8b": 59 * _GIB // 10},
            available=_GIB,
        )
        self.assertEqual(plan.evict, ("moondream:latest",))
        self.assertFalse(plan.fits_after_evict)
        self.assertIn("NOT enough room", plan.describe())

    def test_nothing_resident_means_nothing_to_evict(self) -> None:
        plan = self._plan("qwen3:8b", [], sizes={"qwen3:8b": 59 * _GIB // 10})
        self.assertEqual(plan.evict, ())
        self.assertEqual(plan.resident_models, ())
        self.assertTrue(plan.fits_after_evict)

    def test_the_model_already_alone_is_not_evicted(self) -> None:
        plan = self._plan("qwen3-vl:4b", ["qwen3-vl:4b"], sizes={"qwen3-vl:4b": 33 * _GIB // 10})
        self.assertEqual(plan.evict, ())
        self.assertEqual(plan.resident_models, ())

    def test_an_unmeasurable_situation_never_claims_it_fits(self) -> None:
        # Unknown free memory, unknown model size, unreachable server: all three
        # must read as None, never as a confident "fits".
        plan = self._plan("qwen3:8b", ["qwen3:8b"], available=None)
        self.assertIsNone(plan.fits_after_evict)
        self.assertIsNone(plan.available_bytes)

    def test_missing_model_size_leaves_the_fit_unknown(self) -> None:
        plan = self._plan("qwen3:8b", [], sizes={})  # not in /api/tags either
        self.assertIsNone(plan.incoming_bytes)
        self.assertIsNone(plan.fits_after_evict)

    def test_unreachable_server_evicts_nothing(self) -> None:
        with mock.patch("novacontrol.integrations.llm.urlopen", side_effect=URLError("down")):
            plan = ollama_memory_plan("qwen3:8b", OLLAMA_URL)
        self.assertEqual(plan.evict, ())
        self.assertIsNone(plan.fits_after_evict)

    def test_describe_never_states_an_unmeasured_size_as_zero(self) -> None:
        plan = self._plan("qwen3:8b", [], sizes={})
        self.assertIn("unknown", plan.describe())


class OllamaMemoryGuardedLoadTests(unittest.TestCase):
    """The load-time guard, end to end through ensure_exclusive_ollama_model."""

    def test_memory_pressure_evicts_even_with_the_policy_switch_off(self) -> None:
        server = _FakeOllamaServer(
            ["qwen3:8b"],
            sizes={"qwen3:8b": 59 * _GIB // 10, "qwen3-vl:4b": 33 * _GIB // 10},
        )
        with (
            mock.patch("novacontrol.integrations.llm.urlopen", server),
            mock.patch("novacontrol.integrations.llm.ollama_available_memory_bytes", return_value=_GIB // 2),
            mock.patch.dict(os.environ, {"NOVACONTROL_OLLAMA_UNLOAD_ON_SWITCH": "0"}),
        ):
            evicted = ensure_exclusive_ollama_model("qwen3-vl:4b")
        self.assertEqual(evicted, ["qwen3:8b"])
        self.assertEqual(server.loaded, [])

    def test_the_other_model_never_stays_resident_by_default(self) -> None:
        server = _FakeOllamaServer(
            ["qwen3-vl:4b"],
            sizes={"qwen3-vl:4b": 33 * _GIB // 10, "qwen3:8b": 59 * _GIB // 10},
        )
        with (
            mock.patch("novacontrol.integrations.llm.urlopen", server),
            mock.patch("novacontrol.integrations.llm.ollama_available_memory_bytes", return_value=20 * _GIB),
        ):
            evicted = ensure_exclusive_ollama_model("qwen3:8b")
        self.assertEqual(evicted, ["qwen3-vl:4b"])
        self.assertEqual(server.loaded, [])
        self.assertEqual([call[0] for call in server.calls].count("/api/generate"), 1)


class OllamaExclusiveCompletionTests(unittest.IsolatedAsyncioTestCase):
    """A local completion evicts the other model BEFORE it runs."""

    async def test_completion_evicts_the_other_model_first(self) -> None:
        requested: list[str] = []
        server = _FakeOllamaServer(["qwen3-vl:4b"])

        def fake_default_transport(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
            requested.append(url)
            # The real server brings the requested model into memory on the
            # completion itself — model this so the residency roster is honest.
            server.loaded.append(str(payload["model"]))
            return {"choices": [{"message": {"content": "ok"}}]}

        with (
            mock.patch("novacontrol.integrations.llm._default_transport", fake_default_transport),
            mock.patch("novacontrol.integrations.llm.urlopen", server),
        ):
            provider = OpenAICompatibleLLMProvider(
                name="ollama",
                base_url=OLLAMA_URL,
                api_key="ollama",
                model="qwen3:8b",
                ollama_url=OLLAMA_URL,
            )
            self.assertTrue(provider._manages_ollama_lifecycle)
            answer = await provider.complete([{"role": "user", "content": "hi"}])

        self.assertEqual(answer, "ok")
        # The eviction lands before the completion, and only one model survives.
        self.assertEqual(server.calls[0], ("/api/ps", {}))
        self.assertIn(("/api/generate", {"model": "qwen3-vl:4b", "keep_alive": 0}), server.calls)
        self.assertEqual(server.loaded, ["qwen3:8b"])
        self.assertEqual(requested, [f"{OLLAMA_URL}/v1/chat/completions"])

    async def test_injected_transport_is_never_reached_around(self) -> None:
        """A caller that supplies its own transport owns the HTTP surface."""
        server = _FakeOllamaServer(["qwen3-vl:4b"])
        provider = OpenAICompatibleLLMProvider(
            name="ollama",
            base_url=OLLAMA_URL,
            api_key="ollama",
            model="qwen3:8b",
            transport=lambda url, headers, payload: {"choices": [{"message": {"content": "ok"}}]},
            ollama_url=OLLAMA_URL,
        )
        self.assertFalse(provider._manages_ollama_lifecycle)
        with mock.patch("novacontrol.integrations.llm.urlopen", server):
            await provider.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(server.calls, [])


class VisionCapabilityGateTests(unittest.TestCase):
    """A text-only model must never be reported as a vision model.

    The regression these pin: the vision layer counted any non-Echo provider as
    multimodal, so a machine whose chat brain was a text-only local model
    (qwen3, llama3.2) reported vision available, sent screenshots to a model
    that cannot see them, and — with no failure — clicked invented
    coordinates. Ollama rejected the same image outright with HTTP 400.
    """

    @staticmethod
    def _ollama_provider(model: str) -> OpenAICompatibleLLMProvider:
        return OpenAICompatibleLLMProvider(
            name="ollama", base_url=OLLAMA_URL, api_key="ollama", model=model
        )

    def test_text_only_ollama_model_is_not_a_vision_model(self) -> None:
        with mock.patch(
            "novacontrol.integrations.llm.ollama_model_capabilities",
            return_value=frozenset({"completion", "tools", "thinking"}),
        ):
            self.assertFalse(provider_supports_vision(self._ollama_provider("qwen3:8b")))

    def test_ollama_model_reporting_vision_is_a_vision_model(self) -> None:
        with mock.patch(
            "novacontrol.integrations.llm.ollama_model_capabilities",
            return_value=frozenset({"completion", "vision"}),
        ):
            self.assertTrue(provider_supports_vision(self._ollama_provider("qwen3-vl:4b")))

    def test_missing_capability_metadata_falls_back_to_the_name(self) -> None:
        # Older Ollama omits capabilities; the name heuristic is then the only
        # evidence available, and it must not become an unconditional "yes".
        with mock.patch("novacontrol.integrations.llm.ollama_model_capabilities", return_value=None):
            self.assertTrue(provider_supports_vision(self._ollama_provider("qwen3-vl:4b")))
            self.assertFalse(provider_supports_vision(self._ollama_provider("qwen3:8b")))

    def test_echo_fallback_and_no_provider_are_refused(self) -> None:
        self.assertFalse(provider_supports_vision(None))
        self.assertFalse(provider_supports_vision(EchoLLMProvider()))

    def test_purpose_built_and_cloud_vision_providers_are_trusted(self) -> None:
        self.assertTrue(
            provider_supports_vision(
                OpenAICompatibleLLMProvider(
                    name="vision:ollama", base_url=OLLAMA_URL, api_key="ollama", model="qwen3-vl:4b"
                )
            )
        )
        self.assertTrue(
            provider_supports_vision(
                OpenAICompatibleLLMProvider(
                    name="cloud:openai",
                    base_url="https://api.openai.com",
                    api_key="sk-x",
                    model="gpt-4o-mini",
                )
            )
        )

    def test_vl_model_names_are_recognized(self) -> None:
        for name in ("qwen3-vl:4b", "qwen3vl:2b", "qwen2.5vl:3b", "llava:13b", "moondream:latest"):
            self.assertTrue(is_ollama_vision_model(name), name)
        self.assertFalse(is_ollama_vision_model("qwen3:8b"))

    def test_chat_auto_pick_skips_the_vision_model(self) -> None:
        # The VL model can be listed FIRST on the instance; a chat brain must
        # still not be reassigned to it. A VL model only answers chat when it is
        # the only thing installed.
        self.assertEqual(llm_module._pick_ollama_model(["qwen3-vl:4b", "qwen3:8b"]), "qwen3:8b")
        self.assertEqual(llm_module._pick_ollama_model(["phi3:mini", "qwen3-vl:4b"]), "phi3:mini")
        self.assertEqual(llm_module._pick_ollama_model(["qwen3-vl:4b"]), "qwen3-vl:4b")


if __name__ == "__main__":
    unittest.main()
