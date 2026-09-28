"""DeveloperPlugin: an example plugin that inspects a workspace, read-only.

The code agent, the build scaffold and the workspace artifact store already
exist in the core, so this plugin does not reimplement any of them. It answers
the one question they all ask first — what is actually in this directory? — and
it does so under an honest declaration: ``filesystem:read``, the path it says it
reads, and LOW risk because reading a directory listing changes nothing.

That combination is the interesting part of the example: a permission is not a
verdict. The centralized policy asks a person about MEDIUM risk and above, so a
read-only plugin declaring a scope is still enabled without an approval —
while the same permission on a plugin that declares HIGH risk is not. The
declaration travels with the plugin; the policy decides what to do about it.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from novacontrol.core.security import PermissionScope, RiskLevel
from novacontrol.plugins.models import PluginCapability
from novacontrol.plugins.sdk import Plugin, plugin_tool
from novacontrol.tools.models import ToolParameter
from novacontrol.tools.registry import FunctionTool

#: Never walk more than this many entries in one call: an example that can be
#: asked to enumerate a whole drive is not an example, it is a hang.
_MAX_ENTRIES = 200


class DeveloperPlugin(Plugin):
    plugin_id = "developer-tools"
    name = "Developer Tools"
    version = "1.0.0"
    description = "Read-only inspection of the workspace a developer is in."
    author = "NovaControl examples"
    risk_level = RiskLevel.LOW
    permissions = (PermissionScope.FILESYSTEM_READ,)
    filesystem_access = (".",)

    capabilities = (
        PluginCapability(
            name="developer.workspace_files",
            type="tool",
            entrypoint="developer-tools:workspace_files",
            description="List the files and folders under a workspace directory.",
        ),
    )

    def __init__(self) -> None:
        self.tools: tuple[FunctionTool, ...] = (
            plugin_tool(
                "developer_workspace_files",
                "List the entries of a workspace directory, read-only.",
                self._workspace_files,
                parameters=(
                    ToolParameter(
                        name="path",
                        type="string",
                        description="Directory to list; defaults to the current one.",
                    ),
                    ToolParameter(
                        name="limit",
                        type="integer",
                        description=f"Maximum entries to return (<= {_MAX_ENTRIES}).",
                    ),
                ),
                required_permissions=(PermissionScope.FILESYSTEM_READ,),
            ),
        )

    async def _workspace_files(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        root = Path(str(arguments.get("path") or ".")).expanduser()
        try:
            limit = int(arguments.get("limit", 100))
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, _MAX_ENTRIES))
        if not root.is_dir():
            return {"path": str(root), "exists": False, "entries": []}
        entries: list[dict[str, Any]] = []
        for candidate in sorted(root.iterdir())[:limit]:
            entry = {"name": candidate.name, "directory": candidate.is_dir()}
            if candidate.is_file():
                try:
                    entry["bytes"] = candidate.stat().st_size
                except OSError:
                    entry["bytes"] = None
            entries.append(entry)
        return {
            "path": str(root),
            "exists": True,
            "entries": entries,
            "truncated": len(entries) >= limit,
        }
