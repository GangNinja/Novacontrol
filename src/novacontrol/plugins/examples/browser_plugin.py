"""BrowserPlugin: an example plugin that classifies URLs.

Deliberately NOT another browser controller. NovaControl already has one, and a
plugin that wrapped it would add a second way to do the same thing. What this
plugin contributes is the judgement a browsing layer wants BEFORE it acts: is
this address local or remote, is it secure, does it carry credentials, is it a
file it would have to read. Pure standard library, no network access, no
permissions — the honest declaration for what it actually does.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from novacontrol.core.security import RiskLevel
from novacontrol.plugins.models import PluginCapability
from novacontrol.plugins.sdk import Plugin, plugin_tool
from novacontrol.tools.models import ToolParameter
from novacontrol.tools.registry import FunctionTool

#: Hosts that never leave the machine, and are therefore never a remote
#: action. Kept as a set rather than a guess so the answer is explainable.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

#: Schemes NovaControl considers safe to open as a page.
_WEB_SCHEMES = frozenset({"http", "https"})


class BrowserPlugin(Plugin):
    plugin_id = "browser-tools"
    name = "Browser Tools"
    version = "1.0.0"
    description = "Classifies URLs before anything opens them."
    author = "NovaControl examples"
    risk_level = RiskLevel.LOW

    capabilities = (
        PluginCapability(
            name="browser.classify_url",
            type="tool",
            entrypoint="browser-tools:classify_url",
            description="Judge whether a URL is local, secure, or a file path.",
        ),
    )

    def __init__(self) -> None:
        self.tools: tuple[FunctionTool, ...] = (
            plugin_tool(
                "browser_classify_url",
                "Say whether a URL is local, secure, credential-bearing, or a file.",
                self._classify,
                parameters=(
                    ToolParameter(
                        name="url",
                        type="string",
                        required=True,
                        description="The address to classify.",
                    ),
                ),
            ),
        )

    async def _classify(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        raw = str(arguments.get("url", "")).strip()
        if not raw:
            return {"url": "", "valid": False, "reason": "no url given"}
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        try:
            port = parts.port
        except ValueError:
            # An out-of-range or non-numeric port is a fact about the URL, not a
            # reason for the tool to raise: report the address as unusable.
            return {"url": raw, "valid": False, "reason": "invalid port"}
        return {
            "url": raw,
            "valid": bool(scheme),
            "scheme": scheme,
            "host": host,
            "web": scheme in _WEB_SCHEMES,
            "secure": scheme == "https",
            "local": host in _LOCAL_HOSTS or host.endswith(".local"),
            "has_credentials": bool(parts.username or parts.password),
            "path": parts.path,
            "port": port,
        }
