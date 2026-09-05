"""Durable event journal implementations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
from threading import RLock
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EventJournal(Protocol):
    async def append(self, event: Any) -> None:
        """Persist an event."""

    async def read(self, limit: int | None = None) -> Sequence[Any]:
        """Read persisted events, oldest first."""


@dataclass(frozen=True, slots=True)
class JournalRecord:
    type: str
    payload: dict[str, Any]
    source: str
    correlation_id: str
    causation_id: str | None
    created_at: str


class InMemoryEventJournal:
    """In-process journal for tests and embedded development."""

    def __init__(self) -> None:
        self._events: list[Any] = []

    async def append(self, event: Any) -> None:
        self._events.append(event)

    async def read(self, limit: int | None = None) -> Sequence[Any]:
        if limit is None:
            return tuple(self._events)
        return tuple(self._events[-limit:])


class JsonlEventJournal:
    """JSON Lines journal suitable for local durable development runs."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = RLock()

    async def append(self, event: Any) -> None:
        record = serialize_event(event)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(record), sort_keys=True, default=str))
                handle.write("\n")

    async def read(self, limit: int | None = None) -> Sequence[Any]:
        if not self.path.exists():
            return ()
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        selected = lines if limit is None else lines[-limit:]
        return tuple(deserialize_event(json.loads(line)) for line in selected if line.strip())


def serialize_event(event: Any) -> JournalRecord:
    return JournalRecord(
        type=event.type,
        payload=dict(event.payload),
        source=event.source,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        created_at=event.created_at.isoformat(),
    )


def deserialize_event(data: dict[str, Any]) -> Any:
    from novacontrol.core.events import Event

    return Event(
        type=data["type"],
        payload=data.get("payload", {}),
        source=data.get("source", "system"),
        correlation_id=data["correlation_id"],
        causation_id=data.get("causation_id"),
        created_at=datetime.fromisoformat(data["created_at"]),
    )
