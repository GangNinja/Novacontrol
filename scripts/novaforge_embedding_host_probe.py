"""NF-13A live host probe: embed a REAL native window (not a game).

    PYTHONPATH=src python scripts/novaforge_embedding_host_probe.py

The two installed lightweight games are borderless/fullscreen Unity windows that
refuse reparenting (and exit when asked); the shipped host now refuses them
outright. This driver proves the host itself against a disposable WinForms
window it starts: real HWND, real ``SetParent``, real style/geometry read-back,
real pixels in the panel, real resize/focus/detach/restore. It is explicitly NOT
gameplay evidence and says so in the artifact.

Nothing is captured during gameplay, no input is synthesized, and the probe
window is the only process this script terminates (it started it).
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def _dpi_aware() -> None:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        pass


_dpi_aware()

from PIL import ImageGrab  # noqa: E402

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QMainWindow,
    QVBoxLayout,
    QWidget,
)

from novacontrol.application import NovaControlApplication  # noqa: E402
from novacontrol.desktop.capture_validation import inspect_frame  # noqa: E402
from novacontrol.desktop.controller import LocalDesktopRunner  # noqa: E402
from novacontrol.gui.game_host import GameHostPanel  # noqa: E402
from novacontrol.novaforge.embedding import (  # noqa: E402
    GameIdentity,
    Win32WindowApi,
    WindowApiError,
)

TITLE = "NC NF13A EMBED PROBE"

#: The application the panel reads its library from. Module-level so ``_finish``
#: can stop it from every early-return path without threading it through.
_APP: Any = None

#: The fixture's merged stdout/stderr. The fixture is disposable, but if it dies
#: mid-run the reason has to be readable, or a fixture crash looks like the host
#: having killed a window.
_PROBE_LOG: Any = None


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


def focus_on(handle: int) -> bool:
    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    pid = ctypes.c_ulong()
    thread = int(user32.GetWindowThreadProcessId(ctypes.c_void_p(handle), ctypes.byref(pid)))
    info = _GuiThreadInfo()
    info.cbSize = ctypes.sizeof(_GuiThreadInfo)
    user32.GetGUIThreadInfo.argtypes = [ctypes.c_ulong, ctypes.POINTER(_GuiThreadInfo)]
    return bool(thread and user32.GetGUIThreadInfo(thread, ctypes.byref(info))) and int(
        info.hwndFocus or 0
    ) == int(handle)


def responsive(handle: int, timeout_ms: int = 1500) -> bool:
    user32 = ctypes.windll.user32
    user32.SendMessageTimeoutW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t,
        ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t),
    ]
    user32.SendMessageTimeoutW.restype = ctypes.c_void_p
    result = ctypes.c_size_t()
    return bool(user32.SendMessageTimeoutW(
        ctypes.c_void_p(handle), 0, 0, 0, 0x0002 | 0x0001, int(timeout_ms),
        ctypes.byref(result),
    ))


def grab(path: Path, rect: Any) -> dict[str, Any]:
    image = ImageGrab.grab(all_screens=True)
    crop = image.crop((rect.x, rect.y, rect.x + rect.width, rect.y + rect.height))
    crop.save(path)
    facts = inspect_frame(path).to_dict()
    return {"path": str(path), "size": list(crop.size), "inspection": facts}


def pump(app: QApplication, seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.03)


def capture_seam(handle: int, path: Path) -> dict[str, Any]:
    runner = LocalDesktopRunner()
    try:
        result = asyncio.run(runner.capture_window(handle, str(path)))
        body = {
            "ok": result.ok,
            "source": result.source,
            "backend": result.backend,
            "error": result.error,
            "width": result.width,
            "height": result.height,
            "size_bytes": result.size_bytes,
            "elapsed_ms": result.elapsed_ms,
        }
        if result.ok and Path(result.path).is_file():
            body["inspection"] = inspect_frame(result.path).to_dict()
        return body
    finally:
        asyncio.run(runner.close())


def _measured(read: Any) -> Any:
    """Run one geometry read, returning ``None`` when the window refuses it.

    A read that failed is UNMEASURED: reporting an uninitialised rectangle would
    turn a destroyed window into a position (one probe run recorded x=-31405).
    """
    try:
        return read()
    except WindowApiError:
        return None


def _fixture_state(
    api: Win32WindowApi, probe: subprocess.Popen[bytes] | None, title: str, handle: int
) -> dict[str, Any]:
    """What is left of the disposable fixture after a step that disturbed it.

    A destroyed HWND, a recreated HWND and a dead process are three different
    facts, and the probe must not report them as one: only the last is a fixture
    crash, and only the middle can be re-adopted and detach-tested.
    """
    alive = probe is not None and probe.poll() is None
    exists = api.is_window(handle)
    current = api.find_window(probe.pid, title).handle if (alive and probe is not None) else 0
    return {
        "process_alive": alive,
        "exit_code": None if probe is None else probe.poll(),
        "window_exists": exists,
        "handle": handle,
        "current_handle": current,
        "handle_changed": bool(current) and current != handle,
    }


def run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report: dict[str, Any] = {
        "phase": "NF-13A-host-probe",
        "started_at": datetime.now(UTC).isoformat(),
        "target": "disposable WinForms window (NOT a game)",
        "steps": {},
        "errors": [],
        "gameplay_claimed": False,
    }
    app = QApplication.instance() or QApplication([])
    api = Win32WindowApi()
    window: QMainWindow | None = None
    panel: GameHostPanel | None = None
    probe: subprocess.Popen[bytes] | None = None
    try:
        global _APP, _PROBE_LOG
        _APP = NovaControlApplication(data_dir=Path("data"))
        asyncio.run(_APP.start())
        _PROBE_LOG = open(out_dir / f"{stamp}_probe_stderr.log", "wb")
        report["probe_log"] = str(out_dir / f"{stamp}_probe_stderr.log")
        probe = subprocess.Popen(
            [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(ROOT / "scripts" / "nf13a_embed_probe_window.ps1"),
                "-Seconds", "240", "-Title", TITLE,
                "-LogPath", str(out_dir / f"{stamp}_fixture_paint.log"),
            ],
            stdout=_PROBE_LOG,
            stderr=subprocess.STDOUT,
        )
        lookup = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            lookup = api.find_window(probe.pid, TITLE)
            if lookup.handle:
                break
            time.sleep(0.25)
        if lookup is None or not lookup.handle:
            report["errors"].append("The disposable probe window did not appear.")
            return _finish(report, out_dir, stamp, app, panel, probe)
        info = api.window(lookup.handle)
        created = api.process_creation_time(probe.pid)
        if created is None:
            report["errors"].append("The probe process creation time was unreadable.")
            return _finish(report, out_dir, stamp, app, panel, probe)
        report["steps"]["probe"] = {"pid": probe.pid, "hwnd": info.handle,
                                    "window": info.to_dict()}

        window = QMainWindow()
        window.setWindowTitle("NovaControl — NovaForge embedded host (NF-13A probe)")
        window.resize(1180, 780)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addWidget(QLabel(
            "Disposable native probe embedded by the real host. This window is not a game; "
            "it proves reparenting and rendering, and claims nothing about gameplay."
        ))
        panel = GameHostPanel(_APP, asyncio.run, parent=root)
        layout.addWidget(panel, stretch=1)
        window.setCentralWidget(root)
        window.move(40, 40)
        window.show()
        window.raise_()
        window.activateWindow()
        pump(app, 1.2)

        # The REAL NF-12 catalog, so the panel screenshot shows the surface as
        # shipped (12 rows on this host), not an empty fixture.
        panel.refresh_library()
        pump(app, 0.9)
        report["steps"]["library_in_panel"] = {
            "rows": panel.library.count(),
            "selected": panel.library.currentText(),
        }

        identity = GameIdentity(
            pid=probe.pid,
            start_time=datetime.fromtimestamp(created, UTC).isoformat(),
            window_title=TITLE,
            name="NF-13A probe",
            owned=True,
            launch_id="probe",
        )
        panel.host.set_running(
            {"pid": probe.pid, "start_time": identity.start_time, "window": TITLE,
             "name": "NF-13A probe", "owned": True, "launch_id": "probe"},
            name="NF-13A probe",
        )
        # The probe drives the HOST directly rather than clicking Embed, so it
        # re-renders the panel's own status lines after each host operation.
        panel._render_host()
        pump(app, 0.3)
        surface = panel.surface.native_handle()
        pre = panel.host.preflight(identity)
        report["steps"]["preflight"] = pre.to_dict()
        if not pre.supported:
            report["errors"].append("Preflight refused the probe: " + pre.reason)
            return _finish(report, out_dir, stamp, app, panel, probe)

        result = panel.host.embed(identity, surface)
        report["steps"]["embed"] = {
            "embedded": result.get("embedded"),
            "state": result.get("state"),
            "reason_code": (result.get("embedding") or {}).get("reason_code"),
            "reason": (result.get("embedding") or {}).get("reason"),
            "target": (result.get("embedding") or {}).get("target"),
            "parent": api.parent(info.handle),
            "window": api.window(info.handle).to_dict(),
        }
        if not result.get("embedded"):
            report["errors"].append("Embedding failed: " + str(report["steps"]["embed"]["reason"]))
            return _finish(report, out_dir, stamp, app, panel, probe)

        panel._render_host()
        pump(app, 0.8)
        surface_rect = api.screen_rect(surface)
        report["steps"]["embedded_screen"] = {
            **grab(out_dir / f"{stamp}_probe_embedded.png", surface_rect),
            "surface_rect": surface_rect.to_dict(),
            "parent": api.parent(info.handle),
            "responsive": responsive(info.handle),
        }

        # The whole panel, so the status lines (embedding / identity / capture)
        # are visible next to the hosted pixels. The window is NOT a game and
        # says so in its own banner.
        report["steps"]["panel_screen"] = {
            **grab(out_dir / f"{stamp}_probe_panel.png",
                   api.screen_rect(int(window.winId() or 0))),
            "nova_window": int(window.winId() or 0),
        }

        # Freshness: two grabs of the SAME surface a second apart. The probe
        # repaints its frame counter on a 120 ms timer, so identical pixels would
        # mean a stale or frozen surface, not a live one. This is a DISPLAY
        # capture of the hosted pixels (the same source as DisplayCropBackend),
        # labelled as such: it is not the window-surface backend.
        first = grab(out_dir / f"{stamp}_probe_fresh_a.png", surface_rect)
        pump(app, 1.1)
        second = grab(out_dir / f"{stamp}_probe_fresh_b.png", surface_rect)
        report["steps"]["freshness"] = {
            "source": "display_crop of the embedded surface (not PrintWindow)",
            "hashes": [first["inspection"].get("content_hash"),
                       second["inspection"].get("content_hash")],
            "distinct_frames": first["inspection"].get("content_hash")
            != second["inspection"].get("content_hash"),
            "blank": bool(first["inspection"].get("blank"))
            or bool(second["inspection"].get("blank")),
            "sizes": [first["size"], second["size"]],
        }

        panel.host.focus_game()
        panel._render_host()
        pump(app, 0.4)
        report["steps"]["focus"] = {
            "focus_on_probe": focus_on(info.handle),
            "foreground": int(ctypes.windll.user32.GetForegroundWindow() or 0),
            "nova_window": int(window.winId() or 0),
        }

        geometry: list[dict[str, Any]] = []
        for width, height in ((980, 700), (1240, 760)):
            window.resize(width, height)
            pump(app, 0.9)
            panel.host.resize()
            panel._render_host()
            pump(app, 0.3)
            geometry.append({
                "nova_requested": [width, height],
                "surface_client": _measured(lambda: api.client_rect(surface).to_dict()),
                "probe_rect": _measured(lambda: api.screen_rect(info.handle).to_dict()),
                "parent": api.parent(info.handle),
                "responsive": responsive(info.handle),
                "screenshot": grab(out_dir / f"{stamp}_probe_{width}x{height}.png",
                                   api.screen_rect(surface)),
            })
        report["steps"]["resize"] = geometry

        # Tab-switch: hiding the dashboard hides the surface, and Win32 then
        # reports the child as not visible. That must NOT read as "the game hid
        # itself" (it detached a good embedding on every tab change), and a
        # resize request while hidden must be deferred, not "failed".
        window.hide()
        pump(app, 0.9)
        for _ in range(5):
            panel.host.check()
            pump(app, 0.15)
        hidden_state = panel.host.state
        deferred = panel.host.resize()
        report["steps"]["hidden_surface"] = {
            "state_after_hide": hidden_state,
            "state_after_checks": panel.host.state,
            "last_check_reason": panel.host.status_payload()["embedding"]["last_check_reason"],
            "resize_deferred": bool(deferred.get("deferred")),
            "parent_while_hidden": api.parent(info.handle),
        }
        window.show()
        window.raise_()
        pump(app, 1.2)
        panel.host.resize()
        panel._render_host()
        pump(app, 0.3)
        report["steps"]["hidden_surface"].update({
            "state_after_show": panel.host.state,
            "rect_after_show": _measured(lambda: api.screen_rect(info.handle).to_dict()),
            "surface_client_after_show": _measured(lambda: api.client_rect(surface).to_dict()),
            "fixture": _fixture_state(api, probe, TITLE, info.handle),
        })

        report["steps"]["capture_after_embed"] = capture_seam(
            info.handle, out_dir / f"{stamp}_probe_printwindow.png"
        )

        # Capture can disturb the FIXTURE (a cross-process PrintWindow hands a
        # PowerShell/.NET window a DC it may refuse). Distinguish the three
        # possible outcomes instead of calling them all "the window died":
        # process gone, HWND recreated (WinForms recreates on a style change),
        # or HWND simply destroyed while the process lives.
        fixture = _fixture_state(api, probe, TITLE, info.handle)
        report["steps"]["fixture_after_capture"] = fixture
        if not fixture["window_exists"]:
            report["errors"].append(
                "The probe window no longer exists after the embedded capture attempt; "
                "detach/restore was not measurable in this run."
            )
            return _finish(report, out_dir, stamp, app, panel, probe)
        if fixture["handle_changed"]:
            report["errors"].append(
                "The probe window was recreated under a new handle during capture; "
                "detach/restore was not measurable in this run."
            )
            return _finish(report, out_dir, stamp, app, panel, probe)

        detached = panel.host.detach()
        panel._render_host()
        pump(app, 0.8)
        if not api.is_window(info.handle):
            report["steps"]["fixture_after_detach"] = _fixture_state(
                api, probe, TITLE, info.handle
            )
            report["errors"].append(
                "The probe window stopped existing during detach; the restore could not "
                "be read back in this run."
            )
            return _finish(report, out_dir, stamp, app, panel, probe, status=1)
        after = api.window(info.handle)
        report["steps"]["detach"] = {
            "detached": detached.get("detached"),
            "restored": detached.get("restored"),
            "parent": after.parent,
            "style_before": f"0x{info.style:08X}",
            "style_after": f"0x{after.style:08X}",
            "rect_before": info.rect.to_dict(),
            "rect_after": after.rect.to_dict(),
            "responsive": responsive(info.handle),
            "capture": capture_seam(info.handle, out_dir / f"{stamp}_probe_restored.png"),
        }
        if after.parent != 0 or after.style != info.style or after.rect != info.rect:
            report["errors"].append("Detach did not restore the original window exactly.")

        reembed = panel.host.embed(identity, surface)
        pump(app, 0.6)
        report["steps"]["reembed"] = {
            "embedded": reembed.get("embedded"),
            "parent": api.parent(info.handle),
        }
        if reembed.get("embedded"):
            panel.host.detach()
            pump(app, 0.5)

        report["steps"]["final"] = {
            "parent": api.parent(info.handle),
            "responsive": responsive(info.handle),
        }
        return _finish(report, out_dir, stamp, app, panel, probe)
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        import traceback

        report["traceback"] = traceback.format_exc()
        return _finish(report, out_dir, stamp, app, panel, probe, status=1)


def _finish(
    report: dict[str, Any],
    out_dir: Path,
    stamp: str,
    app: QApplication,
    panel: Any,
    probe: subprocess.Popen[bytes] | None,
    *,
    status: int = 0,
) -> int:
    if panel is not None:
        panel.shutdown()
    if probe is not None and probe.poll() is None:
        lookup = Win32WindowApi().find_window(probe.pid, TITLE)
        if lookup.handle:
            user32 = ctypes.windll.user32
            user32.PostMessageW.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t
            ]
            user32.PostMessageW(ctypes.c_void_p(lookup.handle), 0x0010, 0, 0)
        try:
            probe.wait(timeout=8)
        except subprocess.TimeoutExpired:
            probe.terminate()  # the disposable fixture this script started
            report["steps"]["probe_terminated"] = True
            try:
                probe.wait(timeout=5)
            except subprocess.TimeoutExpired:
                probe.kill()
    if _APP is not None:
        try:
            asyncio.run(_APP.stop())
        except Exception:  # noqa: BLE001 - stopping a probe app must not fail the result
            pass
    if _PROBE_LOG is not None:
        try:
            _PROBE_LOG.close()
        except Exception:  # noqa: BLE001 - a diagnostic file cannot fail the result
            pass
    report["finished_at"] = datetime.now(UTC).isoformat()
    path = out_dir / f"{stamp}_host_probe_report.json"
    report["report_path"] = str(path)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str)[:9000])
    app.quit()
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="data/novaforge/nf13a")
    raise SystemExit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
