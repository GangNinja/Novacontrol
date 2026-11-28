"""Event-driven Phase 39 portfolio capability module."""

from __future__ import annotations

from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.portfolio.service_facade import PortfolioService


class PortfolioModule:
    """Exposes portfolio capabilities without an autonomous action path."""

    def __init__(self, service: PortfolioService) -> None:
        self.service = service
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "portfolio_manager"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability(
                "portfolio.registry",
                "Manage isolated portfolios, venture references, lifecycle provenance and access.",
            ),
            Capability(
                "portfolio.intelligence",
                "Consolidate actuals, compare ventures, evaluate and rank with evidence coverage.",
            ),
            Capability(
                "portfolio.governance",
                "Assess risk, recommend allocations, simulate scenarios and request human decisions.",
            ),
            Capability(
                "portfolio.monitoring",
                "Refresh stale-aware alerts and generate frozen periodic portfolio reports.",
            ),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe("portfolio.status_requested", self._handle_status)
        await event_bus.subscribe("portfolio.monitor_requested", self._handle_monitor)

    async def stop(self) -> None:
        self._event_bus = None

    async def _handle_status(self, event: Event) -> None:
        if self._event_bus is None:
            return
        await self._event_bus.publish(
            Event(
                type="portfolio.status",
                payload=self.service.status(),
                source="portfolio_manager",
                correlation_id=event.correlation_id,
                causation_id=event.correlation_id,
            )
        )

    async def _handle_monitor(self, event: Event) -> None:
        portfolio_id = str(event.payload.get("portfolio_id") or "")
        if not portfolio_id or self._event_bus is None:
            return
        try:
            # Monitoring is a trusted runtime job, not a portfolio member
            # mutation. The internal agent actor stays a read-only viewer.
            summary = self.service.refresh_alerts(portfolio_id, system=True)
        except Exception as exc:  # noqa: BLE001 - event subscribers report, never crash the bus
            await self._event_bus.publish(
                Event(
                    type="portfolio.monitor_failed",
                    payload={"portfolio_id": portfolio_id, "reason": str(exc)},
                    source="portfolio_manager",
                    correlation_id=event.correlation_id,
                    causation_id=event.correlation_id,
                )
            )
            return
        await self._event_bus.publish(
            Event(
                type="portfolio.monitoring_completed",
                payload={
                    "portfolio_id": portfolio_id,
                    "open": summary["open"],
                    "created": summary["created"],
                },
                source="portfolio_manager",
                correlation_id=event.correlation_id,
                causation_id=event.correlation_id,
            )
        )


__all__ = ["PortfolioModule"]
