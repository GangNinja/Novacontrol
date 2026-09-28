"""Event-driven knowledge module (Phase 11 on the bus).

The engine is useful as a library, but the rest of this build talks in events:
a module subscribes to what it can answer and publishes what it did. This
module is that seam for knowledge, and it follows the house rules — one
``name``, a declared ``capabilities`` tuple, ``start``/``stop``, and a
correlated reply for every request.

The requests are deliberately narrow (search, ingest, project context) because
those are the three things another module could want without knowing anything
about an index: "what do we know about X", "read this in", "where am I". The
typed ``knowledge.indexed``/``knowledge.retrieved`` events are published by the
manager itself, so a watcher sees the same vocabulary whether a tool, the CLI
or an event drove the work.
"""

from __future__ import annotations

from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.knowledge.manager import KnowledgeManager
from novacontrol.knowledge.models import positive_int


class KnowledgeModule:
    """Runtime module for local knowledge and project awareness."""

    def __init__(self, manager: KnowledgeManager | None = None) -> None:
        self.manager = manager or KnowledgeManager()
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "knowledge"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability("knowledge.search", "Search the local knowledge index."),
            Capability("knowledge.ingest", "Read documents into the local index."),
            Capability("knowledge.context", "Assemble budgeted context for a question."),
            Capability("project.detect", "Detect the active code project."),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self.manager.attach_events(event_bus)
        await event_bus.subscribe("knowledge.search_requested", self._handle_search)
        await event_bus.subscribe("knowledge.ingest_requested", self._handle_ingest)
        await event_bus.subscribe("knowledge.context_requested", self._handle_context)
        await event_bus.subscribe("project.detect_requested", self._handle_project)

    async def stop(self) -> None:
        self._event_bus = None
        self.manager.attach_events(None)

    async def _handle_search(self, event: Event) -> None:
        query = str(event.payload.get("query", ""))
        limit = positive_int(event.payload.get("limit"), default=8)
        context = await self.manager.retrieve(query, limit=limit)
        await self._publish("knowledge.search_completed", context.to_dict(), event)

    async def _handle_ingest(self, event: Event) -> None:
        path = str(event.payload.get("path", ""))
        project = str(event.payload.get("project", "")) or None
        force = bool(event.payload.get("force", False))
        report = await self.manager.ingest_path(path, project=project, force=force)
        await self._publish("knowledge.ingest_completed", report.to_dict(), event)

    async def _handle_context(self, event: Event) -> None:
        question = str(event.payload.get("question", ""))
        task = str(event.payload.get("active_task", ""))
        budget = positive_int(event.payload.get("budget_tokens"), default=0) or None
        context = await self.manager.context_for(
            question, budget_tokens=budget, active_task=task
        )
        await self._publish("knowledge.context_completed", context.to_dict(), event)

    async def _handle_project(self, event: Event) -> None:
        path = str(event.payload.get("path", ""))
        context = self.manager.set_project(path) if path else self.manager.project_context()
        await self._publish("project.detected", context.to_dict(), event)

    async def _publish(
        self, event_type: str, payload: dict[str, object], source_event: Event
    ) -> None:
        if self._event_bus is None:
            return
        await self._event_bus.publish(
            Event(
                type=event_type,
                payload=payload,
                source="knowledge",
                correlation_id=source_event.correlation_id,
                causation_id=source_event.correlation_id,
            )
        )


__all__ = ["KnowledgeModule"]
