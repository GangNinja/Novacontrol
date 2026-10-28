"""NF-23 Objective H (fallback start): the game's own executable, observed.

The Steam path (`steam://rungameid/1347780`) runs the game's `Launcher.exe`,
which exits ~2 s later without ever starting `Freedom.Exe` (evidence:
`.nc23_tmp/live_game_probe.log` + `E:/steam/logs/console_log.txt`). This probe
keeps every OTHER step on the product path and changes exactly one thing: the
start. It uses the same `LocalDesktopRunner.launch_game_target` seam the
catalog launcher uses, with `kind="path"` against the executable the catalog
itself verified (`artifact.executable`), and it uses the same
`GameLaunchService` gate for memory.

Everything after the start is unchanged product code: `NovaForgeManager` →
`CaptureRouter` → `GameObserver` → `GameStateService`, with the same focus-away,
restore, stop and graceful-close steps as `live_game_probe.py`.

Not done here: input synthesis, display-mode forcing, process kills, adopting a
user's instance.
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

from novacontrol.desktop.capture import PersistentScreenCapture  # noqa: E402
from novacontrol.desktop.controller import LocalDesktopRunner  # noqa: E402
from novacontrol.novaforge.manager import NovaForgeManager  # noqa: E402  (frozen in plan; touch only here)
from live_game_probe import (  # noqa: E402
    FRAMES,
    GAME,
    build_games,
    claim_foreground,
    say,
    _wait_for_count,
    _wait_for_state,
)
from observer_lifecycle_probe import spawn_window  # noqa: E402

#: How long the game's own window may take to appear after the executable
#: starts. A 2003 title initialises DirectX synchronously, so this is generous.
WINDOW_WAIT_SECONDS = 90.0


def game_only(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter a loose name probe down to the game's own processes.

    ``game_processes(["Freedom", "Launcher"])`` matches by substring, so the
    operator's ``SpotifyLauncher`` is a false positive: only a process NAMED
    ``Launcher`` or one containing ``Freedom`` belongs to this title.
    """
    games_rows: list[dict[str, Any]] = []
    for row in rows or []:
        name = str(row.get("name") or "").lower()
        if name == "launcher" or "freedom" in name:
            games_rows.append(row)
    return games_rows


async def wait_for_game_window(runner: LocalDesktopRunner, title: str) -> dict[str, Any] | None:
    """Poll the product's own window seam until the game window exists."""
    deadline = time.monotonic() + WINDOW_WAIT_SECONDS
    while time.monotonic() < deadline:
        geometry = await runner.window_geometry(title=title)
        if geometry is not None:
            return {
                "handle": int(getattr(geometry, "handle", 0)),
                "pid": int(getattr(geometry, "pid", 0)),
                "title": str(getattr(geometry, "title", "")),
                "bounds": list(getattr(geometry, "bounds")),
                "foreground": bool(getattr(geometry, "foreground", False)),
                "minimized": bool(getattr(geometry, "minimized", False)),
                "visible": bool(getattr(geometry, "visible", False)),
            }
        await asyncio.sleep(0.5)
    return None


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
    entry = resolution.get("entry") or {}
    artifact = dict(entry.get("artifact") or {})
    say(
        discovered_status=resolution.get("status"),
        discovered_artifact=artifact,
        already_running=entry.get("running"),
    )
    if str(resolution.get("status")) == "already_running" or entry.get("running"):
        say(blocked="the game is already running; NF-23 must not adopt a user's instance")
        return 2
    executable = str(artifact.get("executable") or "")
    if not executable or not Path(executable).is_file():
        say(blocked=f"the catalog's verified executable is not present: {executable!r}")
        return 2

    before_processes = game_only(await runner.game_processes(["Freedom", "Launcher"]))
    say(processes_before=before_processes)
    if before_processes:
        say(blocked="a process matching the game is already running; refusing to add one")
        return 2

    started_at = time.perf_counter()
    started = await runner.launch_game_target(kind="path", target=executable, name=GAME)
    say(started=started, start_seconds=round(time.perf_counter() - started_at, 2))

    window = await wait_for_game_window(runner, GAME)
    processes = game_only(await runner.game_processes(["Freedom", "Launcher"]))
    say(
        window_found=window is not None,
        window=window,
        processes_after_start=processes,
        window_wait_seconds=round(time.perf_counter() - started_at, 2),
    )
    if window is None:
        say(blocked="no window titled 'Freedom Fighters' appeared after the direct start")
        return 3

    pid = int(window["pid"])
    handle = int(window["handle"])
    started_row = next(
        (
            row
            for row in processes
            if int(row.get("pid") or 0) == pid
        ),
        {},
    )
    identity = {
        "pid": pid,
        "start_time": str(started_row.get("start_time") or ""),
        "process_name": str(started_row.get("name") or ""),
        "window_handle": handle,
        "window_title": str(window["title"]),
    }
    say(target_identity=identity)

    manager: NovaForgeManager | None = None
    # One worker for every geometry read this probe makes (handle-based reads
    # are the worker's seam; the runner's public geometry takes a title), and
    # the manager gets the same read so its ticks and this probe's checks are
    # the same measurement.
    worker = PersistentScreenCapture()
    try:
        manager = NovaForgeManager(
            data_dir=Path(tempfile.mkdtemp(prefix="nf23-direct-")),
            capture=lambda path: runner.capture_screen(path),
            geometry=lambda title: worker.window_geometry(title=title),
            window_lines=lambda: runner.window_lines(),
            capture_window=lambda h, path: runner.capture_window(h, path, thumbnail=True),
            process_identity=runner.process_identity,
            game_launcher=games,
            observation_interval=1.0,
        )
        observation = await manager.start_observation(
            {
                "title": str(window["title"]),
                "pid": pid,
                "start_time": identity["start_time"],
                "interval": 1.0,
            }
        )
        say(
            bind_started=bool(observation.get("started")),
            bind_state=observation.get("state"),
            bind_reason_code=observation.get("reason_code"),
            bind_reason=str(observation.get("reason") or "")[:200],
            bind_target=observation.get("target"),
            bind_interval=observation.get("interval"),
        )

        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status = manager.observation_status()
            if status["counts"]["observed"] >= FRAMES:
                break
            if status["state"] in ("stopped", "failed"):
                break
            await asyncio.sleep(0.5)
        status = manager.observation_status()
        history = [row for row in status["history"] if row.get("status") == "observed"]
        latencies = [float(row.get("capture_ms") or 0.0) for row in history]
        live_geometry = await worker.window_geometry(handle=handle)
        live_rect = list(getattr(live_geometry, "bounds", ()) or ())
        say(
            observed_counts=status["counts"],
            observed_state=status["state"],
            observed_pause=status["pause"],
            observed_backend=status["backend"],
            observed_interval=status["interval"],
            observed_frames=len(history),
            observed_latency_ms={
                "min": round(min(latencies), 1) if latencies else None,
                "median": round(statistics.median(latencies), 1) if latencies else None,
                "p95": (
                    round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 1)
                    if latencies
                    else None
                ),
                "max": round(max(latencies), 1) if latencies else None,
            },
            observed_dims=sorted({(row.get("width"), row.get("height")) for row in history}),
            observed_sources=sorted({str(row.get("source")) for row in history}),
            observed_backends=sorted({str(row.get("backend")) for row in history}),
            observed_contents=sorted(
                {str((row.get("validation") or {}).get("content")) for row in history}
            ),
            observed_live_rect=live_rect,
            observed_last=status["last_success"],
        )

        # Focus away: a fullscreen title is expected to minimize, and the
        # observer must PAUSE with no frame rather than showing the desktop.
        intruder = spawn_window(seconds=120)
        await asyncio.sleep(1.0)
        before_focus = manager.observation_status()["counts"]
        reached = await asyncio.wait_for(
            _wait_for_state(manager, ("paused", "stopped", "failed"), 25.0), timeout=30
        )
        after_focus = manager.observation_status()
        await asyncio.sleep(3.0)
        settled = manager.observation_status()
        geometry_now = await worker.window_geometry(handle=handle)
        say(
            focus_away_reached=reached,
            focus_away_state=after_focus["state"],
            focus_away_pause=after_focus["pause"],
            focus_away_counts_before=before_focus,
            focus_away_counts_now=settled["counts"],
            focus_away_new_frames=(
                settled["counts"]["observed"] != before_focus["observed"]
            ),
            focus_away_geometry={
                "minimized": bool(getattr(geometry_now, "minimized", None)),
                "foreground": bool(getattr(geometry_now, "foreground", None)),
            },
        )

        intruder.terminate()
        await asyncio.sleep(0.5)
        restored = claim_foreground(handle)
        say(restore_activated=restored)
        resumed = await asyncio.wait_for(
            _wait_for_state(manager, ("observing",), 30.0), timeout=35
        )
        base = manager.observation_status()["counts"]["observed"]
        grew = await asyncio.wait_for(
            _wait_for_count(manager, base, 30.0), timeout=35
        )
        after_restore = manager.observation_status()
        say(
            restore_resumed=resumed,
            restore_new_frame=grew,
            restore_state=after_restore["state"],
            restore_counts=after_restore["counts"],
            restore_last=after_restore["last_success"],
            restore_backend=after_restore["backend"],
        )

        say(
            no_input_path=manager.action_input is None,
            sessions_created=len(getattr(manager, "_sessions", {}) or {}),
        )
    finally:
        if manager is not None:
            try:
                await manager.stop_observation("probe complete")
            except Exception:  # noqa: BLE001 - a stop failure is reported below
                pass
        # Graceful close only: WM_CLOSE to the game window this probe started.
        close: dict[str, Any]
        try:
            close = dict(await runner.close_game_window(str(window["title"])))
        except Exception as exc:  # noqa: BLE001 - a refused close is a result
            close = {"status": "CLOSE_FAILED", "closed": False,
                     "reason": f"{type(exc).__name__}: {exc}"}
        say(close_result=close)
        exited = False
        for _ in range(45):
            alive = await runner.process_identity(pid)
            if alive is None:
                exited = True
                break
            await asyncio.sleep(1.0)
        say(
            process_exited=exited,
            process_identity_after=await runner.process_identity(pid),
        )
        if not exited:
            say(blocker="the game did not exit after a graceful close; it is LEFT RUNNING and untouched")
        closer = getattr(worker, "aclose", None)
        if callable(closer):
            await closer()
        closer = getattr(runner, "aclose", None)
        if callable(closer):
            await closer()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
