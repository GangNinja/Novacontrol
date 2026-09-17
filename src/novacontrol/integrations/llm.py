"""LLM provider registry and baseline provider."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
import json
import logging
import os
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
    ) -> None:
        self._name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.chat_path = chat_path
        self.transport = transport or _default_transport
        # Lifetime telemetry for the System panel: cumulative token usage from
        # every usage block the API returned, and the last failure verbatim.
        self.usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.last_error: str = ""

    @property
    def name(self) -> str:
        return self._name

    async def complete(self, messages: Sequence[Mapping[str, str]], **kwargs: object) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(message) for message in messages],
        }
        payload.update(kwargs)
        # The sync transport blocks on the socket; run it off the event loop so a
        # slow LLM response never freezes the rest of the local app.
        try:
            response = await asyncio.to_thread(
                self.transport,
                f"{self.base_url}{self.chat_path}",
                {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                payload,
            )
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise
        self.usage["requests"] += 1
        usage = response.get("usage") or {}
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(field)
            if isinstance(value, int):
                self.usage[field] += value
        choices = response.get("choices", [])
        if not choices:
            return ""
        message = choices[0].get("message", {})
        return str(message.get("content", ""))


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


def _default_transport(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
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


def _pick_ollama_model(models: list[str]) -> str:
    """Pick the best model from an Ollama model list.

    Prefers smaller, faster models for interactive use.
    """
    preferred_prefixes = ("phi3", "phi-3", "qwen2:0.5", "qwen2:1.5", "tinyllama",
                         "gemma:2b", "gemma2:2b", "llama3.2:1b", "llama3.2:3b",
                         "mistral:7b", "llama3.1:8b", "llama3:8b")
    lower_models = [m.lower() for m in models]
    for prefix in preferred_prefixes:
        for i, model in enumerate(lower_models):
            if model.startswith(prefix) or prefix in model:
                return models[i]
    # Fall back to first available model
    return models[0]


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
OLLAMA_VISION_MODEL_PREFIXES: tuple[str, ...] = (
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


def _pick_ollama_vision_model(models: list[str]) -> str:
    """Pick the best vision model from an Ollama model list ('' when none)."""
    for model in models:
        if is_ollama_vision_model(model):
            return model
    return ""


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
            if not is_ollama_vision_model(chosen):
                return None, (
                    f"{chosen!r} is not a vision model. Pull one, e.g. "
                    "`ollama pull llama3.2-vision` or `ollama pull llava`."
                )
        else:
            chosen = _pick_ollama_vision_model(list(info["models"]))
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
        )
        state["provider"] = provider
        return provider

    return reprobe
