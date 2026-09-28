"""Event-driven plugin marketplace module."""

from __future__ import annotations

from novacontrol.core.events import Event, EventBus, EventType
from novacontrol.core.interfaces import Capability
from novacontrol.plugins.manager import PluginMarketplace
from novacontrol.plugins.models import PluginInstallRecord, PluginManifest


class PluginMarketplaceModule:
    """Runtime module for plugin discovery and lifecycle events."""

    def __init__(self, marketplace: PluginMarketplace | None = None) -> None:
        self.marketplace = marketplace or PluginMarketplace()
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "plugins"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability("plugins.discover", "Discover plugin manifests."),
            Capability("plugins.install", "Install plugins with permission checks."),
            Capability("plugins.enable", "Enable trusted plugins."),
            Capability("plugins.disable", "Disable plugins."),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe("plugin.discover_requested", self._handle_discover)
        await event_bus.subscribe("plugin.install_requested", self._handle_install)
        await event_bus.subscribe("plugin.enable_requested", self._handle_enable)
        await event_bus.subscribe("plugin.disable_requested", self._handle_disable)

    async def stop(self) -> None:
        self._event_bus = None

    async def _handle_discover(self, event: Event) -> None:
        manifests = await self.marketplace.discover(str(event.payload["directory"]))
        await self._publish(
            "plugin.discovery_completed",
            {"plugins": [manifest.to_dict() for manifest in manifests]},
            event,
        )

    async def _handle_install(self, event: Event) -> None:
        manifest = PluginManifest.from_mapping(dict(event.payload["manifest"]))
        record = await self.marketplace.install(manifest)
        await self._publish("plugin.install_completed", record.to_dict(), event)

    async def _handle_enable(self, event: Event) -> None:
        record = await self.marketplace.enable(str(event.payload["plugin_name"]))
        await self._publish(
            EventType.PLUGIN_ENABLED,
            _lifecycle_payload(record),
            event,
        )

    async def _handle_disable(self, event: Event) -> None:
        record = await self.marketplace.disable(str(event.payload["plugin_name"]))
        await self._publish(
            EventType.PLUGIN_DISABLED,
            _lifecycle_payload(record),
            event,
        )

    async def _publish(
        self, event_type: str, payload: dict[str, object], source_event: Event
    ) -> None:
        if self._event_bus is not None:
            await self._event_bus.publish(
                Event(
                    type=event_type,
                    payload=payload,
                    source="plugins",
                    correlation_id=source_event.correlation_id,
                    causation_id=source_event.correlation_id,
                )
            )


def _lifecycle_payload(record: PluginInstallRecord) -> dict[str, object]:
    """The shared shape of a plugin lifecycle event, marketplace or SDK.

    ``plugin_id`` is the field the typed vocabulary requires and the one a
    watcher keys on; the installed record travels alongside it for a subscriber
    that wants the install-time facts (trust, approval, timestamps). The SDK
    publishes the same field names for the same events, so one subscriber reads
    both publishers without a second code path.
    """
    return {
        "plugin_id": record.manifest.plugin_id,
        "name": record.manifest.name,
        "version": record.manifest.version,
        "status": record.status.value,
        "record": record.to_dict(),
    }
