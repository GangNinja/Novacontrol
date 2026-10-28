"""NF-23 Objective H: a REAL game observed through the product's own pipeline.

Freedom Fighters (Steam 1347780) is the phase's selected target: single-player,
offline, small, already characterised in H02/H03. The launch goes through the
application's own `GameCatalog`/`GameLaunchService` composition (the same
`_build_game_service` wiring), which enforces the memory gate, verifies the
process identity AND a visible window, and closes only what it started.

What this probe does NOT do: send input, change display settings, kill a
process, or touch any other application. The one window it focuses is the game
it launched itself.
"""

from __future__ import annotations

import asyncio
import ctypes
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
from novacontrol.novaforge.launcher import GameCatalog, GameLaunchService  # noqa: E402
from novacontrol.novaforge.manager import NovaForgeManager  # noqa: E402
from observer_lifecycle_probe import spawn_window  # noqa: E402

GAME = "Freedom Fighters"
FRAMES = 10

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.SetForegroundWindow.restype = ctypes.c_bool
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.AttachThreadInput.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_bool]
user32.AttachThreadInput.restype = ctypes.c_bool
user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
SW_RESTORE = 9


def say(**fields: object) -> None:
    print(json.dumps(fields, default=str), flush=True)


def claim_foreground(handle: int) -> bool:
    """Activate our own window. SetForegroundWindow is refused to a background
    process unless the calling thread shares input state with the current
    foreground thread, so the attachment is made for the call and undone."""
    user32.ShowWindow(ctypes.c_void_p(handle), SW_RESTORE)
    mine = kernel32.GetCurrentThreadId()
    current = user32.GetForegroundWindow()
    theirs = 0
    attached = False
    if current:
        theirs = user32.GetWindowThreadProcessId(current, None)
        if theirs and theirs != mine:
            attached = bool(user32.AttachThreadInput(mine, theirs, True))
    user32.BringWindowToTop(ctypes.c_void_p(handle))
    ok = bool(user32.SetForegroundWindow(ctypes.c_void_p(handle)))
    if attached and theirs:
        user32.AttachThreadInput(mine, theirs, False)
    return ok


def build_games(runner: LocalDesktopRunner) -> GameLaunchService | None:
    """The application's own composition, copied seam for seam."""
    seams = {
        "steam": getattr(runner, "installed_steam_games", None),
        "shortcuts": getattr(runner, "installed_app_targets", None),
        "processes": getattr(runner, "game_processes", None),
        "processes_under": getattr(runner, "game_processes_under", None),
        "launch": getattr(runner, "launch_game_target", None),
        "close": getattr(runner, "close_game_window", None),
        "memory": getattr(runner, "memory_snapshot", None),
    }
    required = {name: seam for name, seam in seams.items() if name != "processes_under"}
    if not all(callable(seam) for seam in required.values()):
        return None
    if not callable(seams["processes_under"]):
        seams["processes_under"] = None
    catalog = GameCatalog(
        steam_games=seams["steam"],
        shortcuts=seams["shortcuts"],
        processes=seams["processes"],
        processes_under=seams["processes_under"],
        configured_dirs=(),
    )
    return GameLaunchService(
        catalog,
        launch=seams["launch"],
        processes=seams["processes"],
        processes_under=seams["processes_under"],
        close_window=seams["close"],
        memory=seams["memory"],
        min_free_gb=2.0,
        max_used_percent=92.0,
        startup_timeout=120.0,
        shutdown_timeout=30.0,
        # A 2003 title's launcher plus Steam's own startup can exceed the
        # default 30 s; the timeout is waiting, not patience with a broken launch.
        window_timeout=90.0,
    )


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
        discovered_reason=str(resolution.get("reason") or "")[:200],
        discovered_artifact=(resolution.get("entry") or {}).get("artifact"),
        already_running=(resolution.get("entry") or {}).get("running"),
    )
    if str(resolution.get("status")) == "already_running":
        say(blocked="the game is already running; NF-23 must not adopt a user's instance")
        return 2

    started = time.perf_counter()
    launch = await games.launch(GAME)
    say(
        launch_status=launch.get("status"),
        launch_reason=str(launch.get("reason") or "")[:300],
        launch_record=launch.get("record") or launch.get("owned") or launch,
        launch_seconds=round(time.perf_counter() - started, 2),
    )
    # Steam's own log is the evidence for WHY a launch produced no window.
    steam_log = Path("E:/steam/logs/console_log.txt")
    if steam_log.is_file():
        try:
            tail = steam_log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            tail = []
        app_lines = [line for line in tail if "1347780" in line][-12:]
        say(steam_console_tail=app_lines)
    processes = await runner.game_processes(["Freedom", "Launcher"])
    say(processes_after_launch=processes)
    owned = games.owned
    if owned is None:
        say(blocked="the launch produced no owned record")
        return 3
    record = owned.to_dict() if hasattr(owned, "to_dict") else dict(owned)
    say(owned_record=record)
    handle = int(record.get("window_handle") or 0)
    pid = int(record.get("pid") or 0)
    window_title = str(record.get("window") or record.get("title") or GAME)
    manager: NovaForgeManager | None = None
    # The worker's handle-based geometry is the read the runner's public
    # title-only seam cannot do; the manager gets the same read.
    worker = PersistentScreenCapture()

    try:
        manager = NovaForgeManager(
            data_dir=Path(tempfile.mkdtemp(prefix="nf23-live-")),
            # Exactly the application's own seam shape: the display crop never
            # uses a side car (it describes the whole screen, not the window).
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
        handle = int(getattr(geometry, "handle", handle))
        say(
            window_identity={
                "handle": handle,
                "pid": int(getattr(geometry, "pid", pid)),
                "title": str(getattr(geometry, "title", "")),
                "bounds": list(getattr(geometry, "bounds")),
                "foreground": bool(getattr(geometry, "foreground", False)),
                "minimized": bool(getattr(geometry, "minimized", False)),
            },
        )

        observation = await manager.start_observation(
            {"title": str(getattr(geometry, "title", window_title)), "pid": pid,
             "start_time": str(record.get("start_time") or ""), "interval": 1.0}
        )
        say(
            bind_started=bool(observation.get("started")),
            bind_state=observation.get("state"),
            bind_reason_code=observation.get("reason_code"),
            bind_reason=str(observation.get("reason") or "")[:200],
            bind_target=observation.get("target"),
        )

        deadline = time.monotonic() + 60
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
                "max": round(max(latencies), 1) if latencies else None,
            },
            observed_dims=sorted({(row.get("width"), row.get("height")) for row in history}),
            observed_sources=sorted({str(row.get("source")) for row in history}),
            observed_contents=sorted(
                {
                    str((row.get("validation") or {}).get("content"))
                    for row in history
                }
            ),
            observed_last=status["last_success"],
            observed_live_window=list(
                getattr(await worker.window_geometry(handle=handle) or geometry, "bounds")
            ),
        )

        # Focus away: the phase's documented behaviour for a fullscreen title is
        # that Windows minimizes it and observation PAUSES with no frame.
        intruder = spawn_window(seconds=120)
        await asyncio.sleep(1.0)
        before_focus_counts = manager.observation_status()["counts"]
        focus_away = await asyncio.wait_for(
            _wait_for_state(manager, ("paused", "stopped", "failed"), 25.0), timeout=30
        )
        after_focus = manager.observation_status()
        await asyncio.sleep(3.0)
        settled = manager.observation_status()
        say(
            focus_away_reached=focus_away,
            focus_away_state=after_focus["state"],
            focus_away_pause=after_focus["pause"],
            focus_away_counts_before=before_focus_counts,
            focus_away_counts_now=settled["counts"],
            focus_away_new_frames=(
                settled["counts"]["observed"] != before_focus_counts["observed"]
            ),
            focus_away_geometry={
                "minimized": bool(getattr(await worker.window_geometry(handle=handle) or geometry, "minimized")),
                "foreground": bool(getattr(await worker.window_geometry(handle=handle) or geometry, "foreground")),
            },
        )

        # Restore: activate the game window this probe launched, then require
        # revalidation before a frame is accepted again.
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

        # No input was sent: the observation pipeline holds no injector.
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
        # Graceful close only: WM_CLOSE through the service that owns it.
        close = await games.close()
        say(close_result=close)
        for _ in range(60):
            alive = await runner.process_identity(pid)
            if alive is None:
                break
            await asyncio.sleep(1.0)
        still_alive = await runner.process_identity(pid)
        say(
            process_exited=still_alive is None,
            process_identity_after=still_alive,
        )
        closer = getattr(worker, "aclose", None)
        if callable(closer):
            await closer()
        closer = getattr(runner, "aclose", None)
        if callable(closer):
            await closer()
    return 0


async def _wait_for_state(manager: NovaForgeManager, states: tuple[str, ...], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if manager.observation_status()["state"] in states:
            return True
        await asyncio.sleep(0.25)
    return False


async def _wait_for_count(manager: NovaForgeManager, base: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if manager.observation_status()["counts"]["observed"] > base:
            return True
        await asyncio.sleep(0.25)
    return False


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
