"""LLM provider registry and baseline provider."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
import json
import logging
import os
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
    """Generic provider for OpenAI-compatible `/v1/chat/completions` APIs."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        transport: LLMTransport | None = None,
    ) -> None:
        self._name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
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
            f"{self.base_url}/v1/chat/completions",
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


# ── Main provider builder ────────────────────────────────


def build_llm_provider_from_environment(environ: Mapping[str, str] | None = None) -> object:
    """Build the best available LLM provider.

    Resolution order:
    1. Explicit env vars (``NOVACONTROL_ENABLE_EXTERNAL_LLM`` + model config)
    2. Ollama auto-detection (if running locally)
    3. Echo fallback (no LLM)
    """
    values = environ or os.environ

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
