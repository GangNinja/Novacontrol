"""GUI state models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DashboardTab(StrEnum):
    DASHBOARD = "dashboard"
    CHAT = "chat"
    INTELLIGENCE = "intelligence"
    EXPLORE = "explore"
    DEMOS = "demos"
    AUTOMATION = "automation"
    TASKS = "tasks"
    MEMORY = "memory"
    PLUGINS = "plugins"
    SETTINGS = "settings"
    LOGS = "logs"
    PERFORMANCE = "performance"
    PROJECTS = "projects"

    @property
    def label(self) -> str:
        return {
            DashboardTab.DASHBOARD: "Dashboard",
            DashboardTab.CHAT: "Chat",
            DashboardTab.INTELLIGENCE: "Intelligence",
            DashboardTab.EXPLORE: "Explore",
            DashboardTab.DEMOS: "Demos",
            DashboardTab.AUTOMATION: "Automation",
            DashboardTab.TASKS: "Tasks",
            DashboardTab.MEMORY: "Memory",
            DashboardTab.PLUGINS: "Plugins",
            DashboardTab.SETTINGS: "Settings",
            DashboardTab.LOGS: "Logs",
            DashboardTab.PERFORMANCE: "Performance",
            DashboardTab.PROJECTS: "Projects",
        }[self]


@dataclass(frozen=True, slots=True)
class DashboardState:
    tabs: tuple[DashboardTab, ...]
    active_tab: DashboardTab = DashboardTab.CHAT
    task_count: int = 0
    memory_count: int = 0
    plugin_count: int = 0
    logs: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tabs": [tab.value for tab in self.tabs],
            "active_tab": self.active_tab.value,
            "task_count": self.task_count,
            "memory_count": self.memory_count,
            "plugin_count": self.plugin_count,
            "logs": self.logs,
            "metrics": self.metrics,
        }
