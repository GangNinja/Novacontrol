"""Latency measurements for the capture backends and the validation layer.

Reads the real screen/window; sends nothing.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

from novacontrol.desktop.capture import PersistentScreenCapture
from novacontrol.desktop.capture_backends import CaptureTarget
from novacontrol.desktop.capture_validation import inspect_frame

TITLE = sys.argv[1] if len(sys.argv) > 1 else "Freedom Fighters"
OUT = Path(".nc_cap_evidence") / "universal"
OUT.mkdir(parents=True, exist_ok=True)
RUNS = 6


async def main() -> None:
    worker = PersistentScreenCapture()
    report: dict = {"target": TITLE, "runs": RUNS}
    try:
        if not await worker.ensure_started():
            report["error"] = worker.last_error
            print(json.dumps(report, indent=2))
            return
        geometry = await worker.window_geometry(title=TITLE)
        if geometry is None:
            report["error"] = "no window"
            print(json.dumps(report, indent=2))
            return
        report["bounds"] = list(geometry.bounds)
        report["foreground"] = geometry.foreground
        surface: list[float] = []
        display: list[float] = []
        sizes: dict[str, int] = {}
        for index in range(RUNS):
            window_path = str((OUT / f"perf_surface_{index}.png").resolve())
            started = time.perf_counter()
            result = await worker.capture_window(geometry.handle, window_path)
            surface.append((time.perf_counter() - started) * 1000.0)
            if result.ok:
                sizes["surface_bytes"] = result.size_bytes
                sizes["surface_px"] = f"{result.width}x{result.height}"
            display_path = str((OUT / f"perf_display_{index}.png").resolve())
            started = time.perf_counter()
            shot = await worker.capture(display_path)
            display.append((time.perf_counter() - started) * 1000.0)
            if shot.ok:
                sizes["display_bytes"] = shot.size_bytes
                sizes["display_px"] = f"{shot.width}x{shot.height}"
        report["surface_ms"] = [round(value, 1) for value in surface]
        report["display_ms"] = [round(value, 1) for value in display]
        report["surface_median_ms"] = round(statistics.median(surface), 1)
        report["display_median_ms"] = round(statistics.median(display), 1)
        report["sizes"] = sizes
        validation: list[float] = []
        for index in range(RUNS):
            path = OUT / f"perf_surface_{index}.png"
            started = time.perf_counter()
            inspection = inspect_frame(path)
            validation.append((time.perf_counter() - started) * 1000.0)
        report["validation_ms"] = [round(value, 1) for value in validation]
        report["validation_median_ms"] = round(statistics.median(validation), 1)
        report["validation_readable"] = inspection.readable
        report["validation_blank"] = inspection.blank
        report["worker"] = {"spawns": worker.spawns, "frames": worker.frames}
    finally:
        await worker.aclose()
        print(json.dumps(report, indent=2))


asyncio.run(main())
