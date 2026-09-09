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
        response = await asyncio.to_thread(
            self.transport,
            f"{self.base_url}{self.chat_path}",
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            payload,
        )
        choices = response.get("choices", [])
        if not choices:
            return ""
        message = choices[0].get("message", {})
        return str(message.get("content", ""))


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


def detect_ollama(base_url: str = _OLLAMA_DEFAULT_URL) -> dict[str, Any] | None:
    """Probe a running Ollama instance and return available models.

    Returns a dict with ``url`` and ``models`` (list of model names) on
    success, or ``None`` if Ollama is not reachable.
    """
    global _ollama_cache  # noqa: PLW0603
    if _ollama_cache is not None:
        return _ollama_cache
    try:
        request = Request(f"{base_url.rstrip('/')}/api/tags", method="GET")
        with urlopen(request, timeout=2) as response:
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


def build_ollama_provider(base_url: str = _OLLAMA_DEFAULT_URL) -> OpenAICompatibleLLMProvider | None:
    """Build an Ollama provider if a running instance is detected."""
    info = detect_ollama(base_url)
    if info is None:
        return None
    model = _pick_ollama_model(info["models"])
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

# One row per OpenAI-compatible cloud endpoint. All of them speak the same
# /chat/completions shape, differing only in base URL, chat path, default
# model, and where the key comes from. Gemini exposes an OpenAI-compatible
# surface under /v1beta/openai (its native generateContent API is a different
# wire format entirely). Anthropic Claude is intentionally absent: its
# /v1/messages endpoint is NOT OpenAI-compatible (different payload and a
# required anthropic-version header) — it needs its own provider class.
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
) -> OpenAICompatibleLLMProvider | None:
    """Build a cloud provider for a preset id + key (None for unknown presets).

    An explicitly passed environ wins over the host env, matching every other
    factory in this module (an empty dict is authoritative, never falsy).
    """
    preset = get_cloud_preset(provider_id)
    if preset is None or not api_key.strip():
        return None
    values = os.environ if environ is None else environ
    # A model from the UI wins; otherwise env; otherwise the preset default.
    chosen_model = (model or values.get("NOVACONTROL_LLM_MODEL", "")).strip() or preset["default_model"]
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


def make_ollama_reprobe(environ: Mapping[str, str] | None = None) -> Callable[[], Awaitable[object | None]]:
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
            model=_pick_ollama_model(info["models"]),
        )
        state["provider"] = provider
        return provider

    return reprobe
