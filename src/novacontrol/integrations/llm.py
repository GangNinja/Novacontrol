"""LLM provider registry and baseline provider."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import logging
import os
import re
import time
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class EchoLLMProvider:
    """Safe baseline LLM provider for local tests and demos."""

    @property
    def name(self) -> str:
        return "echo"

    async def complete(self, messages: Sequence[Mapping[str, str]], **kwargs: object) -> str:
        if not messages:
            return ""
        return str(messages[-1].get("content", ""))


class LLMProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, object] = {}

    def register(self, provider: object) -> None:
        name = str(getattr(provider, "name"))
        if name in self._providers:
            raise ValueError(f"LLM provider already registered: {name}")
        self._providers[name] = provider

    def get(self, name: str) -> object:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise KeyError(f"LLM provider not registered: {name}") from exc

    def list(self) -> tuple[object, ...]:
        return tuple(self._providers[name] for name in sorted(self._providers))


LLMTransport = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]


class OpenAICompatibleLLMProvider:
    """Generic provider for OpenAI-compatible `/v1/chat/completions` APIs.

    ``chat_path`` lets one class serve every OpenAI-style surface: local
    Ollama and most clouds use ``/v1/chat/completions``, while Gemini's
    OpenAI-compatible endpoint lives under ``/v1beta/openai``.
    """

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        chat_path: str = "/v1/chat/completions",
        transport: LLMTransport | None = None,
        ollama_url: str = "",
    ) -> None:
        self._name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.chat_path = chat_path
        self.transport = transport or _default_transport
        # Set for a provider that talks to the local Ollama, so completions can
        # evict the other local model first and hold peak RAM to one model —
        # see _manages_ollama_lifecycle and the lifecycle section below.
        self.ollama_url = ollama_url.rstrip("/")
        # Lifetime telemetry for the System panel: cumulative token usage from
        # every usage block the API returned, and the last failure verbatim.
        self.usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.last_error: str = ""
        # Why the last reply stopped ("stop", "length", "timeout", …). Kept
        # because an EMPTY answer with finish_reason "length" is a specific
        # failure — the budget went to reasoning — and callers must be able to
        # tell it apart from a mere formatting problem.
        self.last_finish_reason: str = ""
        # Timing breakdown of the last reply, straight from the wire. A local
        # Ollama reports nanoseconds for load / prefill / decode, so these are
        # measurements rather than estimates: ``model_load_ms`` is what bringing
        # the weights in costs, ``first_token_ms`` is what has to happen before
        # the first token can appear, and ``tokens_per_second`` comes from the
        # decode phase alone. Empty when the server reported none — a backend
        # that says nothing about its timing reports nothing, never a fake zero.
        self.last_timings: dict[str, float] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def answer_was_truncated(self) -> bool:
        """True when the last reply ran out of token budget.

        A reasoning-capable local model can spend an entire budget on its
        thinking and emit no answer at all; asking it again would burn the same
        budget for the same result, so callers use this to skip a pointless
        retry and fall back to a strategy that works.
        """
        return self.last_finish_reason == "length"

    def _ollama_native_payload(
        self, payload: Mapping[str, Any], *, structured: bool
    ) -> dict[str, Any] | None:
        """Native Ollama chat payload with thinking off, or None for /v1.

        Only for STRUCTURED calls (understanding, JSON extraction) to the local
        Ollama over the default transport, and only while thinking is disabled.
        Chat is deliberately left on the compatible surface: it wants prose, and
        a model's reasoning costs latency it does not need to pay.

        ``num_predict`` carries the caller's budget across, so the truncation
        contract (``answer_was_truncated``) holds on this path too.
        """
        if not structured:
            return None
        if not self.ollama_url or self.transport is not _default_transport:
            return None
        if not ollama_disable_thinking():
            return None
        budget = payload.get("max_tokens")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(message) for message in payload.get("messages", ())],
            "stream": False,
            "think": False,
        }
        if isinstance(budget, int) and budget > 0:
            body["options"] = {"num_predict": budget}
        return body

    @property
    def _manages_ollama_lifecycle(self) -> bool:
        """True when this provider owns the real local-Ollama HTTP surface.

        Only a provider pointed at Ollama AND still using the default transport
        manages model residency. A caller that injected its own transport is
        standing in for the whole HTTP surface (tests, a gateway, a proxy), and
        reaching around it to hit /api/ps on this machine would be wrong.
        """
        return bool(self.ollama_url) and self.transport is _default_transport

    async def complete(self, messages: Sequence[Mapping[str, str]], **kwargs: object) -> str:
        if self._manages_ollama_lifecycle and ollama_unload_on_switch():
            # Local residency is exclusive by design: a machine that cannot hold
            # the chat brain and the vision brain at once should pay the reload
            # at a known instant instead of leaving the choice to memory
            # pressure mid-answer. Best-effort and off-loop; a failure here must
            # never cost the caller an answer it could otherwise get.
            try:
                await asyncio.to_thread(
                    ensure_exclusive_ollama_model,
                    self.model,
                    self.ollama_url,
                    keep_alive=ollama_keep_alive(),
                )
            except Exception as exc:  # pragma: no cover - defensive only
                logger.debug("Ollama lifecycle check skipped: %s", exc)
        # ``json_mode`` is a NovaControl-facing hint, never a wire field: it
        # says "this call wants a structured answer", which is what decides
        # whether the native thinking-free endpoint is the better one.
        structured = bool(kwargs.pop("json_mode", False))
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(message) for message in messages],
        }
        # Bound a LOCAL model's reply unless the caller set its own budget. A
        # local model generates without bound, and a reasoning-capable one can
        # pour every token into its thinking and never answer — an uncapped
        # request then blocks until the socket timeout (measured: a 4B VL model
        # never returned at all, and exhausts 600 tokens of pure reasoning).
        # Cloud providers keep their own defaults: their servers already cap
        # replies, and a small ceiling here would silently shorten answers.
        if self.ollama_url and not any(
            key in kwargs for key in ("max_tokens", "max_completion_tokens")
        ):
            budget = ollama_max_tokens()
            if budget > 0:
                payload["max_tokens"] = budget
        payload.update(kwargs)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # LOCAL REASONING MODELS need the NATIVE endpoint, not just a flag. A
        # reasoning-capable model spends its reply budget on its thinking, so a
        # capped request can come back with NO answer at all — measured on
        # qwen3:8b (CPU-only) with this project's real understanding prompt:
        #
        #   /v1/chat/completions, max_tokens 700   115.4s, 0 chars of answer
        #   /api/chat, think=false                 13.9s, valid JSON
        #
        # The OpenAI-compatible surface ignores ``think`` (it separates the
        # reasoning instead), so the flag alone does not fix anything; matching
        # the endpoint to the capability does. Only the real local transport is
        # redirected — an injected transport stands in for the whole HTTP
        # surface and must keep seeing the shape it was written for.
        native = self._ollama_native_payload(payload, structured=structured)
        url = (
            f"{self.ollama_url.rstrip('/')}/api/chat"
            if native is not None
            else f"{self.base_url}{self.chat_path}"
        )
        outgoing = native if native is not None else payload
        # The sync transport blocks on the socket; run it off the event loop so a
        # slow LLM response never freezes the rest of the local app.
        try:
            response = await asyncio.to_thread(self.transport, url, headers, outgoing)
        except Exception as exc:
            if native is None:
                self.last_error = f"{type(exc).__name__}: {exc}"
                raise
            # An Ollama too old to know ``think`` must not cost the caller its
            # answer: retry the documented OpenAI-compatible surface once.
            logger.debug("Native Ollama chat failed (%s); using the compatible surface", exc)
            try:
                response = await asyncio.to_thread(
                    self.transport, f"{self.base_url}{self.chat_path}", headers, payload
                )
            except Exception as retry_exc:
                self.last_error = f"{type(retry_exc).__name__}: {retry_exc}"
                raise retry_exc from exc
            native = None
        # Captured before the reply is interpreted: the timings describe THIS
        # call, and a backend that reported none must not leave the previous
        # call's numbers standing.
        self.last_timings = _ollama_timings(response)
        self.usage["requests"] += 1
        usage = response.get("usage") or {}
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(field)
            if isinstance(value, int):
                self.usage[field] += value
        choices = response.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            self.last_finish_reason = str(choices[0].get("finish_reason") or "")
        elif "message" in response:
            # Native Ollama: {"message": {...}, "done_reason": "stop"|"length"}.
            # ``length`` means the budget ran out, which is what
            # answer_was_truncated reports — the same contract as the
            # OpenAI-shaped reply, so callers cannot tell the two apart.
            message = response.get("message") or {}
            self.last_finish_reason = str(response.get("done_reason") or "")
            for field, key in (("prompt_tokens", "prompt_eval_count"), ("completion_tokens", "eval_count")):
                value = response.get(key)
                if isinstance(value, int):
                    self.usage[field] += value
            if isinstance(response.get("prompt_eval_count"), int) and isinstance(
                response.get("eval_count"), int
            ):
                self.usage["total_tokens"] += int(response["prompt_eval_count"]) + int(response["eval_count"])
        else:
            return ""
        content = str(message.get("content", ""))
        if not content and self.answer_was_truncated:
            # Report the reasoning-only exhaustion instead of returning an empty
            # string that looks like a caller-side parse bug.
            self.last_error = (
                "reply token budget exhausted before any answer "
                "(reasoning-only model, or raise NOVACONTROL_OLLAMA_MAX_TOKENS)"
            )
        return content


class AnthropicMessagesProvider:
    """Native Anthropic provider for ``/v1/messages`` — NOT OpenAI-compatible.

    Claude's wire format differs from /chat/completions in every dimension
    that matters:

    - auth: ``x-api-key`` header (not ``Authorization: Bearer``);
    - a required ``anthropic-version`` header (``2023-06-01``);
    - the system prompt is a TOP-LEVEL ``system`` field, not a message;
    - response shape: ``content[0].text`` (not ``choices[0].message.content``);
    - max token budget is a REQUIRED ``max_tokens`` argument.

    A prior comment here dismissed Claude as "intentionally absent" because of
    this — but the differences are all shallow, so this class absorbs them and
    exposes the SAME ``complete(messages, **kwargs)`` surface every other
    provider has, so the brain, Explore synthesis, and the code planner can
    use Claude with zero call-site changes. ``system`` and ``temperature``
    kwargs pass through like the OpenAI path; a caller-supplied ``system``
    kwarg wins over a message-role system prompt.
    """

    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        model: str,
        base_url: str = "https://api.anthropic.com",
        messages_path: str = "/v1/messages",
        transport: LLMTransport | None = None,
    ) -> None:
        self._name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.messages_path = messages_path
        self.transport = transport or _default_transport
        # Lifetime telemetry for the System panel — same shape as the
        # OpenAI-compatible class (Claude reports input/output_tokens).
        self.usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.last_error: str = ""

    @property
    def name(self) -> str:
        return self._name

    async def complete(self, messages: Sequence[Mapping[str, str]], **kwargs: object) -> str:
        # Split the OpenAI-style message list into Claude's shape: system text
        # hoists to the top-level `system` field; everything else maps 1:1.
        system_parts: list[str] = []
        claude_messages: list[dict[str, str]] = []
        for message in messages:
            if str(message.get("role", "")) == "system":
                system_parts.append(str(message.get("content", "")))
            else:
                claude_messages.append({"role": str(message.get("role", "user")), "content": str(message.get("content", ""))})
        kw: dict[str, Any] = dict(kwargs)  # temperature, max_tokens, …
        explicit_system = kw.pop("system", None)
        if explicit_system:
            # A caller-supplied system WINS (documented contract): it replaces
            # any message-role system prompts rather than concatenating.
            system_parts = [str(explicit_system)]

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": claude_messages,
            "max_tokens": kw.pop("max_tokens", 1024),  # REQUIRED by /v1/messages
        }
        if system_parts:
            payload["system"] = "\n".join(system_parts)
        payload.update(kw)

        try:
            response = await asyncio.to_thread(
                self.transport,
                f"{self.base_url}{self.messages_path}",
                {
                    "x-api-key": self.api_key,
                    "anthropic-version": self.ANTHROPIC_VERSION,
                    "Content-Type": "application/json",
                },
                payload,
            )
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise
        self.usage["requests"] += 1
        usage = response.get("usage") or {}
        prompt_tokens = usage.get("input_tokens")
        completion_tokens = usage.get("output_tokens")
        if isinstance(prompt_tokens, int):
            self.usage["prompt_tokens"] += prompt_tokens
        if isinstance(completion_tokens, int):
            self.usage["completion_tokens"] += completion_tokens
        if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
            self.usage["total_tokens"] += prompt_tokens + completion_tokens
        content = response.get("content", [])
        if not content:
            return ""
        # Concatenate text blocks (tool-use blocks are ignored by this text-only surface).
        return "".join(str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("type") == "text")


# Socket timeout for provider requests, in seconds. This is a LOCAL-FIRST
# surface: a llama-server backed daemon on CPU answers a one-line prompt in
# 20-60s and takes considerably longer once a screenshot rides in the prompt,
# so the old hardcoded 30s rejected requests the machine could genuinely
# serve — every local completion timed out, and the brain quietly swapped in
# its heuristic fallback while still reporting the Ollama provider. A dead
# socket still raises immediately, so a generous ceiling costs nothing.
# Override with NOVACONTROL_LLM_TIMEOUT (seconds).
_DEFAULT_REQUEST_TIMEOUT = 600.0


def _request_timeout() -> float:
    """Provider socket timeout in seconds (NOVACONTROL_LLM_TIMEOUT else 600).

    An unparseable or non-positive value falls back to the default rather
    than raising: a typo in an env var must not break every LLM call.
    """
    raw = os.environ.get("NOVACONTROL_LLM_TIMEOUT", "").strip()
    if not raw:
        return _DEFAULT_REQUEST_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_REQUEST_TIMEOUT
    return value if value > 0 else _DEFAULT_REQUEST_TIMEOUT


_NANOSECONDS_PER_MS = 1_000_000.0


def _ollama_timings(response: Mapping[str, Any]) -> dict[str, float]:
    """Ollama's own reply timings, in milliseconds, with derived rates.

    Ollama reports nanoseconds for bringing the weights in, reading the prompt
    and decoding the answer. Converting them here is what makes the fallback
    path measurable instead of guessed: load is the cost of residency, prefill
    tells us what has to happen before the first token can appear, and the
    decode phase alone gives tokens per second.

    Only fields the server actually reported are returned. A backend silent
    about its timing reports nothing rather than a fabricated zero, so a
    consumer can always tell "instant" from "unmeasured".
    """

    def _ms(key: str) -> float | None:
        value = response.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return round(float(value) / _NANOSECONDS_PER_MS, 3)
        return None

    timings: dict[str, float] = {}
    load = _ms("load_duration")
    prefill = _ms("prompt_eval_duration")
    decode = _ms("eval_duration")
    total = _ms("total_duration")
    if load is not None:
        timings["model_load_ms"] = load
    if prefill is not None:
        timings["prompt_eval_ms"] = prefill
    if decode is not None:
        timings["decode_ms"] = decode
    if total is not None:
        timings["total_inference_ms"] = total
    if prefill is not None:
        # The weights must be resident and the prompt read before a single token
        # can appear, so load + prefill is the honest time-to-first-token.
        timings["first_token_ms"] = round((load or 0.0) + prefill, 3)
    tokens = response.get("eval_count")
    if isinstance(tokens, int) and tokens > 0 and decode:
        timings["tokens_per_second"] = round(tokens / (decode / 1000.0), 2)
    return timings


def _default_transport(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=_request_timeout()) as response:
        return dict(json.loads(response.read().decode("utf-8")))


# ── Ollama auto-detection ────────────────────────────────

_OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"
_ollama_cache: dict[str, Any] | None = None


def detect_ollama(
    base_url: str = _OLLAMA_DEFAULT_URL,
    *,
    refresh: bool = False,
    timeout: float = 2.0,
) -> dict[str, Any] | None:
    """Probe a running Ollama instance and return available models.

    Returns a dict with ``url`` and ``models`` (list of model names) on
    success, or ``None`` if Ollama is not reachable. The result is cached for
    the process lifetime (boot + the lazy re-probe never need a second hit);
    ``refresh=True`` bypasses the cache — the model picker must show models
    pulled AFTER boot, not the boot-time snapshot.

    ``timeout`` caps the blocking socket wait. The model-picker route passes
    a short value and runs the probe off the event loop, so a dead Ollama
    costs one quick miss instead of freezing every SSE stream and API route.
    """
    global _ollama_cache  # noqa: PLW0603
    if _ollama_cache is not None and not refresh:
        return _ollama_cache
    try:
        request = Request(f"{base_url.rstrip('/')}/api/tags", method="GET")
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        models = [m["name"] for m in data.get("models", []) if "name" in m]
        if not models:
            return None
        result: dict[str, Any] = {"url": base_url, "models": models}
        _ollama_cache = result
        logger.info("Ollama detected at %s with %d models: %s", base_url, len(models), models)
        return result
    except (URLError, OSError, json.JSONDecodeError, KeyError, TypeError):
        return None


def ollama_models(
    base_url: str = _OLLAMA_DEFAULT_URL,
    *,
    refresh: bool = True,
    timeout: float = 2.0,
) -> list[str]:
    """Model names available on the local Ollama ([] when unreachable).

    Refresh is the default here: callers are pickers/listers, not the boot
    resolution path — they want today's list, not the boot-time snapshot.
    """
    info = detect_ollama(base_url, refresh=refresh, timeout=timeout)
    return list(info["models"]) if info else []


_ollama_capabilities_cache: dict[str, frozenset[str]] = {}


def ollama_model_capabilities(
    model: str,
    base_url: str = _OLLAMA_DEFAULT_URL,
    *,
    timeout: float = 5.0,
    refresh: bool = False,
) -> frozenset[str] | None:
    """Capabilities Ollama reports for a model, or None when it cannot be asked.

    ``/api/show`` is the authoritative answer where a name prefix is only a
    guess: Ollama lists ``vision`` for a build that accepts image content and
    omits it for a text-only one — including text-only builds whose names look
    multimodal. Returns None (no evidence either way) when Ollama is
    unreachable, the model is not pulled, or the installed version predates
    capability metadata; an empty set would wrongly assert the model has no
    capabilities at all. Callers fall back to the name heuristic on None.

    Successful answers are cached per (url, model) because the vision gate is
    consulted on every status read; a miss is never cached, so pulling the
    model later is picked up without a restart.
    """
    key = f"{base_url.rstrip('/')}/{model}"
    if not refresh and key in _ollama_capabilities_cache:
        return _ollama_capabilities_cache[key]
    try:
        request = Request(
            f"{base_url.rstrip('/')}/api/show",
            data=json.dumps({"model": model}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (URLError, OSError, json.JSONDecodeError, ValueError, TypeError):
        return None
    raw = data.get("capabilities")
    if not isinstance(raw, list):
        return None  # older Ollama: no capability metadata to trust
    capabilities = frozenset(str(item).lower() for item in raw if isinstance(item, str))
    _ollama_capabilities_cache[key] = capabilities
    return capabilities


def _pick_ollama_model(models: list[str]) -> str:
    """Pick the best model from an Ollama model list.

    Prefers smaller, faster models for interactive use. Vision models are
    skipped while any text model exists: a machine that pulled a VL model for
    the vision layer must not have its CHAT brain silently reassigned to it,
    which is exactly what an auto-pick over an unordered model list would do
    once both are installed. A vision model is still chosen when it is the
    only thing available (it answers text questions perfectly well).
    """
    preferred_prefixes = ("phi3", "phi-3", "qwen2:0.5", "qwen2:1.5", "tinyllama",
                         "gemma:2b", "gemma2:2b", "llama3.2:1b", "llama3.2:3b",
                         "mistral:7b", "llama3.1:8b", "llama3:8b")
    lower_models = [m.lower() for m in models]
    for prefix in preferred_prefixes:
        for i, model in enumerate(lower_models):
            if model.startswith(prefix) or prefix in model:
                return models[i]
    text_models = [m for m in models if not is_ollama_vision_model(m)]
    # Fall back to first available (text-preferred) model.
    return (text_models or models)[0]


def build_ollama_provider(
    base_url: str = _OLLAMA_DEFAULT_URL,
    *,
    model: str = "",
) -> OpenAICompatibleLLMProvider | None:
    """Build an Ollama provider if a running instance is detected.

    An explicit ``model`` pins the provider to that Ollama model (the brain
    model picker's choice); empty means auto-pick the best detected model.
    A pinned model that is not on the instance still builds (Ollama 404s at
    request time) — callers validate membership against ollama_models first.
    """
    info = detect_ollama(base_url)
    if info is None:
        return None
    model = model.strip() or _pick_ollama_model(info["models"])
    return OpenAICompatibleLLMProvider(
        # OpenAICompatibleLLMProvider appends /v1/chat/completions itself, so the
        # base URL must be the Ollama root — a ".../v1" suffix here doubled to
        # /v1/v1/chat/completions and every request 404'd.
        name="ollama",
        base_url=info["url"],
        api_key="ollama",
        model=model,
        ollama_url=info["url"],
    )


# ── Cloud LLM providers (ChatGPT, Gemini, Groq, …) ────────

# One row per cloud endpoint. Most speak the OpenAI /chat/completions shape,
# differing only in base URL, chat path, default model, and where the key
# comes from. Two exceptions:
# - Gemini exposes an OpenAI-compatible surface under /v1beta/openai (its
#   native generateContent API is a different wire format entirely).
# - Anthropic Claude speaks its own /v1/messages wire format (x-api-key,
#   anthropic-version header, top-level system, content[0].text response) and
#   is served by the dedicated AnthropicMessagesProvider class.
CLOUD_LLM_PRESETS: tuple[dict[str, str], ...] = (
    {"id": "openai", "label": "ChatGPT (OpenAI)", "base_url": "https://api.openai.com",
     "chat_path": "/v1/chat/completions", "default_model": "gpt-4o-mini",
     "models": "gpt-4o-mini, gpt-4o, gpt-4.1-mini, o4-mini", "key_hint": "OPENAI_API_KEY"},
    {"id": "gemini", "label": "Gemini (Google)", "base_url": "https://generativelanguage.googleapis.com",
     "chat_path": "/v1beta/openai/chat/completions", "default_model": "gemini-2.0-flash",
     "models": "gemini-2.0-flash, gemini-2.5-flash, gemini-2.5-pro", "key_hint": "GEMINI_API_KEY"},
    {"id": "groq", "label": "Groq", "base_url": "https://api.groq.com/openai",
     "chat_path": "/v1/chat/completions", "default_model": "llama-3.3-70b-versatile",
     "models": "llama-3.3-70b-versatile, llama-3.1-8b-instant, mixtral-8x7b-32768", "key_hint": "GROQ_API_KEY"},
    {"id": "openrouter", "label": "OpenRouter", "base_url": "https://openrouter.ai/api",
     "chat_path": "/v1/chat/completions", "default_model": "openai/gpt-4o-mini",
     "models": "openai/gpt-4o-mini, anthropic/claude-3.5-sonnet, meta-llama/llama-3.3-70b-instruct", "key_hint": "OPENROUTER_API_KEY"},
    {"id": "mistral", "label": "Mistral", "base_url": "https://api.mistral.ai",
     "chat_path": "/v1/chat/completions", "default_model": "mistral-small-latest",
     "models": "mistral-small-latest, mistral-large-latest", "key_hint": "MISTRAL_API_KEY"},
    {"id": "claude", "label": "Claude (Anthropic)", "base_url": "https://api.anthropic.com",
     "chat_path": "/v1/messages", "default_model": "claude-sonnet-4-5",
     "models": "claude-sonnet-4-5, claude-opus-4-1, claude-3-5-haiku-latest", "key_hint": "ANTHROPIC_API_KEY"},
    {"id": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com",
     "chat_path": "/v1/chat/completions", "default_model": "deepseek-chat",
     "models": "deepseek-chat, deepseek-reasoner", "key_hint": "DEEPSEEK_API_KEY"},
)


def cloud_llm_presets() -> list[dict[str, str]]:
    """Metadata for the Settings panel's provider picker (no secrets)."""
    return [
        {k: str(row[k]) for k in ("id", "label", "default_model", "models", "key_hint")}
        for row in CLOUD_LLM_PRESETS
    ]


def get_cloud_preset(provider_id: str) -> dict[str, str] | None:
    for row in CLOUD_LLM_PRESETS:
        if row["id"] == provider_id:
            return dict(row)
    return None


# ── Save-time key-format validation ─────────────────────────
#
# Each provider's paste-time shape check. A key that cannot possibly be
# valid for the chosen provider is rejected BEFORE any network call or
# persistence, with the format the provider actually expects. Deliberately
# shape-only (never a reachability check): a genuinely wrong-but-well-formed
# key must still reach the live Test Connection so its real 401 explains
# itself. Patterns are conservative — anything they don't recognize passes.
_CLOUD_KEY_PATTERNS: dict[str, tuple[re.Pattern[str], str, str]] = {
    # AI Studio keys start with "AIza"; Google Cloud/Vertex service keys and
    # OAuth tokens ("AQ.Ab8R…" / "ya29.") do NOT — they 404 against the AI
    # Studio endpoint, which reads as a mystery failure. Catch that at paste.
    "gemini": (
        re.compile(r"^AIza[0-9A-Za-z_-]{30,}$"),
        "AI Studio keys look like AIza… (39 chars)",
        "This looks like a Google Cloud/Vertex or OAuth token, not an AI Studio key.",
    ),
    "openai": (
        re.compile(r"^sk-[A-Za-z0-9_-]{20,}$"),
        "OpenAI keys look like sk-…",
        "",  # no common mixup worth naming
    ),
    "claude": (
        re.compile(r"^sk-ant-[A-Za-z0-9_-]{20,}$"),
        "Anthropic keys look like sk-ant-…",
        "",  # generic sk- keys usually come from OpenAI; say so
    ),
    "groq": (
        re.compile(r"^gsk_[A-Za-z0-9]{20,}$"),
        "Groq keys look like gsk_…",
        "",
    ),
    "deepseek": (
        re.compile(r"^sk-[A-Za-z0-9]{20,}$"),
        "DeepSeek keys look like sk-… (hex-style characters)",
        "",
    ),
    # openai/mistral/openrouter key formats are not reliably distinctive;
    # leave them unvalidated rather than reject valid keys.
}


def validate_cloud_key(provider_id: str, api_key: str) -> None:
    """Reject a paste-time key that cannot be valid for the provider.

    Raises ValueError with a user-facing explanation when the key's shape
    contradicts the chosen provider (the Vertex-key-vs-Gemini mixup being
    the motivating case). Well-formed keys always pass through untouched —
    the live Test Connection remains the authority on actual validity.
    """
    key = api_key.strip()
    rule = _CLOUD_KEY_PATTERNS.get(provider_id)
    if rule is None or not key:
        return
    pattern, expected, mixup = rule
    if pattern.match(key):
        return
    # Known cross-provider mixup? Name it and point at the fix; otherwise
    # state the expected shape plainly.
    if provider_id == "gemini" and mixup and not key.startswith("AIza"):
        raise ValueError(
            f"That doesn't look like a Gemini (AI Studio) key. {mixup} "
            f"Create one free at aistudio.google.com/apikey — {expected}."
        )
    hint = f"{mixup} " if mixup else ""
    raise ValueError(
        f"That doesn't look like a valid key for this provider. {hint}"
        f"Expected format: {expected}."
    )


def _redact_key(key: str) -> str:
    """Never ship a raw API key anywhere: show only its tail."""
    tail = key[-4:] if len(key) >= 8 else "****"
    return f"…{tail}"


# ── Vision model configuration ───────────────────────────
#
# The vision layer (vision_guide.locate_element, VisionController, agentcore
# perception) needs a MULTIMODAL model — one that accepts image content — to
# locate UI elements semantically. Chat models cannot: they either reject the
# image or hallucinate around the prompt text. This registry names the models
# known to accept {type: image_url} content per surface.

# Ollama model-name prefixes that are multimodal (vision) models, best first.
# This list is the OFFLINE fast path — ollama_model_capabilities() is asked
# whenever a name matches nothing here, so a multimodal build missing from
# this tuple is still usable. Keep the Qwen VL lines explicit anyway: they are
# the strongest local grounds for UI elements and the family that keeps
# shipping new sizes under new tags.
OLLAMA_VISION_MODEL_PREFIXES: tuple[str, ...] = (
    "qwen3-vl",
    "qwen3vl",
    "llava",
    "llama3.2-vision",
    "llama3.1-vision",
    "moondream",
    "minicpm-v",
    "qwen2-vl",
    "qwen2.5vl",
    "bakllava",
)

# Cloud presets whose default/current models accept image_url content. Keys
# reuse CLOUD_LLM_PRESETS ids so the same stored key configures both brains.
# Claude is deliberately NOT here: /v1/messages embeds images as base64
# `source` blocks, not the OpenAI image_url shape the vision layer sends —
# wiring it up needs an image-content shim in AnthropicMessagesProvider, not a
# preset row. Add it there (and to this tuple) when multimodal Claude lands.
VISION_CAPABLE_CLOUD_PRESETS: tuple[str, ...] = ("openai", "gemini", "openrouter")


def is_ollama_vision_model(model_name: str) -> bool:
    """True when an Ollama model name looks like a multimodal (vision) model."""
    lower = model_name.lower()
    return any(lower.startswith(prefix) or f":{prefix}" in lower or f"/{prefix}" in lower
               for prefix in OLLAMA_VISION_MODEL_PREFIXES)


def _pick_ollama_vision_model(models: list[str], *, base_url: str = _OLLAMA_DEFAULT_URL) -> str:
    """Pick the best vision model from an Ollama model list ('' when none).

    Known name prefixes decide first — that is a cheap, offline match against
    models this project has always understood. Only when no name matches do we
    ask Ollama itself, so a multimodal build the prefix list has never heard
    of (a newer qwen-vl tag, say) is still found instead of the user being
    told to pull a model they already have.
    """
    for model in models:
        if is_ollama_vision_model(model):
            return model
    for model in models:
        capabilities = ollama_model_capabilities(model, base_url)
        if capabilities and "vision" in capabilities:
            return model
    return ""


# ── Ollama model lifecycle (one model resident at a time) ──────────
#
# A small local machine cannot hold a chat brain and a vision brain at once.
# On this project's reference box an 8B chat model is ~5.9 GB, a 4B VL model
# ~3.3 GB, and the Windows desktop already holds ~8.7 GB before any model
# loads — so Ollama evicts one to load the other, and the eviction surfaces as
# a cold reload (measured: 49 s for the 8B). Evicting the other model FIRST,
# deliberately, turns that into predictable behavior and holds peak RAM to a
# single model, instead of leaving it to the kernel's memory pressure.
#
# Residency is controlled through the NATIVE api, not the OpenAI-compatible
# one: /v1/chat/completions accepts a keep_alive field and ignores it (verified
# against a live server — the model stayed resident), while
# POST /api/generate {"keep_alive": 0} returns done_reason "unload" and
# POST /api/generate {"keep_alive": "10m"} re-arms residency.

_OLLAMA_PS_TIMEOUT = 2.0
_OLLAMA_UNLOAD_TIMEOUT = 15.0
_OLLAMA_LOAD_TIMEOUT = 300.0

# Slack kept free BEYOND a model's own footprint. Loading a 5.9 GB model into a
# machine with exactly 5.9 GB free leaves the OS and every other process with
# nothing, and that shows up as paging during the load itself. This margin is
# what makes "fits" mean "fits and stays usable" rather than "arithmetically
# not impossible".
_OLLAMA_MEMORY_HEADROOM_BYTES = 512 * 1024 * 1024

# One process-wide telemetry reader (psutil/Win32//proc reads live in that
# layer); created on first use so importing this module stays free of it.
_hardware_telemetry: Any = None


def ollama_unload_on_switch() -> bool:
    """Whether a completion evicts other resident models first (default on).

    Disable with NOVACONTROL_OLLAMA_UNLOAD_ON_SWITCH=0 on a machine with RAM
    to spare, where paying a reload per switch costs more than the memory.
    """
    raw = os.environ.get("NOVACONTROL_OLLAMA_UNLOAD_ON_SWITCH", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def ollama_disable_thinking() -> bool:
    """Whether local Ollama calls ask the model NOT to emit its reasoning.

    Every NovaControl local call wants a short structured answer, and a
    reasoning-capable model that spends its budget thinking returns nothing at
    all (measured: qwen3:8b, 115s, zero characters of answer). Ollama honours
    ``think: false`` where the model's template supports it and ignores the
    flag elsewhere, so leaving it on costs nothing.

    NOVACONTROL_OLLAMA_ALLOW_THINKING=true restores the previous behaviour, for
    the cases where the reasoning itself is what the caller wants.
    """
    raw = os.environ.get("NOVACONTROL_OLLAMA_ALLOW_THINKING", "").strip().lower()
    return raw not in {"1", "true", "yes", "on"}


def ollama_keep_alive() -> str:
    """Configured residency window for a loaded model ('' = Ollama's own default).

    NOVACONTROL_OLLAMA_KEEP_ALIVE takes an Ollama duration ("30s", "10m",
    "24h") or a plain seconds count. Left unset, this module never overrides
    Ollama's built-in residency — the default is to change nothing. When set,
    it is applied to a model that this module (re)loads on a switch, which is
    the one moment residency is ours to choose.
    """
    return os.environ.get("NOVACONTROL_OLLAMA_KEEP_ALIVE", "").strip()


# Reply ceiling applied to LOCAL Ollama completions when the caller sets no
# budget of its own (0 disables the cap). Local models generate without bound:
# a measured qwen3-vl:4b spent 600 consecutive tokens on reasoning and returned
# no answer at all, and with no ceiling the request simply blocks until the
# socket timeout. 1024 tokens is far more than a local UI answer needs while
# still ending a runaway generation.
_OLLAMA_MAX_TOKENS_DEFAULT = 1024


def ollama_max_tokens() -> int:
    """Reply ceiling for local completions in tokens (0 = no ceiling).

    NOVACONTROL_OLLAMA_MAX_TOKENS overrides it. Raise it for a model whose
    reasoning legitimately needs more room; set it to 0 only when the socket
    timeout is the failure mode you actually want.
    """
    raw = os.environ.get(
        "NOVACONTROL_OLLAMA_MAX_TOKENS", str(_OLLAMA_MAX_TOKENS_DEFAULT)
    ).strip()
    try:
        return int(float(raw))
    except ValueError:
        return _OLLAMA_MAX_TOKENS_DEFAULT


def _same_ollama_model(left: str, right: str) -> bool:
    """Ollama identity match: 'qwen3' and 'qwen3:latest' are the same model."""
    def base(name: str) -> str:
        normalized = name.strip().lower()
        return normalized[: -len(":latest")] if normalized.endswith(":latest") else normalized

    return bool(base(left)) and base(left) == base(right)


def _gigabytes(value: int | None) -> str:
    """Human GB for a measurement, or 'unknown' — never a confident 0."""
    return "unknown" if value is None else f"{value / 1024 ** 3:.1f} GB"


def ollama_available_memory_bytes() -> int | None:
    """This machine's available physical memory in bytes (None when unreadable).

    Reuses the telemetry layer's own platform reads (psutil → Win32
    GlobalMemoryStatusEx → /proc/meminfo) so "how much memory is free" has
    exactly ONE definition in this codebase, and reports None rather than a
    convenient guess when the OS will not say.
    """
    global _hardware_telemetry  # noqa: PLW0603
    try:
        if _hardware_telemetry is None:
            from novacontrol.telemetry.hardware import HardwareTelemetry

            _hardware_telemetry = HardwareTelemetry()
        memory = _hardware_telemetry.memory()
    except Exception:  # pragma: no cover - a memory check must never break a load
        return None
    if not memory.get("available"):
        return None
    value = memory.get("available_bytes")
    return int(value) if isinstance(value, (int, float)) else None


def ollama_resident_models(
    base_url: str = _OLLAMA_DEFAULT_URL, *, timeout: float = _OLLAMA_PS_TIMEOUT
) -> list[dict[str, Any]] | None:
    """Models Ollama holds in memory right now, each with its resident size.

    None means "could not ask" (server down, timeout, odd payload), which is
    deliberately distinct from an empty list meaning "nothing is resident":
    only a real answer may drive an eviction. A size of 0 means Ollama did not
    report one — never a claim that the model is free.
    """
    try:
        request = Request(f"{base_url.rstrip('/')}/api/ps", method="GET")
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (URLError, OSError, json.JSONDecodeError, ValueError, TypeError):
        return None
    models = data.get("models")
    if not isinstance(models, list):
        return None
    resident: list[dict[str, Any]] = []
    for entry in models:
        if isinstance(entry, dict) and "name" in entry:
            size = entry.get("size")
            resident.append({
                "name": str(entry["name"]),
                "size": int(size) if isinstance(size, (int, float)) else 0,
            })
    return resident


def ollama_loaded_models(
    base_url: str = _OLLAMA_DEFAULT_URL, *, timeout: float = _OLLAMA_PS_TIMEOUT
) -> list[str] | None:
    """Names of the models Ollama currently holds in memory (None if unaskable)."""
    resident = ollama_resident_models(base_url, timeout=timeout)
    return None if resident is None else [entry["name"] for entry in resident]


def _ollama_size_on_disk(
    model: str, base_url: str = _OLLAMA_DEFAULT_URL, *, timeout: float = _OLLAMA_PS_TIMEOUT
) -> int | None:
    """Weights size from /api/tags: the footprint a not-yet-loaded model takes."""
    try:
        request = Request(f"{base_url.rstrip('/')}/api/tags", method="GET")
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (URLError, OSError, json.JSONDecodeError, ValueError, TypeError):
        return None
    models = data.get("models")
    if not isinstance(models, list):
        return None
    for entry in models:
        if isinstance(entry, dict) and _same_ollama_model(str(entry.get("name", "")), model):
            size = entry.get("size")
            return int(size) if isinstance(size, (int, float)) else None
    return None


def ollama_model_size_bytes(
    model: str, base_url: str = _OLLAMA_DEFAULT_URL, *, timeout: float = _OLLAMA_PS_TIMEOUT
) -> int | None:
    """A model's footprint in bytes: its resident size if loaded, else on disk.

    /api/ps reports the ACTUAL resident size (weights plus the KV cache for the
    loaded context); /api/tags reports the weights on disk for any pulled model,
    which is the best estimate available for one that is not loaded yet.
    """
    resident = ollama_resident_models(base_url, timeout=timeout)
    if resident is not None:
        for entry in resident:
            if _same_ollama_model(entry["name"], model) and entry["size"]:
                return int(entry["size"])
    return _ollama_size_on_disk(model, base_url, timeout=timeout)


def unload_ollama_model(
    model: str, base_url: str = _OLLAMA_DEFAULT_URL, *, timeout: float = _OLLAMA_UNLOAD_TIMEOUT
) -> bool:
    """Release a model's memory immediately. True when Ollama accepted it.

    ``keep_alive: 0`` is Ollama's documented way to unload as soon as the
    (empty) request completes, and it answers ``done_reason: "unload"``.
    """
    try:
        request = Request(
            f"{base_url.rstrip('/')}/api/generate",
            data=json.dumps({"model": model, "keep_alive": 0}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:
            response.read()
        logger.info("Unloaded Ollama model %s", model)
        return True
    except (URLError, OSError, json.JSONDecodeError, ValueError, TypeError):
        return False


def warm_ollama_model(
    model: str,
    base_url: str = _OLLAMA_DEFAULT_URL,
    *,
    keep_alive: str = "",
    timeout: float = _OLLAMA_LOAD_TIMEOUT,
) -> bool:
    """Load a model with a chosen residency window, without generating.

    ``/api/generate`` with no prompt answers ``done_reason: "load"`` — the
    model is brought into memory and ``keep_alive`` decides how long it
    stays. This is the only way to set residency: the OpenAI-compatible
    endpoint drops the field.
    """
    payload: dict[str, Any] = {"model": model}
    if keep_alive:
        payload["keep_alive"] = keep_alive
    try:
        request = Request(
            f"{base_url.rstrip('/')}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:
            response.read()
        return True
    except (URLError, OSError, json.JSONDecodeError, ValueError, TypeError):
        return False


@dataclass(frozen=True)
class OllamaMemoryPlan:
    """What must happen before `model` loads, and the evidence behind it.

    The measured figures travel with the decision so a load can be explained
    with real numbers, and so "could not measure" (None) is never quietly read
    as "nothing to worry about".
    """

    model: str
    available_bytes: int | None
    incoming_bytes: int | None
    resident_models: tuple[str, ...]
    resident_bytes: int
    evict: tuple[str, ...]
    fits_after_evict: bool | None

    def describe(self) -> str:
        """One line for the log: what was measured, and what was decided."""
        incoming = _gigabytes(self.incoming_bytes)
        available = _gigabytes(self.available_bytes)
        if not self.resident_models:
            return f"{self.model} ({incoming}): nothing else resident, {available} free"
        resident = ", ".join(self.resident_models)
        if not self.evict:
            return f"{self.model} ({incoming}): keeping {resident} resident, {available} free"
        if self.fits_after_evict is None:
            verdict = "fit afterwards unknown"
        elif self.fits_after_evict:
            verdict = "room afterwards"
        else:
            verdict = "NOT enough room afterwards — expect paging"
        freed = _gigabytes(self.resident_bytes)
        return f"{self.model} ({incoming}): unloading {resident} (frees {freed}), {available} free — {verdict}"


def ollama_memory_plan(
    model: str,
    base_url: str = _OLLAMA_DEFAULT_URL,
    *,
    exclusive: bool = True,
    timeout: float = _OLLAMA_PS_TIMEOUT,
) -> OllamaMemoryPlan:
    """Decide what must be unloaded before `model` can be loaded.

    There are two independent reasons to evict, which is why the plan reports
    them apart:

    - ``exclusive`` (the default): only ONE model may be resident, whatever the
      memory situation happens to be. A machine that runs a chat brain AND a
      vision brain must not hold both — the second is not a spare, it is the
      reason the first gets paged out mid-answer.
    - MEMORY: whatever that setting says, a machine that cannot hold the
      incoming model alongside what is already resident must unload first.
      ``fits_after_evict`` reports whether unloading actually buys enough room,
      so a load that will thrash is visible BEFORE it starts.

    Never raises. An unreachable server yields a plan that evicts nothing — with
    no evidence, doing nothing is the only honest action.
    """
    resident = ollama_resident_models(base_url, timeout=timeout)
    if resident is None:
        return OllamaMemoryPlan(
            model=model,
            available_bytes=None,
            incoming_bytes=None,
            resident_models=(),
            resident_bytes=0,
            evict=(),
            fits_after_evict=None,
        )
    others = tuple(entry for entry in resident if not _same_ollama_model(entry["name"], model))
    others_bytes = sum(int(entry["size"]) for entry in others)
    available = ollama_available_memory_bytes()
    incoming: int | None = None
    for entry in resident:
        if _same_ollama_model(entry["name"], model) and entry["size"]:
            incoming = int(entry["size"])
            break
    if incoming is None:
        incoming = _ollama_size_on_disk(model, base_url, timeout=timeout)
    required = None if incoming is None else incoming + _OLLAMA_MEMORY_HEADROOM_BYTES
    memory_forces_evict = available is not None and required is not None and available < required
    evict = (
        tuple(entry["name"] for entry in others)
        if (exclusive or memory_forces_evict)
        else ()
    )
    fits: bool | None = None
    if available is not None and required is not None:
        freed = sum(int(entry["size"]) for entry in others if entry["name"] in evict)
        fits = available + freed >= required
    return OllamaMemoryPlan(
        model=model,
        available_bytes=available,
        incoming_bytes=incoming,
        resident_models=tuple(entry["name"] for entry in others),
        resident_bytes=others_bytes,
        evict=evict,
        fits_after_evict=fits,
    )


def ensure_exclusive_ollama_model(
    model: str,
    base_url: str = _OLLAMA_DEFAULT_URL,
    *,
    keep_alive: str = "",
    timeout: float = _OLLAMA_PS_TIMEOUT,
) -> list[str]:
    """Unload whatever must go, so `model` loads with room to actually work.

    Returns the names evicted (empty when nothing had to move). Never raises and
    never blocks: this runs on the critical path of a completion, so a lifecycle
    probe that cannot reach the server must do nothing rather than fail a
    request that could otherwise succeed.

    The decision comes from ollama_memory_plan, so exclusivity AND the measured
    memory situation are both honored — see that function for why they are
    reported separately.

    When a switch happened and ``keep_alive`` is configured, the incoming model
    is re-armed with that window: the one moment residency is ours to choose,
    since the completion itself goes out over the OpenAI-compatible endpoint,
    which discards keep_alive.
    """
    plan = ollama_memory_plan(model, base_url, exclusive=ollama_unload_on_switch(), timeout=timeout)
    evicted: list[str] = []
    for resident in plan.evict:
        if unload_ollama_model(resident, base_url):
            evicted.append(resident)
    if evicted or plan.fits_after_evict is False:
        logger.info("Ollama memory plan: %s", plan.describe())
    if evicted and keep_alive:
        warm_ollama_model(model, base_url, keep_alive=keep_alive)
    return evicted


def provider_supports_vision(provider: object | None) -> bool:
    """True only when a provider can ACTUALLY accept image content.

    A provider's NAME is not evidence of its eyes. The application wires the
    chat brain's provider into the vision layer as a default, and that
    provider is very often a text-only local model (qwen3, llama3.2, …) — it
    reports a healthy name and model id, cannot see a screenshot, and will
    invent click coordinates for a picture it never received. The old gate
    ("anything that is not the Echo fallback") therefore declared vision
    available on a machine that had none, and vision status said so.

    Decided per surface:
    - ``vision:*`` — built by build_vision_provider, already validated against
      the provider's own capability metadata;
    - ``ollama`` — accepted only when Ollama reports ``vision`` for that exact
      model, falling back to the name heuristic when capability metadata is
      unavailable (older Ollama);
    - a registered vision-capable cloud preset (gpt-4o, gemini, …);
    - everything else, the Echo fallback included, is refused.
    """
    if provider is None:
        return False
    name = str(getattr(provider, "name", "") or "").strip().lower()
    if not name:
        return False
    if name.startswith("vision:"):
        return True
    if "echo" in name:
        return False
    surface = name.split(":", 1)[-1] if ":" in name else name
    if surface == "ollama":
        model = str(getattr(provider, "model", "") or "").strip()
        if not model:
            return False
        capabilities = ollama_model_capabilities(model)
        if capabilities is None:
            return is_ollama_vision_model(model)  # no metadata: best guess
        return "vision" in capabilities
    return surface in VISION_CAPABLE_CLOUD_PRESETS


def build_vision_provider(
    provider_id: str,
    credential: str,
    *,
    model: str = "",
    ollama_url: str = _OLLAMA_DEFAULT_URL,
) -> tuple[OpenAICompatibleLLMProvider | None, str]:
    """Build a MULTIMODAL provider for the vision layer.

    Two surfaces, one shape:
    - ``ollama``: probes a running Ollama for a vision model (llava,
      llama3.2-vision, moondream, …). ``credential`` is ignored (local API);
      an explicit ``model`` must BE a vision model. Returns (None, reason)
      when Ollama is unreachable or carries no vision model.
    - a cloud preset id (openai/gemini/openrouter): builds the same
      OpenAI-compatible provider the chat brain uses, pinned to a
      vision-capable model (the preset default is one). The key requirement
      matches build_cloud_provider.

    Returns ``(provider, "")`` on success or ``(None, reason)`` — callers show
    the reason instead of silently degrading to OCR-only.
    """
    if provider_id == "ollama":
        info = detect_ollama(ollama_url)
        if info is None:
            return None, "Ollama is not reachable at " + ollama_url
        if model.strip():
            chosen = model.strip()
            # Name prefix OR Ollama's own capability metadata — whichever
            # recognizes it. Without the second check a genuinely multimodal
            # model the prefix list has not been taught would be refused.
            capabilities = ollama_model_capabilities(chosen, info["url"])
            if not is_ollama_vision_model(chosen) and not (capabilities and "vision" in capabilities):
                return None, (
                    f"{chosen!r} is not a vision model. Pull one, e.g. "
                    "`ollama pull llama3.2-vision` or `ollama pull llava`."
                )
        else:
            chosen = _pick_ollama_vision_model(list(info["models"]), base_url=info["url"])
            if not chosen:
                return None, (
                    "No vision model found in Ollama. Pull one, e.g. "
                    "`ollama pull llama3.2-vision` or `ollama pull llava`."
                )
        return OpenAICompatibleLLMProvider(
            name="vision:ollama",
            base_url=info["url"],
            api_key="ollama",
            model=chosen,
            ollama_url=info["url"],
        ), ""

    preset = get_cloud_preset(provider_id)
    if preset is None:
        return None, (
            f"Unknown vision model provider: {provider_id!r}. "
            "Valid providers: ollama, " + ", ".join(VISION_CAPABLE_CLOUD_PRESETS)
        )
    if provider_id not in VISION_CAPABLE_CLOUD_PRESETS:
        return None, (
            f"{preset['label']} has no vision-capable models registered here. "
            "Valid providers: ollama, " + ", ".join(VISION_CAPABLE_CLOUD_PRESETS)
        )
    if not credential.strip():
        return None, "An API key is required for a cloud vision model."
    chosen = model.strip() or preset["default_model"]
    return OpenAICompatibleLLMProvider(
        name=f"vision:{provider_id}",
        base_url=preset["base_url"],
        api_key=credential.strip(),
        model=chosen,
        chat_path=preset["chat_path"],
    ), ""


def build_cloud_provider(
    provider_id: str,
    api_key: str,
    *,
    model: str = "",
    environ: Mapping[str, str] | None = None,
) -> OpenAICompatibleLLMProvider | AnthropicMessagesProvider | None:
    """Build a cloud provider for a preset id + key (None for unknown presets).

    Claude dispatches to the native AnthropicMessagesProvider (its /v1/messages
    wire format is not OpenAI-compatible); every other preset builds the
    generic OpenAICompatibleLLMProvider. Both expose the same
    complete(messages, **kwargs) surface.

    An explicitly passed environ wins over the host env, matching every other
    factory in this module (an empty dict is authoritative, never falsy).
    """
    preset = get_cloud_preset(provider_id)
    if preset is None or not api_key.strip():
        return None
    values = os.environ if environ is None else environ
    # A model from the UI wins; otherwise env; otherwise the preset default.
    chosen_model = (model or values.get("NOVACONTROL_LLM_MODEL", "")).strip() or preset["default_model"]
    if provider_id == "claude":
        return AnthropicMessagesProvider(
            name=f"cloud:{preset['id']}",
            base_url=preset["base_url"],
            api_key=api_key.strip(),
            model=chosen_model,
        )
    return OpenAICompatibleLLMProvider(
        name=f"cloud:{preset['id']}",
        base_url=preset["base_url"],
        api_key=api_key.strip(),
        model=chosen_model,
        chat_path=preset["chat_path"],
    )


# ── Main provider builder ────────────────────────────────


def build_llm_provider_from_environment(environ: Mapping[str, str] | None = None) -> object:
    """Build the best available LLM provider.

    Resolution order:
    1. Explicit env vars (``NOVACONTROL_ENABLE_EXTERNAL_LLM`` + model config)
    2. Ollama auto-detection (if running locally)
    3. Echo fallback (no LLM)
    """
    # Explicit (possibly empty) mapping wins; only None falls back to the real env.
    values = os.environ if environ is None else environ

    # 1. Explicit configuration takes priority
    enabled = values.get("NOVACONTROL_ENABLE_EXTERNAL_LLM", "").strip().lower()
    if enabled in {"1", "true", "yes", "on"}:
        model = values.get("NOVACONTROL_LLM_MODEL", "").strip()
        base_url = values.get("NOVACONTROL_LLM_BASE_URL", "").strip()
        api_key = (
            values.get("NOVACONTROL_LLM_API_KEY", "").strip()
            or values.get("OPENAI_API_KEY", "").strip()
            or ("local" if base_url else "")
        )
        if not model:
            return EchoLLMProvider()
        if not base_url:
            base_url = "https://api.openai.com"
        return OpenAICompatibleLLMProvider(
            name=values.get("NOVACONTROL_LLM_NAME", "openai-compatible"),
            base_url=base_url,
            api_key=api_key,
            model=model,
        )

    # 2. Ollama auto-detection (skip if explicitly disabled)
    disable_ollama = values.get("NOVACONTROL_DISABLE_OLLAMA", "").strip().lower()
    if disable_ollama not in {"1", "true", "yes", "on"}:
        ollama_url = values.get("NOVACONTROL_OLLAMA_URL", _OLLAMA_DEFAULT_URL).strip()
        ollama = build_ollama_provider(ollama_url)
        if ollama is not None:
            return ollama

    # 3. Fallback
    return EchoLLMProvider()


# ── Lazy Ollama re-probe ─────────────────────────────────

# How often a failed (no-Ollama) re-probe may run. Successful detection stops
# probing entirely: once upgraded, the provider is the real one for the
# process lifetime.
_REPROBE_MIN_INTERVAL_SECONDS = 60.0


def make_ollama_reprobe(
    environ: Mapping[str, str] | None = None,
    *,
    model: str = "",
) -> Callable[[], Awaitable[object | None]]:
    """Build a rate-limited async probe that returns an upgraded provider or None.

    The app resolves its LLM once at boot; if that yielded the Echo fallback
    (Ollama not running yet), this probe lets the running process pick Ollama
    up lazily when it starts later — no restart. Behavior:

    - Returns None immediately when Ollama detection is disabled by env
      (NOVACONTROL_DISABLE_OLLAMA) or when an explicit external LLM is already
      configured (NOVACONTROL_ENABLE_EXTERNAL_LLM) — boot already settled those.
    - Misses are rate-limited (one 2s /api/tags probe per minute) so chat stays
      responsive; a hit upgrades once and every later call returns the SAME
      provider object without touching the network again.
    - The blocking urlopen probe runs in a worker thread (asyncio.to_thread),
      matching how provider completions keep the event loop free.
    - An explicit ``model`` pins the upgraded provider to that Ollama model,
      so the brain model picker's choice survives a lazy Ollama startup.
    """
    # Explicit (possibly empty) mapping wins; only None falls back to the real env.
    values = os.environ if environ is None else environ
    if values.get("NOVACONTROL_ENABLE_EXTERNAL_LLM", "").strip().lower() in {"1", "true", "yes", "on"}:
        async def disabled_by_explicit_config() -> object | None:
            return None
        return disabled_by_explicit_config
    if values.get("NOVACONTROL_DISABLE_OLLAMA", "").strip().lower() in {"1", "true", "yes", "on"}:
        async def disabled_by_flag() -> object | None:
            return None
        return disabled_by_flag
    ollama_url = values.get("NOVACONTROL_OLLAMA_URL", _OLLAMA_DEFAULT_URL).strip()
    state: dict[str, object] = {"provider": None, "last_miss": 0.0}

    async def reprobe() -> object | None:
        if state["provider"] is not None:
            return state["provider"]  # already upgraded; same object, no I/O
        now = time.monotonic()
        last_miss = state["last_miss"]
        if not isinstance(last_miss, (int, float)) or now - last_miss < _REPROBE_MIN_INTERVAL_SECONDS:
            return None  # recent miss: skip silently
        state["last_miss"] = now
        info = await asyncio.to_thread(detect_ollama, ollama_url)
        if info is None:
            return None
        provider: object = OpenAICompatibleLLMProvider(
            name="ollama",
            base_url=info["url"],
            api_key="ollama",
            model=model.strip() or _pick_ollama_model(info["models"]),
            ollama_url=info["url"],
        )
        state["provider"] = provider
        return provider

    return reprobe
