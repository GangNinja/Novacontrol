"""Event-driven capability module for Phase 38 business operations and finance."""

from __future__ import annotations

from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.finops.service import FinOpsService


class FinanceModule:
    """Exposes finance capabilities with no autonomous mutation event path."""

    def __init__(self, service: FinOpsService) -> None:
        self.service = service
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "business_finance"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability(
                "finance.ventures",
                "Manage isolated ventures, operating status, objectives, tasks and financial periods.",
            ),
            Capability(
                "finance.ledger",
                "Post balanced double-entry journal entries, reverse them and reconcile accounts.",
            ),
            Capability(
                "finance.planning",
                "Approve budgets and produce assumption-explicit forecasts and scenarios.",
            ),
            Capability(
                "finance.intelligence",
                "Analyse profitability, cash, working capital and business health without executing payments.",
            ),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe("finance.status_requested", self._handle_status)

    async def stop(self) -> None:
        self._event_bus = None

    async def _handle_status(self, event: Event) -> None:
        payload = self.service.status()
        if self._event_bus is not None:
            await self._event_bus.publish(
                Event(
                    type="finance.status",
                    payload=payload,
                    source="business_finance",
                    correlation_id=event.correlation_id,
                    causation_id=event.correlation_id,
                )
            )


__all__ = ["FinanceModule"]
