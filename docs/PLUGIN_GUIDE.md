# Plugin Guide

Plugins extend NovaControl without modifying the core. A plugin may provide agents, skills, tools, integrations, models, workflows, or UI panels.

## Plugin Contract

A plugin should declare:

- Name and version
- Capabilities
- Permission scopes
- Runtime modules
- Event subscriptions
- Optional configuration schema

## Security

Plugins must declare sensitive capabilities before they are enabled. Examples include filesystem writes, shell execution, network access, browser form submission, credentials, desktop control, camera, microphone, and external API calls.

## Lifecycle

1. Discover plugin metadata.
2. Validate compatibility and permissions.
3. Ask the user to approve requested scopes.
4. Register plugin modules with the runtime.
5. Subscribe plugin handlers to events.
6. Audit plugin actions.

Concrete plugin loading is scheduled for Phase 13. Phase 1 defines the extension boundary.
