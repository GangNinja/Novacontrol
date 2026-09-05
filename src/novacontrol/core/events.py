"""Event primitives for NovaControl."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from novacontrol.core.errors import EventDeliveryError, HandlerFailure
from novacontrol.core.journal import EventJournal

EventHandler = Callable[["Event"], Awaitable[None]]
EventErrorHandler = Callable[[HandlerFailure], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Event:
    """Immutable event exchanged between NovaControl modules."""

    type: str
    payload: MappingProxyType[str, Any] | dict[str, Any] = field(default_factory=dict)
    source: str = "system"
    correlation_id: str = field(default_factory=lambda: uuid4().hex)
    causation_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.type or not self.type.strip():
            raise ValueError("Event type is required.")
        if not isinstance(self.payload, MappingProxyType):
            object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


class EventBus:
    """Small async event bus used by modules to communicate without direct imports."""

    def __init__(
        self,
        *,
        journal: EventJournal | None = None,
        error_handler: EventErrorHandler | None = None,
        continue_on_error: bool = False,
    ) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._wildcard_handlers: list[EventHandler] = []
        self._lock = asyncio.Lock()
        self._journal = journal
        self._error_handler = error_handler
        self._continue_on_error = continue_on_error

    async def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Subscribe a handler to an event type, or `*` for all events."""
        if not event_type or not event_type.strip():
            raise ValueError("event_type is required.")
        async with self._lock:
            if event_type == "*":
                self._wildcard_handlers.append(handler)
            else:
                self._handlers[event_type].append(handler)

    async def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """Remove a handler previously registered via subscribe(). No-op when absent.

        Needed for short-lived subscribers such as SSE channels: a dropped client
        must not leave its forwarder invoked on every publish forever.
        """
        async with self._lock:
            if event_type == "*":
                if handler in self._wildcard_handlers:
                    self._wildcard_handlers.remove(handler)
                return
            bucket = self._handlers.get(event_type)
            if bucket is None:
                return
            if handler in bucket:
                bucket.remove(handler)
            if not bucket:
                del self._handlers[event_type]

    async def publish(self, event: Event) -> None:
        """Publish an event to exact-match and wildcard subscribers."""
        if self._journal is not None:
            await self._journal.append(event)

        async with self._lock:
            handlers = [
                *self._handlers.get(event.type, ()),
                *self._wildcard_handlers,
            ]

        failures: list[HandlerFailure] = []
        for handler in handlers:
            try:
                await handler(event)
            except Exception as exc:
                failure = HandlerFailure(event=event, handler=repr(handler), error=exc)
                failures.append(failure)
                if self._error_handler is not None:
                    await self._error_handler(failure)
                if not self._continue_on_error:
                    raise EventDeliveryError(event, failures) from exc

        if failures and not self._continue_on_error:
            raise EventDeliveryError(event, failures)

    async def clear(self) -> None:
        """Remove all subscriptions."""
        async with self._lock:
            self._handlers.clear()
            self._wildcard_handlers.clear()
