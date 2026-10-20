"""Measure NF-05 evaluation overhead on THIS machine.

    python scripts/benchmark_novaforge_evaluation.py --runs 3 --duration 2

What is measured, not assumed:

1. **Preflight** — a full readiness assessment against the (deterministic)
   double window: every check, the benchmark preflight it composes, and the
   baseline lookup.
2. **Evaluation wall time** — start through terminal status, including the
   underlying NF-04 benchmark run, so the budget overrun is visible.
3. **Read and report cost** — ``get``, ``history``, ``baseline`` and ``report``
   (the report also reads the timeline record and benchmark evidence).
4. **Restart recovery** — a second lab over the same store, reading its
   history, and whether a ``running`` record was recovered as aborted.
5. **Snapshot size** — the persisted evaluation namespace, per run.

The script uses a deterministic 320x200 PNG capture seam, never a game. It
prints what it measured here and invents no capability score.
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
    EVALUATION_VERSION,
    EVALUATIONS_NAMESPACE,
    NovaForgeBenchmarkLab,
    NovaForgeEvaluationLab,
    NovaForgeManager,
)
from novacontrol.persistence import JsonStateStore  # noqa: E402

WINDOW_TITLE = "NF-05 EVAL - deterministic SCENARIO = "


class BenchWindow:
    title = "NF-05 EVAL - deterministic"
    handle = 0xE0A1
    pid = 5511
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
            "capture_mode": "evaluation-benchmark",
        }

    async def release(self) -> tuple[int, int]:
        return 0, 0


def _request(duration: int, interval: float) -> dict[str, Any]:
    return {
        "game_id": "disposable-window",
        "game_version": 1,
        "scenario_id": "observation-baseline",
        "target_title": "NF-05 EVAL",
        "duration_seconds": duration,
        "frame_interval_seconds": interval,
        "notes": "nf05 overhead measurement",
        "confirm": True,
    }


async def measure(runs: int, duration: int, interval: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="nf05-eval-") as tmp:
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
        benchmarks = NovaForgeBenchmarkLab(manager=manager, store=store, poll_interval=0.05)
        lab = NovaForgeEvaluationLab(benchmark=benchmarks, manager=manager, store=store)

        request = _request(duration, interval)
        preflight_started = time.perf_counter()
        preflight = await lab.preflight(request)
        preflight_ms = (time.perf_counter() - preflight_started) * 1000.0

        records: list[dict[str, Any]] = []
        for index in range(runs):
            started = time.perf_counter()
            record = await lab.start(request)
            while True:
                current = lab.get(record["id"])
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
                        max(0.0, (current.get("duration_seconds") or 0.0) - duration) * 1000.0,
                        2,
                    ),
                    "frames": ((current.get("metrics") or {}).get("frames") or {}).get("value"),
                    "baseline_role": (current.get("baseline") or {}).get("role"),
                    "comparison_compatible": (current.get("comparison") or {}).get("compatible"),
                    "objective_verified": (current.get("objective") or {}).get("verified"),
                }
            )

        last_id = str(records and lab.history(limit=1)["evaluations"][0]["id"])

        get_started = time.perf_counter()
        lab.get(last_id)
        get_ms = (time.perf_counter() - get_started) * 1000.0

        history_started = time.perf_counter()
        history = lab.history(limit=20)
        history_ms = (time.perf_counter() - history_started) * 1000.0

        baseline_started = time.perf_counter()
        baseline = lab.baseline("disposable-window", "observation-baseline", 1)
        baseline_ms = (time.perf_counter() - baseline_started) * 1000.0

        report_started = time.perf_counter()
        report = lab.report(last_id)
        report_ms = (time.perf_counter() - report_started) * 1000.0

        snapshot = store.read(EVALUATIONS_NAMESPACE)
        snapshot_bytes = len(json.dumps(snapshot, default=str).encode("utf-8"))

        restart_lab = NovaForgeEvaluationLab(
            benchmark=benchmarks, manager=manager, store=store
        )
        restart_started = time.perf_counter()
        restored = restart_lab.history(limit=20)
        restart_ms = (time.perf_counter() - restart_started) * 1000.0
        recovered_running = [
            row["id"] for row in restored["evaluations"] if row["status"] == "running"
        ]

        return {
            "version": EVALUATION_VERSION,
            "platform_note": "deterministic 320x200 PNG capture seam; not a real game",
            "config": {"runs": runs, "duration_seconds": duration, "interval_seconds": interval},
            "preflight": {
                "ms": round(preflight_ms, 3),
                "ready": preflight["ready"],
                "checks": len(preflight["checks"]),
                "counts": preflight["counts"],
                "blocking": preflight["blocking"],
            },
            "evaluations": records,
            "wall_ms": {
                "min": round(min(item["wall_ms"] for item in records), 2),
                "median": round(statistics.median(item["wall_ms"] for item in records), 2),
                "max": round(max(item["wall_ms"] for item in records), 2),
            },
            "read_path_ms": {
                "get": round(get_ms, 3),
                "history": round(history_ms, 3),
                "baseline": round(baseline_ms, 3),
                "report": round(report_ms, 3),
                "restart_history": round(restart_ms, 3),
            },
            "snapshot_bytes": snapshot_bytes,
            "history": {
                "count": history["count"],
                "total": history["total"],
                "cap": history["retention"]["cap"],
            },
            "baseline_available": baseline["available"],
            "baseline_samples": baseline.get("samples"),
            "report": {
                "status": report["status"],
                "valid": report["valid"],
                "objective_verified": report["objective_verified"],
                "real_gameplay_control_exercised": report["real_gameplay_control_exercised"],
                "missing_evidence": len(report["missing_evidence"]),
                "limitations": len(report["limitations"]),
            },
            "restart_evaluations": restored["count"],
            "recovered_running": recovered_running,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3, help="evaluations to execute")
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
