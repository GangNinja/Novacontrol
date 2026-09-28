# Plugin Marketplace

The plugin subsystem installs plugins (this document) and runs them (see
[PLUGIN_GUIDE.md](PLUGIN_GUIDE.md) for the Phase 10 SDK). Both doors describe a
plugin with the same `PluginManifest` and ask the same approval gateway before
something sensitive is switched on; what differs is the question — *may this be
installed?* versus *is it running, and working?*

## Manifest

Plugins declare:

- Name and version
- Description
- Minimum core version
- Permissions
- Capabilities
- `plugin_id`, `author` (Phase 10)
- Risk level (`low` / `medium` / `high` / `critical`)
- Required capabilities
- External services
- Filesystem access
- Network access
- Configuration schema

Example:

```json
{
  "name": "sample",
  "version": "1.0.0",
  "plugin_id": "sample",
  "description": "Sample plugin",
  "author": "you",
  "min_core_version": "0.1.0",
  "permissions": ["network:access"],
  "risk": "medium",
  "required_capabilities": ["browser.search"],
  "external_services": ["api.example.com"],
  "filesystem_access": [],
  "network_access": true,
  "capabilities": [
    {
      "name": "sample-tool",
      "type": "tool",
      "entrypoint": "sample:tool"
    }
  ],
  "configuration": [
    {"name": "endpoint", "type": "string", "required": true,
     "description": "Base URL of the service."}
  ]
}
```

`plugin_id` is derived from the name when it is absent, so every existing
`plugin.json` keeps working unchanged.

## Components

- `PluginManifest`: plugin metadata, identity and security declarations
- `PluginCapability`: plugin-provided capability
- `PluginConfigurationField`: one declared configuration knob
- `PluginInstallRecord`: install/trust/enable state (marketplace)
- `PluginRecord`: lifecycle status, error and approval id (SDK)
- `PluginMarketplace`: discovery, installation, trust, enable, disable
- `PluginRepository` / `InMemoryPluginRepository`: install-record storage
- `PluginMarketplaceModule`: event-driven runtime module
- `Plugin` / `PluginContext` / `plugin_tool`: the stable SDK interface
- `PluginManager`: discovery, validation, lifecycle, security and containment

## Security

Plugins with permissions require approval before installation; the default
approval gateway denies sensitive plugin installation. The SDK's manager asks
again at enable time, using the build's own policy through the centralized
`PermissionManager` — a plugin that declares `medium` risk or above, or one
whose action cannot be undone, is put to a person before it is switched on, and
a refusal leaves it loaded but not enabled.

A plugin whose Python asks for more than its `plugin.json` declared (extra
scopes, a lower risk level, undeclared network, paths or services) is rejected
when it is discovered rather than trusted: the manifest a person approved is the
ceiling.

## CLI

```powershell
python -m novacontrol demo phase13       # install / trust / enable / deny
python -m novacontrol demo phase10_sdk   # the SDK: load, enable, contain, unload
```
