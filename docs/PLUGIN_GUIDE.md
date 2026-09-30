# Plugin Guide (Phase 10 — the Plugin SDK)

A plugin adds functionality to NovaControl **without modifying the core**. The
extension boundary is one class — `novacontrol.plugins.Plugin` — and one rule:
a plugin declares what it is and what it may touch, and a manager decides
whether it runs.

The marketplace (`docs/PLUGIN_MARKETPLACE.md`) answers *may this be installed?*
This document is about the other half: *how does it run, and what happens when
it breaks?*

## The interface

```python
from novacontrol.core.security import PermissionScope, RiskLevel
from novacontrol.plugins import Plugin, plugin_tool
from novacontrol.plugins.models import PluginCapability, PluginConfigurationField
from novacontrol.tools.models import ToolParameter


class HelloPlugin(Plugin):
    plugin_id = "hello"            # stable identity (lowercase, punctuation-free)
    name = "Hello"                 # display label
    version = "1.0.0"
    description = "Says hello."
    author = "you"

    capabilities = (
        PluginCapability(name="hello.greet", type="tool", description="Greet."),
    )
    permissions = ()                                   # what it may touch
    risk_level = RiskLevel.LOW                         # how bad a mistake would be
    required_capabilities = ()                         # what it needs first
    external_services = ()                             # remote things it talks to
    filesystem_access = ()                             # paths it reads or writes
    network_access = False                             # reaches the network?
    configuration_schema = (
        PluginConfigurationField(name="greeting", default="hello"),
    )

    def __init__(self) -> None:
        self.tools = (
            plugin_tool(
                "hello_greet",
                "Greet by name.",
                self._greet,
                parameters=(ToolParameter(name="who", required=True),),
            ),
        )

    async def load(self, context) -> None:
        self._greeting = str(context.configuration.get("greeting", "hello"))

    async def enable(self) -> None:
        ...      # start serving

    async def disable(self) -> None:
        ...      # stop serving (the manager withdraws the tools for you)

    async def shutdown(self) -> None:
        ...      # release everything

    async def _greet(self, arguments):
        return {"greeting": f"{self._greeting}, {arguments['who']}"}


plugin = HelloPlugin()      # what a plugin.py module exposes
```

Every declaration the specification names is a class attribute, and every
lifecycle hook has a default — a plugin that only contributes a tool implements
`enable` and nothing else.

| Declaration | Meaning |
| --- | --- |
| `plugin_id` | Stable identity. Records, events and approvals are keyed on it; the display name may change, the id may not. Empty means "derive one from `name`". |
| `name`, `version`, `description`, `author` | Identity a person reads. `version` must be a dotted number. |
| `capabilities` | What it contributes: `PluginCapability(name, type, entrypoint, description)` with `type` one of `tool`, `agent`, `skill`, `integration`, `model`, `workflow`, `panel`. |
| `tools` | The callable tools it adds, built with `plugin_tool(...)` (name, description, handler, parameters, required scopes). |
| `permissions` | The `PermissionScope`s it actually needs. |
| `risk_level` | The plugin's own claim: `low`, `medium`, `high`, `critical`. |
| `required_capabilities` | Capability ids it expects this installation to have. |
| `external_services` | Remote services it talks to (each one requires `network:access`). |
| `filesystem_access` | The paths it touches (requires `filesystem:read` or `filesystem:write`). |
| `network_access` | Whether it reaches the network (requires `network:access`). |
| `configuration_schema` | The settings it reads: name, type (`string`, `integer`, `number`, `boolean`, `list`), required, default. |

`manifest()` turns all of that into the same `PluginManifest` a `plugin.json`
produces, so the marketplace, the manager, the permission layer and the event
payloads describe a plugin identically.

## Lifecycle

```
discover → load → initialize → enable → disable → shutdown
                       ▲                     │
                       └────── enable ◀──────┘   (enable again after disable)
```

- **load** — handed a `PluginContext`: its resolved configuration, the live tool
  registry, the event bus, a logger. The plugin reads its settings here.
- **initialize** — open what needs opening (a client, a cache) before anything
  calls it.
- **enable** — switch on. *After* this returns, the manager registers the
  plugin's tools and capabilities; a plugin never registers its own.
- **disable** — stop serving. The manager withdraws the tools, catalogue
  entries, capabilities and the plugin's own permission declarations whether or
  not `disable` succeeded; a disabled plugin is not just uncallable, it is
  unknown to the registries and the risk layer again.
- **shutdown** — release everything. Unloading an enabled plugin disables it
  first, so nothing is ever shut down while it is still callable.

The manager walks a plugin to where it needs to be: `enable()` on a fresh plugin
runs `load` → `initialize` → `enable`, in that order, exactly once. Calling a
hook twice is a no-op, not a second run.

Three consequences worth knowing before you write a plugin:

- **Settings are read by `load`.** `configure()` is therefore refused once a
  plugin has been loaded (`PluginLifecycleError`) — otherwise the new values
  would sit there doing nothing. To reconfigure: `unload`, `configure`, `load`.
- **Unload keeps the plugin known.** Unloading drops the runtime state (its
  context) and calls `shutdown`, and the plugin can be loaded again afterwards;
  the record stays so status still answers. Hooks run once per transition, so a
  reload runs `load` → `initialize` → `enable` a second time, in order.
- **Enable after disable works**, as the diagram shows: a disabled plugin is
  still initialized, so `enable()` switches it back on without a second `load`,
  re-declaring it to the permission layer and re-registering its tools and
  capabilities on the way. (Both halves of that — the transition and the
  exhaustive withdrawal — are pinned by
  `tests/test_plugin_sdk_verification.py`.)

## The manager

```python
from novacontrol.plugins import PluginManager
from novacontrol.tools import ToolRegistry

manager = PluginManager(tools=ToolRegistry())
manager.register(HelloPlugin())                  # validates, records, DISCOVERED
records = await manager.discover("./my-plugins") # one subdirectory per plugin
await manager.load_all()                         # isolation per plugin
await manager.enable_all()
await manager.disable("hello")
await manager.unload("hello")
manager.status("hello")                          # the record, with the reason
manager.rejected()                               # what is not usable, and why
manager.to_dict()                                # the whole picture
```

**Discovery** looks at each subdirectory of the given directory and reads
`plugin.py` (which must expose a `plugin` object — an instance or a `Plugin`
subclass) and, optionally, `plugin.json`. A directory that cannot be imported,
or a `plugin.json` that cannot be read, becomes a FAILED record with the reason
— the scan continues, because a folder someone is halfway through writing must
not hide every other plugin.

**Validation** happens at registration, not at first call: required identity,
version shape, `min_core_version` against this build, known capability kinds,
duplicate names, tool names already taken, permission-scope honesty (declaring
network access or filesystem paths without the scope that permits them is a
rejection), and configuration-schema sanity. A rejected plugin is a record with
`status="failed"` and every problem in `error` — never an exception, because a
plugin's mistake must not be the process's problem.

**Containment** is the point. Every hook is wrapped: an exception in `load`,
`initialize`, `enable`, `disable` or `shutdown` marks that plugin FAILED with
the exception named, publishes `plugin.failed`, and leaves every other plugin
working. `load_all()` / `initialize_all()` / `enable_all()` / `unload_all()`
walk the whole set and stop at nothing. A declaration the manager cannot even
read (`capabilities = None`, an empty version, a tool with no schema) is a
rejection with a reason, never an exception out of the manager: a plugin's
mistake must not become the process's problem.

Registration is also all-or-nothing in the *other* direction: tools and
capabilities are recorded as they land, so if the third tool fails to register
the first two are withdrawn before the plugin is marked FAILED. A half-enabled
plugin leaves nothing behind in the tool registry, the catalogue or the
capability registry.

**Status** is exposed as `PluginRecord`: `plugin_id`, its manifest, its status
(`discovered` / `loaded` / `initialized` / `enabled` / `disabled` / `unloaded` /
`denied` / `failed`), the error when there is one, the approval id when one was
granted, and when it last changed.

## Security

Plugins declare; the centralized `PermissionManager` decides. On `enable`:

1. The plugin's declaration is **registered with the permission layer** —
   `PermissionManager.declare(plugin_id, ...)`, and one declaration per tool, so
   running a plugin's tool is gated by the same layer that gates a core tool.
2. `assess()` answers whether a person must be asked, using this build's policy
   (a plugin at `medium` risk or above, or one that cannot be undone, asks).
3. If an approval is required, it is requested from the same `ApprovalGateway`
   the rest of the system uses. Refused → `status="denied"`, the plugin stays
   loaded and initialized, nothing it declared becomes callable.
4. Only then does `enable()` run, and only after it returns are the tools,
   capabilities and catalogue entries registered.

Two properties worth stating plainly:

- **A permission is not a verdict.** A read-only plugin declaring
  `filesystem:read` at `low` risk is enabled without a person being asked; the
  same scope on a plugin that declares `high` risk is not.
- **The code may not outgrow its manifest.** When a plugin directory also
  carries a `plugin.json`, the Python may not claim *more* than the installed
  manifest: extra scopes, a lower risk level, undeclared network access,
  undeclared paths or services, a different version, or a configuration field
  the manifest never declared are all rejections. What a person approved at
  install time is the ceiling.
- **`required_capabilities` is checked, not decoration.** A plugin that needs
  `browser.search` is not switched on in an installation that does not have it —
  the enable reports that reason instead of letting its tool misbehave later.
  With no capability registry attached the requirement cannot be confirmed,
  which is reported as its own reason rather than assumed either way.

Nothing registered survives a disable or an unload: tools are removed from the
`ToolRegistry`, metadata from the `ToolCatalog`, capabilities from the
`CapabilityRegistry` (all three have an `unregister` for exactly this), so
"what can this machine do?" stops listing work that can no longer be carried
out.

## Events

The lifecycle is announced on the same bus everything else uses, through
`emit()` (a broken watcher cannot fail a plugin operation):

`plugin.loaded`, `plugin.initialized`, `plugin.enabled`, `plugin.disabled`,
`plugin.unloaded`, `plugin.failed` — all carrying `plugin_id`, the last one
carrying `error` as well. They are part of the typed vocabulary in
`core/events.py`, so their payloads are validated where they are published.

## Example plugins

Three minimal plugins ship as the SDK's runnable documentation
(`novacontrol/plugins/examples/`). None of them reimplements core behaviour:

| Plugin | Declares | Contributes |
| --- | --- | --- |
| `SystemPlugin` | nothing (LOW risk, no scopes) | `system_platform_info` — platform, machine, interpreter |
| `DeveloperPlugin` | `filesystem:read`, path `.` | `developer_workspace_files` — a read-only directory listing |
| `BrowserPlugin` | nothing (LOW risk) | `browser_classify_url` — local/secure/credential classification, no browsing |

```powershell
python -m novacontrol demo phase10_sdk
```

The demo loads all three, enables them, calls a plugin tool, and shows the one
deliberately broken plugin failing while everything else keeps working.

## Marketplace relationship

| | `PluginMarketplace` | `PluginManager` |
| --- | --- | --- |
| Answers | may this be installed? | is it running, and working? |
| State | durable `PluginInstallRecord` (trust, approval, timestamps) | in-process `PluginRecord` (lifecycle status, error) |
| Input | `plugin.json` manifests | `Plugin` objects and plugin directories |
| Approval | at install, when scopes are requested | at enable, when the policy asks |

Both publish `plugin.enabled` / `plugin.disabled` with the same `plugin_id`
field, so one subscriber reads either publisher.
