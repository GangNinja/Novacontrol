"""NovaMarket as a NovaControl runtime module.

The module is the thin adapter between the application's runtime and
:class:`~novacontrol.novamarket.service.NovaMarketService`. It exists so
NovaMarket participates in the SAME lifecycle as every other subsystem:

* ``EventDrivenRuntime.register_module`` registers it before start;
* ``start(event_bus)`` hands it the ONE application bus and asks it to auto-start;
* ``stop()`` asks it to shut down, and the runtime stops it in reverse
  registration order with every other module.

``start`` does not propagate an initialisation failure. A financial subsystem
that is misconfigured must leave itself in ERROR and let NovaControl boot — the
status surface explains the failure — because a feature module failing is not a
reason for the whole application to fail. Shutdown is likewise isolated: a stop
that raises is reported by the runtime, not turned into a crash here.
"""

from __future__ import annotations

from novacontrol.core.events import EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.novamarket.service import NovaMarketService


class NovaMarketModule:
    """Runtime module for the NovaMarket financial-intelligence subsystem."""

    def __init__(self, service: NovaMarketService) -> None:
        self.service = service

    @property
    def name(self) -> str:
        return self.service.name

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return self.service.capabilities()

    async def start(self, event_bus: EventBus) -> None:
        self.service.attach_event_bus(event_bus)
        # Deliberately no raise: auto_start records ERROR and status explains it.
        await self.service.auto_start()

    async def stop(self) -> None:
        await self.service.shutdown(reason="runtime module stopped")

    def status(self) -> dict[str, object]:
        return self.service.status()


__all__ = ["NovaMarketModule"]
