"""Resilient LLM provider with retry, backoff, and fallback chain.

Provides production-grade reliability for LLM interactions by wrapping
any OpenAI-compatible provider with automatic retry on transient failures,
exponential backoff, and fallback to alternative providers.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)


class ResilientLLMProvider:
    """Wraps an LLM provider with retry, backoff, and fallback.

    Usage:
        provider = ResilientLLMProvider(
            primary=openai_provider,
            fallbacks=[local_provider, echo_provider],
            max_retries=3,
            base_delay=1.0,
            max_delay=30.0,
        )
        answer = await provider.complete(messages)
    """

    def __init__(
        self,
        *,
        primary: object,
        fallbacks: Sequence[object] = (),
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        backoff_factor: float = 2.0,
        jitter: bool = True,
    ) -> None:
        self._primary = primary
        self._fallbacks = list(fallbacks)
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._backoff_factor = backoff_factor
        self._jitter = jitter
        self._last_provider_used: str = ""
        self._consecutive_failures: int = 0

    @property
    def name(self) -> str:
        return str(getattr(self._primary, "name", "resilient"))

    @property
    def last_provider_used(self) -> str:
        return self._last_provider_used

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    async def complete(self, messages: Sequence[Mapping[str, str]], **kwargs: object) -> str:
        """Complete with retry on primary, then fallback chain."""
        # Try primary with retries
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                result = await self._call_provider(self._primary, messages, **kwargs)
                self._consecutive_failures = 0
                self._last_provider_used = str(getattr(self._primary, "name", "primary"))
                return result
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "LLM attempt %d/%d failed with %s: %s",
                    attempt + 1,
                    self._max_retries,
                    type(exc).__name__,
                    exc,
                )
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(self._delay(attempt))

        # Try fallbacks
        for fallback in self._fallbacks:
            try:
                result = await self._call_provider(fallback, messages, **kwargs)
                self._consecutive_failures = 0
                self._last_provider_used = str(getattr(fallback, "name", "fallback"))
                logger.info("LLM fallback to %s succeeded", self._last_provider_used)
                return result
            except Exception as exc:
                logger.warning(
                    "LLM fallback %s failed with %s: %s",
                    getattr(fallback, "name", "?"),
                    type(exc).__name__,
                    exc,
                )

        self._consecutive_failures += 1
        raise RuntimeError(
            f"All LLM providers failed after {self._max_retries} retries "
            f"and {len(self._fallbacks)} fallbacks"
        ) from last_error

    async def stream(self, messages: Sequence[Mapping[str, str]], **kwargs: object) -> AsyncIterator[str]:
        """Stream tokens from the primary provider.

        Falls back to non-streaming if streaming is not supported.
        """
        try:
            stream_fn = getattr(self._primary, "stream", None)
            if stream_fn is not None:
                async for token in stream_fn(messages, **kwargs):
                    yield token
                return
        except Exception as exc:
            logger.warning("Streaming failed, falling back to complete: %s", exc)

        # Fallback to complete
        result = await self.complete(messages, **kwargs)
        # Yield in chunks for a pseudo-streaming effect
        chunk_size = 20
        for i in range(0, len(result), chunk_size):
            yield result[i : i + chunk_size]

    async def _call_provider(
        self,
        provider: object,
        messages: Sequence[Mapping[str, str]],
        **kwargs: object,
    ) -> str:
        complete = getattr(provider, "complete", None)
        if complete is None:
            raise AttributeError(f"Provider {type(provider).__name__} has no complete method")
        return str(await complete(messages, **kwargs))

    def _delay(self, attempt: int) -> float:
        delay = self._base_delay * (self._backoff_factor ** attempt)
        delay = min(delay, self._max_delay)
        if self._jitter:
            delay *= random.uniform(0.5, 1.0)
        return delay

    def stats(self) -> dict[str, Any]:
        return {
            "primary": getattr(self._primary, "name", "unknown"),
            "fallbacks": [getattr(f, "name", "unknown") for f in self._fallbacks],
            "last_provider_used": self._last_provider_used,
            "consecutive_failures": self._consecutive_failures,
            "max_retries": self._max_retries,
        }
