# GUI

The Phase 11 GUI subsystem defines the NovaControl desktop dashboard and provides a lazy PySide6 launcher.

## Tabs

- Chat
- Explore
- Tasks
- Memory
- Projects
- Plugins
- Settings
- Logs
- Performance

The `Explore` tab is included as requested. It can run the Explore backend to research topics, explain them clearly, show source links, and include related video links.

## Components

- `DashboardTab`: tab identifiers and labels
- `DashboardState`: UI state snapshot
- `DashboardViewModel`: dashboard state updates
- `create_gui_app`: PySide6 application factory
- `python -m novacontrol gui`: GUI launcher

## Theme

The dashboard is themed with a dark futuristic QSS in `novacontrol/gui/theme.py`
that mirrors the web UI's cyan token block (`web/static/styles.css :root`): the
same `#00f0ff` accent on deep navy surfaces, matching text tiers, and the same
interaction policy.

- `build_dashboard_qss()` returns the whole stylesheet as a pure string — no
  PySide6 import, so it is importable and testable anywhere.
- Every interactive widget (`QPushButton`, `QLineEdit`, `QSpinBox`, `QComboBox`,
  `QTextEdit`, `QTabBar::tab`, `QCheckBox`) gets a visible 2px cyan ring on
  `:focus` for keyboard users. Qt has no `:focus-visible`, so the ring is the
  `:focus` state.
- Reduced motion: QSS has no `prefers-reduced-motion` media query, so the theme
  never declares animation/transition properties, and `apply_dashboard_theme`
  turns off Qt's built-in animated widget effects (menu/combo/tooltip animation
  and tooltip fade) when the OS reports animations disabled
  (`QStyleHints.hasAnimations()` is False).

Add the theme by calling `apply_dashboard_theme(app)` after creating the
`QApplication` (already done in `create_gui_app`).

## CLI

Check the GUI state without opening a window:

```powershell
python -m novacontrol gui --dry-run
```

Launch the GUI:

```powershell
python -m novacontrol gui
```

Run the Phase 11 demo:

```powershell
python -m novacontrol demo phase11
```
