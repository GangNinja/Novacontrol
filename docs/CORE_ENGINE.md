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

## Lifecycle Behavior

Runtime startup is retry-aware. If a module fails to start, the runtime publishes `runtime.module_start_failed`, stops modules that already started, and raises a `ModuleLifecycleError`.

Runtime stop proceeds in reverse registration order and publishes `runtime.module_stop_failed` for modules that fail during shutdown.

## Diagnostics

Health checks are intentionally small and composable. The runtime registers `core.runtime` by default, and modules can register additional checks through the shared `DiagnosticsRegistry`.
