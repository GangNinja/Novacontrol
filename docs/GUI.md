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
