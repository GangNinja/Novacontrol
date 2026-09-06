"""Audit logging shared by the desktop and browser automation controllers.

Every action a controller executes — approved, failed, or denied — is appended
to the audit log as a timestamped, serializable entry, so operators can review
exactly what automation ran and when. Both controllers hand the log their
action results (``DesktopActionResult`` / ``BrowserActionResult``), which
declare the same five fields.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol


class AuditableResult(Protocol):
    """The common shape of an executed automation action result.

    ``DesktopActionResult`` and ``BrowserActionResult`` both declare exactly
    these fields; ``status`` is a ``StrEnum``, which is a ``str`` subclass.
    Read-only (properties) so frozen dataclasses satisfy the protocol.
    """

    @property
    def action_id(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def output(self) -> Mapping[str, Any]: ...

    @property
    def error(self) -> str | None: ...

    @property
    def approval_id(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One timestamped trace line for an executed automation action."""

    action_id: str
    status: str
    recorded_at: str
    output: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    approval_id: str | None = None


def _snapshot(result: AuditableResult) -> AuditEntry:
    """Record an action result with the moment it was appended (UTC ISO-8601)."""
    return AuditEntry(
        action_id=result.action_id,
        status=str(result.status),
        recorded_at=datetime.now(UTC).isoformat(),
        output=dict(result.output),
        error=result.error,
        approval_id=result.approval_id,
    )


class AutomationAuditLog(Protocol):
    """Storage boundary for automation audit entries."""

    async def append(self, result: AuditableResult) -> None:
        """Store an executed automation result for review."""

    async def read(self) -> Sequence[AuditEntry]:
        """Read the recorded audit entries, oldest first."""


class InMemoryAutomationAuditLog:
    """In-process audit store used by controllers and tests."""

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    async def append(self, result: AuditableResult) -> None:
        self._entries.append(_snapshot(result))

    async def read(self) -> Sequence[AuditEntry]:
        return tuple(self._entries)
