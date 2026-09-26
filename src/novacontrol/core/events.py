"""Event primitives for NovaControl.

Phase 9.1 added the typed vocabulary the rest of the system publishes its
lifecycle through — :class:`EventType` — and the three things an event bus needs
before a system can be built on it:

  * **typed payloads.** Each lifecycle event declares the fields it must carry,
    and :meth:`EventBus.publish` refuses an event that is missing one, so a
    subscriber never has to guess whether ``event.payload["tool"]`` exists.
  * **one handler cannot take the others down.** Every subscriber of an event
    runs even when an earlier one raised; the failures are logged, collected and
    reported in a :class:`PublishResult`, and the bus only *raises* about them
    when it was asked to (``continue_on_error=False``, the strict mode tests use).
  * **optional history.** A bounded in-memory ring of the most recent events,
    for "what just happened?" without a journal (the durable record is still
    :class:`~novacontrol.core.journal.EventJournal`).

Custom event types continue to work untouched: the vocabulary is a naming and
validation layer over the same bus, not a gate in front of it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from inspect import isawaitable
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from novacontrol.core.errors import EventDeliveryError, HandlerFailure
from novacontrol.core.journal import EventJournal

#: A handler may be async, or a plain function (an observer that only records).
EventHandler = Callable[["Event"], Awaitable[None] | None]
EventErrorHandler = Callable[[HandlerFailure], Awaitable[None]]

_logger = logging.getLogger(__name__)


class EventType(StrEnum):
    """The lifecycle vocabulary — what happens inside NovaControl, by name.

    These are the events a UI, a journal or a test subscribes to. Names are
    ``<subject>.<what happened>`` and every one of them is published from the
    live path (see ``application.py``), because a vocabulary nobody publishes
    is a documentation exercise rather than an event system.
    """

    # -- understanding a request ----------------------------------------------
    INTENT_DETECTED = "intent.detected"
    CONTEXT_RESOLVED = "context.resolved"
    DECISION_CREATED = "decision.created"
    PLAN_CREATED = "plan.created"
    # -- a task's life ---------------------------------------------------------
    TASK_STARTED = "task.started"
    TASK_PAUSED = "task.paused"
    TASK_RESUMED = "task.resumed"
    TASK_CANCELLED = "task.cancelled"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    # -- tools -----------------------------------------------------------------
    TOOL_SELECTED = "tool.selected"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    # -- checking and recovering ----------------------------------------------
    VERIFICATION_STARTED = "verification.started"
    VERIFICATION_COMPLETED = "verification.completed"
    RECOVERY_STARTED = "recovery.started"
    RECOVERY_COMPLETED = "recovery.completed"
    # -- models and senses -----------------------------------------------------
    MODEL_LOADED = "model.loaded"
    MODEL_UNLOADED = "model.unloaded"
    VISION_STARTED = "vision.started"
    VISION_COMPLETED = "vision.completed"


#: The payload fields each lifecycle event MUST carry. Checked when an event of
#: a known type is published, so "the handler will find it" is a guarantee rather
#: than a convention. A custom event type has no schema and is not inspected.
EVENT_PAYLOAD_FIELDS: Mapping[EventType, tuple[str, ...]] = {
    EventType.INTENT_DETECTED: ("intent",),
    EventType.CONTEXT_RESOLVED: ("strategy",),
    EventType.DECISION_CREATED: ("route", "decision_type"),
    EventType.PLAN_CREATED: ("goal", "steps"),
    EventType.TASK_STARTED: ("task_id",),
    EventType.TASK_PAUSED: ("task_id",),
    EventType.TASK_RESUMED: ("task_id",),
    EventType.TASK_CANCELLED: ("task_id",),
    EventType.TASK_COMPLETED: ("task_id",),
    EventType.TASK_FAILED: ("task_id",),
    EventType.TOOL_SELECTED: ("tool",),
    EventType.TOOL_STARTED: ("tool",),
    EventType.TOOL_COMPLETED: ("tool", "status"),
    EventType.TOOL_FAILED: ("tool", "error"),
    EventType.VERIFICATION_STARTED: ("step_id",),
    EventType.VERIFICATION_COMPLETED: ("step_id", "status"),
    EventType.RECOVERY_STARTED: ("step_id",),
    EventType.RECOVERY_COMPLETED: ("step_id", "outcome"),
    EventType.MODEL_LOADED: ("model",),
    EventType.MODEL_UNLOADED: ("model",),
    # The image being read is ``image``, NOT ``source``: every event already
    # carries a ``source`` (the publisher's own name), and a payload field that
    # shadows it could never be set through ``emit``.
    EventType.VISION_STARTED: ("image",),
    EventType.VISION_COMPLETED: ("image", "answered"),
}

#: Which event a Phase 8 task state publishes when it is entered. Kept here as
#: strings so the event layer does not import the reliability layer (which
#: imports this one's siblings) and neither has to know the other's types.
TASK_STATE_EVENTS: Mapping[str, EventType] = {
    "running": EventType.TASK_STARTED,
    "paused": EventType.TASK_PAUSED,
    "cancelled": EventType.TASK_CANCELLED,
    "completed": EventType.TASK_COMPLETED,
    "failed": EventType.TASK_FAILED,
    "resumed": EventType.TASK_RESUMED,
}


def task_event_name(state: str) -> EventType | None:
    """The lifecycle event for a task state, or None for a state with no event.

    Only the states that mean something to a watcher are named: RUNNING means
    work began or resumed, and the planning/verifying/recovering states are
    steps *within* a run rather than lifecycle moments of their own.
    """
    return TASK_STATE_EVENTS.get(str(state).strip().lower())


class InvalidEventPayloadError(ValueError):
    """A lifecycle event was published without the fields its type promises."""

    def __init__(self, type_: str, missing: Iterable[str]) -> None:
        self.type = type_
        self.missing = tuple(missing)
        super().__init__(
            f"Event {type_!r} is missing required payload field(s): "
            + ", ".join(self.missing)
        )


@dataclass(frozen=True, slots=True)
class PublishResult:
    """What one publish did: who heard it, and who failed.

    Returned rather than only logged, because "the event was published" and
    "the subscribers handled it" are different facts and a caller that cares can
    now tell them apart.
    """

    event_type: str
    delivered: int
    failures: tuple[HandlerFailure, ...] = ()

    @property
    def failed(self) -> int:
        return len(self.failures)

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "delivered": self.delivered,
            "failed": self.failed,
            "ok": self.ok,
            "failures": [failure.to_dict() for failure in self.failures],
        }


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
        validate_payload(self.type, self.payload)

    @classmethod
    def of(
        cls,
        type_: EventType | str,
        /,
        *,
        source: str = "system",
        correlation_id: str = "",
        causation_id: str | None = None,
        **payload: Any,
    ) -> Event:
        """Build a typed event, refusing one that is missing what it promises."""
        return cls(
            type=str(type_),
            payload=dict(payload),
            source=source,
            correlation_id=correlation_id or uuid4().hex,
            causation_id=causation_id,
        )

    def child(self, type_: EventType | str, /, **payload: Any) -> Event:
        """A follow-on event caused by this one, keeping the correlation.

        What ties a whole request together — an intent, the decision about it,
        the tools it ran — without every publisher having to thread an id around.
        """
        return Event.of(
            type_,
            source=self.source,
            correlation_id=self.correlation_id,
            causation_id=self.correlation_id,
            **payload,
        )


def validate_payload(type_: str, payload: Mapping[str, Any]) -> None:
    """Check a known event type's required fields; ignore custom types.

    A type this build does not know is left alone on purpose: the vocabulary
    describes the lifecycle events, and a module is free to publish its own
    (``command.progress``, ``activity.completed``) without registering here.
    """
    known = _KNOWN_TYPES.get(str(type_))
    if known is None:
        return
    missing = [name for name in known if name not in payload]
    if missing:
        raise InvalidEventPayloadError(str(type_), missing)


_KNOWN_TYPES: Mapping[str, tuple[str, ...]] = {
    member.value: EVENT_PAYLOAD_FIELDS[member] for member in EventType
}


class EventBus:
    """Small async event bus used by modules to communicate without direct imports.

    Handlers may be coroutines or plain callables, and they are all invoked even
    when one of them fails: an event that a second subscriber never saw because
    a first subscriber raised is the silent coupling this bus exists to avoid.
    The failures still surface — logged, handed to the error handler, and
    returned in the :class:`PublishResult` — and in strict mode
    (``continue_on_error=False``, the default) publishing raises
    :class:`~novacontrol.core.errors.EventDeliveryError` once every subscriber
    has had its turn.
    """

    def __init__(
        self,
        *,
        journal: EventJournal | None = None,
        error_handler: EventErrorHandler | None = None,
        continue_on_error: bool = False,
        history: int = 0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._wildcard_handlers: list[EventHandler] = []
        self._lock = asyncio.Lock()
        self._journal = journal
        self._error_handler = error_handler
        self._continue_on_error = continue_on_error
        #: Bounded in-memory history (0 = off). The durable record is the
        #: journal; this is the "what just happened?" ring a UI or a test reads.
        self._history_size = max(0, int(history))
        self._history: deque[Event] = deque(maxlen=self._history_size or None)
        self._logger = logger or _logger

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

    async def publish(self, event: Event) -> PublishResult:
        """Publish an event to exact-match and wildcard subscribers.

        Every subscriber runs. A handler that raises is recorded, logged and
        reported; in strict mode the publish then raises once, after the last
        handler has had its turn.
        """
        validate_payload(event.type, event.payload)
        if self._journal is not None:
            await self._journal.append(event)
        if self._history_size:
            self._history.append(event)

        async with self._lock:
            handlers = [
                *self._handlers.get(event.type, ()),
                *self._wildcard_handlers,
            ]

        failures: list[HandlerFailure] = []
        delivered = 0
        for handler in handlers:
            try:
                produced = handler(event)
                if isawaitable(produced):
                    await produced
            except Exception as exc:
                failure = HandlerFailure(event=event, handler=repr(handler), error=exc)
                failures.append(failure)
                self._logger.warning(
                    "Event handler failed for %s: %s: %s",
                    event.type,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                if self._error_handler is not None:
                    try:
                        handled = self._error_handler(failure)
                        if isawaitable(handled):
                            await handled
                    except Exception as nested:  # noqa: BLE001 - never mask the original
                        self._logger.warning(
                            "Event error handler failed for %s: %s: %s",
                            event.type,
                            type(nested).__name__,
                            nested,
                            exc_info=True,
                        )
            else:
                delivered += 1

        result = PublishResult(
            event_type=event.type, delivered=delivered, failures=tuple(failures)
        )
        if failures and not self._continue_on_error:
            raise EventDeliveryError(event, failures)
        return result

    async def emit(
        self,
        type_: EventType | str,
        /,
        *,
        source: str = "system",
        correlation_id: str = "",
        causation_id: str | None = None,
        **payload: Any,
    ) -> PublishResult:
        """Build and publish a typed event, never raising from a subscriber.

        The door a live system publishes through: one broken watcher must not be
        able to fail the request that announced itself to it. A payload that does
        not match the event's declared fields still raises, because that is a bug
        in the publisher rather than in a subscriber.

        ``source``, ``correlation_id`` and ``causation_id`` belong to the
        envelope and are therefore RESERVED: a payload field with one of those
        names would be a second value for the same argument, and Python refuses
        it. Name the value for what it is instead — an image being read is
        ``image``, a selection's provenance is ``selection_source``.
        """
        event = Event.of(
            type_,
            source=source,
            correlation_id=correlation_id,
            causation_id=causation_id,
            **payload,
        )
        try:
            return await self.publish(event)
        except EventDeliveryError as exc:
            self._logger.warning(
                "Event %s reached a failing handler: %s", event.type, exc
            )
            return PublishResult(
                event_type=event.type,
                delivered=0,
                failures=tuple(exc.failures),
            )

    def recent(
        self,
        *,
        limit: int = 20,
        type_: EventType | str | None = None,
    ) -> tuple[Event, ...]:
        """The most recent events this bus carried, newest last (history off -> empty)."""
        if not self._history_size:
            return ()
        wanted = str(type_) if type_ is not None else None
        kept = [
            event
            for event in self._history
            if wanted is None or event.type == wanted
        ]
        return tuple(kept[-max(0, int(limit)) :]) if limit else tuple(kept)

    def subscriber_count(self, event_type: str | None = None) -> int:
        """How many handlers a type would reach (wildcards included)."""
        if event_type is None:
            return len(self._wildcard_handlers) + sum(
                len(bucket) for bucket in self._handlers.values()
            )
        return len(self._handlers.get(event_type, ())) + len(self._wildcard_handlers)

    def subscribers(self, event_type: str | None = None) -> tuple[str, ...]:
        """Which event types have handlers, in a stable order (for status output)."""
        if event_type is not None:
            return tuple(
                sorted({type_ for type_ in self._handlers if type_ == event_type})
            )
        return tuple(sorted(self._handlers))

    async def clear(self) -> None:
        """Remove all subscriptions (history is kept)."""
        async with self._lock:
            self._handlers.clear()
            self._wildcard_handlers.clear()
