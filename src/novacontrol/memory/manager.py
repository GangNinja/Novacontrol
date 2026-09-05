"""High-level memory manager and runtime module."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.memory.models import MemoryNamespace, MemoryQuery, MemoryRecord, MemorySearchResult
from novacontrol.memory.stores import MemoryRepository
from novacontrol.memory.summarization import ExtractiveMemorySummarizer, MemorySummarizer


class MemoryManager:
    """Coordinates writing, retrieving, summarizing, and cleaning memory."""

    def __init__(
        self,
        repository: MemoryRepository,
        *,
        summarizer: MemorySummarizer | None = None,
    ) -> None:
        self.repository = repository
        self.summarizer = summarizer or ExtractiveMemorySummarizer()

    async def remember(
        self,
        namespace: MemoryNamespace | str,
        key: str,
        value: Mapping[str, Any],
        *,
        text: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        importance: float = 0.0,
        ttl_seconds: int | None = None,
    ) -> MemoryRecord:
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=ttl_seconds)
            if ttl_seconds is not None
            else None
        )
        record = MemoryRecord.create(
            namespace=str(namespace),
            key=key,
            value=dict(value),
            text=text or _derive_text(value),
            metadata=dict(metadata or {}),
            importance=importance,
            expires_at=expires_at,
        )
        await self.repository.put_record(record)
        return record

    async def recall(self, namespace: MemoryNamespace | str, key: str) -> MemoryRecord | None:
        return await self.repository.get_record(str(namespace), key)

    async def retrieve(
        self,
        namespace: MemoryNamespace | str,
        query: str,
        *,
        limit: int = 10,
    ) -> Sequence[MemorySearchResult]:
        return await self.repository.search_records(MemoryQuery(str(namespace), query, limit))

    async def summarize(
        self,
        namespace: MemoryNamespace | str,
        query: str = "",
        *,
        limit: int = 20,
    ) -> str:
        results = await self.retrieve(namespace, query, limit=limit)
        return await self.summarizer.summarize(tuple(result.record for result in results))

    async def cleanup(self) -> int:
        return await self.repository.cleanup_expired()


class MemoryModule:
    """Event-driven runtime module for the memory subsystem."""

    def __init__(self, manager: MemoryManager) -> None:
        self.manager = manager
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "memory"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability("memory.write", "Store memory records."),
            Capability("memory.read", "Retrieve memory records."),
            Capability("memory.search", "Search memory records."),
            Capability("memory.cleanup", "Remove expired memory records."),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe("memory.remember", self._handle_remember)
        await event_bus.subscribe("memory.cleanup_requested", self._handle_cleanup)

    async def stop(self) -> None:
        self._event_bus = None

    async def _handle_remember(self, event: Event) -> None:
        payload = event.payload
        record = await self.manager.remember(
            payload.get("namespace", MemoryNamespace.LONG_TERM),
            str(payload["key"]),
            _mapping(payload.get("value", {})),
            text=payload.get("text"),
            metadata=_mapping(payload.get("metadata", {})),
            importance=float(payload.get("importance", 0.0)),
            ttl_seconds=payload.get("ttl_seconds"),
        )
        await self._publish(
            Event(
                type="memory.stored",
                payload={
                    "namespace": record.namespace,
                    "key": record.key,
                    "id": record.id,
                },
                source="memory",
                correlation_id=event.correlation_id,
                causation_id=event.correlation_id,
            )
        )

    async def _handle_cleanup(self, event: Event) -> None:
        removed = await self.manager.cleanup()
        await self._publish(
            Event(
                type="memory.cleanup_completed",
                payload={"removed": removed},
                source="memory",
                correlation_id=event.correlation_id,
                causation_id=event.correlation_id,
            )
        )

    async def _publish(self, event: Event) -> None:
        if self._event_bus is not None:
            await self._event_bus.publish(event)


def _derive_text(value: Mapping[str, Any]) -> str:
    return " ".join(str(item) for item in value.values())


def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Expected mapping, got {type(value).__name__}")
