"""Qt Style Sheet theme for the desktop dashboard.

The palette mirrors the web UI's token block (web/static/styles.css :root):
the same brand cyan on deep navy, the same semantic text/border tiers, and the
same interaction policy (hover glow, pressed dim, keyboard focus ring). The
single QSS string builder is the one place the neon palette is authored for Qt,
so any second themed Qt surface inherits the same colors and focus policy.

Qt Style Sheets cannot express media queries, so ``prefers-reduced-motion`` is
honored at runtime instead: when the OS reports animations disabled
(``QStyleHints.hasAnimations() == False``), the built-in widget effects
(menu/combo/tooltip animation and tooltip fade) are turned off, and the QSS
itself never declares animation/transition properties.
"""

from __future__ import annotations

from typing import Any


def build_dashboard_qss() -> str:
    """Return the complete dashboard stylesheet.

    Pure string — no PySide6 import, so it is testable and importable in any
    environment. Focus rings use ``:focus`` (Qt has no ``:focus-visible``);
    every interactive widget gets a visible 2px cyan ring when it owns the
    keyboard focus.
    """
    return _QSS


_QSS = """
/* ============================================================
   NovaControl — dark futuristic Qt theme.
   Palette mirrors web/static/styles.css :root (brand -> semantic).
   ============================================================ */

/* ---- Base surfaces ---- */
QMainWindow, QWidget {
    background-color: #030810;
    color: #e8f0ff;
}

QGroupBox {
    background-color: rgba(10, 22, 40, 0.85);
    border: 1px solid rgba(0, 240, 255, 0.12);
    border-radius: 10px;
    margin-top: 14px;
    padding-top: 8px;
    font-weight: 600;
}

QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: #00f0ff;
    font-family: "Segoe UI", "Cantarell", sans-serif;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 1px;
}

/* ---- Labels ---- */
QLabel {
    color: #e8f0ff;
    background: transparent;
}

/* ---- Buttons ---- */
QPushButton {
    background-color: rgba(14, 30, 56, 0.6);
    border: 1px solid rgba(0, 240, 255, 0.12);
    border-radius: 6px;
    padding: 6px 14px;
    color: #e8f0ff;
    font-weight: 600;
}

QPushButton:hover {
    border-color: #00f0ff;
    color: #00f0ff;
    background-color: rgba(0, 240, 255, 0.10);
}

QPushButton:pressed {
    background-color: rgba(0, 240, 255, 0.20);
}

QPushButton:focus {
    border: 2px solid #00f0ff;
    outline: none;
}

QPushButton:disabled {
    color: #4a6382;
    border-color: rgba(0, 240, 255, 0.06);
    background-color: rgba(10, 22, 40, 0.5);
}

/* ---- Inputs ---- */
QLineEdit, QSpinBox, QComboBox, QTextEdit {
    background-color: rgba(6, 16, 32, 0.8);
    border: 1px solid rgba(0, 240, 255, 0.12);
    border-radius: 6px;
    padding: 6px 8px;
    color: #e8f0ff;
    selection-background-color: rgba(0, 240, 255, 0.35);
    selection-color: #030810;
}

QLineEdit:hover, QSpinBox:hover, QComboBox:hover, QTextEdit:hover {
    border-color: rgba(0, 240, 255, 0.30);
}

QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QTextEdit:focus {
    border: 2px solid #00f0ff;
}

QLineEdit::placeholder, QSpinBox::placeholder, QComboBox::placeholder {
    color: #4a6382;
}

QTextEdit {
    padding: 4px;
}

/* ---- Combo box popup ---- */
QComboBox QAbstractItemView {
    background-color: #0a1628;
    border: 1px solid rgba(0, 240, 255, 0.12);
    border-radius: 6px;
    color: #e8f0ff;
    selection-background-color: rgba(0, 240, 255, 0.20);
    selection-color: #00f0ff;
    outline: none;
    padding: 4px;
}

QComboBox::drop-down {
    border: none;
    width: 22px;
}

QComboBox::down-arrow {
    width: 8px;
    height: 8px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #00f0ff;
    margin-right: 8px;
}

/* ---- Spin box arrows ---- */
QSpinBox::up-button, QSpinBox::down-button {
    background-color: rgba(0, 240, 255, 0.08);
    border: none;
    width: 18px;
}

QSpinBox::up-button:hover, QSpinBox::down-button:hover {
    background-color: rgba(0, 240, 255, 0.20);
}

QSpinBox::up-arrow {
    width: 8px;
    height: 8px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid #00f0ff;
}

QSpinBox::down-arrow {
    width: 8px;
    height: 8px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #00f0ff;
}

/* ---- Check boxes ---- */
QCheckBox {
    color: #e8f0ff;
    spacing: 8px;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid rgba(0, 240, 255, 0.30);
    border-radius: 3px;
    background-color: rgba(6, 16, 32, 0.8);
}

QCheckBox::indicator:hover {
    border-color: #00f0ff;
}

QCheckBox::indicator:checked {
    background-color: #00f0ff;
    border-color: #00f0ff;
}

QCheckBox:focus {
    outline: none;
    color: #00f0ff;
}

/* ---- Tabs ---- */
QTabWidget::pane {
    border: 1px solid rgba(0, 240, 255, 0.12);
    border-radius: 6px;
    top: -1px;
    background-color: rgba(10, 22, 40, 0.60);
}

QTabBar::tab {
    background-color: rgba(14, 30, 56, 0.6);
    border: 1px solid rgba(0, 240, 255, 0.08);
    border-bottom: none;
    padding: 7px 14px;
    margin-right: 2px;
    color: #8da4c2;
    font-weight: 600;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}

QTabBar::tab:hover {
    color: #00f0ff;
}

QTabBar::tab:selected {
    background-color: rgba(0, 240, 255, 0.15);
    border-color: rgba(0, 240, 255, 0.30);
    color: #00f0ff;
}

QTabBar::tab:focus {
    border: 1px solid #00f0ff;
    outline: none;
}

/* ---- Scrollbars ---- */
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 0;
}

QScrollBar::handle:vertical {
    background: rgba(0, 240, 255, 0.25);
    border-radius: 5px;
    min-height: 24px;
}

QScrollBar::handle:vertical:hover {
    background: rgba(0, 240, 255, 0.45);
}

QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 0;
}

QScrollBar::handle:horizontal {
    background: rgba(0, 240, 255, 0.25);
    border-radius: 5px;
    min-width: 24px;
}

QScrollBar::handle:horizontal:hover {
    background: rgba(0, 240, 255, 0.45);
}

QScrollBar::add-line, QScrollBar::sub-line {
    width: 0;
    height: 0;
    background: none;
    border: none;
}

QScrollBar::add-page, QScrollBar::sub-page {
    background: none;
}

/* ---- Tooltips ---- */
QToolTip {
    background-color: #0a1628;
    color: #e8f0ff;
    border: 1px solid rgba(0, 240, 255, 0.40);
    border-radius: 4px;
    padding: 4px 6px;
}
"""


def apply_dashboard_theme(app: Any) -> None:
    """Apply the neon theme to a QApplication.

    Qt Style Sheets have no ``prefers-reduced-motion`` media query, so reduced
    motion is honored at the widget-effect level: when the OS reports
    animations disabled (``QStyleHints.hasAnimations()`` is False), the
    built-in menu/combo/tooltip animation and tooltip fade are switched off.
    The QSS itself declares no animation/transition properties, so nothing
    else animates.
    """
    from PySide6.QtCore import Qt

    app.setStyleSheet(build_dashboard_qss())
    if not _has_animations(app):
        _disable_effects(app, Qt)


def _has_animations(app: Any) -> bool:
    """True when the OS allows animations (QStyleHints.hasAnimations)."""
    style_hints = app.styleHints()
    return bool(style_hints.hasAnimations())


def _disable_effects(app: Any, qt: Any) -> None:
    """Turn off Qt's built-in animated widget effects for reduced-motion users."""
    effects = (
        qt.UI_AnimateMenu,
        qt.UI_AnimateCombo,
        qt.UI_AnimateTooltip,
        qt.UI_FadeTooltip,
    )
    for effect in effects:
        app.setEffectEnabled(effect, False)