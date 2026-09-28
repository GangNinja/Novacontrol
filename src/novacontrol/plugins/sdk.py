"""The stable plugin interface (Phase 10.1).

A plugin is anything that subclasses :class:`Plugin` and declares what it is and
what it needs. The declaration is the whole contract with the rest of
NovaControl: identity (``plugin_id``, ``name``, ``version``, ``description``,
``author``), what it contributes (``capabilities``, ``tools``), what it may
touch (``permissions``, ``risk_level``, ``required_capabilities``,
``external_services``, ``filesystem_access``, ``network_access``), how it is
configured (``configuration_schema``), and the lifecycle hooks a manager drives
(``load`` → ``initialize`` → ``enable`` → ``disable`` → ``shutdown``).

The hooks have defaults, so a plugin overrides only what it has: a stateless
tool provider implements ``enable`` and nothing else, and a plugin that holds a
connection implements ``initialize``/``shutdown`` around it. Every hook may
raise — the manager's job is to contain that, and no plugin failure may reach
the rest of the process (see :mod:`novacontrol.plugins.sdk_manager`).

The declaration is deliberately data rather than magic: the same values are
what the manager validates, what it hands to the centralized
:class:`~novacontrol.reliability.permissions.PermissionManager`, and what it
publishes when the plugin changes state. A plugin cannot be enabled by a claim
it did not make.
"""

from __future__ import annotations

import logging
from abc import ABC
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.core.events import EventBus
from novacontrol.core.security import PermissionScope, RiskLevel
from novacontrol.plugins.models import (
    PluginCapability,
    PluginConfigurationField,
    PluginManifest,
)
from novacontrol.reliability.permissions import PermissionDeclaration
from novacontrol.tools.models import ToolParameter, ToolSchema
from novacontrol.tools.registry import FunctionTool, ToolCallable, ToolRegistry

_logger = logging.getLogger("novacontrol.plugins")


@dataclass(frozen=True, slots=True)
class PluginContext:
    """What every hook is handed: the plugin's id, settings, and the system.

    Passed to ``load`` and available to the plugin afterwards. The tools
    registry and event bus are the LIVE ones the application is using, so a
    plugin registers into the same system the core does rather than into a copy
    of it. ``None`` means this installation has no such surface — a fact, not an
    error, and a plugin that requires one says so through
    ``required_capabilities``.
    """

    plugin_id: str
    configuration: Mapping[str, Any] = field(default_factory=dict)
    tools: ToolRegistry | None = None
    event_bus: EventBus | None = None
    logger: logging.Logger = _logger

    async def publish(self, type_: str, /, **payload: Any) -> None:
        """Announce something on the shared bus, never failing the caller.

        The publisher's own name travels as the event source, so a subscriber
        can tell which plugin spoke without the payload having to say it.
        """
        if self.event_bus is None:
            return
        await self.event_bus.emit(type_, source=f"plugin.{self.plugin_id}", **payload)


class Plugin(ABC):  # noqa: B024 - a base class with defaults, not an abstract contract
    """Base class for every NovaControl plugin.

    Subclasses declare the class attributes below and override the lifecycle
    hooks they need. Nothing is abstract: a plugin that exists only to register
    one tool should not have to write four empty methods.
    """

    #: Stable identity, lowercase and punctuation-free. The manager keys every
    #: record on this, so a name may change and the id may not.
    plugin_id: str = ""
    #: Display name.
    name: str = ""
    version: str = "0.1.0"
    description: str = ""
    author: str = ""
    min_core_version: str = "0.1.0"

    # -- what it contributes ---------------------------------------------------
    capabilities: tuple[PluginCapability, ...] = ()
    tools: tuple[FunctionTool, ...] = ()

    # -- what it may touch (Phase 10.3) ----------------------------------------
    permissions: tuple[PermissionScope, ...] = ()
    risk_level: RiskLevel = RiskLevel.LOW
    required_capabilities: tuple[str, ...] = ()
    external_services: tuple[str, ...] = ()
    filesystem_access: tuple[str, ...] = ()
    network_access: bool = False

    # -- how it is configured --------------------------------------------------
    configuration_schema: tuple[PluginConfigurationField, ...] = ()

    # -- lifecycle hooks -------------------------------------------------------
    #: Called once with the context: read settings, prepare state. The plugin is
    #: known to the manager but contributes nothing to the system yet.
    async def load(self, context: PluginContext) -> None:
        del context

    #: Called once after load: open what the plugin needs (a client, a cache).
    async def initialize(self) -> None:
        return None

    #: Called when the plugin is switched on: its tools and capabilities become
    #: visible to the rest of the system.
    async def enable(self) -> None:
        return None

    #: Called when it is switched off: stop serving, release the loud resources.
    async def disable(self) -> None:
        return None

    #: Called once when it is unloaded: release everything. After this the
    #: plugin's tools are gone from the registry and it holds nothing.
    async def shutdown(self) -> None:
        return None

    # -- descriptions the manager reads ---------------------------------------

    def manifest(self) -> PluginManifest:
        """The plugin's own declarations, in the manifest vocabulary.

        One shape for both doors — a ``plugin.json`` on disk and a Python
        plugin — so the marketplace, the manager, the permission layer and the
        event payloads all describe a plugin the same way.
        """
        return PluginManifest(
            plugin_id=self.plugin_id,
            name=self.name or self.plugin_id,
            version=self.version,
            description=self.description,
            permissions=tuple(scope.value for scope in self.permissions),
            capabilities=tuple(self.capabilities),
            min_core_version=self.min_core_version,
            author=self.author,
            risk=self.risk_level,
            required_capabilities=tuple(self.required_capabilities),
            external_services=tuple(self.external_services),
            filesystem_access=tuple(self.filesystem_access),
            network_access=self.network_access,
            configuration=tuple(self.configuration_schema),
        )

    def declaration(self) -> PermissionDeclaration:
        """What the centralized permission layer should know about this plugin.

        Stated, not guessed: the plugin's own risk level, its first declared
        scope as the scope its sensitivity is "about", and the effects its
        declarations imply. Shell execution counts as an external side effect
        because what a shell command reaches cannot be bounded from here — and
        treating it as retryable would be a claim nobody can support.
        """
        scopes = tuple(self.permissions)
        return PermissionDeclaration(
            risk_level=self.risk_level,
            required_permission=scopes[0] if scopes else None,
            requires_confirmation=False,
            reversible=True,
            destructive=False,
            external_side_effect=(
                self.network_access
                or bool(self.external_services)
                or PermissionScope.SHELL_EXECUTE in scopes
            ),
        )

    def declaration_for_tool(self, tool: FunctionTool) -> PermissionDeclaration:
        """The same declaration, narrowed to one tool's own scopes.

        A tool that declares scopes is about those scopes; a tool that declares
        none inherits the plugin's. The plugin's risk level stays the risk
        level, because a plugin cannot make one of its tools safer than it has
        declared itself to be.
        """
        scopes = tuple(tool.required_permissions) or tuple(self.permissions)
        declared = self.declaration()
        return PermissionDeclaration(
            risk_level=self.risk_level,
            required_permission=scopes[0] if scopes else declared.required_permission,
            requires_confirmation=declared.requires_confirmation,
            reversible=declared.reversible,
            destructive=declared.destructive,
            external_side_effect=declared.external_side_effect,
        )


def plugin_tool(
    name: str,
    description: str,
    function: ToolCallable,
    *,
    parameters: Sequence[ToolParameter] = (),
    required_permissions: Sequence[PermissionScope] = (),
    allow_extra_arguments: bool = False,
) -> FunctionTool:
    """Build a :class:`FunctionTool` from a plugin's declarations.

    The one-liner plugins actually need: a name, a sentence describing it, the
    callable, and the arguments it takes. The schema is built here so a plugin
    author never assembles a ``ToolSchema`` by hand and never accidentally
    publishes a tool the registry cannot validate.
    """
    schema = ToolSchema(
        name=name,
        description=description,
        parameters=tuple(parameters),
        allow_extra_arguments=allow_extra_arguments,
    )
    return FunctionTool(
        name,
        schema,
        function,
        required_permissions=tuple(required_permissions),
    )


__all__ = ["Plugin", "PluginContext", "plugin_tool"]
