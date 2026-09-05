"""Small TTL cache."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CacheEntry(Generic[T]):
    value: T
    expires_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= datetime.now(UTC)


class TtlCache(Generic[T]):
    """In-memory TTL cache for adapters and expensive local work."""

    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry[T]] = {}

    def set(self, key: str, value: T, *, ttl_seconds: float | None = None) -> None:
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=ttl_seconds)
            if ttl_seconds is not None
            else None
        )
        self._entries[key] = CacheEntry(value=value, expires_at=expires_at)

    def get(self, key: str) -> T | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expired():
            del self._entries[key]
            return None
        return entry.value

    def cleanup(self) -> int:
        expired = [key for key, entry in self._entries.items() if entry.expired()]
        for key in expired:
            del self._entries[key]
        return len(expired)

    def size(self) -> int:
        self.cleanup()
        return len(self._entries)
