"""NF-23 F-05: the REAL observation pipeline, on a real window, through a lifecycle.

The manager is wired exactly as the application wires it (window-surface seam with
the paint side car first, display crop second, a process-identity probe), and the
target is this probe's own disposable window — no game, no synthesized input, no
other application touched. Every claim printed here is a value the pipeline
itself reported.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novacontrol.desktop.capture import PersistentScreenCapture  # noqa: E402
from novacontrol.novaforge.manager import NovaForgeManager  # noqa: E402

WINDOW = ROOT / ".nc23_tmp" / "lifecycle_window.ps1"
#: The disposable probe window's own title (the script sets it; this probe only
#: consumes it). The lifecycle probe uses the same window, never concurrently.
TITLE = "NC NF23 LIFECYCLE PROBE"
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
SW_MINIMIZE = 6
SW_RESTORE = 9
WM_CLOSE = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


# A HANDLE is pointer-sized. Without these declarations ctypes assumes c_int
# and truncates the handle on 64-bit Windows, so every read answers "unknown".
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
kernel32.GetProcessTimes.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
]
kernel32.GetProcessTimes.restype = ctypes.c_int
kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
kernel32.GetExitCodeProcess.restype = ctypes.c_int
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

#: What GetExitCodeProcess answers for a process that has not ended.
STILL_ACTIVE = 259


async def process_identity(pid: int) -> dict[str, object] | None:
    """A real process-identity read: creation time by pid, or None when gone.

    Async because that is the seam the observer awaits; a sync function here
    would raise on ``await`` and be reported as "could not be checked".
    """
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
    if not handle:
        return None
    try:
        created = FILETIME()
        exited = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        if ticks == 0:
            return None
        # A HANDLE outlives the process on Windows: a terminated process whose
        # handle somebody still holds answers GetProcessTimes and would look
        # alive. The exit code is what tells "running" from "ended", which is
        # the answer the product's own Get-Process probe gives.
        code = ctypes.c_uint32(0)
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != STILL_ACTIVE:
            return None
        moment = datetime(1601, 1, 1, tzinfo=UTC) + timedelta(microseconds=ticks // 10)
        return {"pid": int(pid), "start_time": moment.isoformat()}
    finally:
        kernel32.CloseHandle(handle)


def say(**fields: object) -> None:
    print(json.dumps(fields, default=str), flush=True)


def spawn_window(seconds: int = 180) -> subprocess.Popen:
    return subprocess.Popen(
        [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(WINDOW),
            "-Seconds", str(seconds),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


async def wait_until(predicate, timeout: float, step: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return False


def outcome(out: object) -> dict[str, object]:
    """An ``AnalysisOutcome``'s own answer, without inventing a field name."""
    if out is None:
        return {"code": "", "ok": None, "reason": ""}
    return {
        "code": str(getattr(out, "reason_code", "") or ""),
        "ok": getattr(out, "analyzed", None),
        "reason": str(getattr(out, "reason", "") or ""),
        "state": bool(getattr(out, "state", None) is not None),
    }


async def main() -> int:  # noqa: C901 - a scripted probe, linear by design
    window = spawn_window()
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
    handle = int(getattr(geometry, "handle"))
    pid = int(getattr(geometry, "pid"))
    say(target_handle=handle, target_pid=pid, target_bounds=list(getattr(geometry, "bounds")))

    tmp = tempfile.TemporaryDirectory()
    manager = NovaForgeManager(
        data_dir=Path(tmp.name),
        capture=lambda path: worker.capture(path, thumbnail=True),
        geometry=lambda title: worker.window_geometry(title=title),
        window_lines=lambda: (None),
        capture_window=lambda h, path: worker.capture_window(h, path, thumbnail=True),
        process_identity=process_identity,
        observation_interval=0.5,
    )
    before = await worker.send_input("stats")
    say(worker_stats_before=before.detail)

    started = await manager.start_observation({"title": TITLE, "pid": pid, "interval": 0.5})
    say(
        start_started=bool(started.get("started")),
        start_state=started.get("state"),
        start_reason=started.get("reason", ""),
        start_reason_code=started.get("reason_code", ""),
        start_target=(started.get("target") or {}).get("identity")
        if isinstance(started.get("target"), dict)
        else None,
        start_interval=started.get("interval"),
    )

    status = manager.observation_status()
    reached = await wait_until(
        lambda: manager.observation_status()["counts"]["observed"] >= 8, 40.0
    )
    status = manager.observation_status()
    say(
        observe_reached_8=reached,
        observe_state=status["state"],
        observe_counts=status["counts"],
        observe_backend=status["backend"],
        observe_interval=status["interval"],
        observe_pause=status["pause"],
        observe_target_foreground=(status.get("target") or {}).get("foreground")
        if isinstance(status.get("target"), dict)
        else None,
        observe_last_success=status["last_success"],
    )
    history = [row for row in status["history"] if row.get("status") == "observed"]
    say(
        observe_history_rows=len(history),
        observe_capture_ms=[row.get("capture_ms") for row in history],
        observe_dims=sorted({(row.get("width"), row.get("height")) for row in history}),
        observe_sources=sorted({str(row.get("source")) for row in history}),
        observe_backends=sorted({str(row.get("backend")) for row in history}),
        observe_ages=[
            row.get("verdict", {}).get("limitations") if isinstance(row.get("verdict"), dict) else None
            for row in history
        ][-1:],
    )
    first_success_at = status["last_success"]["at"] if status["last_success"] else None

    # Downstream while VALID: the state service reads the newest validated frame.
    analyze_ok = outcome(await manager.state.analyze())
    say(
        state_while_observing_code=analyze_ok.get("code"),
        state_while_observing_ok=analyze_ok.get("ok"),
        state_analyzed_count=manager.state.status()["counts"]["analyzed"],
        state_failed_count=manager.state.status()["counts"]["failed"],
    )

    # ── minimized: the pipeline must pause, not keep serving the old frame ──
    user32.ShowWindow(handle, SW_MINIMIZE)
    paused = await wait_until(
        lambda: manager.observation_status()["state"] == "paused", 12.0
    )
    counts_at_pause = manager.observation_status()["counts"]
    await asyncio.sleep(4.5)
    status = manager.observation_status()
    say(
        minimize_paused=paused,
        minimize_state=status["state"],
        minimize_pause=status["pause"],
        minimize_counts_at_pause=counts_at_pause,
        minimize_counts_now=status["counts"],
        minimize_frames_grew=status["counts"]["observed"] != counts_at_pause["observed"],
        minimize_last_success_unchanged=(status.get("last_success") or {}).get("at")
        == first_success_at,
        minimize_history_tail=(
            status["history"][-1] if status["history"] else None
        ),
    )
    stale = outcome(await manager.state.analyze())
    state_status = manager.state.status()
    say(
        state_while_paused_code=stale.get("code"),
        state_while_paused_ok=stale.get("ok"),
        state_while_paused_reason=str(stale.get("reason", ""))[:160],
        state_analyzed_after_pause=state_status["counts"]["analyzed"],
        state_last_refusal=state_status.get("last_refusal"),
        state_timestamp_unchanged=(
            (state_status.get("state") or {}).get("timestamp")
            if isinstance(state_status.get("state"), dict)
            else None
        ),
    )

    # ── restored: revalidation, then frames again ─────────────────────────
    user32.ShowWindow(handle, SW_RESTORE)
    resumed = await wait_until(
        lambda: manager.observation_status()["state"] == "observing", 12.0
    )
    counts_at_resume = manager.observation_status()["counts"]["observed"]
    grew = await wait_until(
        lambda: manager.observation_status()["counts"]["observed"] > counts_at_resume, 15.0
    )
    status = manager.observation_status()
    say(
        restore_resumed=resumed,
        restore_state=status["state"],
        restore_counts=status["counts"],
        restore_new_frames=grew,
        restore_last_success=status["last_success"],
        restore_backend=status["backend"],
    )
    analyze_again = outcome(await manager.state.analyze())
    say(
        state_after_restore_code=analyze_again.get("code"),
        state_after_restore_ok=analyze_again.get("ok"),
        state_analyzed_after_restore=manager.state.status()["counts"]["analyzed"],
    )

    # ── destroyed: the target is gone, and nothing may be served for it ───
    user32.PostMessageW(handle, WM_CLOSE, 0, 0)
    gone = False
    for _ in range(40):
        if await worker.window_geometry(handle=handle) is None:
            gone = True
            break
        await asyncio.sleep(0.25)
    lost = await wait_until(
        lambda: manager.observation_status()["state"] in ("paused", "failed", "stopped"),
        15.0,
    )
    status = manager.observation_status()
    say(
        destroy_window_gone=gone,
        destroy_lost=lost,
        destroy_state=status["state"],
        destroy_pause=status["pause"],
        destroy_reason=str(status.get("reason"))[:160],
        destroy_counts=status["counts"],
    )
    after_lost = outcome(await manager.state.analyze())
    say(
        state_after_loss_code=after_lost.get("code"),
        state_after_loss_ok=after_lost.get("ok"),
    )

    # ── the process itself ends: the pipeline must name that, not a window ──
    window.terminate()
    exited = await wait_until(
        lambda: manager.observation_status()["pause"].get("code") == "process_exited",
        20.0,
    )
    status = manager.observation_status()
    say(
        exit_reported=exited,
        exit_state=status["state"],
        exit_pause=status["pause"],
        exit_reason=str(status.get("reason"))[:160],
        exit_counts=status["counts"],
    )

    stopped = await manager.stop_observation("probe complete")
    say(stop_state=stopped.get("state"), stop_reason=stopped.get("reason", ""))
    after = await worker.send_input("stats")
    say(worker_stats_after=after.detail)
    await worker.aclose()
    window.terminate()
    tmp.cleanup()
    say(closed=True, worker_spawns=worker.spawns, worker_frames=worker.frames)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
