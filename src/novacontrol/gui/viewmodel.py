"""Dashboard view model."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from novacontrol.gui.models import DashboardState, DashboardTab


class DashboardViewModel:
    """UI-facing state container for the NovaControl dashboard."""

    def __init__(self, state: DashboardState) -> None:
        self.state = state

    @classmethod
    def with_default_tabs(cls) -> "DashboardViewModel":
        return cls(
            DashboardState(
                tabs=(
                    DashboardTab.DASHBOARD,
                    DashboardTab.CHAT,
                    DashboardTab.INTELLIGENCE,
                    DashboardTab.EXPLORE,
                    DashboardTab.DEMOS,
                    DashboardTab.AUTOMATION,
                    DashboardTab.TASKS,
                    DashboardTab.MEMORY,
                    DashboardTab.PROJECTS,
                    DashboardTab.PLUGINS,
                    DashboardTab.SETTINGS,
                    DashboardTab.LOGS,
                    DashboardTab.PERFORMANCE,
                )
            )
        )

    def activate(self, tab: DashboardTab) -> None:
        if tab not in self.state.tabs:
            raise ValueError(f"Tab is not available: {tab.value}")
        self.state = replace(self.state, active_tab=tab)

    def record_log(self, message: str) -> None:
        self.state = replace(self.state, logs=(*self.state.logs, message))

    def update_counts(self, *, tasks: int | None = None, memories: int | None = None, plugins: int | None = None) -> None:
        self.state = replace(
            self.state,
            task_count=self.state.task_count if tasks is None else tasks,
            memory_count=self.state.memory_count if memories is None else memories,
            plugin_count=self.state.plugin_count if plugins is None else plugins,
        )

    def update_metric(self, name: str, value: Any) -> None:
        metrics = dict(self.state.metrics)
        metrics[name] = value
        self.state = replace(self.state, metrics=metrics)
