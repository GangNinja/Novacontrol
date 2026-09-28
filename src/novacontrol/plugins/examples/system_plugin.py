"""SystemPlugin: an example plugin that reports where it is running.

The smallest honest plugin there is: it declares nothing it does not need —
no permissions, no network, no external service — and contributes one read-only
tool. That the declarations are empty is the point of the example: a plugin is
not asked to justify itself, it is asked to say the truth, and "this reads
nothing and reaches nothing" is a complete answer.

The telemetry layer's job is measuring the machine over time (CPU, memory,
models); this reports the fixed facts about the interpreter and the platform,
which is what a support report or a compatibility check wants.
"""

from __future__ import annotations

import platform
import sys
from collections.abc import Mapping
from typing import Any

from novacontrol.core.security import RiskLevel
from novacontrol.plugins.models import PluginCapability
from novacontrol.plugins.sdk import Plugin, plugin_tool
from novacontrol.tools.registry import FunctionTool


class SystemPlugin(Plugin):
    plugin_id = "system-tools"
    name = "System Tools"
    version = "1.0.0"
    description = "Reports the platform and interpreter NovaControl is running on."
    author = "NovaControl examples"
    risk_level = RiskLevel.LOW

    capabilities = (
        PluginCapability(
            name="system.platform_info",
            type="tool",
            entrypoint="system-tools:platform_info",
            description="Report platform, machine, and interpreter facts.",
        ),
    )

    def __init__(self) -> None:
        self.tools: tuple[FunctionTool, ...] = (
            plugin_tool(
                "system_platform_info",
                "Report the platform, machine type, and Python version in use.",
                self._platform_info,
            ),
        )

    async def _platform_info(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        del arguments
        return {
            "platform": platform.platform(),
            "system": platform.system(),
            "machine": platform.machine(),
            "release": platform.release(),
            "python": platform.python_version(),
            "executable": sys.executable,
            "implementation": platform.python_implementation(),
        }
