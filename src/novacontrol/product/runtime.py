"""Event-driven capability module for the Phase 36 product pipeline."""

from __future__ import annotations

from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.product.service import ProductService


class ProductModule:
    """Publishes product status on request and exposes its capabilities.

    The module has no mutation path: approvals and execution are explicit API
    calls, never an event a module can trigger on its own.
    """

    def __init__(self, service: ProductService) -> None:
        self.service = service
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "product_pipeline"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability(
                "product.projects",
                "Track validated products from requirements through launch readiness.",
            ),
            Capability(
                "product.quality",
                "Record test evidence, quality gates and defects without fabricating results.",
            ),
            Capability(
                "product.release",
                "Prepare and approve release artifacts; never deploy externally.",
            ),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe("product.status_requested", self._handle_status)

    async def stop(self) -> None:
        self._event_bus = None

    async def _handle_status(self, event: Event) -> None:
        status = self.service.status()
        if self._event_bus is not None:
            await self._event_bus.publish(
                Event(
                    type="product.status",
                    payload=status,
                    source="product_pipeline",
                    correlation_id=event.correlation_id,
                    causation_id=event.correlation_id,
                )
            )


__all__ = ["ProductModule"]
