"""NF-24 Objective H/I: state understanding from a REAL game window, if one can exist.

The launch goes through the application's own ``GameCatalog``/``GameLaunchService``
composition (reusing the NF-23 probe's seams), the observation and state services
are the manager's OWN wired instances (``manager.state`` → ``manager.observer``),
and the objectives/knowledge services are fed by ``manager._consume_state`` exactly
as in production.

Safety: no gameplay input, no display-mode change, no process kill. The only
window touched is the game this probe launched, and it is closed gracefully
through the launch service (WM_CLOSE) at the end.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / ".nc23_tmp"))

from live_game_probe import build_games, say  # noqa: E402
from novacontrol.desktop.capture import PersistentScreenCapture  # noqa: E402
from novacontrol.desktop.controller import LocalDesktopRunner  # noqa: E402
from novacontrol.novaforge.manager import NovaForgeManager  # noqa: E402

GAME = "Freedom Fighters"
READINGS = 4


async def wait_for_frames(manager: NovaForgeManager, target: int, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = manager.observation_status()
        if status["counts"]["observed"] >= target or status["state"] in ("stopped", "failed"):
            return status
        await asyncio.sleep(0.4)
    return manager.observation_status()


async def main() -> int:  # noqa: C901 - a scripted probe, linear by design
    runner = LocalDesktopRunner()
    memory = await runner.memory_snapshot()
    say(
        memory_before=memory,
        gate_free_gb=round(float(memory.get("free_gb") or 0.0), 2),
        gate_used_pct=round(float(memory.get("used_percent") or 0.0), 1),
    )
    if float(memory.get("free_gb") or 0.0) < 2.0 or float(memory.get("used_percent") or 100) > 92:
        say(blocked="the resource gate is closed; no game was launched")
        return 2

    games = build_games(runner)
    if games is None:
        say(blocked="the launch service could not be built from this runner")
        return 2

    resolution = await games.catalog.resolve(GAME)
    say(
        discovered_status=resolution.get("status"),
        discovered_artifact=(resolution.get("entry") or {}).get("artifact"),
        already_running=(resolution.get("entry") or {}).get("running"),
    )
    if str(resolution.get("status")) == "already_running":
        say(blocked="the game is already running; this probe must not adopt a user's instance")
        return 2

    started = time.perf_counter()
    launch = await games.launch(GAME)
    say(
        launch_status=launch.get("status"),
        launch_reason=str(launch.get("reason") or "")[:300],
        launch_seconds=round(time.perf_counter() - started, 2),
    )
    processes = await runner.game_processes(["Freedom", "Launcher"])
    say(processes_after_launch=processes)
    owned = games.owned
    if owned is None:
        say(blocked="the launch produced no owned record: no game window exists to observe")
        return 3
    record = owned.to_dict() if hasattr(owned, "to_dict") else dict(owned)
    say(owned_record=record)

    pid = int(record.get("pid") or 0)
    window_title = str(record.get("window") or record.get("title") or GAME)
    worker = PersistentScreenCapture()
    manager: NovaForgeManager | None = None
    readings: list[dict[str, Any]] = []
    try:
        manager = NovaForgeManager(
            data_dir=Path(tempfile.mkdtemp(prefix="nf24-live-")),
            capture=lambda path: runner.capture_screen(path),
            geometry=lambda title: worker.window_geometry(title=title),
            window_lines=lambda: runner.window_lines(),
            capture_window=lambda h, path: runner.capture_window(h, path, thumbnail=True),
            process_identity=runner.process_identity,
            game_launcher=games,
            observation_interval=1.0,
        )
        geometry = await worker.window_geometry(title=window_title)
        if geometry is None:
            say(blocked=f"no window matched {window_title!r} after the launch")
            return 3
        say(
            window_identity={
                "handle": int(getattr(geometry, "handle", 0)),
                "pid": int(getattr(geometry, "pid", pid)),
                "title": str(getattr(geometry, "title", "")),
                "bounds": list(getattr(geometry, "bounds")),
                "foreground": bool(getattr(geometry, "foreground", False)),
                "minimized": bool(getattr(geometry, "minimized", False)),
            },
        )
        observation = await manager.start_observation(
            {
                "title": str(getattr(geometry, "title", window_title)),
                "pid": pid,
                "start_time": str(record.get("start_time") or ""),
                "interval": 1.0,
            }
        )
        say(
            bind_started=bool(observation.get("started")),
            bind_state=observation.get("state"),
            bind_reason_code=observation.get("reason_code"),
            bind_reason=str(observation.get("reason") or "")[:200],
        )

        status = await wait_for_frames(manager, 3, 45.0)
        history = [row for row in status["history"] if row.get("status") == "observed"]
        capture_ms = [float(row.get("capture_ms") or 0.0) for row in history]
        say(
            observed_state=status["state"],
            observed_counts=status["counts"],
            observed_frames=len(history),
            observed_dims=sorted({(row.get("width"), row.get("height")) for row in history}),
            observed_backend=status.get("backend"),
            observed_capture_ms={
                "min": round(min(capture_ms), 1) if capture_ms else None,
                "median": round(statistics.median(capture_ms), 1) if capture_ms else None,
                "max": round(max(capture_ms), 1) if capture_ms else None,
            },
        )

        # The product's own reading path: state → objectives → knowledge.
        for index in range(READINGS):
            observed_before = manager.observation_status()["counts"]["observed"]
            await wait_for_frames(manager, observed_before + 1, 20.0)
            started_at = time.monotonic()
            outcome = await manager.state.analyze()
            wall_ms = round((time.monotonic() - started_at) * 1000.0, 1)
            row: dict[str, Any] = {
                "reading": index + 1,
                "analyzed": outcome.analyzed,
                "refusal": outcome.reason_code,
                "wall_ms": wall_ms,
            }
            if outcome.state is not None:
                state = outcome.state
                row.update(
                    {
                        "observation": state.observation.observation_id,
                        "status": state.status.value,
                        "game": state.game,
                        "profile": (
                            state.profile.profile.profile_id if state.profile.profile else ""
                        ),
                        "scene": state.scene.to_dict(),
                        "transition": state.transition.to_dict(),
                        "hud": [
                            {
                                "element": item.element_id,
                                "status": item.status.value,
                                "text": item.text,
                            }
                            for item in state.hud
                        ],
                        "confidence": state.confidence,
                        "processing_ms": state.latency.get("processing_ms"),
                        "entities": len(state.entities),
                        "text_lines": [str(line.get("text")) for line in state.text][:6],
                    }
                )
                objectives = manager.objective_state.current
                row["objectives_status"] = (
                    [item.status.value for item in objectives.objectives]
                    if objectives is not None
                    else []
                )
                row["objectives_unattributed"] = (
                    list(objectives.diagnostics.unattributed)
                    if objectives is not None
                    else []
                )
                row["knowledge"] = manager.knowledge.status()["counts"]
            readings.append(row)
            say(**row)
    finally:
        if manager is not None:
            try:
                await manager.stop_observation("probe complete")
            except Exception:  # noqa: BLE001 - a stop failure is reported below
                pass
        close = await games.close()
        say(close_result=close)
        for _ in range(60):
            alive = await runner.process_identity(pid)
            if alive is None:
                break
            await asyncio.sleep(1.0)
        still_alive = await runner.process_identity(pid)
        say(process_exited=still_alive is None, process_identity_after=still_alive)
        for closable in (worker, runner):
            closer = getattr(closable, "aclose", None)
            if callable(closer):
                await closer()

    say(
        summary={
            "readings": len([row for row in readings if row.get("analyzed")]),
            "scenes": [row.get("scene", {}).get("kind") for row in readings],
            "confirmed": [row.get("transition", {}).get("confirmed") for row in readings],
            "transitions": [row.get("transition", {}).get("state") for row in readings],
            "processing_ms": [row.get("processing_ms") for row in readings],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
