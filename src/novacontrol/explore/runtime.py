"""Event-driven Explore runtime module."""

from __future__ import annotations

from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.explore.models import ExploreRequest
from novacontrol.explore.service import ExploreService


class ExploreModule:
    """Runtime module for online research and learning reports."""

    def __init__(self, service: ExploreService | None = None) -> None:
        self.service = service or ExploreService()
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "explore"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability("explore.research", "Research topics online."),
            Capability("explore.explain", "Create understandable explanations."),
            Capability("explore.videos", "Include related videos."),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe("explore.topic_requested", self._handle_topic_requested)

    async def stop(self) -> None:
        self._event_bus = None

    async def _handle_topic_requested(self, event: Event) -> None:
        payload = event.payload
        request = ExploreRequest(
            topic=str(payload["topic"]),
            depth=str(payload.get("depth", "deep")),
            include_videos=bool(payload.get("include_videos", True)),
            max_sources=int(payload.get("max_sources", 6)),
            max_videos=int(payload.get("max_videos", 5)),
        )
        report = await self.service.research(request)
        if self._event_bus is not None:
            await self._event_bus.publish(
                Event(
                    type="explore.report_created",
                    payload=report.to_dict(),
                    source="explore",
                    correlation_id=event.correlation_id,
                    causation_id=event.correlation_id,
                )
            )
