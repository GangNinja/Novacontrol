"""NF-14 live probe — compatibility classification and safe recovery.

What this measures, on this host, with the REAL application and the real Win32
adapter (no test doubles):

1. An installed game (Shadow Fight Arena, launched through the NF-12 token path
   and closed gracefully) is classified ``UNSUPPORTED`` for native embedding,
   with zero native window calls made.
2. A disposable WinForms window (NOT a game) is classified ``SUPPORTED``, is
   embedded and verified, survives losing the foreground, is resized, and ends
   in a truthful ``closed`` state when its process is destroyed.
3. Capture is measured separately: a hosted window's ``PrintWindow`` refusal is
   recorded next to the successful embedding, never instead of it.
4. The compatibility records persist through the application's state store.

Safety: no game input is synthesized, no game is force-killed, GTA V is never
launched, and the fixture is a disposable window this script itself started.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QVBoxLayout, QWidget  # noqa: E402

from novacontrol.application import NovaControlApplication  # noqa: E402
from novacontrol.desktop.capture_validation import inspect_frame  # noqa: E402
from novacontrol.desktop.controller import LocalDesktopRunner  # noqa: E402
from novacontrol.gui.game_host import GameHostPanel  # noqa: E402
from novacontrol.novaforge.compat import CompatibilityLog  # noqa: E402
from novacontrol.novaforge.embedding import (  # noqa: E402
    EmbeddedGameHost,
    GameIdentity,
    Win32WindowApi,
)

TITLE = "NC NF14 RECOVERY PROBE"
FOCUS_TITLE = "NC NF14 FOCUS PROBE"
GAME = "Shadow Fight Arena"


class RecordingApi:
    """Win32 adapter with every WRITE recorded; reads pass straight through."""

    def __init__(self) -> None:
        self.inner = Win32WindowApi()
        self.writes: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def set_parent(self, child: int, parent: int) -> None:
        self.writes.append(f"set_parent({child} -> {parent})")
        self.inner.set_parent(child, parent)

    def set_style(self, handle: int, style: int) -> int:
        self.writes.append(f"set_style({handle}, 0x{style:08X})")
        return self.inner.set_style(handle, style)

    def set_window_pos(
        self, handle: int, x: int, y: int, width: int, height: int, flags: int
    ) -> bool:
        self.writes.append(f"set_window_pos({handle}, {x},{y} {width}x{height}, 0x{flags:X})")
        return self.inner.set_window_pos(handle, x, y, width, height, flags)

    def focus(self, handle: int) -> bool:
        self.writes.append(f"focus({handle})")
        return self.inner.focus(handle)


def pump(app: QApplication, seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def wait_for_window(api: Win32WindowApi, pid: int, title: str, timeout: float = 20.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lookup = api.find_window(pid, title)
        if lookup.handle:
            return int(lookup.handle)
        time.sleep(0.2)
    return 0


def start_fixture(out_dir: Path, stamp: str, title: str, seconds: int = 240) -> subprocess.Popen[bytes]:
    log = open(out_dir / f"{stamp}_{title.replace(' ', '_').lower()}_stderr.log", "wb")
    return subprocess.Popen(
        [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(ROOT / "scripts" / "nf13a_embed_probe_window.ps1"),
            "-Seconds", str(seconds), "-Title", title,
            "-LogPath", str(out_dir / f"{stamp}_{title.replace(' ', '_').lower()}_paint.log"),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def foreground_window() -> int:
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def focus_handle(handle: int) -> bool:
    return bool(ctypes.windll.user32.SetForegroundWindow(handle))


async def capture_window(handle: int, path: Path) -> dict[str, Any]:
    runner = LocalDesktopRunner()
    try:
        result = await runner.capture_window(int(handle), str(path))
        body = {
            "ok": bool(result.ok),
            "backend": getattr(result, "backend", "printwindow"),
            "source": getattr(result, "source", "window_surface"),
            "width": int(getattr(result, "width", 0) or 0),
            "height": int(getattr(result, "height", 0) or 0),
            "size_bytes": int(getattr(result, "size_bytes", 0) or 0),
            "error": str(getattr(result, "error", "") or ""),
        }
        if body["ok"] and Path(result.path).is_file():
            body["inspection"] = inspect_frame(result.path).to_dict()
        return body
    finally:
        await runner.close()


async def sfa_classification(
    nova: NovaControlApplication, api: RecordingApi, report: dict[str, Any]
) -> dict[str, Any] | None:
    """Launch SFA through the NF-12 token path, classify, close gracefully."""
    library = await nova.game_library()
    report["memory"] = library.get("memory")
    gate = library.get("memory") or {}
    if not gate.get("ok"):
        report["sfa"] = {"tested": False, "reason": "The memory gate refused; nothing launched."}
        return None
    command = f"launch {GAME}"
    plan = await nova.plan_game_launch(command)
    token = str((plan.get("approval") or {}).get("token") or "")
    if not token:
        report["sfa"] = {
            "tested": False,
            "reason": "The plan carried no token.",
            "summary": plan.get("summary"),
        }
        return None
    result = await nova.execute_game_launch(command, approval_token=token)
    launch = (result.get("game") or {}).get("launch") or {}
    if not launch.get("pid"):
        report["sfa"] = {
            "tested": False,
            "reason": "The launch did not produce a process.",
            "summary": result.get("summary"),
        }
        return None
    identity = GameIdentity(
        pid=int(launch.get("pid") or 0),
        start_time=str(launch.get("start_time") or ""),
        window_title=str(launch.get("window") or GAME),
        name=str(launch.get("name") or GAME),
        owned=True,
        launch_id=str(launch.get("launch_id") or ""),
    )
    host = EmbeddedGameHost(api)
    try:
        record = host.probe_compatibility(identity, app_id="steam:1446890")
        preflight = host.preflight(identity)
        attempt = host.embed(identity, 1)  # any handle: the refusal happens first
    finally:
        # Safety: the test-owned game is closed through the service (WM_CLOSE,
        # exit confirmed) even when a measurement raised.
        close = await nova.close_game_launch(launch_id=identity.launch_id)
    body = {
        "tested": True,
        "pid": identity.pid,
        "start_time": identity.start_time,
        "launch": {key: launch.get(key) for key in ("pid", "start_time", "window", "name")},
        "stages": result.get("stages"),
        "classification": str(record.classification),
        "reason_code": record.reason_code,
        "reason": record.reason,
        "window_mode": record.window_mode,
        "window_class": record.window_class,
        "evidence": [item.to_dict() for item in record.evidence],
        "preflight": preflight.to_dict(),
        "attempt": {
            "embedded": attempt.get("embedded"),
            "reason_code": (attempt.get("embedding") or {}).get("reason_code"),
            "applied": (attempt.get("embedding") or {}).get("applied"),
            "recovery": (attempt.get("embedding") or {}).get("recovery"),
            "partial": (attempt.get("embedding") or {}).get("partial"),
            "class": (attempt.get("compatibility") or {}).get("classification"),
            "capture": (attempt.get("compatibility") or {}).get("capture"),
        },
        "fallback": attempt.get("fallback"),
        "native_writes": list(api.writes),
    }
    body["close"] = {
        "status": close.get("status"),
        "closed": close.get("closed"),
        "summary": close.get("summary"),
    }
    report["sfa"] = body
    return record


def run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report: dict[str, Any] = {
        "phase": "NF-14-compatibility-recovery",
        "started_at": datetime.now(UTC).isoformat(),
        "gameplay_claimed": False,
        "input_sent": False,
        "errors": [],
        "steps": {},
    }
    app = QApplication.instance() or QApplication([])
    nova: NovaControlApplication | None = None
    fixture: subprocess.Popen[bytes] | None = None
    focus_fixture: subprocess.Popen[bytes] | None = None
    window: QMainWindow | None = None
    panel: GameHostPanel | None = None
    api = RecordingApi()
    try:
        nova = NovaControlApplication(data_dir=Path("data"))
        asyncio.run(nova.start())

        sfa_record = asyncio.run(sfa_classification(nova, api, report))

        # -- the fixture: a real native window, NOT a game ------------------
        fixture = start_fixture(out_dir, stamp, TITLE)
        fixture_handle = wait_for_window(api, fixture.pid, TITLE)
        if not fixture_handle:
            report["errors"].append("The disposable probe window did not appear.")
            return _finish(report, out_dir, stamp, novacontrol_app=nova, panel=panel,
                           fixture=fixture, focus_fixture=focus_fixture, app=app, status=1)
        info = api.window(fixture_handle)
        report["steps"]["fixture"] = {"pid": fixture.pid, "window": info.to_dict()}
        created = api.process_creation_time(fixture.pid)
        identity = GameIdentity(
            pid=fixture.pid,
            start_time=datetime.fromtimestamp(created, UTC).isoformat() if created else "",
            window_title=TITLE,
            name="NF-14 recovery probe",
            owned=True,
            launch_id="nf14-probe",
        )

        window = QMainWindow()
        window.setWindowTitle("NovaControl — NF-14 compatibility/recovery probe")
        window.resize(1180, 780)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addWidget(QLabel(
            "Disposable native window hosted by the real NF-13 host. Not a game; "
            "no gameplay, input or capture claim is made."
        ))
        panel = GameHostPanel(nova, asyncio.run, parent=root)
        layout.addWidget(panel, stretch=1)
        window.setCentralWidget(root)
        window.move(40, 40)
        window.show()
        window.raise_()
        window.activateWindow()
        pump(app, 1.0)
        panel.refresh_library()
        pump(app, 0.6)
        panel.host.set_running(
            {
                "pid": fixture.pid,
                "start_time": identity.start_time,
                "window": TITLE,
                "name": identity.name,
                "owned": True,
                "launch_id": "nf14-probe",
            },
            name=identity.name,
        )
        panel._render_host()

        compat = panel.host.probe_compatibility(identity, app_id="probe")
        report["steps"]["fixture_compatibility"] = {
            "classification": str(compat.classification),
            "reason_code": compat.reason_code,
            "window_mode": compat.window_mode,
            "capture": compat.capture,
            "evidence": [item.to_dict() for item in compat.evidence],
        }
        embedded = panel.host.embed(identity, panel.surface.native_handle())
        report["steps"]["fixture_embed"] = {
            "embedded": embedded.get("embedded"),
            "reason": (embedded.get("embedding") or {}).get("reason"),
            "partial": (embedded.get("embedding") or {}).get("partial"),
            "applied": (embedded.get("embedding") or {}).get("applied"),
            "parent": api.parent(fixture_handle),
            "window": api.window(fixture_handle).to_dict(),
            "class": (embedded.get("compatibility") or {}).get("classification"),
            "capture": (embedded.get("compatibility") or {}).get("capture"),
        }
        if not embedded.get("embedded"):
            report["errors"].append("The disposable window could not be embedded.")

        # -- focus change: another window takes the foreground ---------------
        focus_fixture = start_fixture(out_dir, stamp, FOCUS_TITLE, seconds=120)
        focus_handle_value = wait_for_window(api, focus_fixture.pid, FOCUS_TITLE)
        moved = focus_handle(focus_handle_value) if focus_handle_value else False
        pump(app, 0.8)
        after_focus = panel.host.check()
        report["steps"]["focus_change"] = {
            "other_window": focus_handle_value,
            "foreground_is_other": foreground_window() in {focus_handle_value, 0},
            "focus_request_accepted": moved,
            "state_after": after_focus.get("state"),
            "parent_after": api.parent(fixture_handle),
            "check_reason": (after_focus.get("embedding") or {}).get("last_check_reason"),
        }

        # -- resize ----------------------------------------------------------
        window.resize(1000, 720)
        pump(app, 0.9)
        resized = panel.host.resize()
        report["steps"]["resize"] = {
            "resized": resized.get("resized"),
            "deferred": resized.get("deferred"),
            "target": resized.get("target"),
            "surface_client": api.client_rect(panel.surface.native_handle()).to_dict(),
            "game_rect": api.screen_rect(fixture_handle).to_dict(),
            "parent": api.parent(fixture_handle),
        }

        # -- capture, reported independently of the embedding ----------------
        capture_result = asyncio.run(
            capture_window(fixture_handle, out_dir / f"{stamp}_embedded_capture.png")
        )
        report["steps"]["capture_of_embedded"] = capture_result
        # The capture axis is set from THIS measurement, never from the embedding
        # and never from the capability read.
        panel.host.note_capture(
            bool(capture_result.get("ok")), error=str(capture_result.get("error") or "")
        )
        status = panel.host.status_payload(
            observation=dict(panel._library.get("observation") or {})
        )
        report["steps"]["capture_separation"] = {
            "embedding_verified": status["embedding"]["verified"],
            "capture_ok": report["steps"]["capture_of_embedded"].get("ok"),
            "compatibility_embedding_axis": status["compatibility"]["embedding"],
            "compatibility_capture_axis": status["compatibility"]["capture"],
            "observation_available": (status.get("observation") or {}).get("available"),
            "implied_by_embedding": (status.get("observation") or {}).get("implied_by_embedding"),
        }

        # -- process destruction: the host must say CLOSED, not "embedded" ---
        fixture.terminate()
        try:
            fixture.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            fixture.kill()
            fixture.wait(timeout=10)
        pump(app, 0.8)
        after_death = panel.host.check()
        report["steps"]["window_destroyed"] = {
            "process_exit_code": fixture.poll(),
            "state": after_death.get("state"),
            "reason_code": (after_death.get("embedding") or {}).get("reason_code"),
            "window": after_death.get("window"),
        }
        cleanup = panel.host.cleanup()
        report["steps"]["cleanup_after_destruction"] = {
            "cleaned": cleanup.get("cleaned"),
            "changed": cleanup.get("changed"),
            "restored": cleanup.get("restored"),
            "state": cleanup.get("state"),
            "reason": cleanup.get("reason"),
        }

        # -- records persist -------------------------------------------------
        log = CompatibilityLog(getattr(nova, "state_store", None))
        live_records = [record for record in (sfa_record, panel.host.compatibility) if record]
        for record in live_records:
            log.record(record)
        reloaded = CompatibilityLog(getattr(nova, "state_store", None))
        report["steps"]["compatibility_log"] = {
            "recorded": len(live_records),
            "persisted": len(reloaded.all()),
            "latest_sfa": reloaded.latest(GAME).to_dict() if reloaded.latest(GAME) else None,
            "latest_fixture": (
                reloaded.latest("NF-14 recovery probe").to_dict()
                if reloaded.latest("NF-14 recovery probe")
                else None
            ),
            "namespace": CompatibilityLog.NAMESPACE,
        }
        return _finish(report, out_dir, stamp, novacontrol_app=nova, panel=panel,
                       fixture=fixture, focus_fixture=focus_fixture, app=app)
    except Exception as exc:  # noqa: BLE001 - the probe records, never pretends
        import traceback

        report["errors"].append(f"{type(exc).__name__}: {exc}")
        report["traceback"] = traceback.format_exc()
        return _finish(report, out_dir, stamp, novacontrol_app=nova, panel=panel,
                       fixture=fixture, focus_fixture=focus_fixture, app=app, status=1)


def _finish(
    report: dict[str, Any],
    out_dir: Path,
    stamp: str,
    *,
    novacontrol_app: NovaControlApplication | None,
    panel: GameHostPanel | None,
    fixture: subprocess.Popen[bytes] | None,
    focus_fixture: subprocess.Popen[bytes] | None,
    app: QApplication,
    status: int = 0,
) -> int:
    if panel is not None:
        try:
            panel.shutdown()
        except Exception:  # noqa: BLE001 - shutdown of a probe panel
            pass
    for process in (fixture, focus_fixture):
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                process.kill()
                process.wait(timeout=10)
    if novacontrol_app is not None:
        try:
            asyncio.run(novacontrol_app.stop())
        except Exception:  # noqa: BLE001 - probe teardown
            pass
    report["finished_at"] = datetime.now(UTC).isoformat()
    path = out_dir / f"{stamp}_nf14_report.json"
    report["report_path"] = str(path)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "report": str(path),
        "errors": report["errors"],
        "sfa_class": (report.get("sfa") or {}).get("classification"),
        "fixture_embedded": (report["steps"].get("fixture_embed") or {}).get("embedded"),
        "state_after_destruction": (report["steps"].get("window_destroyed") or {}).get("state"),
    }))
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="data/novaforge/nf14")
    raise SystemExit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
