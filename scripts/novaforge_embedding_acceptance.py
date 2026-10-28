"""NF-13A live acceptance: embed a real game window in the NovaForge surface.

Run on Windows against an installed, catalogued game:

    PYTHONPATH=src python scripts/novaforge_embedding_acceptance.py \
        --game "Shadow Fight Arena"

The script launches the game through NF-12's plan/token/execute path, hosts its
window with the real Win32 adapter in the real GameHostPanel, screenshots the
surface, checks focus/responsiveness/resize, detaches and restores, and closes
only the instance it started. It never sends game input and never kills a
process. Every fact it reports is measured; a step that cannot run is recorded
as skipped with the reason, never as a pass.

The JSON artifact and PNGs land under ``data/novaforge/nf13a/`` (runtime data,
git-ignored).
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def _dpi_aware() -> None:
    """Make this process per-monitor DPI aware BEFORE Qt starts.

    Win32 geometry and an ``ImageGrab`` of the whole display then share one
    physical-pixel space; mixing logical Qt sizes with physical screenshots is
    how a crop ends up half the game and half the desktop.
    """
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:  # noqa: BLE001 - older builds only expose shcore
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001 - already aware is fine
        pass


_dpi_aware()

from PIL import ImageGrab  # noqa: E402 - after DPI awareness

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QMainWindow,
    QVBoxLayout,
    QWidget,
)

from novacontrol.application import NovaControlApplication  # noqa: E402
from novacontrol.desktop.capture_validation import inspect_frame  # noqa: E402
from novacontrol.gui.game_host import GameHostPanel  # noqa: E402
from novacontrol.novaforge.embedding import Win32WindowApi  # noqa: E402


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("hwndActive", ctypes.c_void_p),
        ("hwndFocus", ctypes.c_void_p),
        ("hwndCapture", ctypes.c_void_p),
        ("hwndMenuOwner", ctypes.c_void_p),
        ("hwndMoveSize", ctypes.c_void_p),
        ("hwndCaret", ctypes.c_void_p),
        ("rcCaret", ctypes.c_long * 4),
    ]


def _window_thread(handle: int) -> int:
    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    pid = ctypes.c_ulong()
    return int(user32.GetWindowThreadProcessId(ctypes.c_void_p(handle), ctypes.byref(pid)))


def focus_is_on(handle: int) -> bool:
    """Whether the game's own thread reports ``handle`` as its keyboard focus."""
    thread = _window_thread(handle)
    if not thread:
        return False
    info = _GuiThreadInfo()
    info.cbSize = ctypes.sizeof(_GuiThreadInfo)
    user32 = ctypes.windll.user32
    user32.GetGUIThreadInfo.argtypes = [ctypes.c_ulong, ctypes.POINTER(_GuiThreadInfo)]
    if not user32.GetGUIThreadInfo(thread, ctypes.byref(info)):
        return False
    return int(info.hwndFocus or 0) == int(handle)


def responsive(handle: int, timeout_ms: int = 1500) -> bool:
    """Send WM_NULL and wait: a hung window cannot answer. No input is sent."""
    user32 = ctypes.windll.user32
    result = ctypes.c_size_t()
    user32.SendMessageTimeoutW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t,
        ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t),
    ]
    user32.SendMessageTimeoutW.restype = ctypes.c_void_p
    ok = user32.SendMessageTimeoutW(
        ctypes.c_void_p(handle), 0, 0, 0, 0x0002 | 0x0001, int(timeout_ms),
        ctypes.byref(result),
    )
    return bool(ok)


def pump(app: QApplication, seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.03)


def grab_screen_rect(path: Path, rect: Any) -> dict[str, Any]:
    image = ImageGrab.grab(all_screens=True)
    crop = image.crop((rect.x, rect.y, rect.x + rect.width, rect.y + rect.height))
    crop.save(path)
    return {"path": str(path), "size": list(crop.size)}


def frame_facts(path: str) -> dict[str, Any]:
    return inspect_frame(path).to_dict()


def _capture(nova: Any, handle: int, path: Path) -> dict[str, Any]:
    capture = getattr(getattr(nova.desktop, "runner", None), "capture_window", None)
    if not callable(capture):
        return {"ok": False, "error": "The configured desktop runner has no capture_window seam."}
    result = asyncio.run(capture(handle, str(path)))
    body = {
        "ok": bool(result.ok),
        "source": result.source,
        "backend": result.backend,
        "error": result.error,
        "width": result.width,
        "height": result.height,
        "size_bytes": result.size_bytes,
        "elapsed_ms": result.elapsed_ms,
    }
    if result.ok and Path(result.path).is_file():
        body["inspection"] = frame_facts(result.path)
    return body


def run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report: dict[str, Any] = {
        "phase": "NF-13A",
        "started_at": datetime.now(UTC).isoformat(),
        "host": {"platform": sys.platform, "python": sys.version.split()[0]},
        "game": args.game,
        "steps": {},
        "errors": [],
    }

    app = QApplication.instance() or QApplication([])
    nova = NovaControlApplication(data_dir=Path("data"))
    api = Win32WindowApi()
    window: QMainWindow | None = None
    panel: GameHostPanel | None = None
    launched: dict[str, Any] = {}
    try:
        asyncio.run(nova.start())
        library = asyncio.run(nova.game_library())
        report["catalog"] = {
            "available": library.get("available"),
            "total": library.get("total"),
            "counts": library.get("counts"),
            "memory": (library.get("memory") or {}).get("snapshot"),
        }
        report["memory_before_launch"] = asyncio.run(nova.games.memory_check()) if nova.games else {}
        plan = asyncio.run(nova.plan_game_launch(f"launch {args.game}"))
        report["steps"]["plan"] = {
            "status": plan.get("status"),
            "summary": plan.get("summary"),
            "memory": plan.get("memory"),
            "has_token": bool((plan.get("approval") or {}).get("token")),
        }
        token = str((plan.get("approval") or {}).get("token") or "")
        if not token:
            report["errors"].append("The plan carried no token; nothing was launched.")
            return _finish(report, out_dir, stamp, nova, None, app)

        executed = asyncio.run(
            nova.execute_game_launch(f"launch {args.game}", approval_token=token)
        )
        game = executed.get("game") or {}
        launched = dict(game.get("launch") or {})
        report["steps"]["launch"] = {
            "status": executed.get("status"),
            "summary": executed.get("summary"),
            "stages": executed.get("stages"),
            "launch": launched,
        }
        if not launched.get("pid") or not launched.get("window"):
            report["errors"].append(
                "The launch produced no verified window; embedding was not attempted."
            )
            return _finish(report, out_dir, stamp, nova, None, app)

        window = QMainWindow()
        window.setWindowTitle("NovaControl — NovaForge embedded game (NF-13A acceptance)")
        window.resize(1180, 780)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addWidget(QLabel(
            "Live acceptance surface. The game below is its own native window, "
            "reparented into this widget — not a screenshot or a stream."
        ))
        panel = GameHostPanel(nova, asyncio.run, parent=root)
        layout.addWidget(panel, stretch=1)
        window.setCentralWidget(root)
        window.move(30, 30)
        window.show()
        window.raise_()
        window.activateWindow()
        pump(app, 1.2)

        panel.refresh_library()
        pump(app, 0.8)
        report["steps"]["library_in_panel"] = {
            "rows": panel.library.count(),
            "selected": panel.library.currentText(),
        }

        record = {
            "pid": launched.get("pid"),
            "start_time": launched.get("start_time"),
            "window": launched.get("window"),
            "name": launched.get("name"),
            "owned": True,
            "launch_id": launched.get("launch_id"),
        }
        panel.host.set_running(record, name=str(launched.get("name") or args.game))
        surface_handle = panel.surface.native_handle()
        identity = panel.host.identity
        assert identity is not None
        lookup = api.find_window(int(launched["pid"]), str(launched["window"]))
        if lookup.handle <= 0:
            report["errors"].append(lookup.reason)
            return _finish(report, out_dir, stamp, nova, panel, app)
        before_window = api.window(lookup.handle)
        game_handle = before_window.handle
        report["steps"]["before_embed"] = {
            "pid": identity.pid,
            "start_time": identity.start_time,
            "hwnd": game_handle,
            "window": before_window.to_dict(),
            "host_surface_hwnd": surface_handle,
            "capture": _capture(nova, game_handle, out_dir / f"{stamp}_before_embed.png"),
        }

        pre = panel.host.preflight(identity)
        report["steps"]["preflight"] = pre.to_dict()
        result = panel.host.embed(identity, surface_handle)
        report["steps"]["embed"] = {
            "state": result.get("state"),
            "embedded": result.get("embedded"),
            "reason": (result.get("embedding") or {}).get("reason"),
            "reason_code": (result.get("embedding") or {}).get("reason_code"),
            "target": (result.get("embedding") or {}).get("target"),
            "window": result.get("window"),
            "status": panel.host.status_payload(),
        }
        if not result.get("embedded"):
            report["errors"].append("Embedding was refused: " + str(report["steps"]["embed"]["reason"]))
            # The game was started by this run; close it even though embedding
            # failed, so the test never leaves an owned instance behind.
            cleanup = asyncio.run(
                nova.close_game_launch(launch_id=str(launched.get("launch_id") or ""))
            )
            report["steps"]["cleanup_close"] = {
                "status": cleanup.get("status"),
                "closed": cleanup.get("closed"),
                "summary": cleanup.get("summary"),
            }
            return _finish(report, out_dir, stamp, nova, panel, app)

        pump(app, 1.2)
        surface_rect = api.screen_rect(surface_handle)
        shot = grab_screen_rect(out_dir / f"{stamp}_embedded.png", surface_rect)
        report["steps"]["embedded_screen"] = {
            **shot,
            "surface_rect": surface_rect.to_dict(),
            "inspection": frame_facts(shot["path"]),
            "parent": api.parent(game_handle),
            "responsive": responsive(game_handle),
        }

        panel.host.focus_game()
        pump(app, 0.4)
        report["steps"]["focus"] = {
            "requested": True,
            "focus_on_game": focus_is_on(game_handle),
            "foreground": int(ctypes.windll.user32.GetForegroundWindow() or 0),
            "host_handle": int(window.winId() or 0),
        }

        geometry: list[dict[str, Any]] = []
        for width, height in ((980, 700), (1240, 760)):
            window.resize(width, height)
            pump(app, 1.0)
            panel.host.resize()
            pump(app, 0.3)
            rect = api.screen_rect(game_handle)
            geometry.append({
                "window_requested": [width, height],
                "surface_client": api.client_rect(surface_handle).to_dict(),
                "game_rect": rect.to_dict(),
                "parent": api.parent(game_handle),
                "responsive": responsive(game_handle),
                "screenshot": grab_screen_rect(
                    out_dir / f"{stamp}_resized_{width}x{height}.png",
                    api.screen_rect(surface_handle),
                ),
            })
        report["steps"]["resize"] = geometry

        report["steps"]["capture_after_embed"] = _capture(
            nova, game_handle, out_dir / f"{stamp}_after_embed.png"
        )

        detached = panel.host.detach()
        pump(app, 1.0)
        after = api.window(game_handle)
        report["steps"]["detach"] = {
            "detached": detached.get("detached"),
            "restored": detached.get("restored"),
            "state": detached.get("state"),
            "parent": after.parent,
            "style": f"0x{after.style:08X}",
            "rect": after.rect.to_dict(),
            "original_rect": before_window.rect.to_dict(),
            "original_style": f"0x{before_window.style:08X}",
            "responsive": responsive(game_handle),
            "capture": _capture(nova, game_handle, out_dir / f"{stamp}_restored.png"),
        }

        reembed = panel.host.embed(identity, surface_handle)
        pump(app, 0.8)
        report["steps"]["reembed"] = {
            "embedded": reembed.get("embedded"),
            "state": reembed.get("state"),
            "parent": api.parent(game_handle),
        }
        if reembed.get("embedded"):
            panel.host.detach()
            pump(app, 0.6)

        close_surface = api.window(game_handle)
        close = asyncio.run(
            nova.close_game_launch(launch_id=str(launched.get("launch_id") or ""))
        )
        report["steps"]["close"] = {
            "status": close.get("status"),
            "closed": close.get("closed"),
            "summary": close.get("summary"),
            "window_before_close": {
                "parent": close_surface.parent,
                "rect": close_surface.rect.to_dict(),
            },
        }
        time.sleep(1.0)
        history = nova.games.history() if nova.games else []
        report["steps"]["close"]["history_tail"] = history[-2:]
        print("Acceptance finished. Close:", close.get("status"))
        return _finish(report, out_dir, stamp, nova, panel, app)
    except Exception as exc:  # noqa: BLE001 - the artifact records the failure
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        import traceback

        report["traceback"] = traceback.format_exc()
        return _finish(report, out_dir, stamp, nova, panel, app, status=1)


def _finish(
    report: dict[str, Any],
    out_dir: Path,
    stamp: str,
    nova: Any,
    panel: Any,
    app: QApplication | None,
    *,
    status: int = 0,
) -> int:
    if panel is not None:
        try:
            panel.shutdown()
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"panel shutdown: {type(exc).__name__}: {exc}")
    path = out_dir / f"{stamp}_report.json"
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["report_path"] = str(path)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: report.get(k) for k in ("game", "steps", "errors")}, indent=2, default=str)[:8000])
    try:
        asyncio.run(nova.stop())
    except Exception:  # noqa: BLE001
        pass
    if app is not None:
        app.quit()
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", default="Shadow Fight Arena")
    parser.add_argument("--out-dir", default="data/novaforge/nf13a")
    args = parser.parse_args()
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
