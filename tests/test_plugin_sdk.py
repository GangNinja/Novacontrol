"""Tests for the Phase 10 plugin SDK: discovery, lifecycle, security, containment.

The tests are named after the promises the SDK makes, because those are the
things a reader should be able to check one by one: that a plugin is found, that
a bad one is refused with a reason, that the hooks run in order exactly once,
that a broken plugin does not take its neighbours down, that what a plugin
declares is what the permission layer is told, and that removing a plugin
removes what it contributed.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from conftest import AllowGateway
from novacontrol.core.events import EventBus, EventType
from novacontrol.core.security import PermissionScope, RiskLevel
from novacontrol.intelligence.intent import CapabilityRegistry
from novacontrol.plugins import (
    Plugin,
    PluginCapability,
    PluginManager,
    PluginStatus,
    PluginValidationError,
    plugin_tool,
)
from novacontrol.plugins.models import PluginConfigurationField
from novacontrol.tools.catalog import ToolCatalog
from novacontrol.tools.models import ToolParameter
from novacontrol.tools.registry import ToolRegistry


class RecorderPlugin(Plugin):
    """A valid plugin that records every hook it is handed, in order."""

    plugin_id = "recorder"
    name = "Recorder"
    version = "1.2.3"
    description = "Records its own lifecycle."
    author = "tests"
    configuration_schema = (
        PluginConfigurationField(name="greeting", default="hi"),
        PluginConfigurationField(name="retries", type="integer", default=2),
    )
    capabilities = (
        PluginCapability(
            name="recorder.echo",
            type="tool",
            entrypoint="recorder:echo",
            description="Echo the text back.",
        ),
    )

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.context = None
        self.tools = (
            plugin_tool(
                "recorder_echo",
                "Echo the text back.",
                self._echo,
                parameters=(ToolParameter(name="text", required=True),),
            ),
        )

    async def load(self, context) -> None:
        self.calls.append("load")
        self.context = context

    async def initialize(self) -> None:
        self.calls.append("initialize")

    async def enable(self) -> None:
        self.calls.append("enable")

    async def disable(self) -> None:
        self.calls.append("disable")

    async def shutdown(self) -> None:
        self.calls.append("shutdown")

    async def _echo(self, arguments):
        return {"text": str(arguments.get("text", ""))}


class SensitivePlugin(Plugin):
    """High risk, reaches the network: the policy must ask before this runs."""

    plugin_id = "sensitive"
    name = "Sensitive"
    version = "1.0.0"
    description = "Needs a person's approval."
    permissions = (PermissionScope.NETWORK_ACCESS,)
    risk_level = RiskLevel.HIGH
    network_access = True
    external_services = ("api.example.com",)

    def __init__(self) -> None:
        self.enabled_called = False
        self.tools = (
            plugin_tool(
                "sensitive_fetch",
                "Fetch something far away.",
                self._fetch,
                required_permissions=(PermissionScope.NETWORK_ACCESS,),
            ),
        )

    async def enable(self) -> None:
        self.enabled_called = True

    async def _fetch(self, arguments):
        return {"ok": True}


class BrokenEnablePlugin(Plugin):
    plugin_id = "broken-enable"
    name = "Broken Enable"
    version = "1.0.0"
    description = "Raises when switched on."

    async def enable(self) -> None:
        raise RuntimeError("enable exploded")


class BrokenLoadPlugin(Plugin):
    plugin_id = "broken-load"
    name = "Broken Load"
    version = "1.0.0"
    description = "Raises while loading."

    async def load(self, context) -> None:
        raise ValueError("load exploded")


class BrokenShutdownPlugin(Plugin):
    plugin_id = "broken-shutdown"
    name = "Broken Shutdown"
    version = "1.0.0"
    description = "Raises while being taken out of service."

    async def shutdown(self) -> None:
        raise RuntimeError("shutdown exploded")


async def _events_of(bus: EventBus, event_type: EventType):
    return bus.recent(type_=event_type)


class PluginSdkTests(unittest.IsolatedAsyncioTestCase):

    # -- discovery -------------------------------------------------------------

    async def test_discovery_finds_a_plugin_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            plugin_dir = Path(temp_dir) / "demo"
            plugin_dir.mkdir()
            (plugin_dir / "plugin.py").write_text(_GOOD_PLUGIN, encoding="utf-8")
            records = await PluginManager().discover(temp_dir)
        self.assertEqual([record.plugin_id for record in records], ["discovered-demo"])
        self.assertEqual(records[0].status, PluginStatus.DISCOVERED)

    async def test_discovery_reports_a_broken_directory_and_keeps_going(self) -> None:
        with TemporaryDirectory() as temp_dir:
            good = Path(temp_dir) / "good"
            good.mkdir()
            (good / "plugin.py").write_text(_GOOD_PLUGIN, encoding="utf-8")
            bad = Path(temp_dir) / "bad"
            bad.mkdir()
            (bad / "plugin.py").write_text("this is not valid python !!\n", encoding="utf-8")
            records = await PluginManager().discover(temp_dir)
        by_id = {record.plugin_id: record for record in records}
        self.assertEqual(by_id["discovered-demo"].status, PluginStatus.DISCOVERED)
        broken = by_id["bad"]
        self.assertEqual(broken.status, PluginStatus.FAILED)
        self.assertIn("could not be loaded", broken.error)

    async def test_discovery_refuses_code_that_outgrows_its_manifest(self) -> None:
        with TemporaryDirectory() as temp_dir:
            plugin_dir = Path(temp_dir) / "greedy"
            plugin_dir.mkdir()
            (plugin_dir / "plugin.json").write_text(
                json.dumps({"name": "Greedy", "version": "1.0.0", "plugin_id": "greedy"}),
                encoding="utf-8",
            )
            (plugin_dir / "plugin.py").write_text(_GREEDY_PLUGIN, encoding="utf-8")
            records = await PluginManager().discover(temp_dir)
        self.assertEqual(records[0].status, PluginStatus.FAILED)
        self.assertIn("plugin.json does not declare", records[0].error)

    # -- validation ------------------------------------------------------------

    def test_validation_names_every_problem_it_finds(self) -> None:
        manager = PluginManager()
        problems = manager.validate(_InvalidPlugin())
        joined = " | ".join(problems)
        self.assertIn("not a dotted number", joined)
        self.assertIn("unknown kind", joined)
        self.assertIn("without", joined)
        self.assertIn("requires core", joined)

    async def test_invalid_plugin_is_rejected_and_never_loaded(self) -> None:
        manager = PluginManager()
        record = manager.register(_InvalidPlugin())
        self.assertEqual(record.status, PluginStatus.FAILED)
        self.assertIn("Plugin rejected", record.error)
        loaded = await manager.load(record.plugin_id)
        self.assertEqual(loaded.status, PluginStatus.FAILED)
        self.assertIn("rejected", loaded.error)
        self.assertEqual(
            [item.plugin_id for item in manager.rejected()], [record.plugin_id]
        )

    async def test_a_tool_name_already_registered_is_a_rejection(self) -> None:
        tools = ToolRegistry()
        tools.register(
            plugin_tool("recorder_echo", "Taken.", lambda arguments: {"ok": True})
        )
        record = PluginManager(tools=tools).register(RecorderPlugin())
        self.assertEqual(record.status, PluginStatus.FAILED)
        self.assertIn("already registered", record.error)

    async def test_registering_one_id_twice_is_a_caller_bug(self) -> None:
        manager = PluginManager()
        manager.register(RecorderPlugin())
        with self.assertRaises(PluginValidationError):
            manager.register(RecorderPlugin())

    # -- lifecycle -------------------------------------------------------------

    async def test_lifecycle_runs_each_hook_once_in_order(self) -> None:
        plugin = RecorderPlugin()
        manager = PluginManager(tools=ToolRegistry())
        record = manager.register(plugin)
        manager.configure("recorder", {"greeting": "hello"})

        self.assertEqual(record.status, PluginStatus.DISCOVERED)
        loaded = await manager.load("recorder")
        self.assertEqual(loaded.status, PluginStatus.LOADED)
        self.assertEqual(plugin.calls, ["load"])
        self.assertEqual(plugin.context.configuration["greeting"], "hello")
        self.assertEqual(plugin.context.configuration["retries"], 2)

        enabled = await manager.enable("recorder")
        self.assertEqual(enabled.status, PluginStatus.ENABLED)
        self.assertEqual(plugin.calls, ["load", "initialize", "enable"])
        # Enabling again is a no-op, not a second run of the hooks.
        await manager.enable("recorder")
        self.assertEqual(plugin.calls, ["load", "initialize", "enable"])

        disabled = await manager.disable("recorder")
        self.assertEqual(disabled.status, PluginStatus.DISABLED)
        unloaded = await manager.unload("recorder")
        self.assertEqual(unloaded.status, PluginStatus.UNLOADED)
        self.assertEqual(plugin.calls, ["load", "initialize", "enable", "disable", "shutdown"])

        # Unloading twice does not shut the plugin down twice.
        await manager.unload("recorder")
        self.assertEqual(plugin.calls[-1], "shutdown")

    async def test_unload_of_an_enabled_plugin_disables_it_first(self) -> None:
        plugin = RecorderPlugin()
        manager = PluginManager(tools=ToolRegistry())
        manager.register(plugin)
        await manager.enable("recorder")
        record = await manager.unload("recorder")
        self.assertEqual(plugin.calls, ["load", "initialize", "enable", "disable", "shutdown"])
        self.assertEqual(record.status, PluginStatus.UNLOADED)

    async def test_configuration_unknown_field_is_refused(self) -> None:
        manager = PluginManager()
        manager.register(RecorderPlugin())
        with self.assertRaises(ValueError):
            manager.configure("recorder", {"nonsense": 1})
        with self.assertRaises(ValueError):
            manager.configure("recorder", {"retries": "two"})

    async def test_missing_required_configuration_fails_the_load(self) -> None:
        manager = PluginManager()
        manager.register(_NeedsConfigurationPlugin())
        record = await manager.load("needs-config")
        self.assertEqual(record.status, PluginStatus.FAILED)
        self.assertIn("missing required configuration", record.error)

    # -- failure isolation -----------------------------------------------------

    async def test_a_broken_plugin_does_not_stop_its_neighbours(self) -> None:
        manager = PluginManager(tools=ToolRegistry())
        healthy = manager.register(RecorderPlugin())
        broken_enable = manager.register(BrokenEnablePlugin())
        manager.register(BrokenLoadPlugin())

        loaded = await manager.load_all()
        self.assertEqual({record.status for record in loaded if record.ok}, {PluginStatus.LOADED})
        self.assertEqual(manager.status("broken-load").status, PluginStatus.FAILED)
        self.assertIn("load exploded", manager.status("broken-load").error)

        enabled = await manager.enable_all()
        self.assertEqual(manager.status("recorder").status, PluginStatus.ENABLED)
        self.assertEqual(manager.status("broken-enable").status, PluginStatus.FAILED)
        self.assertIn("enable exploded", manager.status("broken-enable").error)
        self.assertEqual(healthy.status, PluginStatus.DISCOVERED)
        self.assertIn(broken_enable.plugin_id, [record.plugin_id for record in enabled])
        # The manager is still perfectly usable afterwards.
        self.assertIn("recorder_echo", manager.tools)
        await manager.disable_all()
        self.assertEqual(manager.status("recorder").status, PluginStatus.DISABLED)
        self.assertEqual(len(manager.rejected()), 2)

    async def test_a_shutdown_failure_still_withdraws_the_plugin(self) -> None:
        manager = PluginManager(tools=ToolRegistry())
        manager.register(BrokenShutdownPlugin())
        await manager.enable("broken-shutdown")
        record = await manager.unload("broken-shutdown")
        self.assertEqual(record.status, PluginStatus.FAILED)
        self.assertIn("shutdown exploded", record.error)

    async def test_every_lifecycle_step_is_announced(self) -> None:
        bus = EventBus(history=50)
        manager = PluginManager(tools=ToolRegistry(), event_bus=bus)
        manager.register(RecorderPlugin())
        await manager.enable("recorder")
        await manager.unload("recorder")
        types = [event.type for event in bus.recent(limit=50)]
        self.assertIn(EventType.PLUGIN_LOADED.value, types)
        self.assertIn(EventType.PLUGIN_INITIALIZED.value, types)
        self.assertIn(EventType.PLUGIN_ENABLED.value, types)
        self.assertIn(EventType.PLUGIN_UNLOADED.value, types)
        enabled = bus.recent(type_=EventType.PLUGIN_ENABLED)[0]
        self.assertEqual(enabled.payload["plugin_id"], "recorder")

        failed_bus = EventBus(history=20)
        failing = PluginManager(tools=ToolRegistry(), event_bus=failed_bus)
        failing.register(BrokenEnablePlugin())
        await failing.enable("broken-enable")
        failure = failed_bus.recent(type_=EventType.PLUGIN_FAILED)[0]
        self.assertEqual(failure.payload["plugin_id"], "broken-enable")
        self.assertIn("enable exploded", failure.payload["error"])

    # -- permissions -----------------------------------------------------------

    async def test_a_sensitive_plugin_is_denied_by_default(self) -> None:
        plugin = SensitivePlugin()
        manager = PluginManager(tools=ToolRegistry())
        manager.register(plugin)
        record = await manager.enable("sensitive")
        self.assertEqual(record.status, PluginStatus.DENIED)
        self.assertFalse(plugin.enabled_called)
        self.assertNotIn("sensitive_fetch", manager.tools)
        self.assertIn("sensitive", [item.plugin_id for item in manager.rejected()])

    async def test_an_approved_plugin_is_enabled_and_records_the_approval(self) -> None:
        manager = PluginManager(tools=ToolRegistry(), approval_gateway=AllowGateway())
        manager.register(SensitivePlugin())
        record = await manager.enable("sensitive")
        self.assertEqual(record.status, PluginStatus.ENABLED)
        self.assertIsNotNone(record.approval_id)
        self.assertIn("sensitive_fetch", manager.tools)

    async def test_declarations_reach_the_central_permission_manager(self) -> None:
        manager = PluginManager(tools=ToolRegistry(), approval_gateway=AllowGateway())
        manager.register(RecorderPlugin())
        manager.register(SensitivePlugin())
        await manager.enable_all()

        declared = manager.permissions.declared_for("recorder")
        self.assertIsNotNone(declared)
        self.assertEqual(declared.risk_level, RiskLevel.LOW)
        self.assertEqual(declared.required_permission, None)

        sensitive = manager.permissions.declared_for("sensitive")
        self.assertEqual(sensitive.risk_level, RiskLevel.HIGH)
        self.assertEqual(sensitive.required_permission, PermissionScope.NETWORK_ACCESS)
        self.assertTrue(sensitive.external_side_effect)
        # Each tool is declared too, so running it is gated by the same layer.
        tool_declaration = manager.permissions.declared_for("sensitive_fetch")
        self.assertIsNotNone(tool_declaration)
        self.assertEqual(
            tool_declaration.required_permission, PermissionScope.NETWORK_ACCESS
        )

    async def test_a_denied_plugin_may_be_enabled_once_approved(self) -> None:
        manager = PluginManager(tools=ToolRegistry())
        manager.register(SensitivePlugin())
        self.assertEqual((await manager.enable("sensitive")).status, PluginStatus.DENIED)
        manager.approval_gateway = AllowGateway()
        self.assertEqual((await manager.enable("sensitive")).status, PluginStatus.ENABLED)

    async def test_declaring_network_access_without_the_scope_is_rejected(self) -> None:
        record = PluginManager().register(_DishonestPlugin())
        self.assertEqual(record.status, PluginStatus.FAILED)
        self.assertIn("network", record.error)

    # -- capability and tool registration --------------------------------------

    async def test_enable_registers_capabilities_and_disable_removes_them(self) -> None:
        registry = CapabilityRegistry()
        manager = PluginManager(tools=ToolRegistry(), capabilities=registry)
        manager.register(RecorderPlugin())
        await manager.enable("recorder")

        capability_id = "plugin.recorder.recorder.echo"
        self.assertIsNotNone(registry.get(capability_id))
        self.assertEqual(manager.capability_ids(), (capability_id,))
        self.assertIn("recorder_echo", manager.tool_names())

        await manager.disable("recorder")
        self.assertIsNone(registry.get(capability_id))
        self.assertEqual(manager.capability_ids(), ())

    async def test_tools_are_callable_while_enabled_and_gone_after_unload(self) -> None:
        catalog = ToolCatalog()
        tools = ToolRegistry()
        manager = PluginManager(tools=tools, catalog=catalog)
        manager.register(RecorderPlugin())
        await manager.enable("recorder")

        result = await tools.get("recorder_echo").tool.run({"text": "hi"})
        self.assertEqual(result, {"text": "hi"})
        metadata = catalog.get("recorder_echo")
        self.assertIsNotNone(metadata)
        self.assertTrue(metadata.registered)
        self.assertIn("plugin", metadata.tags)

        await manager.unload("recorder")
        self.assertNotIn("recorder_echo", tools)
        self.assertIsNone(catalog.get("recorder_echo"))
        with self.assertRaises(KeyError):
            tools.get("recorder_echo")

    async def test_status_output_covers_records_not_just_enabled_plugins(self) -> None:
        manager = PluginManager(tools=ToolRegistry())
        manager.register(RecorderPlugin())
        manager.register(_InvalidPlugin())
        snapshot = manager.to_dict()
        self.assertEqual(len(snapshot["plugins"]), 2)
        self.assertEqual(snapshot["enabled"], [])
        self.assertEqual(len(manager.rejected()), 1)


_GOOD_PLUGIN = """
from novacontrol.plugins import Plugin


class DiscoveredPlugin(Plugin):
    plugin_id = "discovered-demo"
    name = "Discovered Demo"
    version = "1.0.0"
    description = "Found on disk."


plugin = DiscoveredPlugin()
"""

_GREEDY_PLUGIN = """
from novacontrol.core.security import PermissionScope
from novacontrol.plugins import Plugin


class GreedyPlugin(Plugin):
    plugin_id = "greedy"
    name = "Greedy"
    version = "1.0.0"
    permissions = (PermissionScope.NETWORK_ACCESS,)
    network_access = True


plugin = GreedyPlugin()
"""


class _InvalidPlugin(Plugin):
    """Every kind of declaration mistake at once, so validation reports them all."""

    plugin_id = "invalid"
    name = "Invalid"
    version = "not-a-version"
    min_core_version = "99.0.0"
    filesystem_access = ("/",)
    capabilities = (PluginCapability(name="invalid.capability", type="gadget"),)


class _DishonestPlugin(Plugin):
    plugin_id = "dishonest"
    name = "Dishonest"
    version = "1.0.0"
    network_access = True


class _NeedsConfigurationPlugin(Plugin):
    plugin_id = "needs-config"
    name = "Needs Config"
    version = "1.0.0"
    configuration_schema = (PluginConfigurationField(name="token", required=True),)


if __name__ == "__main__":
    unittest.main()
