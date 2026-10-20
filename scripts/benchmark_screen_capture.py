"""Measure both screen-capture arms on THIS machine.

    python scripts/benchmark_screen_capture.py --frames 12 --baseline-frames 4

Arm 1 — ``oneshot``: today's path. One PowerShell process per frame, which
re-loads ``System.Windows.Forms``/``System.Drawing`` and re-compiles the
``Add-Type -MemberDefinition`` DPI declaration before it can BitBlt a pixel.

Arm 2 — ``persistent``: one worker process serves every frame
(``novacontrol.desktop.capture.PersistentScreenCapture``).

Both arms run the SAME capture program text, so the difference measured is the
process model and nothing else. Reported per arm:

* latency distribution (min / median / mean / p95 / max) and effective FPS
* frames written, bytes per frame, and the worker's own report
* FRESHNESS — the file's mtime must advance past the request stamp for every
  frame, otherwise a stale file is being counted as a capture
* worker resource use (spawns, working set) and CLEANUP (process must be gone
  after close)

The script never claims a target FPS: it prints what it measured, on this
machine, in this run. ``--json`` writes the raw records.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novacontrol.desktop.capture import (  # noqa: E402
    PersistentScreenCapture,
    capture_supported_here,
    oneshot_capture_script,
)


async def _powershell(script: str, *, timeout: float) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        "powershell", "-NoProfile", "-NoLogo", "-NonInteractive", "-Command", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    text = stdout.decode("utf-8", "replace") + stderr.decode("utf-8", "replace")
    return process.returncode or 0, text.strip()


def _record(
    path: Path, *, requested_wall: datetime, started: float, frame: int, arm: str
) -> dict[str, object]:
    elapsed_ms = (time.monotonic() - started) * 1000
    try:
        stat = path.stat()
        payload = path.read_bytes()
    except OSError:
        return {"arm": arm, "frame": frame, "ok": False, "error": "no file written"}
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    return {
        "arm": arm,
        "frame": frame,
        "ok": True,
        "latency_ms": round(elapsed_ms, 2),
        "size_bytes": stat.st_size,
        "sha256": hashlib.sha256(payload).hexdigest()[:16],
        # Freshness: the file must have been written AFTER the request stamp.
        # A capture that returned an old file on disk would fail this.
        "fresh": mtime >= requested_wall,
        "mtime": mtime.isoformat(),
    }


async def _baseline(frames: int, directory: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for frame in range(frames):
        target = directory / f"oneshot_{frame}.png"
        requested_wall = datetime.now(UTC)
        started = time.monotonic()
        try:
            await _powershell(oneshot_capture_script(str(target)), timeout=60)
        except Exception as exc:  # a failed frame is recorded, never hidden
            records.append({
                "arm": "oneshot", "frame": frame, "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        records.append(_record(
            target, requested_wall=requested_wall, started=started, frame=frame, arm="oneshot"
        ))
    return records


async def _persistent(
    frames: int, directory: Path
) -> tuple[list[dict[str, object]], dict[str, object]]:
    worker = PersistentScreenCapture()
    records: list[dict[str, object]] = []
    for frame in range(frames):
        target = directory / "persistent.png"
        requested_wall = datetime.now(UTC)
        started = time.monotonic()
        result = await worker.capture(str(target), timeout=60)
        if not result.ok:
            records.append({
                "arm": "persistent", "frame": frame, "ok": False, "error": result.error,
            })
            continue
        record = _record(
            target, requested_wall=requested_wall, started=started, frame=frame, arm="persistent"
        )
        record["worker_ms"] = result.elapsed_ms
        record["persistent"] = result.persistent
        records.append(record)
    stats = await worker.stats()
    summary = {
        "spawns": worker.spawns,
        "frames": worker.frames,
        "working_set_bytes": stats.working_set_bytes,
        "cpu_ms": stats.cpu_ms,
        "size": list(worker.size),
        "last_error": worker.last_error,
    }
    process = worker._process  # the benchmark inspects the worker it created
    pid = process.pid if process is not None else None
    await worker.aclose()
    summary["closed"] = not worker.running
    summary["pid"] = pid
    summary["pid_gone"] = not _pid_alive(pid)
    return records, summary


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform.startswith("win"):
        import subprocess

        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=20,
            ).stdout
        except Exception:
            return True
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _stats(records: list[dict[str, object]], *, skip: int = 0) -> dict[str, object]:
    ok = [record for record in records if record.get("ok")]
    latencies = sorted(float(record["latency_ms"]) for record in ok[skip:])
    if not latencies:
        return {"frames_ok": 0, "frames_failed": len(records) - len(ok)}
    return {
        "frames_ok": len(latencies),
        "frames_failed": len(records) - len(ok),
        "min_ms": round(latencies[0], 1),
        "median_ms": round(statistics.median(latencies), 1),
        "mean_ms": round(statistics.fmean(latencies), 1),
        "p95_ms": round(latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))], 1),
        "max_ms": round(latencies[-1], 1),
        "effective_fps": round(1000.0 / statistics.fmean(latencies), 2),
        "stdev_ms": round(statistics.stdev(latencies), 1) if len(latencies) > 1 else 0.0,
        "fresh_all": all(bool(record.get("fresh")) for record in ok),
        "distinct_frames": len({record["sha256"] for record in ok}),
        "bytes": ok[-1]["size_bytes"] if ok else 0,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark screen capture process models.")
    parser.add_argument("--frames", type=int, default=12,
                        help="persistent-arm frames (steady state)")
    parser.add_argument("--baseline-frames", type=int, default=4,
                        help="one-shot-arm frames")
    parser.add_argument("--json", type=str, default="", help="write raw records here")
    args = parser.parse_args()

    if not capture_supported_here():
        print(f"screen capture is not implemented on {sys.platform}; nothing to measure")
        return 0

    with tempfile.TemporaryDirectory(prefix="nc_capture_bench_") as tmp:
        directory = Path(tmp)
        print(f"benchmarking in {directory}")
        print(f"one-shot arm: {args.baseline_frames} frame(s), one PowerShell process each")
        baseline = await _baseline(args.baseline_frames, directory)
        baseline_stats = _stats(baseline)

        print(f"persistent arm: {args.frames} frame(s), one worker process")
        persistent, summary = await _persistent(args.frames, directory)
        # The first frame after `ready` is the one that pays any lazy cost in
        # the worker, so report both: with it (honest) and steady-state.
        persistent_stats = _stats(persistent)
        persistent_steady = _stats(persistent, skip=1)

        print("\n=== one-shot (process per frame) ===")
        print(json.dumps(baseline_stats, indent=2))
        print("\n=== persistent worker (all frames) ===")
        print(json.dumps(persistent_stats, indent=2))
        print("\n=== persistent worker (steady state, first frame excluded) ===")
        print(json.dumps(persistent_steady, indent=2))
        print("\n=== worker resource use / cleanup ===")
        print(json.dumps(summary, indent=2))

        if baseline_stats.get("mean_ms") and persistent_stats.get("mean_ms"):
            speedup = float(baseline_stats["mean_ms"]) / float(persistent_stats["mean_ms"])
            print(f"\nmean-latency speedup (one-shot / persistent): {speedup:.2f}x")
        if not baseline_stats.get("fresh_all", False) and baseline_stats.get("frames_ok"):
            print("WARNING: the one-shot arm produced a frame whose mtime did not advance")
        if persistent_stats.get("frames_ok") and not persistent_stats.get("fresh_all"):
            print("WARNING: the persistent arm produced a frame whose mtime did not advance")

        if args.json:
            Path(args.json).write_text(
                json.dumps(
                    {
                        "baseline": baseline,
                        "baseline_stats": baseline_stats,
                        "persistent": persistent,
                        "persistent_stats": persistent_stats,
                        "persistent_steady_stats": persistent_steady,
                        "worker": summary,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
