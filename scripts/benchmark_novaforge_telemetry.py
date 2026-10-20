"""Measure the NF-02 observation-loop overhead on THIS machine.

    python scripts/benchmark_novaforge_telemetry.py --frames 40

Three figures, each measured rather than assumed:

1. **Telemetry aggregation cost** — ``TelemetrySnapshot.record_frame`` plus a
   ``describe`` call per frame, timed in isolation over many iterations. This is
   the work NF-02 added to the capture path.
2. **Persistence policy** — the same session loop with:
   * ``--legacy-persist``: a full snapshot write after every frame (the NF-01
     behavior), and
   * the default throttled write (lifecycle moves write at once; ticks write at
     most once per ``persist_interval``).
   The difference is the loop cost the throttling removes.
3. **Session telemetry** — the figures the session itself reports after the run
   (frames, measured capture latency, gaps), so the benchmark and the product
   agree about what happened.

The capture seam is a deterministic PNG writer, so the two persistence arms run
the identical amount of capture work. The script never claims a target frame
rate; it prints what it measured, here, in this run. ``--json`` writes the raw
records.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novacontrol.novaforge import NovaForgeManager  # noqa: E402
from novacontrol.novaforge.models import FrameRecord, MissionConfig, SessionRecord  # noqa: E402
from novacontrol.novaforge.telemetry import TelemetrySnapshot  # noqa: E402

from PIL import Image  # noqa: E402

WINDOW_TITLE = "NF-02 BENCH - deterministic"


class BenchWindow:
    title = WINDOW_TITLE
    handle = 0xBEEF
    pid = 5150
    bounds = (0, 0, 320, 200)
    client = (0, 0, 320, 200)
    scale = 1.0
    foreground = False


class BenchDesktop:
    """Writes a real 320x200 PNG per capture, exactly like the test double."""

    def __init__(self) -> None:
        self.window: BenchWindow | None = BenchWindow()
        self.release_calls = 0

    async def window_lines(self) -> list[str]:
        return [f"python|{WINDOW_TITLE}"]

    async def geometry(self, _title: str) -> BenchWindow | None:
        return self.window

    async def capture(self, path: str) -> dict[str, Any]:
        image = Image.new("RGB", (320, 200), (12, 18, 30))
        image.save(path, format="PNG")
        return {
            "fresh": True,
            "verification": "frame written now",
            "captured_at": datetime.now(UTC).isoformat(),
            "capture_ms": 0.0,
            "capture_mode": "benchmark",
        }

    async def release(self) -> tuple[int, int]:
        self.release_calls += 1
        return (0, 0)


def mission() -> dict[str, Any]:
    return {
        "name": "NF-02 telemetry benchmark",
        "objective": "Measure the observation loop with a deterministic capture.",
        "target_title": "NF-02 BENCH",
        "success_criteria": "",
    }


async def run_session(*, frames: int, persist_interval: float, data_dir: Path) -> dict[str, Any]:
    desktop = BenchDesktop()
    manager = NovaForgeManager(
        data_dir=data_dir,
        capture=desktop.capture,
        geometry=desktop.geometry,
        window_lines=desktop.window_lines,
        release_inputs=desktop.release,
        capture_supported=True,
        frame_interval=0.001,
        persist_interval=persist_interval,
    )
    started = time.perf_counter()
    session = await manager.start(mission())
    session_id = session["session_id"]
    # The record keeps only its newest frames, so the COUNT comes from the
    # telemetry snapshot, which is not bounded by the frame list.
    while manager.get(session_id).telemetry.frames < frames:
        await asyncio.sleep(0.005)
        if time.perf_counter() - started > 60:  # a hang is a failure, not a result
            raise RuntimeError("the benchmark session did not reach its frame count in 60s")
    stopped = await manager.stop(session_id)
    elapsed = time.perf_counter() - started
    telemetry = stopped["telemetry"]
    return {
        "persist_interval_seconds": persist_interval,
        "frames": telemetry["capture"]["frames"],
        "wall_seconds": round(elapsed, 3),
        "frames_per_second": round(telemetry["capture"]["frames"] / elapsed, 3),
        "latency_ms": telemetry["capture"]["latency_ms"],
        "release_calls": desktop.release_calls,
    }


def measure_snapshot(iterations: int = 200) -> dict[str, Any]:
    """The cost a throttle skips: serialize a full session snapshot."""
    record = SessionRecord(mission=MissionConfig("bench", "o", WINDOW_TITLE))
    for seq in range(1, 31):
        record.record_frame(
            FrameRecord(
                seq=seq, captured_at=datetime.now(UTC).isoformat(), path=f"x/{seq}.png",
                width=320, height=200, size_bytes=4096, capture_ms=2.0,
                capture_mode="benchmark", fresh=True, source="window_crop",
            )
        )
    # Time BOTH halves: the manager serializes the record with ``to_dict()``
    # before the store writes it, so timing only ``json.dumps`` would report a
    # cost the loop does not actually pay.
    started = time.perf_counter()
    for _ in range(iterations):
        json.dumps(record.to_dict())
    elapsed = time.perf_counter() - started
    return {
        "iterations": iterations,
        "milliseconds_per_snapshot": round((elapsed / iterations) * 1000, 4),
        "note": "SessionRecord.to_dict() + json.dumps with 30 retained frames",
    }


def measure_aggregation(iterations: int) -> dict[str, Any]:
    """record_frame + describe per iteration, timed without the event loop."""
    snapshot = TelemetrySnapshot()
    snapshot.begin(0.0)
    started = time.perf_counter()
    for index in range(iterations):
        snapshot.record_frame(float(index % 50), "", float(index))
        if index % 10 == 0:
            snapshot.describe(
                state="RUNNING", duration_seconds=1.0, state_changes=1, end_reason="", now=float(index)
            )
    elapsed = time.perf_counter() - started
    per_call_us = (elapsed / iterations) * 1_000_000
    return {
        "iterations": iterations,
        "total_seconds": round(elapsed, 4),
        "per_iteration_microseconds": round(per_call_us, 3),
        "note": "one record_frame per iteration; a full describe every 10th",
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=40)
    parser.add_argument("--iterations", type=int, default=20000)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--legacy-persist", action="store_true", help="write a snapshot after every frame (NF-01 behavior)")
    args = parser.parse_args()

    aggregation = measure_aggregation(args.iterations)
    snapshot_cost = measure_snapshot()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        arms = [
            {
                "arm": "throttled_persist",
                **await run_session(frames=args.frames, persist_interval=2.0, data_dir=root / "throttled"),
            }
        ]
        if args.legacy_persist:
            arms.append(
                {
                    "arm": "write_every_frame",
                    **await run_session(frames=args.frames, persist_interval=0.0, data_dir=root / "legacy"),
                }
            )

    payload = {
        "measured_at": datetime.now(UTC).isoformat(),
        "frames_per_arm": args.frames,
        "aggregation": aggregation,
        "snapshot_serialization": snapshot_cost,
        "runs": arms,
    }
    if args.json:
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"telemetry aggregation: {aggregation['per_iteration_microseconds']} us/iteration")
    print(f"snapshot serialization: {snapshot_cost['milliseconds_per_snapshot']} ms/snapshot")
    for run in arms:
        latency = run["latency_ms"]
        print(
            f"{run['arm']}: frames={run['frames']} wall={run['wall_seconds']}s "
            f"fps={run['frames_per_second']} latency_median={latency['median_ms']}ms "
            f"latency_p95={latency['p95_ms']}ms release_calls={run['release_calls']}"
        )
    if len(arms) == 2:
        legacy, throttled = arms[1], arms[0]
        saved = legacy["wall_seconds"] - throttled["wall_seconds"]
        print(f"persistence policy difference over {args.frames} frames: {saved:.3f}s")
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
