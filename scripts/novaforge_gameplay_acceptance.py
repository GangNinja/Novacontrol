"""Disposable-window acceptance for NovaForge's gameplay input path (R1/R2).

This is the evidence the remediation must produce before anything may claim a
usable action path: a REAL OS window, REAL injected keystrokes, and a verdict
read back from the window by the verifier — no game, no fabricated result.

Run it on Windows:

    python scripts/novaforge_gameplay_acceptance.py

The script LAUNCHES its own disposable window (``scripts/forge_harness_window.ps1``,
a WinForms window titled ``NC FORGE HARNESS``) and closes it again, so a run is
reproducible and the window is guaranteed to start un-satisfied. It refuses to
start when a harness window is already open, because a window left in the
``READY`` state by a previous run would make "the verifier read READY"
unfalsifiable.

It prints one JSON line per step and exits non-zero if any step fails. Every
step is an assertion about observed behaviour:

  1. exactly one harness window exists, it is restored, and it holds the
     foreground (injected input would otherwise land in another app);
  2. a session starts and confirms its target identity for this run;
  3. a PLAN injects nothing (proposal only);
  4. a mismatched digest is DENIED and injects nothing;
  5. a matching digest EXECUTES one tap and the verifier reads "READY" from the
     window — verified_success, achieved true, session COMPLETED;
  6. a stop request during a HOLD aborts it and leaves no key held;
  7. the window vanishing pauses the session and refuses the next action;
  8. emergency stop on a second session closes it and releases input.

Safety notes: input only goes to the harness window this script owns, the action
policy allows only gameplay keys, and the guard releases every held key on every
exit path. Nothing is launched beyond the disposable harness, installed or
downloaded. The user's own windows are never addressed.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from novacontrol.desktop.capture import capture_supported_here
from novacontrol.desktop.controller import LocalDesktopRunner
from novacontrol.novaforge import NovaForgeManager, ScreenInput, SessionState

TARGET = "NC FORGE HARNESS"
READY = "READY"
HARNESS = Path(__file__).with_name("forge_harness_window.ps1")
STEPS: list[dict[str, Any]] = []


def record(step: str, ok: bool, detail: Any = "") -> None:
    STEPS.append({"step": step, "ok": bool(ok), "detail": detail})
    print(json.dumps({"step": step, "ok": bool(ok), "detail": detail}), flush=True)


async def wait_for(predicate, timeout: float = 10.0) -> bool:
    """Poll a SYNC predicate; every check here reads one attribute."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return False


async def _harness_lines(runner: LocalDesktopRunner) -> list[str]:
    return [line for line in await runner.window_lines() or [] if TARGET.lower() in line.lower()]


def _launch_harness(seconds: int) -> subprocess.Popen[bytes]:
    """Start the disposable window as this process's own child."""
    return subprocess.Popen(
        [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(HARNESS), "-Seconds", str(seconds), "-KeepOpen",
        ],
        cwd=str(HARNESS.parent.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def _stop_harness(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - a wedged host
        process.kill()


async def main() -> int:
    if not capture_supported_here() or sys.platform != "win32":
        print(json.dumps({"step": "platform", "ok": False,
                          "detail": f"the disposable-window harness needs Windows; this is {sys.platform}"}))
        return 2

    runner = LocalDesktopRunner()
    worker = runner.input_worker()
    harness: subprocess.Popen[bytes] | None = None
    with TemporaryDirectory(prefix="nc_forge_accept_") as tmp:
        manager = NovaForgeManager(
            data_dir=Path(tmp),
            capture=runner.capture_screen,
            geometry=runner.window_geometry,
            window_lines=runner.window_lines,
            release_inputs=runner.release_all_inputs,
            action_input=ScreenInput(send=worker.send_input),
            objective_probe=runner.window_by_handle,
            capture_supported=True,
            frame_interval=0.25,
            pause_timeout=5.0,
            stop_timeout=8.0,
        )
        try:
            # ── 1. The disposable window is the only target ─────────────
            pre_existing = await _harness_lines(runner)
            record("no harness window was already open", not pre_existing, pre_existing[:3])
            if pre_existing:
                print(json.dumps({"hint": "close the open NC FORGE HARNESS window first"}))
                return 2
            harness = _launch_harness(600)
            lines: list[str] = []
            deadline = asyncio.get_running_loop().time() + 30.0
            while asyncio.get_running_loop().time() < deadline:
                lines = await _harness_lines(runner)
                if lines:
                    break
                await asyncio.sleep(0.2)
            record("the disposable harness window opened", bool(lines), lines[:3])
            if not lines:
                return 2
            geometry = await runner.window_geometry(lines[0].split("|", 1)[1].strip())
            record(
                "the harness is restored with a real client area",
                geometry is not None and geometry.client[2] > 0 and geometry.client[3] > 0,
                None if geometry is None else {"handle": geometry.handle, "client": geometry.client},
            )
            focused = False
            deadline = asyncio.get_running_loop().time() + 15.0
            while asyncio.get_running_loop().time() < deadline:
                probe = await runner.window_geometry(lines[0].split("|", 1)[1].strip())
                if probe is not None and probe.foreground:
                    focused = True
                    break
                await asyncio.sleep(0.3)
            record("the harness holds the foreground (injected input lands in it)", focused)

            described = await manager.start({
                "name": "Disposable harness acceptance",
                "objective": "Prove a guarded key reaches the window and the verifier sees it",
                "target_title": TARGET,
                "time_limit_seconds": 180,
                "max_actions": 3,
            })
            session_id = described["session_id"]
            session = manager.get(session_id)
            assert session is not None
            validated = await wait_for(lambda: bool(session.target_validated))
            record("session pinned and target identity confirmed", validated,
                   {"handle": session.target.handle if session.target else None,
                    "pid": session.target.pid if session.target else None})

            # The proof is causal: the window must NOT already say READY, so a
            # verified success can only come from THIS run's injected key.
            start_window = await runner.window_by_handle(session.target.handle)
            start_title = getattr(start_window, "title", "")
            record("the harness starts un-satisfied (title has no READY)",
                   bool(start_title) and READY not in start_title.upper(), start_title)

            objective = manager.set_objective(session_id, {"kind": "window_title", "parameter": READY})
            record("window_title objective configured", bool(objective["supported"]), objective["objective"])

            plan = manager.plan_action(session_id, {"kind": "key", "key": "w"})
            record("a plan is a proposal, not an action",
                   plan["status"] == "proposed" and plan["executed"] is False, plan["proposal"]["proposal_id"])

            denied = await manager.execute_action(
                session_id, {"proposal_id": plan["proposal"]["proposal_id"], "digest": "0" * 64}
            )
            record("a mismatched digest is denied", denied["status"] == "denied",
                   denied["outcome"]["reason"][:120])

            executed = await manager.execute_action(
                session_id,
                {"proposal_id": plan["proposal"]["proposal_id"], "digest": plan["proposal"]["digest"]},
            )
            verdict = executed.get("verification") or {}
            record("the guarded key executed", executed["status"] == "executed",
                   {"status": executed["status"], "steps": executed["outcome"]["steps"],
                    "presses": executed["outcome"]["presses"], "releases": executed["outcome"]["releases"],
                    "held_after": executed["outcome"]["held_after"],
                    "reason": executed["outcome"]["reason"][:160]})
            record("the verifier read READY from the window",
                   verdict.get("outcome") == "verified_success" and verdict.get("achieved") is True,
                   {"outcome": verdict.get("outcome"), "evidence": (verdict.get("evidence") or [{}])[0]})
            record("a verified objective completed the session",
                   session.state is SessionState.COMPLETED, session.state.value)

            # ── 6. A Guardian cancel during a hold aborts it ────────────
            # The hold is 1500 ms; the cancel must interrupt it, not wait it out.
            second = await manager.start({
                "name": "Hold and cancel", "objective": "Hold then cancel",
                "target_title": TARGET, "max_actions": 2, "time_limit_seconds": 120,
            })
            hold_id = second["session_id"]
            hold_session = manager.get(hold_id)
            assert hold_session is not None
            await wait_for(lambda: bool(hold_session.target_validated))
            hold_plan = manager.plan_action(hold_id, {"kind": "hold", "key": "w", "duration_ms": 1500})
            task = asyncio.create_task(
                manager.execute_action(
                    hold_id,
                    {"proposal_id": hold_plan["proposal"]["proposal_id"],
                     "digest": hold_plan["proposal"]["digest"]},
                )
            )
            await asyncio.sleep(0.3)
            cancelled = await manager.cancel(hold_id)
            stopped = await task
            record("a Guardian cancel interrupted the hold and released it",
                   stopped["outcome"]["status"] in {"aborted", "failed"}
                   and stopped["outcome"]["held_after"] == 0
                   and stopped["outcome"]["elapsed_ms"] < 1400
                   and hold_session.state is SessionState.STOPPED,
                   {"status": stopped["outcome"]["status"],
                    "presses": stopped["outcome"]["presses"],
                    "held_after": stopped["outcome"]["held_after"],
                    "elapsed_ms": stopped["outcome"]["elapsed_ms"],
                    "session": hold_session.state.value,
                    "cancel_state": cancelled.get("state")})

            # ── 7. The window vanishing pauses the session and refuses ───
            # The harness closes itself on Esc, so this is a real window loss
            # caused through the same guarded path, not a stubbed probe.
            third = await manager.start({
                "name": "Target loss", "objective": "Close the window with a key",
                "target_title": TARGET, "max_actions": 2, "time_limit_seconds": 120,
            })
            loss_id = third["session_id"]
            hold_session = manager.get(loss_id)
            assert hold_session is not None
            await wait_for(lambda: bool(hold_session.target_validated))
            close_plan = manager.plan_action(loss_id, {"kind": "key", "key": "esc"})
            close_result = await manager.execute_action(
                loss_id,
                {"proposal_id": close_plan["proposal"]["proposal_id"],
                 "digest": close_plan["proposal"]["digest"]},
            )
            gone = False
            deadline = asyncio.get_running_loop().time() + 8.0
            while asyncio.get_running_loop().time() < deadline:
                if not await _harness_lines(runner):
                    gone = True
                    break
                await asyncio.sleep(0.2)
            paused = await wait_for(
                lambda: hold_session.state is SessionState.PAUSED, timeout=10.0
            )
            refused = manager.plan_action(loss_id, {"kind": "key", "key": "w"})
            refusal_reason = str((refused.get("outcome") or {}).get("reason", ""))
            record("a vanished target pauses the session and refuses the next action",
                   gone and paused and refused.get("status") == "denied"
                   and hold_session.pause_reason in {"target_lost", "target_unreadable"},
                   {"close": close_result.get("status"), "gone": gone,
                    "pause_reason": hold_session.pause_reason,
                    "refusal": refusal_reason[:120]})

            # ── 8. Emergency stop closes the session (idempotent) ────────
            first_stop = await manager.emergency_stop(loss_id)
            second_stop = await manager.emergency_stop(loss_id)
            record("emergency stop closed the session and is idempotent",
                   hold_session.state.value == "STOPPED"
                   and first_stop.get("input_cleanup", {}).get("ok") is not False
                   and second_stop is not None,
                   {"state": hold_session.state.value,
                    "cleanup": hold_session.input_cleanup})
        finally:
            await manager.shutdown()
            if harness is not None:
                _stop_harness(harness)
            await runner.close()

    ok = all(step["ok"] for step in STEPS)
    print(json.dumps({"summary": {"steps": len(STEPS),
                                  "failed": [s["step"] for s in STEPS if not s["ok"]],
                                  "ok": ok}}), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
