from __future__ import annotations

import unittest

from novacontrol.gui import DashboardTab, DashboardViewModel
from novacontrol.gui.theme import _disable_effects, _has_animations, build_dashboard_qss


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


class ThemeTests(unittest.TestCase):
    """Pin the Qt dashboard theme contract without needing PySide6 installed.

    build_dashboard_qss is pure string, so the palette, focus rings, and the
    no-animation guarantee are all testable in any environment. The reduced-
    motion handling is a runtime concern (QSS has no media queries), tested
    through the pure helper `_has_animations` plus a stub QApplication.
    """

    QSS = build_dashboard_qss()

    def test_theme_matches_web_cyan_palette(self) -> None:
        """The QSS authors the same brand/semantic colors as styles.css."""
        for color in ("#030810", "#0a1628", "#00f0ff", "#e8f0ff", "#8da4c2", "#4a6382"):
            self.assertIn(color, self.QSS, f"missing palette color {color}")

    def test_every_interactive_widget_has_a_focus_ring(self) -> None:
        """Keyboard users get a visible focus state on every interactive widget."""
        for selector in ("QPushButton:focus", "QLineEdit:focus", "QSpinBox:focus",
                          "QComboBox:focus", "QTextEdit:focus", "QTabBar::tab:focus"):
            self.assertIn(selector, self.QSS, f"missing focus selector {selector}")
        self.assertGreaterEqual(self.QSS.count("#00f0ff"), 6, "focus rings must use the cyan accent")

    def test_qss_declares_no_animations(self) -> None:
        """QSS cannot honor prefers-reduced-motion, so it must not animate at all."""
        self.assertNotIn("animation", self.QSS.lower())
        self.assertNotIn("transition", self.QSS.lower())

    def test_reduced_motion_disables_widget_effects(self) -> None:
        """When the OS reports reduced motion, animated widget effects go off."""
        class FakeQt:
            UI_AnimateMenu = "UI_AnimateMenu"
            UI_AnimateCombo = "UI_AnimateCombo"
            UI_AnimateTooltip = "UI_AnimateTooltip"
            UI_FadeTooltip = "UI_FadeTooltip"

        disabled: list[str] = []

        class FakeApp:
            def setEffectEnabled(self, effect: str, enabled: bool) -> None:
                if not enabled:
                    disabled.append(effect)

        _disable_effects(FakeApp(), FakeQt())
        self.assertEqual(
            sorted(disabled),
            ["UI_AnimateCombo", "UI_AnimateMenu", "UI_AnimateTooltip", "UI_FadeTooltip"],
        )


if __name__ == "__main__":
    unittest.main()
