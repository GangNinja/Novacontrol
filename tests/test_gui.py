from __future__ import annotations

import importlib.util
import os
import unittest

from novacontrol.gui import DashboardTab, DashboardViewModel

_HAS_PYSIDE = importlib.util.find_spec("PySide6") is not None
from novacontrol.gui.theme import _disable_effects, _has_animations, build_dashboard_qss


class GuiTests(unittest.TestCase):
    def test_default_tabs_include_explore(self) -> None:
        view_model = DashboardViewModel.with_default_tabs()

        self.assertIn(DashboardTab.DASHBOARD, view_model.state.tabs)
        self.assertIn(DashboardTab.INTELLIGENCE, view_model.state.tabs)
        self.assertIn(DashboardTab.EXPLORE, view_model.state.tabs)
        self.assertIn(DashboardTab.DEMOS, view_model.state.tabs)
        self.assertIn(DashboardTab.AUTOMATION, view_model.state.tabs)
        self.assertIn(DashboardTab.NOVAFORGE, view_model.state.tabs)
        self.assertEqual(DashboardTab.NOVAFORGE.label, "NovaForge")

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


@unittest.skipUnless(_HAS_PYSIDE, "PySide6 is an optional GUI extra")
class GameHostTabTests(unittest.TestCase):
    """The embedded-game tab builds, reads the NF-12 library and detaches cleanly.

    Runs offscreen: it proves widget construction and the panel's wiring, not
    native embedding. The live reparenting evidence is in the NF-13A report.
    """

    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def make_panel(self) -> object:
        import asyncio

        from novacontrol.gui.game_host import GameHostPanel

        class FakeNova:
            async def game_library(self, query: str = "") -> dict[str, object]:
                return {
                    "available": True,
                    "entries": [
                        {
                            "title": "Shadow Fight Arena",
                            "status": "ALREADY_RUNNING",
                            "reason": "Already running as SFA (pid 7).",
                            "artifact": {"source": "steam", "launch_target": "1446890"},
                            "running": {"pid": 7, "name": "SFA", "window": "Shadow Fight Arena",
                                        "start_time": "2026-10-23T01:43:30.5955071Z"},
                            "tested": False,
                        }
                    ],
                    "matched": 1,
                    "total": 1,
                    "owned": None,
                    "observation": {"available": False, "reason": "No backend validated a frame."},
                }

            async def close_game_launch(self, *, launch_id: str = "") -> dict[str, object]:
                return {"closed": False, "summary": "not ours"}

        return GameHostPanel(FakeNova(), asyncio.run)

    def test_panel_reads_the_library_and_shows_a_native_surface(self) -> None:
        panel = self.make_panel()
        try:
            panel.refresh_library()  # type: ignore[attr-defined]
            self.assertEqual(panel.library.count(), 1)  # type: ignore[attr-defined]
            self.assertIn("Shadow Fight Arena", panel.library.itemText(0))  # type: ignore[attr-defined]
            self.assertIsInstance(panel.surface.native_handle(), int)  # type: ignore[attr-defined]
            self.assertIn("unavailable", panel.capture_label.text())  # type: ignore[attr-defined]
            self.assertEqual(panel.host.state, "closed")  # type: ignore[attr-defined]
            self.assertFalse(panel.close_button.isEnabled())  # type: ignore[attr-defined]
        finally:
            panel.shutdown()  # type: ignore[attr-defined]
            panel.deleteLater()  # type: ignore[attr-defined]

    def test_compatibility_is_rendered_separately_from_capture(self) -> None:
        panel = self.make_panel()
        try:
            panel.refresh_library()  # type: ignore[attr-defined]
            text = panel.compatibility_label.text()  # type: ignore[attr-defined]
            self.assertIn("Compatibility: NOT_TESTED", text)
            self.assertIn("capture compatibility: not_tested", text)
            self.assertIn("assessed separately", text)
            # Capture availability is its own line; compatibility must not be
            # derived from it (and vice versa).
            self.assertIn("unavailable", panel.capture_label.text())  # type: ignore[attr-defined]
        finally:
            panel.shutdown()  # type: ignore[attr-defined]
            panel.deleteLater()  # type: ignore[attr-defined]

    def test_shutdown_is_idempotent(self) -> None:
        panel = self.make_panel()
        panel.refresh_library()  # type: ignore[attr-defined]
        panel.shutdown()  # type: ignore[attr-defined]
        panel.shutdown()  # type: ignore[attr-defined]
        self.assertEqual(panel.host.state, "closed")  # type: ignore[attr-defined]
        panel.deleteLater()  # type: ignore[attr-defined]

    def test_shutdown_without_a_game_is_a_no_op(self) -> None:
        panel = self.make_panel()
        self.assertEqual(panel.host.state, "closed")  # type: ignore[attr-defined]
        panel.shutdown()  # type: ignore[attr-defined]
        self.assertEqual(panel.host.state, "closed")  # type: ignore[attr-defined]
        panel.deleteLater()  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
