"""Event-driven capability module for Phase 37 customer growth."""

from __future__ import annotations

from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.customer.service import CustomerService


class CustomerModule:
    """Exposes customer-growth capabilities with no mutation event path."""

    def __init__(self, service: CustomerService) -> None:
        self.service = service
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "customer_growth"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability(
                "customer.lifecycle",
                "Track segments, ICPs, customers, leads and interactions for launch-authorized products.",
            ),
            Capability(
                "customer.sales",
                "Record an explainable sales pipeline, deal estimates and proposal history.",
            ),
            Capability(
                "customer.support",
                "Manage support tickets and ground assistance in approved product knowledge.",
            ),
            Capability(
                "customer.retention",
                "Record onboarding, feedback, success indicators and analytics without fabricated metrics.",
            ),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe("customer.status_requested", self._handle_status)

    async def stop(self) -> None:
        self._event_bus = None

    async def _handle_status(self, event: Event) -> None:
        status = self.service.status()
        if self._event_bus is not None:
            await self._event_bus.publish(
                Event(
                    type="customer.status",
                    payload=status,
                    source="customer_growth",
                    correlation_id=event.correlation_id,
                    causation_id=event.correlation_id,
                )
            )


__all__ = ["CustomerModule"]
