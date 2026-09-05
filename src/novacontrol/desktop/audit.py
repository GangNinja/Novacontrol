"""Audit logging for desktop automation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from novacontrol.desktop.models import DesktopActionResult


@runtime_checkable
class AutomationAuditLog(Protocol):
    async def append(self, result: DesktopActionResult) -> None:
        """Store an automation result for review."""

    async def read(self) -> Sequence[DesktopActionResult]:
        """Read audit entries."""


class InMemoryAutomationAuditLog:
    def __init__(self) -> None:
        self._results: list[DesktopActionResult] = []

    async def append(self, result: DesktopActionResult) -> None:
        self._results.append(result)

    async def read(self) -> Sequence[DesktopActionResult]:
        return tuple(self._results)
