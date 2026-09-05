"""Runtime diagnostics and health checks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from inspect import isawaitable
from typing import Any


class HealthState(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILING = "failing"


@dataclass(frozen=True, slots=True)
class HealthReport:
    name: str
    state: HealthState
    details: dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))


HealthCheck = Callable[[], HealthReport | Awaitable[HealthReport]]


class DiagnosticsRegistry:
    """Collects runtime health checks without coupling feature modules together."""

    def __init__(self) -> None:
        self._checks: dict[str, HealthCheck] = {}

    def register(self, name: str, check: HealthCheck) -> None:
        if not name or not name.strip():
            raise ValueError("Diagnostic check name is required.")
        if name in self._checks:
            raise ValueError(f"Diagnostic check already registered: {name}")
        self._checks[name] = check

    async def run(self) -> tuple[HealthReport, ...]:
        reports: list[HealthReport] = []
        for name, check in self._checks.items():
            try:
                result = check()
                if isawaitable(result):
                    result = await result
                reports.append(result)
            except Exception as exc:
                reports.append(
                    HealthReport(
                        name=name,
                        state=HealthState.FAILING,
                        details={"error": type(exc).__name__, "message": str(exc)},
                    )
                )
        return tuple(reports)
