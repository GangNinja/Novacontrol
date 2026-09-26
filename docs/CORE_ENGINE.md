# Core Engine

The Phase 2 core engine provides the runtime substrate for NovaControl modules.

## Services

- `EventBus`: async event publication, exact subscriptions, wildcard subscriptions, optional event journal, and handler error policy
- `EventDrivenRuntime`: module registration, lifecycle start/stop, retry-aware startup, diagnostics, and runtime health
- `ServiceContainer`: explicit service registry for core adapters
- `DiagnosticsRegistry`: health check registration and aggregation
- `RetryPolicy`: bounded exponential retry helper for runtime operations
- `JsonlEventJournal`: local durable event persistence
- `InMemoryEventJournal`: test and embedded journal

## Event Durability

The event bus can be configured with an `EventJournal`. The JSONL journal writes each event as one line with:

- event type
- payload
- source
- correlation id
- causation id
- creation timestamp

This gives local development a durable event trail while leaving room for PostgreSQL, Redis Streams, Kafka, or cloud-native event stores later.

## Typed Lifecycle Events (staged phase 9)

The bus also carries the typed lifecycle vocabulary declared in `novacontrol.core.events.EventType`:

- `intent.detected`, `context.resolved`, `decision.created`, `plan.created`
- `task.started`, `task.paused`, `task.resumed`, `task.cancelled`, `task.completed`, `task.failed`
- `tool.selected`, `tool.started`, `tool.completed`, `tool.failed`
- `verification.started`, `verification.completed`
- `recovery.started`, `recovery.completed`
- `model.loaded`, `model.unloaded`
- `vision.started`, `vision.completed`

Each name has a payload schema in `EVENT_PAYLOAD_FIELDS`, and `Event.__post_init__` validates the payload against it — a misspelled or incomplete event raises `InvalidEventPayloadError` where it is published rather than confusing a subscriber later. The reserved envelope kwargs (`source`, `correlation_id`, `causation_id`) belong to the envelope: a payload key must never shadow one. `Event.of`/`Event.child` keep the request's `correlation_id` on nested work (the application holds the request being served in a context variable), the bus keeps a bounded in-memory history readable through `recent()`, and `EventBus.emit` is the publish door that cannot raise — a subscriber failure is logged and never breaks the request that published. Components receive one injected observer or sink instead of importing the bus, so the publisher does not have to know the bus exists.

## Lifecycle Behavior

Runtime startup is retry-aware. If a module fails to start, the runtime publishes `runtime.module_start_failed`, stops modules that already started, and raises a `ModuleLifecycleError`.

Runtime stop proceeds in reverse registration order and publishes `runtime.module_stop_failed` for modules that fail during shutdown.

## Diagnostics

Health checks are intentionally small and composable. The runtime registers `core.runtime` by default, and modules can register additional checks through the shared `DiagnosticsRegistry`.
