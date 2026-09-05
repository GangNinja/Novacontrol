"""Tests for plugin marketplace: install, trust, enable, events."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novacontrol.core.events import Event, EventBus
from novacontrol.core.security import PermissionScope
from novacontrol.plugins import (
    PluginCapability,
    PluginManifest,
    PluginMarketplace,
    PluginMarketplaceModule,
    PluginStatus,
)
from conftest import AllowGateway, collect_events


def sample_manifest() -> PluginManifest:
    return PluginManifest(
        name="sample",
        version="1.0.0",
        description="Sample plugin",
        permissions=(PermissionScope.NETWORK_ACCESS.value,),
        capabilities=(PluginCapability("sample-tool", "tool", "sample:tool"),),
    )


class PluginTests(unittest.IsolatedAsyncioTestCase):

    async def test_manifest_loads_from_mapping(self) -> None:
        manifest = PluginManifest.from_mapping({
            "name": "demo", "version": "1.2.3",
            "capabilities": [{"name": "agent", "type": "agent"}],
        })
        self.assertEqual(manifest.name, "demo")
        self.assertEqual(manifest.capabilities[0].type, "agent")

    async def test_discover_reads_plugin_json_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            plugin_dir = Path(temp_dir) / "demo"
            plugin_dir.mkdir()
            (plugin_dir / "plugin.json").write_text(
                json.dumps({"name": "demo", "version": "1.0.0"}), encoding="utf-8",
            )
            manifests = await PluginMarketplace().discover(temp_dir)
            self.assertEqual(manifests[0].name, "demo")

    async def test_install_denies_sensitive_plugin_by_default(self) -> None:
        record = await PluginMarketplace().install(sample_manifest())
        self.assertEqual(record.status, PluginStatus.DENIED)

    async def test_install_trust_and_enable(self) -> None:
        marketplace = PluginMarketplace(approval_gateway=AllowGateway())
        installed = await marketplace.install(sample_manifest())
        trusted = await marketplace.trust(installed.manifest.name)
        enabled = await marketplace.enable(trusted.manifest.name)
        self.assertEqual(installed.status, PluginStatus.INSTALLED)
        self.assertTrue(trusted.trusted)
        self.assertEqual(enabled.status, PluginStatus.ENABLED)

    async def test_module_emits_install_event(self) -> None:
        bus = EventBus()
        module = PluginMarketplaceModule(PluginMarketplace(approval_gateway=AllowGateway()))
        seen = await collect_events(
            bus, "plugin.install_completed",
            "plugin.install_requested",
            {"manifest": sample_manifest().to_dict()},
            start_fn=module.start,
        )
        self.assertEqual(seen[0].payload["manifest"]["name"], "sample")


if __name__ == "__main__":
    unittest.main()
