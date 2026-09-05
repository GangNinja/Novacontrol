"""Retry policies for resilient runtime operations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 1
    initial_delay_seconds: float = 0.0
    backoff_multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")
        if self.initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds cannot be negative.")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be at least 1.")


async def with_retries(
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    on_retry: Callable[[int, BaseException], Awaitable[None]] | None = None,
) -> T:
    """Run an async operation with bounded exponential backoff."""
    delay = policy.initial_delay_seconds
    attempt = 1
    while True:
        try:
            return await operation()
        except Exception as exc:
            if attempt >= policy.max_attempts:
                raise
            if on_retry is not None:
                await on_retry(attempt, exc)
            if delay:
                await asyncio.sleep(delay)
            delay *= policy.backoff_multiplier
            attempt += 1
