"""NF-15 live probe: observe a real game window through the unified pipeline.

Run on Windows against an installed, catalogued game:

    .venv/Scripts/python.exe scripts/novaforge_nf15_observation_probe.py \
        --game "Shadow Fight Arena"

The probe launches the game through NF-12's plan/token/execute path, binds it in
the NF-15 observer with the launch's own pid/start time PINNED, waits for
validated frames, measures the achieved rate and latency, minimizes and restores
the window (unless ``--no-minimize``), stops observation, and closes only the
instance it started (WM_CLOSE, exit confirmed). It sends no gameplay input, kills
no process, and never launches a second instance.

Every fact in the report was measured. A step that cannot run is recorded as
``tested: false`` with the reason, never as a pass. The JSON artifact lands under
``data/novaforge/nf15/`` (runtime data, git-ignored).
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from novacontrol.application import NovaControlApplication  # noqa: E402
from novacontrol.desktop.capture_validation import inspect_frame  # noqa: E402

GAME = "Shadow Fight Arena"


async def wait_for(predicate, *, timeout: float, message: str) -> Any:
    """Poll a synchronous predicate; return its truthy value or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.2)
    return None


def _window_command(handle: int, command: int) -> bool:
    """ShowWindow as a window command, not gameplay input."""
    if handle <= 0:
        return False
    return bool(ctypes.windll.user32.ShowWindow(ctypes.c_void_p(int(handle)), int(command)))


def _frame_body(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"available": False}
    try:
        inspection = inspect_frame(path)
    except Exception as exc:  # noqa: BLE001 - an unreadable frame is a reported fact
        return {"available": True, "path": str(path), "error": f"{type(exc).__name__}: {exc}"}
    try:
        age = round(max(0.0, time.time() - path.stat().st_mtime), 3)
        size = path.stat().st_size
    except OSError:
        age, size = None, 0
    return {
        "available": True,
        "path": str(path),
        "size_bytes": size,
        "age_seconds_at_read": age,
        "inspection": inspection.to_dict(),
    }


def _frames_produced(status: dict[str, Any]) -> int:
    """Frames a backend actually produced, accepted or rejected.

    ``unavailable`` is a recorded read with NO frame (the target was missing or
    minimized); it must not be counted as a capture.
    """
    counts = status.get("counts") or {}
    return int(counts.get("observed") or 0) + int(counts.get("rejected") or 0)


def _history_rows(status: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            key: row.get(key)
            for key in ("seq", "status", "reason_code", "backend", "width", "height",
                        "capture_ms", "fresh", "target_valid", "captured_at")
        }
        for row in status.get("history") or []
    ]


async def observe(
    nova: NovaControlApplication, launch: dict[str, Any], args: argparse.Namespace,
    report: dict[str, Any],
) -> None:
    """Bind the launched window, measure frames, pause/resume, stop."""
    title = str(launch.get("window") or GAME)
    body: dict[str, Any] = {
        "tested": False,
        "title": title,
        "pid": int(launch.get("pid") or 0),
        "start_time": str(launch.get("start_time") or ""),
    }
    report["observation"] = body
    started = await nova.novaforge.start_observation(
        {
            "title": title,
            "game": str(launch.get("name") or GAME),
            "pid": body["pid"],
            "start_time": body["start_time"],
            "interval": args.interval,
        }
    )
    body["start"] = {
        "started": bool(started.get("started")),
        "reason_code": started.get("reason_code", ""),
        "reason": started.get("reason", ""),
        "session_id": started.get("session_id", ""),
        "interval": started.get("interval"),
    }
    if not started.get("started"):
        return
    body["tested"] = True
    status = nova.novaforge.observation_status()
    body["target"] = status.get("target")
    wiring = nova.novaforge.capabilities()
    body["capability"] = wiring.get("observation")
    body["backends"] = (wiring.get("capture") or {}).get("backends")

    wanted = max(1, int(args.frames))
    got = await wait_for(
        lambda: nova.novaforge.observation_status()["counts"]["observed"] >= wanted,
        timeout=args.timeout,
        message="validated frames",
    )
    status = nova.novaforge.observation_status()
    body["frames_requested"] = wanted
    body["frames_observed"] = status["counts"]["observed"]
    body["within_timeout"] = bool(got)
    body["state"] = status["state"]
    body["last_observation"] = status.get("last_observation")
    body["measured"] = status.get("measured")
    body["counts"] = status.get("counts")
    body["interval"] = status.get("interval")
    body["limits"] = status.get("limitations")
    body["frame"] = _frame_body(nova.novaforge.observation_frame_path())
    body["frames"] = status.get("frames")
    body["history"] = _history_rows(status)

    if args.minimize and status["counts"]["observed"]:
        handle = int((status.get("target") or {}).get("handle") or 0)
        before = nova.novaforge.observation_status()
        frames_before = _frames_produced(before)
        observed_before = int(before["counts"]["observed"])
        body["minimize"] = {
            "handle": handle,
            "sent": _window_command(handle, 6),  # SW_MINIMIZE
            "frames_before": frames_before,
        }
        paused = await wait_for(
            lambda: nova.novaforge.observation_status()["state"] == "paused",
            timeout=8.0,
            message="pause",
        )
        paused_status = nova.novaforge.observation_status()
        body["minimize"].update(
            {
                "paused": bool(paused),
                "pause": paused_status.get("pause"),
                "frames_at_pause": _frames_produced(paused_status),
            }
        )
        await asyncio.sleep(max(1.0, float(args.interval) * 2.0))
        during = nova.novaforge.observation_status()
        body["minimize"].update(
            {
                "reads_while_minimized": during["counts"]["attempts"]
                - int(paused_status["counts"]["attempts"]),
                "frames_while_minimized": _frames_produced(during)
                - int(body["minimize"]["frames_at_pause"]),
                "state_while_minimized": during["state"],
                "pause_while_minimized": during.get("pause"),
            }
        )
        restore_started = time.monotonic()
        body["minimize"]["restore_sent"] = _window_command(handle, 9)  # SW_RESTORE
        resumed = await wait_for(
            lambda: nova.novaforge.observation_status()["state"] == "observing"
            and nova.novaforge.observation_status()["counts"]["observed"] > observed_before,
            timeout=args.timeout,
            message="resume",
        )
        resumed_status = nova.novaforge.observation_status()
        body["minimize"].update(
            {
                "resumed": bool(resumed),
                "resume_seconds": round(time.monotonic() - restore_started, 2),
                "state_after_restore": resumed_status["state"],
                "observed_after_restore": resumed_status["counts"]["observed"],
                "frames_after_restore": _frames_produced(resumed_status),
                "pause_after_restore": resumed_status.get("pause"),
            }
        )
        body["history_after_restore"] = _history_rows(resumed_status)

    stopped = await nova.novaforge.stop_observation()
    stopped_status = nova.novaforge.observation_status()
    frames_at_stop = _frames_produced(stopped_status)
    await asyncio.sleep(max(1.0, float(args.interval) * 2.0))
    after = nova.novaforge.observation_status()
    body["stop"] = {
        "stopped": bool(stopped.get("stopped")),
        "already_stopped": bool(stopped.get("already_stopped")),
        "state": after["state"],
        "frames_at_stop": frames_at_stop,
        "frames_after_wait": _frames_produced(after),
        "frames_after_stop": _frames_produced(after) - frames_at_stop,
        "reads_after_stop": after["counts"]["attempts"]
        - int(stopped_status["counts"]["attempts"]),
    }


async def _work(
    nova: NovaControlApplication, args: argparse.Namespace, report: dict[str, Any]
) -> None:
    """Plan, launch, observe. ``report['launch_id']`` is read by the closer."""
    await nova.start()
    library = await nova.game_library()
    report["memory"] = library.get("memory")
    gate = report["memory"] or {}
    if not gate.get("ok"):
        report["launch"] = {
            "tested": False,
            "reason": "The memory gate refused; nothing was launched.",
            "memory": gate,
        }
        return
    report["catalog"] = {
        "available": library.get("available"),
        "total": library.get("total"),
        "counts": library.get("counts"),
    }
    command = f"launch {args.game}"
    plan = await nova.plan_game_launch(command)
    token = str((plan.get("approval") or {}).get("token") or "")
    report["launch"] = {"plan_status": plan.get("status"), "has_token": bool(token)}
    if not token:
        report["launch"]["reason"] = "The plan carried no token; nothing was launched."
        return
    executed = await nova.execute_game_launch(command, approval_token=token)
    launch = (executed.get("game") or {}).get("launch") or {}
    report["launch_id"] = str(launch.get("launch_id") or "")
    report["launch"].update(
        {
            "tested": bool(launch.get("pid")),
            "status": executed.get("status"),
            "stages": executed.get("stages"),
            "pid": launch.get("pid"),
            "start_time": launch.get("start_time"),
            "window": launch.get("window"),
            "name": launch.get("name"),
        }
    )
    if not launch.get("pid") or not launch.get("window"):
        report["errors"].append(
            "The launch produced no verified window; observation was not started."
        )
        return
    await observe(nova, launch, args, report)


async def _identity(nova: NovaControlApplication, report: dict[str, Any]) -> Any:
    pid = (report.get("launch") or {}).get("pid")
    probe = getattr(nova.desktop, "process_identity", None)
    if not pid or probe is None:
        return None
    try:
        return await probe(int(pid))
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


async def run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report: dict[str, Any] = {
        "phase": "NF-15-unified-observation",
        "started_at": datetime.now(UTC).isoformat(),
        "game": args.game,
        "gameplay_claimed": False,
        "input_sent": False,
        "errors": [],
    }
    nova = NovaControlApplication(data_dir=Path("data"))
    try:
        await _work(nova, args, report)
    except Exception as exc:  # noqa: BLE001 - one probe failure is a reported fact
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        # The close happens BEFORE the report is written: a close result nobody
        # recorded is how "we left a game running" goes unnoticed.
        launch_id = str(report.get("launch_id") or "")
        if launch_id:
            try:
                closed = await nova.close_game_launch(launch_id=launch_id)
                report["close"] = {
                    "status": closed.get("status"),
                    "closed": closed.get("closed"),
                    "summary": closed.get("summary"),
                }
                await asyncio.sleep(1.0)
                report["after_close"] = {
                    "observation_state": nova.novaforge.observation_status()["state"],
                    "process": await _identity(nova, report),
                }
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                report["errors"].append(f"close: {type(exc).__name__}: {exc}")
        try:
            await nova.stop()
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"app stop: {type(exc).__name__}: {exc}")
    return _finish(report, out_dir, stamp, nova)


def _finish(
    report: dict[str, Any], out_dir: Path, stamp: str, nova: NovaControlApplication
) -> int:
    path = out_dir / f"{stamp}_observation_report.json"
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["report_path"] = str(path)
    if nova.novaforge is not None:
        report["final_status"] = nova.novaforge.observation_status()
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str)[:12000])
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", default=GAME)
    parser.add_argument("--out-dir", default="data/novaforge/nf15")
    parser.add_argument("--frames", type=int, default=3)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--minimize", action="store_true", default=True)
    parser.add_argument("--no-minimize", dest="minimize", action="store_false")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
