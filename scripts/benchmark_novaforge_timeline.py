"""Measure NF-03 timeline persistence and replay retrieval on THIS machine.

    python scripts/benchmark_novaforge_timeline.py --events 240 --frames 20

Four figures, each measured rather than assumed:

1. **Append cost** — ``TimelineLog.append`` with a realistic payload and
   evidence references, timed over the full retention cap. This is the work
   NF-03 adds to the session loop for every recorded event.
2. **Snapshot persistence** — one full ``JsonStateStore`` write and one read +
   ``TimelineLog.from_dict`` for logs of growing size, with the bytes each
   snapshot occupies. A payload that grows with the session is exactly what a
   bounded timeline must not do, so the growth is printed instead of hidden.
3. **Replay retrieval** — ``timeline_payload`` (unfiltered, filtered, paged),
   ``timeline_event`` and ``compare_sessions`` against a REAL session recorded
   by the manager, including a restart read from the persisted store.
4. **Session overhead** — the wall time of a recorded session and the size of
   its timeline page, so the cost is reported next to the evidence it buys.

The capture seam is a deterministic PNG writer, so the session work is the same
kind the application performs. The script never claims a target frame rate; it
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

from novacontrol.novaforge import NovaForgeManager  # noqa: E402
from novacontrol.novaforge.timeline import (  # noqa: E402
    MAX_EVENTS_PER_SESSION,
    TimelineLog,
    classify,
    compare,
    filter_events,
    frame_ref,
    observation_ref,
    paginate,
    telemetry_ref,
    timeline_namespace,
)
from novacontrol.persistence import JsonStateStore  # noqa: E402

WINDOW_TITLE = "NF-03 BENCH - deterministic"
SESSION_ID = "0123456789abcdef0123456789abcdef"


class BenchWindow:
    title = WINDOW_TITLE
    handle = 0xBEEF
    pid = 6103
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


def _mission() -> dict[str, Any]:
    return {
        "name": "NF-03 benchmark",
        "objective": "Record frames for timeline measurement.",
        "target_title": WINDOW_TITLE,
        "success_criteria": "At least one frame is captured.",
    }


def _filled_log(count: int) -> TimelineLog:
    log = TimelineLog(session_id=SESSION_ID, started_monotonic=time.monotonic())
    for index in range(count):
        log.append(
            "frame.captured",
            source="capture",
            payload={
                "frame_seq": index + 1,
                "width": 320,
                "height": 200,
                "size_bytes": 1024,
                "capture_ms": 7.5,
                "capture_mode": "benchmark",
                "source": "window_crop",
                "note": "",
                "observation_index": index + 1,
            },
            evidence=[
                frame_ref(SESSION_ID, index + 1),
                observation_ref(SESSION_ID, index + 1),
                telemetry_ref(SESSION_ID),
            ],
        )
    return log


def measure_append(events: int) -> dict[str, Any]:
    log = TimelineLog(session_id=SESSION_ID, started_monotonic=time.monotonic())
    started = time.perf_counter()
    for index in range(events):
        log.append(
            "frame.captured",
            source="capture",
            payload={
                "frame_seq": index + 1,
                "width": 320,
                "height": 200,
                "size_bytes": 1024,
                "capture_ms": 7.5,
                "capture_mode": "benchmark",
                "source": "window_crop",
                "observation_index": index + 1,
            },
            evidence=[frame_ref(SESSION_ID, index + 1), telemetry_ref(SESSION_ID)],
        )
    elapsed = time.perf_counter() - started
    return {
        "events": events,
        "retention_cap": MAX_EVENTS_PER_SESSION,
        "total_seconds": round(elapsed, 4),
        "microseconds_per_event": round(elapsed / max(1, events) * 1_000_000, 3),
        "dropped": log.dropped,
    }


def measure_persistence(sizes: list[int]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        store = JsonStateStore(Path(tmp))
        for size in sizes:
            log = _filled_log(size)
            namespace = timeline_namespace(SESSION_ID)
            assert namespace is not None
            payload = log.to_dict()
            write_samples: list[float] = []
            read_samples: list[float] = []
            for _ in range(10):
                started = time.perf_counter()
                store.write(namespace, payload)
                write_samples.append((time.perf_counter() - started) * 1000)
                started = time.perf_counter()
                stored = store.read(namespace)
                TimelineLog.from_dict(stored, session_id=SESSION_ID)
                read_samples.append((time.perf_counter() - started) * 1000)
            results.append(
                {
                    "events": size,
                    "snapshot_bytes": len(json.dumps(payload, separators=(",", ":"))),
                    "write_ms_median": round(statistics.median(write_samples), 3),
                    "write_ms_max": round(max(write_samples), 3),
                    "read_restore_ms_median": round(statistics.median(read_samples), 3),
                }
            )
    return results


def measure_retrieval(count: int, iterations: int) -> dict[str, Any]:
    log = _filled_log(count)
    events = log.events
    assert events
    record = _SyntheticRecord(events)
    classify_samples: list[float] = []
    page_samples: list[float] = []
    filter_samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        classify(record, events)  # type: ignore[arg-type]
        classify_samples.append((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        page, _info = paginate(events, after_seq=0, limit=100)
        page_samples.append((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        filter_events(events, categories=["capture"], sources=["capture"], severities=["info"])
        filter_samples.append((time.perf_counter() - started) * 1000)
        assert page
    return {
        "events": count,
        "iterations": iterations,
        "classify_ms_median": round(statistics.median(classify_samples), 3),
        "page_100_ms_median": round(statistics.median(page_samples), 3),
        "filter_ms_median": round(statistics.median(filter_samples), 3),
        "derived_events_supported": False,
    }


class _SyntheticRecord:
    """The two fields ``classify`` reads beyond the events themselves."""

    def __init__(self, events: list[Any]) -> None:
        self.session_id = SESSION_ID
        self.end_reason = "user"
        self.state = _State()
        self.observations: list[Any] = []
        self.frames: list[Any] = []


class _State:
    value = "STOPPED"


async def run_session(frames: int, data_dir: Path) -> dict[str, Any]:
    desktop = BenchDesktop()
    manager = NovaForgeManager(
        data_dir=data_dir,
        capture=desktop.capture,
        geometry=desktop.geometry,
        window_lines=desktop.window_lines,
        release_inputs=desktop.release,
        capture_supported=True,
        frame_interval=0.05,
        pause_timeout=2.0,
        stop_timeout=4.0,
    )
    started = time.perf_counter()
    session = await manager.start(_mission())
    session_id = session["session_id"]
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and len(manager.get(session_id).frames) < frames:
        await asyncio.sleep(0.01)
    stopped = await manager.stop(session_id)
    wall = time.perf_counter() - started
    captured = len(manager.get(session_id).frames)

    page_samples: list[float] = []
    filtered_samples: list[float] = []
    event_samples: list[float] = []
    for _ in range(20):
        began = time.perf_counter()
        payload = manager.timeline_payload(session_id, limit=100)
        page_samples.append((time.perf_counter() - began) * 1000)

        began = time.perf_counter()
        manager.timeline_payload(session_id, limit=100, categories=["capture"])
        filtered_samples.append((time.perf_counter() - began) * 1000)

        began = time.perf_counter()
        manager.timeline_event(session_id, payload["events"][0]["seq"])
        event_samples.append((time.perf_counter() - began) * 1000)

    began = time.perf_counter()
    compared = manager.compare_sessions(session_id, session_id)
    compare_ms = (time.perf_counter() - began) * 1000

    restarted = NovaForgeManager(
        data_dir=data_dir,
        capture=desktop.capture,
        geometry=desktop.geometry,
        window_lines=desktop.window_lines,
        release_inputs=desktop.release,
        capture_supported=True,
        frame_interval=0.05,
    )
    began = time.perf_counter()
    restored = restarted.timeline_payload(session_id, limit=100)
    restore_ms = (time.perf_counter() - began) * 1000

    fresh = manager.timeline_payload(session_id, limit=100)
    return {
        "frames_requested": frames,
        "frames_captured": captured,
        "wall_seconds": round(wall, 3),
        "frames_per_second": round(captured / wall, 2) if wall else None,
        "state": stopped["state"],
        "events": fresh["page"]["total"],
        "timeline_source": fresh["source"],
        "page_json_bytes": len(json.dumps(fresh, separators=(",", ":"))),
        "page_ms_median": round(statistics.median(page_samples), 3),
        "filtered_ms_median": round(statistics.median(filtered_samples), 3),
        "single_event_ms_median": round(statistics.median(event_samples), 3),
        "compare_ms": round(compare_ms, 3),
        "compare_rows": len(compared["rows"]),
        "restart_read_ms": round(restore_ms, 3),
        "restart_events": restored["page"]["total"],
        "persistence": fresh["persistence"]["state"],
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=MAX_EVENTS_PER_SESSION)
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--frames", type=int, default=20)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    sizes = [size for size in (1, 60, 120, args.events) if size <= args.events]
    append_cost = measure_append(args.events)
    persistence = measure_persistence(sizes)
    retrieval = measure_retrieval(args.events, args.iterations)
    with tempfile.TemporaryDirectory() as tmp:
        session = await run_session(args.frames, Path(tmp) / "session")

    payload = {
        "measured_at": datetime.now(UTC).isoformat(),
        "append": append_cost,
        "persistence": persistence,
        "retrieval": retrieval,
        "session": session,
        "note": (
            "Measured on this machine in this run. The session wall time includes "
            "start validation, capture, stop confirmation and input cleanup, not "
            "timeline work alone."
        ),
    }
    if args.json:
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(
        f"append: {append_cost['microseconds_per_event']} us/event over "
        f"{append_cost['events']} events ({append_cost['total_seconds']}s total)"
    )
    for row in persistence:
        print(
            f"persist {row['events']:>3} events: {row['snapshot_bytes']:>6} B, "
            f"write {row['write_ms_median']} ms (max {row['write_ms_max']}), "
            f"read+restore {row['read_restore_ms_median']} ms"
        )
    print(
        f"retrieval {retrieval['events']} events: classify {retrieval['classify_ms_median']} ms, "
        f"page(100) {retrieval['page_100_ms_median']} ms, filter {retrieval['filter_ms_median']} ms"
    )
    print(
        f"session: {session['frames_captured']} frames / {session['events']} events in "
        f"{session['wall_seconds']}s; timeline page {session['page_ms_median']} ms "
        f"({session['page_json_bytes']} B); filtered {session['filtered_ms_median']} ms; "
        f"single event {session['single_event_ms_median']} ms; compare {session['compare_ms']} ms; "
        f"restart read {session['restart_read_ms']} ms; persistence {session['persistence']}"
    )
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
