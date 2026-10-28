"""Real-host occlusion probe: a covered and a minimized window.

Uses a DISPOSABLE animated window (never a game) so a covered, painting window
is a real case on this machine: the installed fullscreen game minimizes itself
when it loses focus, which cannot test occlusion. Observes only; no input.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

from novacontrol.desktop.capture import PersistentScreenCapture
from novacontrol.desktop.capture_backends import (
    CaptureRouter,
    CaptureTarget,
    DisplayCropBackend,
    WindowSurfaceBackend,
)
from novacontrol.desktop.capture_validation import inspect_frame, validate_frame

TITLE = "NC CAPTURE OCCLUSION TARGET"
COVER = "Freebuff"
HERE = Path(".nc_cap_evidence") / "universal"
HERE.mkdir(parents=True, exist_ok=True)


def ps(script: str, *args: str) -> str:
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, *args],
        capture_output=True, text=True, timeout=60,
    )
    return (completed.stdout or completed.stderr).strip()[:300]


def focus(title: str) -> str:
    return ps(".nc_win_ctl.ps1", "-Action", "focus", "-Title", title)


def ctl(action: str) -> dict:
    raw = ps(".nc_cap_evidence/occlusion_ctl.ps1", "-Action", action, "-Title", TITLE)
    try:
        return json.loads(raw.splitlines()[-1])
    except Exception:
        return {"raw": raw}


async def capture_many(worker, router, label: str, target: CaptureTarget, count: int = 5):
    rows = []
    for index in range(count):
        path = str((HERE / f"{label}_{index}.png").resolve())
        started = time.perf_counter()
        outcome = await router.capture(path, target, previous_hash=router.last_content_hash)
        rows.append(
            {
                "index": index,
                "ok": outcome.ok,
                "backend": outcome.backend,
                "source": outcome.source,
                "content": outcome.verdict.get("content"),
                "livable": outcome.verdict.get("livable"),
                "reason": outcome.reason,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
                "width": outcome.width,
                "height": outcome.height,
                "hash": (outcome.inspection or {}).get("content_hash", "")[:16],
                "attempts": list(outcome.attempts),
            }
        )
        await asyncio.sleep(0.15)
    return rows


async def main() -> None:
    report: dict = {"target": TITLE, "cover_with": COVER, "phases": {}}
    process = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         ".nc_cap_evidence/occlusion_target.ps1", "-Seconds", "120"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    worker = PersistentScreenCapture()
    try:
        if not await worker.ensure_started():
            report["error"] = f"worker did not start: {worker.last_error}"
            print(json.dumps(report, indent=2))
            return
        geometry = None
        for _ in range(40):
            geometry = await worker.window_geometry(title=TITLE)
            if geometry is not None and not geometry.minimized:
                break
            await asyncio.sleep(0.25)
        if geometry is None:
            report["error"] = "the disposable window never appeared"
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

        def target_for(geo) -> CaptureTarget:
            return CaptureTarget(
                handle=geo.handle, title=geo.title, bounds=geo.bounds,
                minimized=geo.minimized, visible=geo.visible, foreground=geo.foreground,
            )

        report["phases"]["foreground"] = await capture_many(
            worker, router, "occ_foreground", target_for(geometry)
        )

        # Cover it: another window gets the focus and sits on top. The disposable
        # window keeps painting and does NOT minimize itself.
        report["focus_cover"] = focus(COVER)
        await asyncio.sleep(1.2)
        covered = await worker.window_geometry(handle=geometry.handle)
        report["geometry_covered"] = (
            {"foreground": covered.foreground, "minimized": covered.minimized,
             "bounds": list(covered.bounds)}
            if covered is not None else None
        )
        if covered is not None and not covered.minimized:
            report["phases"]["covered"] = await capture_many(
                worker, router, "occ_covered", target_for(covered)
            )
            # The desktop crop of the same covered window must be REFUSED as the
            # target: the crop contains the covering window.
            crop_path = str((HERE / "occ_covered_crop.png").resolve())
            crop = await DisplayCropBackend(worker.capture).capture_frame(
                crop_path, target_for(covered)
            )
            report["covered_crop"] = {
                "ok": crop.ok,
                "source": crop.source,
                "error": crop.error,
                "verdict": validate_frame(
                    capture_ok=crop.ok, backend=crop.backend, source=crop.source,
                    foreground=covered.foreground,
                    inspection=inspect_frame(crop_path) if crop.ok else None,
                ).to_dict(),
            }

        # Minimize: the honest answer is a refusal, never a black frame.
        report["minimize"] = ctl("minimize")
        await asyncio.sleep(0.8)
        minimized = await worker.window_geometry(handle=geometry.handle)
        report["geometry_minimized"] = (
            {"foreground": minimized.foreground, "minimized": minimized.minimized,
             "bounds": list(minimized.bounds)}
            if minimized is not None else None
        )
        if minimized is not None:
            path = str((HERE / "occ_minimized.png").resolve())
            outcome = await router.capture(path, target_for(minimized))
            report["minimized_capture"] = {
                "ok": outcome.ok,
                "code": outcome.verdict.get("code"),
                "reason": outcome.reason,
                "attempts": list(outcome.attempts),
                "file_exists": Path(path).exists(),
            }

        # Restore and confirm capture resumes (recovery, not a wedged state).
        report["restore"] = ctl("restore")
        await asyncio.sleep(1.0)
        back = await worker.window_geometry(handle=geometry.handle)
        if back is not None:
            report["phases"]["restored"] = await capture_many(
                worker, router, "occ_restored", target_for(back), count=2
            )
        report["worker"] = {"spawns": worker.spawns, "frames": worker.frames}
    finally:
        await worker.aclose()
        report["close"] = ctl("close")
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        print(json.dumps(report, indent=2))


asyncio.run(main())
