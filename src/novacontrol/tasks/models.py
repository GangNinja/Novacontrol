"""Task tracking models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class TaskRecordStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TaskRecord:
    title: str
    kind: str = "general"
    status: TaskRecordStatus = TaskRecordStatus.PENDING
    progress: float = 0.0
    result: dict[str, object] = field(default_factory=dict)
    error: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "status": self.status.value,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskRecord":
        return cls(
            title=str(payload["title"]),
            kind=str(payload.get("kind", "general")),
            status=TaskRecordStatus(str(payload.get("status", TaskRecordStatus.PENDING.value))),
            progress=float(payload.get("progress", 0.0)),
            result=dict(payload.get("result", {})),
            error=payload.get("error"),
            id=str(payload["id"]),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
        )
