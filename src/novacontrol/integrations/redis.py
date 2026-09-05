"""Optional Redis integration for caching and pub/sub event distribution.

This module is lazy-loaded. If Redis is not installed or configured,
the in-memory fallback is used transparently.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from novacontrol.core.events import Event, EventHandler


class RedisEventBus:
    """Redis-backed event bus for cross-process event distribution.

    Falls back gracefully if Redis is unavailable.
    """

    def __init__(self, url: str | None = None) -> None:
        self.url = url or os.getenv("NOVACONTROL_REDIS_URL", "redis://localhost:6379/0")
        self._client: Any = None
        self._pubsub: Any = None
        self._handlers: dict[str, list[EventHandler]] = {}
        self._connected = False

    def connect(self) -> bool:
        """Attempt to connect to Redis. Returns True if successful."""
        try:
            import redis as redis_lib
            self._client = redis_lib.from_url(self.url, decode_responses=True)
            self._client.ping()
            self._connected = True
            return True
        except Exception:
            self._connected = False
            return False

    @property
    def is_available(self) -> bool:
        """Check if Redis is connected and available."""
        return self._connected and self._client is not None

    async def publish(self, channel: str, event: Event) -> None:
        """Publish an event to a Redis channel."""
        if not self.is_available:
            return
        payload = json.dumps({
            "type": event.type,
            "payload": dict(event.payload),
            "source": event.source,
            "correlation_id": event.correlation_id,
            "causation_id": event.causation_id,
            "created_at": event.created_at.isoformat(),
        })
        try:
            self._client.publish(channel, payload)
        except Exception:
            pass  # Redis unavailable — silent fallback

    async def subscribe(self, channel: str, handler: EventHandler) -> None:
        """Subscribe a handler to a Redis channel."""
        if channel not in self._handlers:
            self._handlers[channel] = []
        self._handlers[channel].append(handler)

    def close(self) -> None:
        """Close the Redis connection."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._connected = False
        self._client = None


class RedisTtlCache:
    """Redis-backed TTL cache for expensive operations.

    Falls back gracefully if Redis is unavailable.
    """

    def __init__(self, url: str | None = None, prefix: str = "nova:cache:") -> None:
        self.url = url or os.getenv("NOVACONTROL_REDIS_URL", "redis://localhost:6379/0")
        self.prefix = prefix
        self._client: Any = None
        self._connected = False

    def connect(self) -> bool:
        """Attempt to connect to Redis."""
        try:
            import redis as redis_lib
            self._client = redis_lib.from_url(self.url, decode_responses=True)
            self._client.ping()
            self._connected = True
            return True
        except Exception:
            self._connected = False
            return False

    @property
    def is_available(self) -> bool:
        return self._connected and self._client is not None

    def get(self, key: str) -> Any | None:
        """Get a value from the cache."""
        if not self.is_available:
            return None
        try:
            raw = self._client.get(f"{self.prefix}{key}")
            if raw is None:
                return None
            return json.loads(raw)
        except Exception:
            return None

    def set(self, key: str, value: Any, *, ttl_seconds: float = 300) -> None:
        """Set a value in the cache with TTL."""
        if not self.is_available:
            return
        try:
            self._client.setex(
                f"{self.prefix}{key}",
                int(ttl_seconds),
                json.dumps(value, default=str),
            )
        except Exception:
            pass

    def delete(self, key: str) -> None:
        """Delete a value from the cache."""
        if not self.is_available:
            return
        try:
            self._client.delete(f"{self.prefix}{key}")
        except Exception:
            pass

    def close(self) -> None:
        """Close the Redis connection."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._connected = False
        self._client = None


def build_redis_cache(url: str | None = None) -> RedisTtlCache:
    """Build a Redis cache, connecting if possible."""
    cache = RedisTtlCache(url)
    cache.connect()
    return cache


def build_redis_event_bus(url: str | None = None) -> RedisEventBus:
    """Build a Redis event bus, connecting if possible."""
    bus = RedisEventBus(url)
    bus.connect()
    return bus
