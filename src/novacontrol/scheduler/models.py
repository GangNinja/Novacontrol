"""Scheduler models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class ScheduledTaskStatus(StrEnum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    name: str
    run_at: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)
    status: ScheduledTaskStatus = ScheduledTaskStatus.SCHEDULED
    id: str = field(default_factory=lambda: uuid4().hex)

    def due(self, now: datetime | None = None) -> bool:
        return self.status is ScheduledTaskStatus.SCHEDULED and self.run_at <= (now or datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "run_at": self.run_at.isoformat(),
            "payload": dict(self.payload),
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ScheduledTask":
        return cls(
            name=str(payload["name"]),
            run_at=datetime.fromisoformat(str(payload["run_at"])),
            payload=dict(payload.get("payload", {})),
            status=ScheduledTaskStatus(str(payload.get("status", ScheduledTaskStatus.SCHEDULED.value))),
            id=str(payload["id"]),
        )
