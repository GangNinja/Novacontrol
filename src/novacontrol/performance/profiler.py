"""Lightweight profiling helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ProfileResult:
    name: str
    duration_seconds: float
    result: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "duration_seconds": self.duration_seconds,
            "result": self.result,
        }


class Profiler:
    async def measure_async(self, name: str, operation: Callable[[], Awaitable[T]]) -> ProfileResult:
        started = perf_counter()
        result = await operation()
        return ProfileResult(name=name, duration_seconds=perf_counter() - started, result=result)

    def measure_sync(self, name: str, operation: Callable[[], T]) -> ProfileResult:
        started = perf_counter()
        result = operation()
        return ProfileResult(name=name, duration_seconds=perf_counter() - started, result=result)
