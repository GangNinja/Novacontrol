"""Phase 10 verified against its own specification, clause by clause.

`tests/test_plugin_sdk.py` tests the SDK the way its author would; this module
tests it the way the PHASE does. Each requirement is driven through the real
manager and asserted on its result, so a clause that is documented but not
enforced fails here:

    10.1 the interface declares identity, capabilities, tools, permissions,
         configuration and five lifecycle hooks (and every hook has a default)
    10.2 the manager discovers, validates, loads, initializes, enables,
         disables, unloads, exposes status and CONTAINS failures
    10.3 plugins declare permissions, risk, required capabilities, external
         services, filesystem and network access — and the centralized
         PermissionManager is what decides whether one may run
    10.4 three minimal examples that duplicate no core functionality
    the demo the docs advertise actually runs

Everything this pass found is pinned below with the test that would have caught
it, so the same class of defect cannot come back unnoticed.
"""

from __future__ import annotations

import inspect
import json
import unittest
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from conftest import AllowGateway
from novacontrol.core.events import EventBus, EventType
from novacontrol.core.security import PermissionScope, RiskLevel
from novacontrol.intelligence.intent import CapabilityRegistry
from novacontrol.plugins import (
    Plugin,
    PluginCapability,
    PluginContext,
    PluginLifecycleError,
    PluginManager,
    PluginStatus,
    plugin_tool,
)
from novacontrol.plugins.examples import BrowserPlugin, DeveloperPlugin, SystemPlugin
from novacontrol.plugins.models import PluginConfigurationField
from novacontrol.tools.catalog import ToolCatalog
from novacontrol.tools.models import ToolParameter
from novacontrol.tools.registry import ToolRegistry


class Good(Plugin):
    plugin_id = "good"
    name = "Good"
    version = "1.0.0"
    description = "A working plugin."
    author = "tests"
    configuration_schema = (PluginConfigurationField(name="greeting", default="hi"),)
    capabilities = (PluginCapability(name="good.greet", type="tool", description="Greet."),)

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.tools = (
            plugin_tool(
                "good_greet",
                "Greet.",
                self._greet,
                parameters=(ToolParameter(name="who", required=True),),
            ),
        )

    async def load(self, context: PluginContext) -> None:
        self.calls.append(f"load:{context.configuration.get('greeting')}")

    async def initialize(self) -> None:
        self.calls.append("initialize")

    async def enable(self) -> None:
        self.calls.append("enable")

    async def disable(self) -> None:
        self.calls.append("disable")

    async def shutdown(self) -> None:
        self.calls.append("shutdown")

    async def _greet(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"greeting": f"hi {arguments.get('who', '')}"}


class _Pluggable(Plugin):
    """A plugin whose failure mode is chosen by the test."""

    plugin_id = "pluggable"
    name = "Pluggable"
    version = "1.0.0"

    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure
        self.calls: list[str] = []

    def _maybe_fail(self, hook: str) -> None:
        self.calls.append(hook)
        if self.failure == hook:
            raise RuntimeError(f"{hook} blew up")

    async def load(self, context: PluginContext) -> None:
        self._maybe_fail("load")

    async def initialize(self) -> None:
        self._maybe_fail("initialize")

    async def enable(self) -> None:
        self._maybe_fail("enable")

    async def disable(self) -> None:
        self._maybe_fail("disable")

    async def shutdown(self) -> None:
        self._maybe_fail("shutdown")


class Phase10InterfaceTests(unittest.IsolatedAsyncioTestCase):
    """10.1 — the stable Plugin interface."""

    async def test_the_interface_declares_everything_the_phase_names(self) -> None:
        for attribute in (
            "plugin_id",
            "name",
            "version",
            "description",
            "author",
            "capabilities",
            "tools",
            "permissions",
            "configuration_schema",
            "risk_level",
            "required_capabilities",
            "external_services",
            "filesystem_access",
            "network_access",
        ):
            self.assertTrue(hasattr(Plugin, attribute), attribute)
        for hook in ("load", "initialize", "enable", "disable", "shutdown"):
            self.assertTrue(
                inspect.iscoroutinefunction(getattr(Plugin, hook)), f"{hook} is not async"
            )

    async def test_every_hook_has_a_default_so_a_plugin_implements_only_what_it_has(
        self,
    ) -> None:
        class Bare(Plugin):
            plugin_id = "bare"
            name = "Bare"
            version = "1.0.0"

        plugin = Bare()
        await plugin.load(PluginContext(plugin_id="bare"))
        await plugin.initialize()
        await plugin.enable()
        await plugin.disable()
        await plugin.shutdown()
        self.assertEqual(plugin.manifest().plugin_id, "bare")

    async def test_declarations_become_the_manifest_the_rest_of_the_system_reads(
        self,
    ) -> None:
        manifest = DeveloperPlugin().manifest()
        self.assertEqual(manifest.plugin_id, "developer-tools")
        self.assertEqual(manifest.author, "NovaControl examples")
        self.assertIn(PermissionScope.FILESYSTEM_READ.value, manifest.permissions)
        self.assertEqual(manifest.filesystem_access, (".",))
        self.assertEqual(manifest.risk, RiskLevel.LOW)
        self.assertEqual([capability.name for capability in manifest.capabilities], [
            "developer.workspace_files"
        ])

    async def test_a_plugin_id_is_derived_when_only_a_name_is_declared(self) -> None:
        record = PluginManager().register(SystemPlugin())
        self.assertEqual(record.plugin_id, "system-tools")
        self.assertEqual(record.manifest.plugin_id, "system-tools")

    async def test_the_context_hands_the_plugin_the_live_system(self) -> None:
        tools = ToolRegistry()
        bus = EventBus(history=10)
        manager = PluginManager(tools=tools, event_bus=bus)

        class Spy(Plugin):
            plugin_id = "spy"
            name = "Spy"
            version = "1.0.0"

            def __init__(self) -> None:
                self.seen: list[Any] = []

            async def load(self, context: PluginContext) -> None:
                self.seen.append(context)
                await context.publish("spy.loaded", note="hello")

        spy = Spy()
        manager.register(spy)
        await manager.load("spy")
        seen = spy.seen[0]
        self.assertIs(seen.tools, tools)
        self.assertIs(seen.event_bus, bus)
        self.assertEqual(seen.plugin_id, "spy")
        self.assertEqual(bus.recent(type_="spy.loaded")[0].source, "plugin.spy")


class Phase10ManagerTests(unittest.IsolatedAsyncioTestCase):
    """10.2 — discovery, validation, lifecycle, status, containment."""

    async def test_discover_reads_a_manifest_and_the_code_beside_it(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "demo"
            directory.mkdir()
            (directory / "plugin.json").write_text(
                json.dumps(
                    {"name": "demo", "version": "1.0.0", "plugin_id": "demo", "risk": "low"}
                ),
                encoding="utf-8",
            )
            (directory / "plugin.py").write_text(_DISCOVERED_PLUGIN.format(name="demo"), "utf-8")
            records = await PluginManager().discover(temp_dir)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, PluginStatus.DISCOVERED)
        self.assertEqual(records[0].manifest.plugin_id, "demo")
        self.assertEqual(records[0].manifest.version, "1.0.0")
        self.assertEqual(records[0].manifest.risk, RiskLevel.LOW)

    async def test_a_code_and_manifest_disagreement_is_refused(self) -> None:
        with TemporaryDirectory() as temp_dir:
            version = Path(temp_dir) / "version-mismatch"
            version.mkdir()
            (version / "plugin.json").write_text(
                json.dumps({"name": "demo", "version": "9.9.9", "plugin_id": "demo"}),
                encoding="utf-8",
            )
            (version / "plugin.py").write_text(
                _DISCOVERED_PLUGIN.format(name="demo"), encoding="utf-8"
            )
            settings = Path(temp_dir) / "settings-mismatch"
            settings.mkdir()
            (settings / "plugin.json").write_text(
                json.dumps({"name": "nosy", "version": "1.0.0", "plugin_id": "nosy"}),
                encoding="utf-8",
            )
            (settings / "plugin.py").write_text(_NOSY_PLUGIN, encoding="utf-8")
            discovered = await PluginManager().discover(temp_dir)
            records = {record.plugin_id: record for record in discovered}
        self.assertEqual(records["demo"].status, PluginStatus.FAILED)
        self.assertIn(
            "declares version '1.0.0' but plugin.json declares '9.9.9'", records["demo"].error
        )
        self.assertEqual(records["nosy"].status, PluginStatus.FAILED)
        self.assertIn(
            "reads configuration field 'token' that plugin.json does not declare",
            records["nosy"].error,
        )

    async def test_discover_reports_a_manifest_without_code_and_a_bad_manifest(self) -> None:
        with TemporaryDirectory() as temp_dir:
            no_code = Path(temp_dir) / "no-code"
            no_code.mkdir()
            (no_code / "plugin.json").write_text(
                json.dumps({"name": "no-code", "version": "1.0.0"}), encoding="utf-8"
            )
            bad_json = Path(temp_dir) / "bad-json"
            bad_json.mkdir()
            (bad_json / "plugin.json").write_text("{not json", encoding="utf-8")
            (bad_json / "plugin.py").write_text("x = 1\n", encoding="utf-8")
            discovered = await PluginManager().discover(temp_dir)
            records = {record.plugin_id: record for record in discovered}
        self.assertEqual(records["no-code"].status, PluginStatus.FAILED)
        self.assertIn("no plugin.py", records["no-code"].error)
        self.assertEqual(records["bad-json"].status, PluginStatus.FAILED)
        self.assertIn("could not be read", records["bad-json"].error)

    async def test_a_rejection_never_overwrites_a_working_record(self) -> None:
        # A directory whose name is also another plugin's id: the broken copy is
        # rejected under its own key instead of replacing the working record.
        with TemporaryDirectory() as temp_dir:
            working = Path(temp_dir) / "from-file"
            working.mkdir()
            (working / "plugin.py").write_text(
                _DISCOVERED_PLUGIN.format(name="zz-broken"), encoding="utf-8"
            )
            broken = Path(temp_dir) / "zz-broken"
            broken.mkdir()
            (broken / "plugin.py").write_text("raise RuntimeError('broken on import')\n", "utf-8")
            records = await PluginManager().discover(temp_dir)
        by_id = {record.plugin_id: record for record in records}
        self.assertEqual(len(records), 2)
        self.assertEqual(sorted(by_id), ["zz-broken", "zz-broken-2"])
        self.assertEqual(by_id["zz-broken"].status, PluginStatus.DISCOVERED)
        self.assertEqual(by_id["zz-broken-2"].status, PluginStatus.FAILED)
        self.assertIn("could not be loaded", by_id["zz-broken-2"].error)

    async def test_broken_declarations_are_rejections_and_never_exceptions(self) -> None:
        class Unreadable(Plugin):
            plugin_id = "unreadable"
            name = "Unreadable"
            version = "1.0.0"
            capabilities = None  # type: ignore[assignment]

        class NoVersion(Plugin):
            plugin_id = "no-version"
            name = "No Version"
            version = ""

        class NoSchema(Plugin):
            plugin_id = "no-schema"
            name = "No Schema"
            version = "1.0.0"

            def __init__(self) -> None:
                class Broken:
                    name = "broken_tool"
                    schema = None
                    required_permissions: tuple[PermissionScope, ...] = ()

                self.tools = (Broken(),)  # type: ignore[assignment]

        class BadDefault(Plugin):
            plugin_id = "bad-default"
            name = "Bad Default"
            version = "1.0.0"
            configuration_schema = (
                PluginConfigurationField(name="count", type="integer", default="many"),
            )

        manager = PluginManager(tools=ToolRegistry())
        for plugin in (Unreadable(), NoVersion(), NoSchema(), BadDefault()):
            record = manager.register(plugin)
            self.assertEqual(record.status, PluginStatus.FAILED, plugin.plugin_id)
            self.assertTrue(record.error.startswith("Plugin rejected"), record.error)
            self.assertIn(plugin.plugin_id, [item.plugin_id for item in manager.rejected()])

    async def test_lifecycle_runs_each_hook_once_in_order(self) -> None:
        plugin = Good()
        manager = PluginManager(tools=ToolRegistry())
        manager.register(plugin)
        manager.configure("good", {"greeting": "hello"})

        self.assertEqual((await manager.load("good")).status, PluginStatus.LOADED)
        self.assertEqual((await manager.initialize("good")).status, PluginStatus.INITIALIZED)
        self.assertEqual((await manager.enable("good")).status, PluginStatus.ENABLED)
        self.assertEqual([call.split(":")[0] for call in plugin.calls], [
            "load",
            "initialize",
            "enable",
        ])
        self.assertEqual(plugin.calls[0], "load:hello")

        # Each hook is idempotent: a second call is the same state, not a rerun.
        await manager.load("good")
        await manager.initialize("good")
        await manager.enable("good")
        self.assertEqual(len(plugin.calls), 3)

        self.assertEqual((await manager.disable("good")).status, PluginStatus.DISABLED)
        await manager.disable("good")
        unloaded = await manager.unload("good")
        self.assertEqual(unloaded.status, PluginStatus.UNLOADED)
        await manager.unload("good")
        self.assertEqual(
            [call.split(":")[0] for call in plugin.calls],
            ["load", "initialize", "enable", "disable", "shutdown"],
        )

    async def test_an_unloaded_plugin_can_be_loaded_again(self) -> None:
        plugin = Good()
        manager = PluginManager(tools=ToolRegistry())
        manager.register(plugin)
        await manager.enable("good")
        await manager.unload("good")
        self.assertIn("good", manager.plugins())
        reloaded = await manager.enable("good")
        self.assertEqual(reloaded.status, PluginStatus.ENABLED)
        self.assertEqual(
            [call.split(":")[0] for call in plugin.calls],
            ["load", "initialize", "enable", "disable", "shutdown", "load", "initialize", "enable"],
        )

    async def test_configuration_is_read_at_load_so_it_is_refused_afterwards(self) -> None:
        manager = PluginManager(tools=ToolRegistry())
        manager.register(Good())
        await manager.load("good")
        with self.assertRaises(PluginLifecycleError):
            manager.configure("good", {"greeting": "later"})
        await manager.unload("good")
        manager.configure("good", {"greeting": "later"})
        self.assertEqual((await manager.load("good")).status, PluginStatus.LOADED)

    async def test_missing_required_configuration_fails_the_load_with_the_reason(self) -> None:
        class NeedsToken(Plugin):
            plugin_id = "needs-token"
            name = "Needs Token"
            version = "1.0.0"
            configuration_schema = (PluginConfigurationField(name="token", required=True),)

        manager = PluginManager()
        manager.register(NeedsToken())
        record = await manager.load("needs-token")
        self.assertEqual(record.status, PluginStatus.FAILED)
        self.assertIn("missing required configuration: 'token'", record.error)

    async def test_status_exposes_every_plugin_with_its_reason(self) -> None:
        manager = PluginManager(tools=ToolRegistry())
        manager.register(Good())
        manager.register(_Pluggable())
        await manager.enable("pluggable")
        await manager.enable("good")

        snapshot = manager.to_dict()
        self.assertEqual(len(snapshot["plugins"]), 2)
        self.assertEqual(snapshot["enabled"], ["good", "pluggable"])
        self.assertEqual(snapshot["tools"], ["good_greet"])
        self.assertEqual(manager.status("good").status, PluginStatus.ENABLED)
        self.assertEqual(manager.tool_names(), ("good_greet",))
        with self.assertRaises(KeyError):
            manager.status("never-registered")

    async def test_every_hook_failure_is_contained_and_the_others_keep_working(self) -> None:
        bus = EventBus(history=50)
        tools = ToolRegistry()
        manager = PluginManager(tools=tools, event_bus=bus)
        failing = {hook: _Pluggable() for hook in ("load", "initialize", "enable", "disable")}
        for hook, plugin in failing.items():
            plugin.plugin_id = f"broken-{hook}"
            plugin.name = f"Broken {hook}"
            plugin.failure = hook
            manager.register(plugin)
        manager.register(Good())

        # load_all: load, initialize, enable — each broken hook is reached in turn.
        await manager.load_all()
        self.assertEqual(manager.status("broken-load").status, PluginStatus.FAILED)
        self.assertIn("load blew up", manager.status("broken-load").error)
        await manager.enable_all()
        self.assertEqual(manager.status("good").status, PluginStatus.ENABLED)
        self.assertEqual(manager.status("broken-enable").status, PluginStatus.FAILED)
        self.assertIn("enable blew up", manager.status("broken-enable").error)
        self.assertEqual(manager.status("broken-load").status, PluginStatus.FAILED)

        # A plugin that fails to initialize never switches on, and says why.
        await manager.initialize_all()
        self.assertIn("initialize blew up", manager.status("broken-initialize").error)

        # disable_all keeps going, and the healthy plugin was disabled properly.
        await manager.disable_all()
        self.assertEqual(manager.status("good").status, PluginStatus.DISABLED)
        self.assertEqual(manager.status("broken-disable").status, PluginStatus.FAILED)

        failed = bus.recent(type_=EventType.PLUGIN_FAILED)
        failures = {event.payload["plugin_id"] for event in failed}
        self.assertLessEqual({"broken-load", "broken-enable", "broken-initialize"}, failures)
        await manager.unload_all()
        self.assertNotIn("good_greet", tools)

    async def test_a_shutdown_failure_is_contained_and_the_tools_still_go(self) -> None:
        class WithTool(_Pluggable):
            plugin_id = "shutdown-failure"
            name = "Shutdown Failure"

            def __init__(self) -> None:
                super().__init__(failure="shutdown")
                self.tools = (
                    plugin_tool("with_tool", "Do nothing.", lambda arguments: {"ok": True}),
                )

        tools = ToolRegistry()
        manager = PluginManager(tools=tools)
        manager.register(WithTool())
        await manager.enable("shutdown-failure")
        self.assertIn("with_tool", tools)
        record = await manager.unload("shutdown-failure")
        self.assertEqual(record.status, PluginStatus.FAILED)
        self.assertIn("shutdown blew up", record.error)
        self.assertNotIn("with_tool", tools)

    async def test_enable_all_disable_all_unload_all_are_isolated_walks(self) -> None:
        manager = PluginManager(tools=ToolRegistry())
        healthy = Good()
        manager.register(healthy)
        manager.register(_Pluggable())
        enabled = await manager.enable_all()
        self.assertEqual(len(enabled), 2)
        self.assertEqual(manager.enabled_ids(), ("good", "pluggable"))
        await manager.unload_all()
        self.assertEqual(manager.enabled_ids(), ())
        self.assertEqual((await manager.load_all())[0].status, PluginStatus.LOADED)


class Phase10SecurityTests(unittest.IsolatedAsyncioTestCase):
    """10.3 — declarations, the centralized permission layer, and the gate."""

    async def test_every_declaration_reaches_the_permission_layer(self) -> None:
        class Sensitive(Plugin):
            plugin_id = "sensitive"
            name = "Sensitive"
            version = "1.0.0"
            permissions = (PermissionScope.NETWORK_ACCESS, PermissionScope.FILESYSTEM_WRITE)
            risk_level = RiskLevel.HIGH
            network_access = True
            external_services = ("api.example.com",)
            required_capabilities = ()

            def __init__(self) -> None:
                self.tools = (
                    plugin_tool(
                        "sensitive_write",
                        "Write something far away.",
                        lambda arguments: {"ok": True},
                        required_permissions=(PermissionScope.FILESYSTEM_WRITE,),
                    ),
                )

        manager = PluginManager(tools=ToolRegistry(), approval_gateway=AllowGateway())
        manager.register(Sensitive())
        await manager.enable("sensitive")

        plugin_declaration = manager.permissions.declared_for("sensitive")
        self.assertEqual(plugin_declaration.risk_level, RiskLevel.HIGH)
        self.assertEqual(plugin_declaration.required_permission, PermissionScope.NETWORK_ACCESS)
        self.assertTrue(plugin_declaration.external_side_effect)
        tool_declaration = manager.permissions.declared_for("sensitive_write")
        self.assertEqual(tool_declaration.required_permission, PermissionScope.FILESYSTEM_WRITE)
        self.assertIn("sensitive", manager.permissions.declared())

    async def test_risk_and_scopes_decide_who_is_asked(self) -> None:
        # LOW + a read scope: no person is asked (a permission is not a verdict).
        low = PluginManager(tools=ToolRegistry())
        low.register(DeveloperPlugin())
        self.assertEqual((await low.enable("developer-tools")).status, PluginStatus.ENABLED)
        self.assertIsNone(low.status("developer-tools").approval_id)

        # HIGH: the policy asks, and the default gateway refuses.
        denied = PluginManager(tools=ToolRegistry())
        denied.register(_HighRiskPlugin())
        record = await denied.enable("high-risk")
        self.assertEqual(record.status, PluginStatus.DENIED)
        self.assertFalse(any(name.startswith("high_risk") for name in denied.tool_names()))

        # The same plugin with an approving gateway is enabled, with the id.
        allowed = PluginManager(tools=ToolRegistry(), approval_gateway=AllowGateway())
        allowed.register(_HighRiskPlugin())
        record = await allowed.enable("high-risk")
        self.assertEqual(record.status, PluginStatus.ENABLED)
        self.assertIsNotNone(record.approval_id)

    async def test_a_denied_plugin_can_be_enabled_once_approved(self) -> None:
        manager = PluginManager(tools=ToolRegistry())
        manager.register(_HighRiskPlugin())
        self.assertEqual((await manager.enable("high-risk")).status, PluginStatus.DENIED)
        manager.approval_gateway = AllowGateway()
        self.assertEqual((await manager.enable("high-risk")).status, PluginStatus.ENABLED)

    async def test_required_capabilities_are_checked_against_this_installation(self) -> None:
        class NeedsSearch(Plugin):
            plugin_id = "needs-search"
            name = "Needs Search"
            version = "1.0.0"
            required_capabilities = ("browser.search",)

        # Not present -> refused, with the reason.
        missing = PluginManager(tools=ToolRegistry(), capabilities=CapabilityRegistry())
        missing.register(NeedsSearch())
        record = await missing.enable("needs-search")
        self.assertEqual(record.status, PluginStatus.FAILED)
        self.assertIn("requires capability 'browser.search'", record.error)

        # Present -> enabled.
        registry = CapabilityRegistry()
        registry.register_action(
            action="search", capability_id="browser.search", description="Search."
        )
        present = PluginManager(tools=ToolRegistry(), capabilities=registry)
        present.register(NeedsSearch())
        self.assertEqual((await present.enable("needs-search")).status, PluginStatus.ENABLED)

        # No registry to check against -> reported, not assumed.
        unverifiable = PluginManager(tools=ToolRegistry())
        unverifiable.register(NeedsSearch())
        record = await unverifiable.enable("needs-search")
        self.assertEqual(record.status, PluginStatus.FAILED)
        self.assertIn("no capability registry is attached", record.error)

    async def test_code_may_not_outgrow_the_manifest_a_person_approved(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "greedy"
            directory.mkdir()
            (directory / "plugin.json").write_text(
                json.dumps(
                    {
                        "name": "greedy",
                        "version": "1.0.0",
                        "plugin_id": "greedy",
                        "risk": "low",
                    }
                ),
                encoding="utf-8",
            )
            (directory / "plugin.py").write_text(_GREEDY_PLUGIN, encoding="utf-8")
            records = await PluginManager(tools=ToolRegistry()).discover(temp_dir)
        self.assertEqual(records[0].status, PluginStatus.FAILED)
        joined = records[0].error
        self.assertIn("network:access", joined)
        self.assertIn("network access", joined)

    async def test_a_failed_registration_withdraws_what_it_already_registered(self) -> None:
        # The capability registry already holds the plugin's SECOND id, so the
        # first one registers and the second raises: nothing may be left behind.
        class Dual(Plugin):
            plugin_id = "dual"
            name = "Dual"
            version = "1.0.0"
            capabilities = (
                PluginCapability(name="one", type="skill", description="First."),
                PluginCapability(name="two", type="skill", description="Second."),
            )

        registry = CapabilityRegistry()
        registry.register_action(
            action="taken",
            capability_id="plugin.dual.two",
            description="Already here.",
        )
        manager = PluginManager(tools=ToolRegistry(), capabilities=registry)
        manager.register(Dual())
        record = await manager.enable("dual")
        self.assertEqual(record.status, PluginStatus.FAILED)
        self.assertIn("could not be registered", record.error)
        self.assertIsNone(registry.get("plugin.dual.one"))
        self.assertEqual(manager.capability_ids(), ())
        self.assertEqual(manager.tool_names(), ())

    async def test_unload_withdraws_tools_capabilities_and_catalogue_entries(self) -> None:
        registry = CapabilityRegistry()
        catalog = ToolCatalog()
        tools = ToolRegistry()
        manager = PluginManager(tools=tools, catalog=catalog, capabilities=registry)
        manager.register(Good())
        await manager.enable("good")
        self.assertIsNotNone(registry.get("plugin.good.good.greet"))
        self.assertIsNotNone(catalog.get("good_greet"))

        await manager.unload("good")
        self.assertNotIn("good_greet", tools)
        self.assertIsNone(catalog.get("good_greet"))
        self.assertIsNone(registry.get("plugin.good.good.greet"))
        self.assertEqual(manager.tool_names(), ())
        self.assertEqual(manager.capability_ids(), ())


class Phase10ExampleTests(unittest.IsolatedAsyncioTestCase):
    """10.4 — the examples, and the demo the documentation advertises."""

    async def test_the_three_examples_validate_and_enable(self) -> None:
        tools = ToolRegistry()
        manager = PluginManager(tools=tools)
        for plugin in (SystemPlugin(), DeveloperPlugin(), BrowserPlugin()):
            self.assertEqual(manager.validate(plugin), (), plugin.plugin_id)
            manager.register(plugin)
        records = await manager.enable_all()
        self.assertEqual([record.status for record in records], [PluginStatus.ENABLED] * 3)
        self.assertEqual(
            sorted(manager.tool_names()),
            ["browser_classify_url", "developer_workspace_files", "system_platform_info"],
        )

    async def test_the_examples_declare_only_what_they_use(self) -> None:
        system = SystemPlugin()
        self.assertEqual(system.permissions, ())
        self.assertFalse(system.network_access)
        self.assertEqual(system.external_services, ())
        browser = BrowserPlugin()
        self.assertEqual(browser.permissions, ())
        self.assertFalse(browser.network_access)
        developer = DeveloperPlugin()
        self.assertEqual(developer.permissions, (PermissionScope.FILESYSTEM_READ,))
        self.assertTrue(developer.filesystem_access)

    async def test_the_example_tools_do_what_they_say(self) -> None:
        tools = ToolRegistry()
        manager = PluginManager(tools=tools)
        for plugin in (SystemPlugin(), DeveloperPlugin(), BrowserPlugin()):
            manager.register(plugin)
        await manager.enable_all()

        platform = await tools.get("system_platform_info").tool.run({})
        self.assertIn("python", platform)

        listing = await tools.get("developer_workspace_files").tool.run({"limit": 3})
        self.assertTrue(listing["exists"])
        self.assertLessEqual(len(listing["entries"]), 3)

        classified = await tools.get("browser_classify_url").tool.run(
            {"url": "https://user:pass@example.com:8443/x"}
        )
        self.assertTrue(classified["secure"])
        self.assertTrue(classified["has_credentials"])
        self.assertEqual(classified["port"], 8443)
        # A bad port is an answer, not a crash.
        broken = await tools.get("browser_classify_url").tool.run({"url": "http://x:99999/"})
        self.assertFalse(broken["valid"])
        self.assertEqual(broken["reason"], "invalid port")

    async def test_the_demo_runs_and_contains_its_broken_plugin(self) -> None:
        from novacontrol.cli.demos import run_phase_demo

        result = await run_phase_demo("phase10_sdk")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["after_enable"]["broken-demo"], PluginStatus.FAILED.value)
        self.assertEqual(len(result["enabled_plugins"]), 2)
        self.assertTrue(result["tool_withdrawn"])
        self.assertIn("entries", result["workspace_sample"])


class _HighRiskPlugin(Plugin):
    plugin_id = "high-risk"
    name = "High Risk"
    version = "1.0.0"
    permissions = (PermissionScope.NETWORK_ACCESS,)
    risk_level = RiskLevel.HIGH
    network_access = True

    def __init__(self) -> None:
        self.tools = (
            plugin_tool(
                "high_risk_call",
                "Call somewhere.",
                lambda arguments: {"ok": True},
                required_permissions=(PermissionScope.NETWORK_ACCESS,),
            ),
        )


_DISCOVERED_PLUGIN = """
from novacontrol.plugins import Plugin


class Discovered(Plugin):
    plugin_id = "{name}"
    name = "{name}"
    version = "1.0.0"


plugin = Discovered()
"""

_NOSY_PLUGIN = """
from novacontrol.plugins import Plugin
from novacontrol.plugins.models import PluginConfigurationField


class Nosy(Plugin):
    plugin_id = "nosy"
    name = "nosy"
    version = "1.0.0"
    configuration_schema = (PluginConfigurationField(name="token"),)


plugin = Nosy()
"""

_GREEDY_PLUGIN = """
from novacontrol.core.security import PermissionScope
from novacontrol.plugins import Plugin


class Greedy(Plugin):
    plugin_id = "greedy"
    name = "greedy"
    version = "1.0.0"
    permissions = (PermissionScope.NETWORK_ACCESS,)
    network_access = True


plugin = Greedy()
"""


if __name__ == "__main__":
    unittest.main()
