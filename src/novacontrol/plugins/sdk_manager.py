"""PluginManager: the thing that runs plugins without trusting them (Phase 10.2/10.3).

The manager owns one plugin's whole life — discovered, loaded, initialized,
enabled, disabled, unloaded — and one rule above all others: **a plugin that
fails is a plugin that is contained.** Every hook the manager calls is wrapped,
every failure becomes a :class:`~novacontrol.plugins.models.PluginRecord` with
the reason attached and a ``plugin.failed`` event on the bus, and the manager
itself keeps working. Nothing a plugin does — raising in ``load``, raising only
when ``enable`` runs, declaring a tool name that is already taken, being
un-importable — reaches the caller as an exception from a *plugin*, because
"one plugin broke" must never mean "NovaControl broke".

Three layers decide what a plugin may do, and they are the existing ones, not
new copies of them:

* the declarations the plugin makes about itself (permissions, risk, external
  services, filesystem and network access) are checked for internal honesty —
  *may* a plugin that declares no network access claim an external service? no;
* the same declarations are handed to the centralized
  :class:`~novacontrol.reliability.permissions.PermissionManager`, which decides
  whether a person must be asked before the plugin is switched on, using the
  build's own policy — the manager does not invent a second risk table;
* an approval the policy asks for is requested from the same
  :class:`~novacontrol.core.security.ApprovalGateway` the rest of the system
  uses, and a refusal leaves the plugin loaded and NOT enabled.

What a plugin contributes is registered where the rest of the system already
looks: its tools into the ``ToolRegistry`` (and the tool catalogue when one is
attached, so the selector can find them), its capabilities into the Phase 9
``CapabilityRegistry`` (when one is attached), and its permission declarations
into the ``PermissionManager`` — so a plugin's tool is gated by exactly the
layer that gates a core tool. Registering is undone on disable and unload:
``ToolRegistry.unregister`` and ``CapabilityRegistry.unregister`` exist for this.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novacontrol.core.events import EventBus, EventType
from novacontrol.core.security import (
    ApprovalGateway,
    ApprovalRequest,
    DenyByDefaultApprovalGateway,
    PermissionScope,
    RiskLevel,
)
from novacontrol.plugins.models import (
    CONFIGURATION_TYPES,
    PluginManifest,
    PluginRecord,
    PluginStatus,
    _slug,
)
from novacontrol.plugins.sdk import Plugin, PluginContext
from novacontrol.reliability.permissions import PermissionManager
from novacontrol.tools.metadata import ToolCategory, ToolMetadata
from novacontrol.tools.models import ToolSchema
from novacontrol.tools.registry import FunctionTool, ToolRegistry

_logger = logging.getLogger(__name__)

#: The capability kinds a plugin may declare. Deliberately the same families
#: the plugin guide names, so "a plugin may provide agents, skills, tools, ..."
#: is checkable rather than decorative.
CAPABILITY_KINDS: frozenset[str] = frozenset(
    {"tool", "agent", "skill", "integration", "model", "workflow", "panel"}
)

_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")

_TYPE_CHECKS: Mapping[str, Callable[[Any], bool]] = {
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "list": lambda value: isinstance(value, (list, tuple)),
}


class PluginValidationError(ValueError):
    """A plugin broke the contract before it was ever run.

    Raised only for CALLER mistakes — registering two plugins under one id. A
    plugin whose own declarations are wrong is not an exception: it is a
    rejected :class:`PluginRecord`, because a plugin's mistake must be data the
    manager can report rather than a signal that interrupts the process.
    """

    def __init__(self, plugin_id: str, problems: Sequence[str]) -> None:
        self.plugin_id = plugin_id
        self.problems = tuple(problems)
        super().__init__(
            f"Plugin {plugin_id!r} is not valid: " + "; ".join(self.problems)
        )


class PluginLifecycleError(RuntimeError):
    """A lifecycle request that does not make sense in the plugin's state."""

    def __init__(self, plugin_id: str, status: PluginStatus, wanted: str) -> None:
        self.plugin_id = plugin_id
        self.status = status
        self.wanted = wanted
        super().__init__(
            f"Cannot {wanted} plugin {plugin_id!r} while it is {status.value}."
        )


@dataclass(frozen=True, slots=True)
class PluginToolMetadata:
    """What the manager records about one plugin tool, for status output."""

    name: str
    plugin_id: str
    risk: RiskLevel
    permissions: tuple[PermissionScope, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "plugin_id": self.plugin_id,
            "risk": self.risk.value,
            "permissions": [scope.value for scope in self.permissions],
        }


class PluginManager:
    """Discovers, validates, runs, and contains plugins.

    Construction is wiring, not loading: pass the registries this installation
    has (all optional) and the manager uses them where it can. A manager with
    nothing attached still discovers, validates, loads and enables plugins —
    it just has nowhere to put their tools, and says so when one declares any.
    """

    def __init__(
        self,
        *,
        tools: ToolRegistry | None = None,
        catalog: Any | None = None,
        capabilities: Any | None = None,
        permissions: PermissionManager | None = None,
        approval_gateway: ApprovalGateway | None = None,
        event_bus: EventBus | None = None,
        core_version: str = "0.1.0",
    ) -> None:
        self.tools = tools
        self.catalog = catalog
        self.capabilities = capabilities
        self.permissions = permissions or PermissionManager()
        self.approval_gateway = approval_gateway or DenyByDefaultApprovalGateway()
        self.core_version = str(core_version)
        self._events = event_bus
        self._plugins: dict[str, Plugin] = {}
        self._records: dict[str, PluginRecord] = {}
        self._configuration: dict[str, dict[str, Any]] = {}
        self._contexts: dict[str, PluginContext] = {}
        self._plugin_tools: dict[str, tuple[str, ...]] = {}
        self._plugin_capabilities: dict[str, tuple[str, ...]] = {}
        self._catalog_created: set[str] = set()

    # -- wiring ----------------------------------------------------------------

    def attach_events(self, event_bus: EventBus | None) -> None:
        """Point the manager at an event bus after construction."""
        self._events = event_bus

    # -- registration and discovery -------------------------------------------

    def register(
        self,
        plugin: Plugin,
        *,
        manifest: PluginManifest | None = None,
    ) -> PluginRecord:
        """Validate a plugin and record it as DISCOVERED — or as FAILED, why.

        ``manifest`` is what a ``plugin.json`` said about this plugin, when one
        was found: the code may not claim LESS than the installed manifest, so a
        manifest that names the permissions and the code that quietly demands
        more is a rejection rather than a surprise at enable time.
        """
        plugin_id = self._identity(plugin)
        if not plugin.plugin_id.strip():
            plugin.plugin_id = plugin_id
        existing = self._plugins.get(plugin_id)
        if existing is not None and existing is not plugin:
            raise PluginValidationError(
                plugin_id, [f"plugin_id {plugin_id!r} is already registered"]
            )
        if existing is None and plugin_id in self._records:
            # The id is taken by a record — a plugin that was rejected, denied,
            # or unloaded cannot be. Registering over it would silently replace
            # one plugin's answer with another's, so it is a conflict.
            raise PluginValidationError(
                plugin_id, [f"plugin_id {plugin_id!r} is already registered"]
            )
        try:
            problems = list(self.validate(plugin))
            if manifest is not None:
                problems.extend(_manifest_conflicts(manifest, plugin))
        except Exception as exc:  # noqa: BLE001 - unreadable declarations are a rejection
            problems = [f"the declarations could not be read: {type(exc).__name__}: {exc}"]
        if problems:
            return self._store(
                PluginRecord(
                    plugin_id=plugin_id,
                    manifest=manifest or _placeholder_manifest(plugin_id, plugin),
                    status=PluginStatus.FAILED,
                    error="Plugin rejected: " + "; ".join(problems),
                )
            )
        self._plugins[plugin_id] = plugin
        return self._store(
            PluginRecord(
                plugin_id=plugin_id,
                manifest=plugin.manifest(),
                status=PluginStatus.DISCOVERED,
            )
        )

    async def discover(self, directory: str | Path) -> tuple[PluginRecord, ...]:
        """Find plugins in a directory: one subdirectory per plugin.

        A plugin directory holds ``plugin.json`` (what it declares to the
        marketplace), ``plugin.py`` (what it is), or both. Anything that cannot
        be imported or read is recorded as FAILED with the reason — discovery
        never stops at the first broken directory, because a folder someone is
        halfway through writing must not hide every other plugin.
        """
        root = Path(directory)
        if not root.is_dir():
            raise ValueError(f"Plugin directory does not exist: {root}")
        records: list[PluginRecord] = []
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if not (entry / "plugin.py").exists() and not (entry / "plugin.json").exists():
                continue
            records.append(await self._discover_one(entry))
        return tuple(records)

    async def _discover_one(self, path: Path) -> PluginRecord:
        label = path.name
        manifest: PluginManifest | None = None
        json_path = path / "plugin.json"
        if json_path.exists():
            try:
                manifest = PluginManifest.from_file(json_path)
            except Exception as exc:  # noqa: BLE001 - a bad file is a rejection
                return self._reject(
                    label, f"plugin.json could not be read: {type(exc).__name__}: {exc}"
                )
        py_path = path / "plugin.py"
        if not py_path.exists():
            return self._reject(
                label, "the plugin has a plugin.json but no plugin.py to load"
            )
        try:
            plugin = _import_plugin(py_path, label)
            return self.register(plugin, manifest=manifest)
        except Exception as exc:  # noqa: BLE001 - import errors are rejections
            return self._reject(label, f"'{py_path.name}' could not be loaded: "
                                       f"{type(exc).__name__}: {exc}")

    def validate(self, plugin: Plugin) -> tuple[str, ...]:
        """Every problem with this plugin's declaration, in the order found.

        Checked here rather than discovered later: an unknown capability kind, a
        tool name the registry already holds, a plugin that declares filesystem
        access without the scope that permits it, a version that is not a
        version — all of these are refusals at registration, not surprises at
        the first call.
        """
        problems: list[str] = []
        if not str(plugin.plugin_id).strip():
            problems.append("plugin_id is required")
        if not str(plugin.name).strip():
            problems.append("name is required")
        if not str(plugin.version).strip():
            problems.append("version is required")
        elif not _VERSION_RE.match(plugin.version.strip()):
            problems.append(f"version {plugin.version!r} is not a dotted number")
        if not _VERSION_RE.match(self.core_version):
            problems.append(f"core_version {self.core_version!r} is not a dotted number")
        elif _VERSION_RE.match(plugin.min_core_version or "") and (
            _version_tuple(plugin.min_core_version) > _version_tuple(self.core_version)
        ):
            problems.append(
                f"requires core {plugin.min_core_version} but this build is {self.core_version}"
            )
        if not isinstance(plugin.risk_level, RiskLevel):
            problems.append(f"risk_level {plugin.risk_level!r} is not a RiskLevel")

        seen_capabilities: set[str] = set()
        for capability in plugin.capabilities:
            name = str(capability.name or "").strip()
            if not name:
                problems.append("a capability has no name")
            elif name in seen_capabilities:
                problems.append(f"capability {name!r} is declared twice")
            seen_capabilities.add(name)
            if capability.type not in CAPABILITY_KINDS:
                problems.append(
                    f"capability {name!r} has unknown kind {capability.type!r} "
                    f"(valid: {', '.join(sorted(CAPABILITY_KINDS))})"
                )

        seen_tools: set[str] = set()
        for tool in plugin.tools:
            name = str(tool.name or "").strip()
            if not name:
                problems.append("a tool has no name")
            elif name in seen_tools:
                problems.append(f"tool {name!r} is declared twice")
            elif self.tools is not None and name in self.tools:
                problems.append(f"tool {name!r} is already registered")
            seen_tools.add(name)
            # A tool that cannot be registered must be refused HERE rather than
            # halfway through enabling the plugin, where the failure is a
            # rollback instead of a reason.
            if not isinstance(getattr(tool, "schema", None), ToolSchema):
                problems.append(
                    f"tool {name or '<unnamed>'!r} has no ToolSchema to register"
                    if not name
                    else f"tool {name!r} has no ToolSchema to register"
                )

        for scope in plugin.permissions:
            if not isinstance(scope, PermissionScope):
                problems.append(f"permission {scope!r} is not a PermissionScope")
        scopes = {scope for scope in plugin.permissions if isinstance(scope, PermissionScope)}
        if plugin.filesystem_access and not (
            {PermissionScope.FILESYSTEM_READ, PermissionScope.FILESYSTEM_WRITE} & scopes
        ):
            problems.append(
                "declares filesystem access without "
                f"{PermissionScope.FILESYSTEM_READ.value!r} or "
                f"{PermissionScope.FILESYSTEM_WRITE.value!r}"
            )
        if (plugin.network_access or plugin.external_services) and (
            PermissionScope.NETWORK_ACCESS not in scopes
        ):
            problems.append(
                "declares network access or an external service without "
                f"{PermissionScope.NETWORK_ACCESS.value!r}"
            )

        seen_fields: set[str] = set()
        for field in plugin.configuration_schema:
            if field.name in seen_fields:
                problems.append(f"configuration field {field.name!r} is declared twice")
            seen_fields.add(field.name)
            if field.type not in CONFIGURATION_TYPES:
                problems.append(
                    f"configuration field {field.name!r} has unknown type {field.type!r}"
                )
            # A default the field's own type would reject is a schema bug: it
            # would make load() fail for every plugin that relies on it.
            check = _TYPE_CHECKS.get(field.type)
            if field.default is not None and check is not None and not check(field.default):
                problems.append(
                    f"configuration field {field.name!r} declares a default that is not "
                    f"a {field.type}: {field.default!r}"
                )
        return tuple(problems)

    # -- configuration ---------------------------------------------------------

    def configure(self, plugin_id: str, values: Mapping[str, Any]) -> None:
        """Store settings for a plugin, checking them against its own schema.

        Unknown keys are refused rather than ignored: a setting that silently
        does nothing is worse than one that is rejected. Reconfiguring a plugin
        that is already enabled is a lifecycle error — its ``load`` has already
        read the old values.
        """
        record = self.status(plugin_id)
        if record.status not in (
            PluginStatus.DISCOVERED,
            PluginStatus.FAILED,
            PluginStatus.UNLOADED,
        ):
            raise PluginLifecycleError(record.plugin_id, record.status, "configure")
        fields = {field.name: field for field in record.manifest.configuration}
        unknown = sorted(set(map(str, values)) - set(fields))
        if unknown:
            raise ValueError(
                f"Plugin {record.plugin_id!r} does not declare configuration "
                f"field(s): {', '.join(unknown)}"
            )
        problems = _configuration_problems(record.manifest, values, require_all=False)
        if problems:
            raise ValueError("; ".join(problems))
        self._configuration[record.plugin_id] = {
            str(key): value for key, value in values.items()
        }

    # -- lifecycle ------------------------------------------------------------

    async def load(self, plugin_id: str) -> PluginRecord:
        """Call ``load`` with the plugin's context, and record where it got to.

        A plugin already loaded stays loaded and is not loaded twice — hooks are
        run once per transition, never re-entrantly. An unloaded plugin may be
        loaded again; a failed one may be loaded again too, because a failure
        that cannot be retried is a plugin you can never repair.
        """
        record = self._status_of(plugin_id)
        if record.status in (
            PluginStatus.LOADED,
            PluginStatus.INITIALIZED,
            PluginStatus.ENABLED,
            PluginStatus.DISABLED,
        ):
            return record
        plugin = self._plugins.get(record.plugin_id)
        if plugin is None:
            return await self._fail(
                record.plugin_id,
                record.error or "no plugin instance is registered for this plugin id",
            )
        configuration = self._resolved_configuration(record.manifest)
        problems = _configuration_problems(record.manifest, configuration, require_all=True)
        if problems:
            return await self._fail(record.plugin_id, "; ".join(problems))
        context = PluginContext(
            plugin_id=record.plugin_id,
            configuration=configuration,
            tools=self.tools,
            event_bus=self._events,
        )
        self._contexts[record.plugin_id] = context
        try:
            await plugin.load(context)
        except Exception as exc:  # noqa: BLE001 - a plugin's failure is contained
            return await self._fail(
                record.plugin_id, f"load() failed: {type(exc).__name__}: {exc}"
            )
        record = self._set(record.plugin_id, PluginStatus.LOADED)
        await self._emit(EventType.PLUGIN_LOADED, plugin_id=record.plugin_id, name=plugin.name)
        return record

    async def initialize(self, plugin_id: str) -> PluginRecord:
        """Call ``initialize`` once the plugin is loaded (loading it if needed)."""
        record = self._status_of(plugin_id)
        if record.status in (
            PluginStatus.INITIALIZED,
            PluginStatus.ENABLED,
            PluginStatus.DISABLED,
        ):
            return record
        if record.status in (PluginStatus.DISCOVERED, PluginStatus.UNLOADED, PluginStatus.FAILED):
            record = await self.load(record.plugin_id)
        if record.status is not PluginStatus.LOADED:
            return record
        plugin = self._plugins[record.plugin_id]
        try:
            await plugin.initialize()
        except Exception as exc:  # noqa: BLE001
            return await self._fail(
                record.plugin_id, f"initialize() failed: {type(exc).__name__}: {exc}"
            )
        record = self._set(record.plugin_id, PluginStatus.INITIALIZED)
        await self._emit(EventType.PLUGIN_INITIALIZED, plugin_id=record.plugin_id)
        return record

    async def enable(self, plugin_id: str) -> PluginRecord:
        """Walk the plugin to INITIALIZED, clear it for security, and switch it on.

        The security gate is the centralized one: the plugin's declaration is
        registered with the ``PermissionManager``, the manager asks that layer
        whether a person must be asked, and only then — with an approval, when
        one is required — does the plugin's ``enable`` run and its tools become
        callable. A refusal is a DENIED record, not an exception, and it leaves
        the plugin loaded and initialized so the same approval can be retried.
        """
        record = self._status_of(plugin_id)
        if record.status is PluginStatus.ENABLED:
            return record
        if record.status in (
            PluginStatus.DISCOVERED,
            PluginStatus.UNLOADED,
            PluginStatus.FAILED,
            PluginStatus.LOADED,
        ):
            record = await self.initialize(record.plugin_id)
        # DENIED is a state a plugin may leave: a refusal is about this attempt,
        # and the same plugin with an approval (or a policy change) may be
        # enabled afterwards without being loaded a second time.
        if record.status not in (PluginStatus.INITIALIZED, PluginStatus.DENIED):
            return record
        plugin = self._plugins.get(record.plugin_id)
        if plugin is None:
            return await self._fail(
                record.plugin_id, record.error or "the plugin was rejected"
            )

        problems = self._enable_problems(plugin)
        if problems:
            return await self._fail(record.plugin_id, "; ".join(problems))

        self._declare(plugin)
        # Asked about the PLUGIN, not about an action with the plugin's id as its
        # name: the permission layer derives risk from the verb of an action,
        # and an id is a label, not a verb — "recorder" must not be read as an
        # instruction to place an order. The declaration is the whole question.
        decision = self.permissions.assess(record.plugin_id)
        approval_id: str | None = None
        if decision.requires_confirmation:
            requested = ", ".join(scope.value for scope in plugin.permissions)
            approval = await self.approval_gateway.request_approval(
                ApprovalRequest(
                    action=f"Enable plugin {plugin.name or record.plugin_id}",
                    reason=(
                        f"Plugin {record.plugin_id!r} declares {decision.risk.value} risk"
                        + (f" and requests {requested}" if requested else "")
                        + "."
                    ),
                    permissions=tuple(plugin.permissions),
                    risk=decision.risk,
                    metadata=record.manifest.to_dict(),
                )
            )
            approval_id = approval.request_id
            if not approval.approved:
                return self._set(
                    record.plugin_id,
                    PluginStatus.DENIED,
                    error=approval.reason or "the approval was refused",
                    approval_id=approval_id,
                )
            decision = self.permissions.check(record.plugin_id, approved=True)
            if not decision.allow:
                return self._set(
                    record.plugin_id,
                    PluginStatus.DENIED,
                    error=decision.reason,
                    approval_id=approval_id,
                )
        elif not self.permissions.check(record.plugin_id).allow:
            return await self._fail(record.plugin_id, decision.reason)

        try:
            await plugin.enable()
        except Exception as exc:  # noqa: BLE001
            return await self._fail(
                record.plugin_id, f"enable() failed: {type(exc).__name__}: {exc}"
            )
        try:
            # Registered INCREMENTALLY, so a failure on the third tool still
            # leaves the first two recorded — and therefore withdrawn. A tuple
            # assigned only on success is exactly how a half-registered plugin
            # leaves orphans in registries that no longer owe it anything.
            self._register_tools(plugin, record.plugin_id)
            self._register_capabilities(plugin, record.plugin_id)
        except Exception as exc:  # noqa: BLE001 - registration failed: withdraw it all
            self._withdraw(record.plugin_id)
            await self._call_quietly(plugin, "disable")
            return await self._fail(
                record.plugin_id,
                f"the plugin could not be registered: {type(exc).__name__}: {exc}",
            )
        record = self._set(
            record.plugin_id,
            PluginStatus.ENABLED,
            approval_id=approval_id or record.approval_id,
        )
        await self._emit(
            EventType.PLUGIN_ENABLED,
            plugin_id=record.plugin_id,
            name=plugin.name,
            version=plugin.version,
            risk=decision.risk.value,
            tools=list(self._plugin_tools[record.plugin_id]),
            capabilities=list(self._plugin_capabilities[record.plugin_id]),
        )
        return record

    async def disable(self, plugin_id: str) -> PluginRecord:
        """Switch the plugin off and withdraw everything it contributed."""
        record = self._status_of(plugin_id)
        if record.status is not PluginStatus.ENABLED:
            return record
        plugin = self._plugins.get(record.plugin_id)
        if plugin is not None:
            try:
                await plugin.disable()
            except Exception as exc:  # noqa: BLE001 - still withdraw it
                self._withdraw(record.plugin_id)
                return await self._fail(
                    record.plugin_id, f"disable() failed: {type(exc).__name__}: {exc}"
                )
        self._withdraw(record.plugin_id)
        record = self._set(record.plugin_id, PluginStatus.DISABLED)
        await self._emit(EventType.PLUGIN_DISABLED, plugin_id=record.plugin_id)
        return record

    async def unload(self, plugin_id: str) -> PluginRecord:
        """Take the plugin out of the running system, in the safe order.

        An enabled plugin is disabled first (its tools stop being callable),
        then ``shutdown`` runs, then the runtime state is dropped — the context
        it was loaded with — while the plugin itself stays known, so it can be
        loaded again. The record stays too: a person asking "what happened to
        that plugin?" gets an answer, and unloading twice is a no-op rather than
        a second shutdown.
        """
        record = self._status_of(plugin_id)
        if record.status is PluginStatus.UNLOADED:
            return record
        if record.status is PluginStatus.ENABLED:
            record = await self.disable(record.plugin_id)
        plugin = self._plugins.get(record.plugin_id)
        loaded = self._contexts.pop(record.plugin_id, None) is not None
        if plugin is not None and loaded:
            try:
                await plugin.shutdown()
            except Exception as exc:  # noqa: BLE001
                self._withdraw(record.plugin_id)
                return await self._fail(
                    record.plugin_id, f"shutdown() failed: {type(exc).__name__}: {exc}"
                )
        self._withdraw(record.plugin_id)
        record = self._set(record.plugin_id, PluginStatus.UNLOADED)
        await self._emit(EventType.PLUGIN_UNLOADED, plugin_id=record.plugin_id)
        return record

    # -- bulk operations, where isolation is the point -------------------------

    async def load_all(self) -> tuple[PluginRecord, ...]:
        """Load every known plugin, one failure at a time."""
        return await self._walk(self.load)

    async def initialize_all(self) -> tuple[PluginRecord, ...]:
        """Initialize every known plugin, one failure at a time."""
        return await self._walk(self.initialize)

    async def enable_all(self) -> tuple[PluginRecord, ...]:
        """Enable every known plugin. Plugins that fail stay behind."""
        return await self._walk(self.enable)

    async def disable_all(self) -> tuple[PluginRecord, ...]:
        """Disable every enabled plugin."""
        return await self._walk(self.disable)

    async def unload_all(self) -> tuple[PluginRecord, ...]:
        """Unload every plugin, withdrawing everything each one contributed."""
        return await self._walk(self.unload)

    async def _walk(
        self, step: Callable[[str], Awaitable[PluginRecord]]
    ) -> tuple[PluginRecord, ...]:
        records: list[PluginRecord] = []
        for plugin_id in self.plugins():
            try:
                records.append(await step(plugin_id))
            except Exception as exc:  # noqa: BLE001 - the walk never stops
                records.append(
                    await self._fail(
                        plugin_id,
                        f"{getattr(step, '__name__', 'step')} could not run: "
                        f"{type(exc).__name__}: {exc}",
                    )
                )
        return tuple(records)

    # -- what the manager knows ------------------------------------------------

    def plugins(self) -> tuple[str, ...]:
        """The ids of the plugins this manager knows — unloaded ones included.

        An unloaded plugin can be loaded again, so it stays known; a plugin
        REJECTED at registration never enters this set, because there is no
        instance to run.
        """
        return tuple(sorted(self._plugins))

    def status(self, plugin_id: str) -> PluginRecord:
        """This plugin's record, including rejected ones. Raises if never seen."""
        return self._status_of(plugin_id)

    def statuses(self) -> tuple[PluginRecord, ...]:
        """Every record, sorted by id — rejected plugins included."""
        return tuple(self._records[plugin_id] for plugin_id in sorted(self._records))

    def rejected(self) -> tuple[PluginRecord, ...]:
        """The plugins that are not usable, each with the reason why."""
        return tuple(
            record
            for record in self.statuses()
            if record.status in (PluginStatus.FAILED, PluginStatus.DENIED)
        )

    def enabled_ids(self) -> tuple[str, ...]:
        return tuple(
            record.plugin_id
            for record in self.statuses()
            if record.status is PluginStatus.ENABLED
        )

    def capability_ids(self) -> tuple[str, ...]:
        """The capabilities the ENABLED plugins contribute, mirrored or not."""
        ids: list[str] = []
        for plugin_id in self.enabled_ids():
            for name in self._plugin_capabilities.get(plugin_id, ()):
                ids.append(name)
        return tuple(ids)

    def tool_names(self) -> tuple[str, ...]:
        """The tools the ENABLED plugins contribute."""
        names: list[str] = []
        for plugin_id in self.enabled_ids():
            names.extend(self._plugin_tools.get(plugin_id, ()))
        return tuple(names)

    def tools_of(self, plugin_id: str) -> tuple[PluginToolMetadata, ...]:
        record = self._status_of(plugin_id)
        plugin = self._plugins.get(record.plugin_id)
        if plugin is None:
            return ()
        return tuple(
            PluginToolMetadata(
                name=tool.name,
                plugin_id=record.plugin_id,
                risk=plugin.risk_level,
                permissions=tuple(tool.required_permissions) or tuple(plugin.permissions),
            )
            for tool in plugin.tools
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugins": [record.to_dict() for record in self.statuses()],
            "enabled": list(self.enabled_ids()),
            "capabilities": list(self.capability_ids()),
            "tools": list(self.tool_names()),
            "core_version": self.core_version,
        }

    def __contains__(self, plugin_id: object) -> bool:
        return str(plugin_id) in self._records

    # -- internals -------------------------------------------------------------

    def _identity(self, plugin: Plugin) -> str:
        declared = str(plugin.plugin_id or "").strip()
        if declared:
            return declared
        fallback = str(plugin.name or "").strip()
        return _slug(fallback) if fallback else f"plugin-{len(self._records) + 1}"

    def _status_of(self, plugin_id: str) -> PluginRecord:
        try:
            return self._records[str(plugin_id)]
        except KeyError as exc:
            raise KeyError(f"Plugin is not registered: {plugin_id}") from exc

    def _store(self, record: PluginRecord) -> PluginRecord:
        self._records[record.plugin_id] = record
        return record

    def _set(
        self,
        plugin_id: str,
        status: PluginStatus,
        *,
        error: str | None = None,
        approval_id: str | None = None,
    ) -> PluginRecord:
        record = self._records[plugin_id]
        return self._store(
            record.with_updates(status=status, error=error, approval_id=approval_id)
        )

    def _reject(self, label: str, reason: str) -> PluginRecord:
        # A rejection must never overwrite a record that already exists: two
        # directories whose names slug to the same id (plugin.py vs RECORDER/)
        # would otherwise turn a working plugin into a failed one.
        plugin_id = self._unique_key(_slug(label))
        return self._store(
            PluginRecord(
                plugin_id=plugin_id,
                manifest=PluginManifest(name=label, version="0.0.0"),
                status=PluginStatus.FAILED,
                error=reason,
            )
        )

    def _unique_key(self, base: str) -> str:
        """A record key that is free to use, derived from ``base``."""
        key = base or "plugin"
        if key not in self._records:
            return key
        index = 2
        while f"{key}-{index}" in self._records:
            index += 1
        return f"{key}-{index}"

    async def _fail(self, plugin_id: str, reason: str) -> PluginRecord:
        """Record a failure, announce it, and hand the record back.

        The event is what makes a contained failure VISIBLE: the caller gets a
        FAILED record instead of an exception, so without ``plugin.failed`` a
        plugin that quietly stopped working would be noticed only by whoever
        went looking for its status.
        """
        record = self._records.get(plugin_id)
        if record is None:
            record = PluginRecord(
                plugin_id=plugin_id,
                manifest=PluginManifest(name=plugin_id, version="0.0.0"),
                status=PluginStatus.FAILED,
            )
        _logger.warning("Plugin %s failed: %s", plugin_id, reason)
        stored = self._store(
            record.with_updates(status=PluginStatus.FAILED, error=reason)
        )
        await self._emit(EventType.PLUGIN_FAILED, plugin_id=stored.plugin_id, error=reason)
        return stored

    def _resolved_configuration(self, manifest: PluginManifest) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for field in manifest.configuration:
            if field.default is not None:
                resolved[field.name] = field.default
        resolved.update(self._configuration.get(manifest.plugin_id, {}))
        return resolved

    def _enable_problems(self, plugin: Plugin) -> tuple[str, ...]:
        try:
            problems = list(self.validate(plugin))
        except Exception as exc:  # noqa: BLE001 - unreadable declarations are a refusal
            problems = [f"the declarations could not be read: {type(exc).__name__}: {exc}"]
        if plugin.tools and self.tools is None:
            problems.append("the plugin declares tools but no tool registry is attached")
        problems.extend(self._requirement_problems(plugin))
        return tuple(problems)

    def _requirement_problems(self, plugin: Plugin) -> tuple[str, ...]:
        """What the plugin says it requires, checked against what is here.

        ``required_capabilities`` is a declaration, not a decoration: a plugin
        that needs ``browser.search`` must not be switched on in an installation
        that does not have it, because the failure would surface later as its
        tool misbehaving. When no registry is attached the requirement cannot be
        confirmed — which is reported as its own reason rather than assumed
        either way.
        """
        if not plugin.required_capabilities:
            return ()
        if self.capabilities is None:
            return tuple(
                f"requires capability {capability_id!r}, and no capability registry is "
                "attached to confirm this installation has it"
                for capability_id in plugin.required_capabilities
            )
        getter = getattr(self.capabilities, "get", None)
        if not callable(getter):
            return ()
        problems: list[str] = []
        for capability_id in plugin.required_capabilities:
            try:
                found = getter(capability_id)
            except Exception:  # noqa: BLE001 - a registry that cannot answer is not a denial
                found = None
            if found is None:
                problems.append(
                    f"requires capability {capability_id!r}, which this installation "
                    "does not have"
                )
        return tuple(problems)

    def _declare(self, plugin: Plugin) -> None:
        """Publish the plugin's declarations to the centralized layer.

        Registered once per plugin and once per tool, so the layer that gates a
        plugin's tools is the same one that gates core tools — with the plugin's
        own statement as the source, and the tool's own scopes where it has any.
        """
        self.permissions.declare(plugin.plugin_id, plugin.declaration())
        for tool in plugin.tools:
            self.permissions.declare(tool.name, plugin.declaration_for_tool(tool))

    def _register_tools(self, plugin: Plugin, plugin_id: str) -> tuple[str, ...]:
        self._plugin_tools[plugin_id] = ()
        if not plugin.tools:
            return ()
        if self.tools is None:
            raise RuntimeError("the plugin declares tools but no tool registry is attached")
        names: list[str] = []
        for tool in plugin.tools:
            self.tools.register(tool)
            names.append(tool.name)
            self._plugin_tools[plugin_id] = tuple(names)
            if self.catalog is not None:
                existed = tool.name in self.catalog
                self.catalog.register(self._metadata_for(plugin, tool))
                if not existed:
                    self._catalog_created.add(tool.name)
        return tuple(names)

    def _metadata_for(self, plugin: Plugin, tool: FunctionTool) -> ToolMetadata:
        declaration = plugin.declaration_for_tool(tool)
        return ToolMetadata(
            name=tool.name,
            description=tool.schema.description,
            category=ToolCategory.GENERIC,
            capabilities=tuple(capability.name for capability in plugin.capabilities),
            input_schema=tool.schema,
            risk=plugin.risk_level,
            permissions=tuple(tool.required_permissions) or tuple(plugin.permissions),
            tags=("plugin", plugin.plugin_id),
            registered=True,
            required_permission=declaration.required_permission,
            destructive=declaration.destructive,
            reversible=declaration.reversible,
            external_side_effect=declaration.external_side_effect,
        )

    def _register_capabilities(self, plugin: Plugin, plugin_id: str) -> tuple[str, ...]:
        """Mirror the plugin's capabilities into the registry, as they land.

        Recorded one at a time for the same reason the tools are: a failure on
        the second capability must not leave the first one visible in the
        capability registry with nothing left to withdraw it.
        """
        self._plugin_capabilities[plugin_id] = ()
        register_action = getattr(self.capabilities, "register_action", None)
        if not callable(register_action):
            declared = tuple(
                f"plugin.{plugin.plugin_id}.{capability.name}"
                for capability in plugin.capabilities
            )
            self._plugin_capabilities[plugin_id] = declared
            return declared
        registered: list[str] = []
        for capability in plugin.capabilities:
            capability_id = f"plugin.{plugin.plugin_id}.{capability.name}"
            register_action(
                action=capability.name,
                description=capability.description
                or capability.entrypoint
                or f"Provided by plugin {plugin.plugin_id}.",
                capability_id=capability_id,
                category="plugin",
                tools=tuple(tool.name for tool in plugin.tools),
                risk=plugin.risk_level,
                permissions=tuple(scope.value for scope in plugin.permissions),
                executor="plugin",
                tags=("plugin", plugin.plugin_id),
            )
            registered.append(capability_id)
            self._plugin_capabilities[plugin_id] = tuple(registered)
        return tuple(registered)

    def _withdraw(self, plugin_id: str) -> None:
        """Take back everything one plugin contributed, and nothing else."""
        for name in self._plugin_tools.pop(plugin_id, ()):
            if self.tools is not None and name in self.tools:
                self.tools.unregister(name)
            if self.catalog is not None and name in self._catalog_created:
                self._catalog_created.discard(name)
                unregister = getattr(self.catalog, "unregister", None)
                if callable(unregister):
                    unregister(name)
        unregister_capability = getattr(self.capabilities, "unregister", None)
        for capability_id in self._plugin_capabilities.pop(plugin_id, ()):
            if callable(unregister_capability):
                unregister_capability(capability_id)

    async def _call_quietly(self, plugin: Plugin, hook: str) -> None:
        method = getattr(plugin, hook, None)
        if not callable(method):
            return
        try:
            await method()
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise again
            _logger.warning(
                "Plugin %s %s() failed during cleanup: %s: %s",
                plugin.plugin_id,
                hook,
                type(exc).__name__,
                exc,
            )

    async def _emit(self, type_: EventType, **payload: Any) -> None:
        """Announce a lifecycle moment, never letting a watcher break it."""
        if self._events is None:
            return
        await self._events.emit(type_, source="plugins", **payload)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.strip().split("."))


def _import_plugin(path: Path, label: str) -> Plugin:
    """Import a plugin module from a file and hand back its ``plugin`` object."""
    module_name = f"novacontrol_plugin_{re.sub(r'[^0-9a-zA-Z_]+', '_', label)}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(
            f"cannot load a module from {path}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    candidate = getattr(module, "plugin", None)
    if candidate is None:
        raise AttributeError("'plugin.py' does not define a 'plugin' object")
    if isinstance(candidate, type) and issubclass(candidate, Plugin):
        candidate = candidate()
    if not isinstance(candidate, Plugin):
        raise TypeError(
            f"'plugin' must be a Plugin (or a Plugin subclass), not {type(candidate).__name__}"
        )
    return candidate


def _manifest_conflicts(manifest: PluginManifest, plugin: Plugin) -> tuple[str, ...]:
    """Where the installed manifest and the code disagree, the code must not win.

    A ``plugin.json`` is what a person approved at install time; a plugin whose
    Python asks for MORE than that has changed its demands after the fact, and
    is rejected rather than silently granted the extra scope.
    """
    problems: list[str] = []
    if (
        plugin.plugin_id.strip()
        and manifest.plugin_id.strip()
        and plugin.plugin_id.strip() != manifest.plugin_id.strip()
        and plugin.name.strip() != manifest.name.strip()
    ):
        problems.append(
            f"plugin.py declares {plugin.plugin_id!r} / {plugin.name!r} but "
            f"plugin.json declares {manifest.plugin_id!r} / {manifest.name!r}"
        )
    installed_scopes = set(manifest.permissions)
    for scope in plugin.permissions:
        if scope.value not in installed_scopes:
            problems.append(
                f"plugin.py requires {scope.value!r}, which plugin.json does not declare"
            )
    if _risk_rank(plugin.risk_level) < _risk_rank(manifest.risk):
        problems.append(
            f"plugin.py claims {plugin.risk_level.value!r} risk, below the "
            f"{manifest.risk.value!r} risk plugin.json declares"
        )
    if plugin.version.strip() and manifest.version.strip() and (
        plugin.version.strip() != manifest.version.strip()
    ):
        problems.append(
            f"plugin.py declares version {plugin.version!r} but plugin.json declares "
            f"{manifest.version!r}"
        )
    installed_fields = {field.name for field in manifest.configuration}
    for field in plugin.configuration_schema:
        if field.name not in installed_fields:
            problems.append(
                f"plugin.py reads configuration field {field.name!r} that plugin.json "
                "does not declare"
            )
    if plugin.network_access and not manifest.network_access:
        problems.append("plugin.py declares network access that plugin.json does not")
    for path in plugin.filesystem_access:
        if path not in manifest.filesystem_access:
            problems.append(
                f"plugin.py declares filesystem access to {path!r} that plugin.json does not"
            )
    for service in plugin.external_services:
        if service not in manifest.external_services:
            problems.append(
                f"plugin.py declares external service {service!r} that plugin.json does not"
            )
    return tuple(problems)


def _risk_rank(level: RiskLevel) -> int:
    return {
        RiskLevel.LOW: 0,
        RiskLevel.MEDIUM: 1,
        RiskLevel.HIGH: 2,
        RiskLevel.CRITICAL: 3,
    }[level]


def _placeholder_manifest(plugin_id: str, plugin: Plugin) -> PluginManifest:
    """A manifest for a plugin whose own declarations cannot be trusted.

    Used on the rejection path, where the plugin may have no usable name or
    version at all — the record still has to exist and still has to describe
    which plugin it is about, so nothing here is allowed to raise.
    """
    name = str(getattr(plugin, "name", "") or "").strip() or plugin_id
    version = str(getattr(plugin, "version", "") or "").strip() or "0.0.0"
    try:
        return PluginManifest(
            name=name,
            version=version,
            plugin_id=plugin_id,
            description=str(getattr(plugin, "description", "") or ""),
        )
    except Exception:  # noqa: BLE001 - the fallback must not raise either
        return PluginManifest(name="plugin", version="0.0.0", plugin_id=plugin_id)


def _configuration_problems(
    manifest: PluginManifest,
    values: Mapping[str, Any],
    *,
    require_all: bool,
) -> tuple[str, ...]:
    problems: list[str] = []
    for field in manifest.configuration:
        if field.name in values:
            check = _TYPE_CHECKS.get(field.type)
            if check is not None and not check(values[field.name]):
                problems.append(
                    f"configuration field {field.name!r} must be a {field.type}, "
                    f"got {type(values[field.name]).__name__}"
                )
        elif field.required and field.default is None and require_all:
            problems.append(f"missing required configuration: {field.name!r}")
    return tuple(problems)


__all__ = [
    "CAPABILITY_KINDS",
    "PluginLifecycleError",
    "PluginManager",
    "PluginToolMetadata",
    "PluginValidationError",
]
