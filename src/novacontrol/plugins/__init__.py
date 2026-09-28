"""Plugin subsystems: the marketplace (Phase 13) and the SDK (Phase 10).

Both doors describe a plugin the same way — :class:`PluginManifest` — and both
ask the same approval gateway before something sensitive is switched on. The
marketplace answers "may this be installed?"; the manager answers "is it
loaded, enabled, and working right now".
"""

from novacontrol.plugins.manager import PluginMarketplace
from novacontrol.plugins.models import (
    PluginCapability,
    PluginConfigurationField,
    PluginInstallRecord,
    PluginManifest,
    PluginRecord,
    PluginStatus,
)
from novacontrol.plugins.repository import InMemoryPluginRepository, PluginRepository
from novacontrol.plugins.runtime import PluginMarketplaceModule
from novacontrol.plugins.sdk import Plugin, PluginContext, plugin_tool
from novacontrol.plugins.sdk_manager import (
    PluginLifecycleError,
    PluginManager,
    PluginToolMetadata,
    PluginValidationError,
)

__all__ = [
    "InMemoryPluginRepository",
    "Plugin",
    "PluginCapability",
    "PluginConfigurationField",
    "PluginContext",
    "PluginInstallRecord",
    "PluginLifecycleError",
    "PluginManager",
    "PluginManifest",
    "PluginMarketplace",
    "PluginMarketplaceModule",
    "PluginRecord",
    "PluginRepository",
    "PluginStatus",
    "PluginToolMetadata",
    "PluginValidationError",
    "plugin_tool",
]
