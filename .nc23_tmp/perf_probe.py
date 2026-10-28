"""NF-23 Objective F: the measured operating envelope, not a single good sample.

Twenty consecutive window-surface frames of the probe's own disposable window,
at a 0.5 s cadence, with the worker's own working set and CPU time before/after.
No game, no input, nothing focused.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / ".nc23_tmp"))

from novacontrol.desktop.capture import PersistentScreenCapture  # noqa: E402
from novacontrol.desktop.capture_backends import (  # noqa: E402
    CaptureRouter,
    CaptureTarget,
    DisplayCropBackend,
    WindowSurfaceBackend,
)
from observer_lifecycle_probe import spawn_window  # noqa: E402

OUT = ROOT / ".nc23_tmp" / "out"
TITLE = "NC NF23 LIFECYCLE PROBE"
FRAMES = 20


def say(**fields: object) -> None:
    print(json.dumps(fields, default=str), flush=True)


def ms(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    if not ordered:
        return {}
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return {
        "n": len(ordered),
        "min": round(ordered[0], 1),
        "median": round(statistics.median(ordered), 1),
        "p95": round(ordered[index], 1),
        "max": round(ordered[-1], 1),
    }


async def main() -> int:  # noqa: C901 - a scripted probe, linear by design
    OUT.mkdir(parents=True, exist_ok=True)
    window = spawn_window(seconds=150)
    worker = PersistentScreenCapture(startup_timeout=60.0, frame_timeout=60.0)
    if not await worker.ensure_started():
        say(error=worker.last_error)
        return 1
    geometry = None
    for _ in range(40):
        geometry = await worker.window_geometry(title=TITLE)
        if geometry is not None:
            break
        await asyncio.sleep(0.5)
    if geometry is None:
        say(error="the disposable window never appeared")
        window.terminate()
        await worker.aclose()
        return 1
    target = CaptureTarget(
        handle=int(getattr(geometry, "handle")),
        title=str(getattr(geometry, "title", "")),
        bounds=tuple(getattr(geometry, "bounds")),
        minimized=bool(getattr(geometry, "minimized", False)),
        visible=bool(getattr(geometry, "visible", True)),
        foreground=bool(getattr(geometry, "foreground", False)),
    )
    router = CaptureRouter(
        (
            WindowSurfaceBackend(lambda h, path: worker.capture_window(h, path, thumbnail=True)),
            DisplayCropBackend(worker.capture),
        )
    )
    await asyncio.sleep(2.0)
    idle = await worker.stats()
    say(
        worker_idle_working_set_mb=round(idle.working_set_bytes / 1048576, 1),
        worker_idle_cpu_ms=idle.cpu_ms,
        window_bounds=list(target.bounds),
    )

    frame_latency: list[float] = []
    schedule: list[float] = []
    validation: list[float] = []
    dims: set[tuple[int, int]] = set()
    contents: set[str] = set()
    accepted = 0
    expected = time.monotonic()
    for index in range(FRAMES):
        expected = expected + (0.5 if index else 0.0)
        delay = max(0.0, expected - time.monotonic())
        if delay:
            await asyncio.sleep(delay)
        path = OUT / f"perf_{index:03d}.png"
        started = time.perf_counter()
        outcome = await router.capture(str(path), target)
        elapsed = (time.perf_counter() - started) * 1000.0
        frame_latency.append(elapsed)
        schedule.append(round(max(0.0, started - expected) * 1000.0, 1))
        if outcome.ok:
            accepted += 1
            dims.add((outcome.width, outcome.height))
            contents.add(str(outcome.verdict.get("content")))
        # The validation share, measured separately from the capture it judges.
        thumb = Path(str(path) + ".thumb")
        if thumb.exists():
            at = time.perf_counter()
            router.last_content_hash = ""
            from novacontrol.desktop.capture_validation import inspect_frame

            inspect_frame(path, thumbnail=thumb)
            validation.append((time.perf_counter() - at) * 1000.0)
            thumb.unlink(missing_ok=True)
        else:
            from novacontrol.desktop.capture_validation import inspect_frame

            at = time.perf_counter()
            inspect_frame(path)
            validation.append((time.perf_counter() - at) * 1000.0)
    busy = await worker.stats()
    cpu_delta = busy.cpu_ms - idle.cpu_ms
    say(
        frames_requested=FRAMES,
        frames_accepted=accepted,
        validated_rate=round(accepted / FRAMES, 3),
        latency_ms=ms(frame_latency),
        schedule_delay_ms=ms(schedule),
        validation_ms=ms(validation),
        dims=sorted(dims),
        contents=sorted(contents),
        worker_busy_working_set_mb=round(busy.working_set_bytes / 1048576, 1),
        worker_working_set_growth_mb=round((busy.working_set_bytes - idle.working_set_bytes) / 1048576, 1),
        worker_cpu_ms_delta=cpu_delta,
        worker_cpu_ms_per_frame=round(cpu_delta / max(1, accepted), 1),
        cadence_achieved_hz=round(accepted / (FRAMES * 0.5), 2),
    )

    # Time-to-stale: the same file, read after the freshness window, is refused.
    last = OUT / f"perf_{FRAMES - 1:03d}.png"
    await asyncio.sleep(3.6)
    from novacontrol.desktop.capture_validation import validate_frame, inspect_frame

    inspection = inspect_frame(last)
    age = max(0.0, time.time() - last.stat().st_mtime)
    stale = validate_frame(
        capture_ok=True,
        backend="printwindow",
        source="window_surface",
        target_visible=True,
        foreground=False,
        inspection=inspection,
        # The router measures age from the file's own mtime; this is that
        # number at the moment of the read.
        age_seconds=age,
        max_age_seconds=3.0,
    )
    say(
        stale_probe_age_seconds=round(age, 2),
        stale_probe_livable=stale.livable,
        stale_probe_content=stale.content,
        stale_probe_reason=stale.reason,
        stale_probe_limitations=list(stale.limitations),
    )

    await worker.aclose()
    window.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
