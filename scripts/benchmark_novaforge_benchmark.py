"""Measure NF-04 benchmark-runner overhead and read-path cost on THIS machine.

    python scripts/benchmark_novaforge_benchmark.py --runs 3 --duration 2

Five figures, each measured rather than assumed:

1. **Runner wall time** — three real benchmark runs through the real session
   manager with a deterministic PNG capture seam, reporting configured budget,
   observed run duration, poll count and the runner's own post-session
   collection time. The runner sleeps between polls; "overhead" here is the
   time it adds AFTER the session ends, not the budget itself.
2. **Session overhead** — session wall time minus budget, so a budget the
   capture loop overruns is visible instead of assumed to be exact.
3. **Finalize cost** — the runner's collection step (telemetry + timeline +
   metric computation) measured separately from the session.
4. **Read path** — ``history``, ``aggregate`` and ``compare`` latency against
   the persisted runs, plus the size of the persisted snapshot.
5. **Restart recovery** — constructing a second lab over the same store and
   reading its history, including the recovery classification of any run left
   ``running`` (none is expected after a clean finish).

The script never claims a game was evaluated and never invents a score: it
prints what it measured, here, in this run. ``--json`` writes the raw records.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image  # noqa: E402

from novacontrol.novaforge import (  # noqa: E402
    BENCHMARK_VERSION,
    NovaForgeBenchmarkLab,
    NovaForgeManager,
    RUNS_NAMESPACE,
)
from novacontrol.persistence import JsonStateStore  # noqa: E402

WINDOW_TITLE = "NF-04 BENCH - deterministic SCENARIO = "


class BenchWindow:
    title = "NF-04 BENCH - deterministic"
    handle = 0xBEEF
    pid = 7704
    bounds = (0, 0, 320, 200)
    client = (0, 0, 320, 200)
    scale = 1.0
    foreground = False


class BenchDesktop:
    """Writes a real 320x200 PNG per capture, exactly like the test double."""

    def __init__(self) -> None:
        self.window: BenchWindow | None = BenchWindow()
        self.capture_count = 0

    async def window_lines(self) -> list[str]:
        return [f"python|{WINDOW_TITLE}"]

    async def geometry(self, _title: str) -> BenchWindow | None:
        return self.window

    async def capture(self, path: str) -> dict[str, Any]:
        self.capture_count += 1
        image = Image.new("RGB", (320, 200), (12, 18, 30))
        image.save(path, format="PNG")
        return {
            "fresh": True,
            "verification": "frame written now",
            "captured_at": datetime.now(UTC).isoformat(),
            "capture_ms": 1.5,
            "capture_mode": "benchmark",
        }

    async def release(self) -> tuple[int, int]:
        return 0, 0


async def measure(runs: int, duration: int, interval: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="nf04-bench-") as tmp:
        root = Path(tmp)
        desktop = BenchDesktop()
        manager = NovaForgeManager(
            data_dir=root,
            capture=desktop.capture,
            geometry=desktop.geometry,
            window_lines=desktop.window_lines,
            release_inputs=desktop.release,
            frame_interval=interval,
            pause_timeout=2.0,
            stop_timeout=4.0,
        )
        store = JsonStateStore(root)
        lab = NovaForgeBenchmarkLab(
            manager=manager,
            store=store,
            poll_interval=0.05,
        )
        request = {
            "scenario_id": "observation-baseline",
            "target_title": "NF-04 BENCH",
            "duration_seconds": duration,
            "frame_interval_seconds": interval,
        }

        records: list[dict[str, Any]] = []
        for index in range(runs):
            started = time.perf_counter()
            run = await lab.start(request)
            while True:
                current = lab.get(run["id"])
                if current and current["status"] != "running":
                    break
                await asyncio.sleep(0.02)
            wall_ms = (time.perf_counter() - started) * 1000.0
            records.append(
                {
                    "run": index + 1,
                    "status": current["status"],
                    "valid": bool(current["valid"]),
                    "wall_ms": round(wall_ms, 2),
                    "budget_seconds": duration,
                    "observed_duration_seconds": current.get("duration_seconds"),
                    "session_overrun_ms": round(
                        max(0.0, (current.get("duration_seconds") or 0.0) - duration) * 1000.0, 2
                    ),
                    "polls": (current.get("runner") or {}).get("polls"),
                    "collection_ms": (current.get("runner") or {}).get("collection_ms"),
                    "finalize_ms": (current.get("runner") or {}).get("finalize_ms"),
                    "frames": ((current.get("metrics") or {}).get("frames") or {}).get("value"),
                }
            )

        history_started = time.perf_counter()
        history = lab.history(limit=20)
        history_ms = (time.perf_counter() - history_started) * 1000.0

        aggregate_started = time.perf_counter()
        aggregate = lab.aggregate("observation-baseline")
        aggregate_ms = (time.perf_counter() - aggregate_started) * 1000.0

        compare_ms = None
        if len(history["runs"]) >= 2:
            left, right = history["runs"][0]["id"], history["runs"][1]["id"]
            compare_started = time.perf_counter()
            comparison = lab.compare([left], [right])
            compare_ms = (time.perf_counter() - compare_started) * 1000.0
        else:  # pragma: no cover - runs is at least 1 and 2 by default
            comparison = None

        snapshot = store.read(RUNS_NAMESPACE)
        snapshot_bytes = len(json.dumps(snapshot, default=str).encode("utf-8"))

        restart_lab = NovaForgeBenchmarkLab(manager=manager, store=store, poll_interval=0.05)
        restart_started = time.perf_counter()
        restored = restart_lab.history(limit=20)
        restart_ms = (time.perf_counter() - restart_started) * 1000.0

        return {
            "version": BENCHMARK_VERSION,
            "platform_note": "deterministic 320x200 PNG capture seam; not a real game",
            "config": {"runs": runs, "duration_seconds": duration, "interval_seconds": interval},
            "runs": records,
            "runner_wall_ms": {
                "min": round(min(item["wall_ms"] for item in records), 2),
                "median": round(statistics.median(item["wall_ms"] for item in records), 2),
                "max": round(max(item["wall_ms"] for item in records), 2),
            },
            "collection_ms": {
                "min": min(item["collection_ms"] or 0.0 for item in records),
                "median": round(
                    statistics.median(item["collection_ms"] or 0.0 for item in records), 2
                ),
                "max": max(item["collection_ms"] or 0.0 for item in records),
            },
            "read_path_ms": {
                "history": round(history_ms, 3),
                "aggregate": round(aggregate_ms, 3),
                "compare": None if compare_ms is None else round(compare_ms, 3),
                "restart_history": round(restart_ms, 3),
            },
            "snapshot_bytes": snapshot_bytes,
            "aggregate": {
                "attempts": aggregate["attempts"],
                "eligible": aggregate["eligible"],
                "sample_size": aggregate["sample_size"],
                "status_counts": aggregate["status_counts"],
                "frames_median": (aggregate["metrics"].get("frames") or {}).get("value"),
            },
            "comparison_compatible": None if comparison is None else comparison["compatible"],
            "restart_runs": restored["count"],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3, help="benchmark runs to execute")
    parser.add_argument("--duration", type=int, default=2, help="budget per run, seconds")
    parser.add_argument("--interval", type=float, default=0.1, help="observation interval, seconds")
    parser.add_argument("--json", type=Path, default=None, help="write the raw records here")
    args = parser.parse_args()

    result = asyncio.run(measure(max(1, args.runs), max(1, args.duration), args.interval))
    print(json.dumps(result, indent=2, default=str))
    if args.json is not None:
        args.json.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
