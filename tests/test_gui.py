from __future__ import annotations

import unittest

from novacontrol.gui import DashboardTab, DashboardViewModel


class GuiTests(unittest.TestCase):
    def test_default_tabs_include_explore(self) -> None:
        view_model = DashboardViewModel.with_default_tabs()

        self.assertIn(DashboardTab.DASHBOARD, view_model.state.tabs)
        self.assertIn(DashboardTab.INTELLIGENCE, view_model.state.tabs)
        self.assertIn(DashboardTab.EXPLORE, view_model.state.tabs)
        self.assertIn(DashboardTab.DEMOS, view_model.state.tabs)
        self.assertIn(DashboardTab.AUTOMATION, view_model.state.tabs)

    def test_view_model_tracks_state(self) -> None:
        view_model = DashboardViewModel.with_default_tabs()

        view_model.activate(DashboardTab.EXPLORE)
        view_model.record_log("started")
        view_model.update_counts(tasks=3, memories=4, plugins=2)
        view_model.update_metric("latency_ms", 12)

        self.assertEqual(view_model.state.active_tab, DashboardTab.EXPLORE)
        self.assertEqual(view_model.state.logs, ("started",))
        self.assertEqual(view_model.state.task_count, 3)
        self.assertEqual(view_model.state.metrics["latency_ms"], 12)


if __name__ == "__main__":
    unittest.main()
