"""Async load-test harness."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any


@dataclass(frozen=True, slots=True)
class LoadTestResult:
    total_requests: int
    successes: int
    failures: int
    duration_seconds: float

    @property
    def requests_per_second(self) -> float:
        if self.duration_seconds == 0:
            return 0.0
        return self.total_requests / self.duration_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_requests": self.total_requests,
            "successes": self.successes,
            "failures": self.failures,
            "duration_seconds": self.duration_seconds,
            "requests_per_second": self.requests_per_second,
        }


class LoadTester:
    """Runs an async operation repeatedly with bounded concurrency."""

    async def run(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        requests: int,
        concurrency: int = 1,
    ) -> LoadTestResult:
        if requests < 1:
            raise ValueError("requests must be at least 1.")
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1.")

        semaphore = asyncio.Semaphore(concurrency)
        successes = 0
        failures = 0

        async def one() -> None:
            nonlocal successes, failures
            async with semaphore:
                try:
                    await operation()
                    successes += 1
                except Exception:
                    failures += 1

        started = perf_counter()
        await asyncio.gather(*(one() for _ in range(requests)))
        duration = perf_counter() - started
        return LoadTestResult(
            total_requests=requests,
            successes=successes,
            failures=failures,
            duration_seconds=duration,
        )
