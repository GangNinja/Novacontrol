"""Real-host capture probe: the production router against the running game.

Observes only. Sends no gameplay input, launches no process beyond the capture
worker, and restores the window that had focus when it started.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from novacontrol.desktop.capture import PersistentScreenCapture
from novacontrol.desktop.capture_backends import (
    CaptureRouter,
    CaptureTarget,
    DisplayCropBackend,
    WindowSurfaceBackend,
)
from novacontrol.desktop.capture_validation import validate_frame

TITLE = sys.argv[1] if len(sys.argv) > 1 else "Freedom Fighters"
BACKGROUND_TITLE = sys.argv[2] if len(sys.argv) > 2 else "Freebuff"
HERE = Path(".nc_cap_evidence") / "universal"
HERE.mkdir(parents=True, exist_ok=True)


def focus(title: str) -> str:
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         ".nc_win_ctl.ps1", "-Action", "focus", "-Title", title],
        capture_output=True, text=True, timeout=30,
    )
    return (completed.stdout or completed.stderr).strip()[:200]


def foreground_title() -> str:
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;"
         "public static class FG{[DllImport(\"user32.dll\")]public static extern IntPtr GetForegroundWindow();"
         "[DllImport(\"user32.dll\")]public static extern int GetWindowText(IntPtr h,System.Text.StringBuilder s,int m);}';"
         "$b=New-Object System.Text.StringBuilder 512;"
         "[FG]::GetWindowText([FG]::GetForegroundWindow(),$b,512)|Out-Null;$b.ToString()"],
        capture_output=True, text=True, timeout=30,
    )
    return (completed.stdout or "").strip()


async def run(worker: PersistentScreenCapture, router: CaptureRouter, label: str,
              target: CaptureTarget, count: int = 5) -> list[dict]:
    rows = []
    for index in range(count):
        path = str((HERE / f"{label}_{index}.png").resolve())
        started = time.perf_counter()
        outcome = await router.capture(path, target, previous_hash=router.last_content_hash)
        latency = (time.perf_counter() - started) * 1000.0
        rows.append(
            {
                "label": label,
                "index": index,
                "ok": outcome.ok,
                "backend": outcome.backend,
                "source": outcome.source,
                "reason": outcome.reason,
                "latency_ms": round(latency, 1),
                "backend_ms": outcome.elapsed_ms,
                "width": outcome.width,
                "height": outcome.height,
                "content": outcome.verdict.get("content"),
                "livable": outcome.verdict.get("livable"),
                "limitations": outcome.verdict.get("limitations"),
                "hash": (outcome.inspection or {}).get("content_hash", "")[:16],
                "attempts": list(outcome.attempts),
            }
        )
        await asyncio.sleep(0.15)
    return rows


async def main() -> None:
    worker = PersistentScreenCapture()
    started_foreground = foreground_title()
    report: dict = {"game": TITLE, "start_foreground": started_foreground, "phases": {}}
    try:
        if not await worker.ensure_started():
            report["error"] = f"worker did not start: {worker.last_error}"
            print(json.dumps(report, indent=2))
            return
        geometry = await worker.window_geometry(title=TITLE)
        if geometry is None:
            report["error"] = f"no window matches {TITLE!r}"
            print(json.dumps(report, indent=2))
            return
        report["geometry"] = {
            "title": geometry.title, "handle": geometry.handle, "pid": geometry.pid,
            "bounds": list(geometry.bounds), "client": list(geometry.client),
            "foreground": geometry.foreground, "minimized": geometry.minimized,
            "visible": geometry.visible, "scale": geometry.scale,
        }
        router = CaptureRouter(
            (WindowSurfaceBackend(worker.capture_window), DisplayCropBackend(worker.capture))
        )
        report["backends"] = router.capabilities()

        def target_for(geo) -> CaptureTarget:
            return CaptureTarget(
                handle=geo.handle, title=geo.title, bounds=geo.bounds,
                minimized=geo.minimized, visible=geo.visible, foreground=geo.foreground,
            )

        report["phases"]["as_found"] = await run(worker, router, "as_found", target_for(geometry))

        # Restore the game (it was found minimized, hence the placeholder bounds)
        # so the foreground and covered cases can actually be measured. Restoring
        # a window is not gameplay input.
        report["restore_game"] = focus(TITLE)
        await asyncio.sleep(1.5)
        restored = await worker.window_geometry(handle=geometry.handle)
        if restored is not None:
            report["geometry_restored"] = {
                "foreground": restored.foreground, "minimized": restored.minimized,
                "bounds": list(restored.bounds), "client": list(restored.client),
            }
            report["phases"]["foreground"] = await run(
                worker, router, "foreground", target_for(restored)
            )

        # Prove the DESKTOP backend really produces a crop of a background window
        # and that validation refuses it as the target.
        report["focus_change"] = focus(BACKGROUND_TITLE)
        await asyncio.sleep(1.0)
        report["foreground_after_change"] = foreground_title()
        background = await worker.window_geometry(handle=geometry.handle)
        if background is not None:
            report["geometry_after_focus_change"] = {
                "foreground": background.foreground, "minimized": background.minimized,
                "bounds": list(background.bounds),
            }
            crop_path = str((HERE / "background_crop.png").resolve())
            crop = await DisplayCropBackend(worker.capture).capture_frame(
                crop_path, target_for(background)
            )
            report["background_crop"] = {
                "ok": crop.ok, "source": crop.source, "error": crop.error,
                "verdict": validate_frame(
                    capture_ok=crop.ok, backend=crop.backend, source=crop.source,
                    foreground=background.foreground,
                    inspection=None,
                ).to_dict(),
            }
            report["phases"]["background"] = await run(
                worker, router, "background", target_for(background)
            )
            report["phases"]["background_router_attempts"] = report["phases"]["background"][0][
                "attempts"
            ]
        report["worker"] = {"spawns": worker.spawns, "frames": worker.frames}
    finally:
        await worker.aclose()
        if started_foreground:
            report["restore_focus"] = focus(started_foreground[:40])
        print(json.dumps(report, indent=2))


asyncio.run(main())
