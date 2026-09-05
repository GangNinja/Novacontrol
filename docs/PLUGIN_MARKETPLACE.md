# Plugin Marketplace

The Phase 13 plugin marketplace subsystem provides plugin metadata, discovery, installation records, permission approval, trust, enable, and disable flows.

## Manifest

Plugins declare:

- Name
- Version
- Description
- Minimum core version
- Permissions
- Capabilities

Example:

```json
{
  "name": "sample",
  "version": "1.0.0",
  "description": "Sample plugin",
  "permissions": ["network:access"],
  "capabilities": [
    {
      "name": "sample-tool",
      "type": "tool",
      "entrypoint": "sample:tool"
    }
  ]
}
```

## Components

- `PluginManifest`: plugin metadata
- `PluginCapability`: plugin-provided capability
- `PluginInstallRecord`: install/trust/enable state
- `PluginMarketplace`: discovery and lifecycle manager
- `PluginRepository`: plugin record storage interface
- `InMemoryPluginRepository`: test and embedded repository
- `PluginMarketplaceModule`: event-driven runtime module

## Security

Plugins with permissions require approval before installation. The default approval gateway denies sensitive plugin installation.

## CLI

```powershell
python -m novacontrol demo phase13
```
