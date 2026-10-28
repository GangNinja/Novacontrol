"""NF-22 acceptance — the bounded autonomous loop, and what is NOT proven.

Run it from the repository root:

    PYTHONPATH=src python scripts/novaforge_nf22_autonomy_acceptance.py

The receipt this prints is deliberately narrow. It drives the SHIPPED
``AutonomousSessionController`` over the SHIPPED NF-16 → NF-21 services through
``novacontrol.novaforge.simulation``, whose declared fixture scenario is the
build's own offline world. That is a real closed loop over simulated evidence —
observe → understand → identify objective → plan → approve → act → verify →
recover/continue → complete — and every verdict it prints says ``simulated``.

It does NOT prove live gameplay. No game is launched, no window is focused and
no real key is sent by this script, so the live section reports ``NOT_TESTED``
with the concrete dependency that is missing rather than borrowing the
simulator's result. The phase's own rule is the reason: a game is not fit for
autonomous control because it launches, embeds or displays a frame, and no game
was validated for a measurable objective in this session.

What each step is actually evidence of:

  * the gate: a run without the operator's approval sends nothing;
  * the loop: the objective — not the plan and not the game staying open —
    decides completion, and the verdict is read from fresh evidence;
  * the bounds: the action, cycle, recovery and replan budgets stop a run;
  * the safety stops: an unresponsive target, a refused input backend and an
    unreadable counter end a run with NO input rather than pressing blind;
  * the lifecycle: pause/resume, cancellation, session stop with input
    released, and no automatic restart after a terminal outcome.

Exit codes: 0 when every simulated step passed, 1 when one failed, 2 when the
environment cannot run the checks at all.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from novacontrol.novaforge.autonomy import (  # noqa: E402 - path set above
    AUTONOMY_TERMINAL_PHASES,
    AutonomyOutcome,
    AutonomyPhase,
)
from novacontrol.novaforge.simulation import (  # noqa: E402 - path set above
    SIM_GAME,
    SIM_SESSION_ID,
    SimulatedGame,
    SimulatedNovaForge,
    simulated_limits,
)

STEPS: list[dict[str, Any]] = []

#: The upstream phases this one composes, the module that must exist and the
#: name on its surface the supervisor actually consumes. A phase whose report
#: exists but whose module is missing here is a blocker, not a dependency.
DEPENDENCIES: tuple[tuple[str, str, str], ...] = (
    ("NF-15 unified observation", "novacontrol.novaforge.observer", "GameObserver"),
    ("NF-16 state understanding", "novacontrol.novaforge.state_service", "GameStateService"),
    ("NF-17 objectives", "novacontrol.novaforge.objective_service", "ObjectiveService"),
    ("NF-18 knowledge", "novacontrol.novaforge.knowledge_service", "KnowledgeService"),
    ("NF-19 control interface", "novacontrol.novaforge.control", "GameControlService"),
    ("NF-20 planning", "novacontrol.novaforge.planner", "GamePlanService"),
    ("NF-21 outcome verification", "novacontrol.novaforge.verification", "GameVerificationService"),
)


def record(step: str, ok: bool, detail: Any = "") -> None:
    STEPS.append({"step": step, "ok": bool(ok), "detail": detail})
    print(json.dumps({"step": step, "ok": bool(ok), "detail": detail}, default=str), flush=True)


def dependency_matrix() -> bool:
    """Which upstream capabilities this build really has, module and name."""
    rows: list[dict[str, Any]] = []
    for phase, module_name, attribute in DEPENDENCIES:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - a missing dependency is a row
            rows.append({"phase": phase, "module": module_name, "state": "BLOCKED",
                         "detail": f"{type(exc).__name__}: {exc}"})
            continue
        rows.append({
            "phase": phase,
            "module": module_name,
            "state": "implemented" if hasattr(module, attribute) else "partial",
            "detail": attribute if hasattr(module, attribute) else f"{attribute} is missing",
        })
    usable = all(row["state"] == "implemented" for row in rows)
    record("every composed phase exposes the surface NF-22 consumes", usable, rows)
    return usable


async def approval_gate() -> bool:
    """The operator's approval is the gate: without it, no run and no input."""
    forge = SimulatedNovaForge()
    try:
        controller = forge.controller()
        answer = await controller.start(SIM_SESSION_ID, approved=False, note="")
        ok = (
            answer.get("started") is False
            and answer.get("code") == "approval_required"
            and forge.game.presses == 0
        )
        record("a run without the operator's approval starts nothing", ok,
               {"code": answer.get("code"), "started": answer.get("started"),
                "presses": forge.game.presses})
        return ok
    finally:
        await forge.shutdown()


async def happy_path() -> dict[str, Any]:
    """One complete bounded session over the fixture scenario."""
    forge = SimulatedNovaForge()
    try:
        run = await forge.run()
        cycles = run.get("cycles") or []
        outcomes = [
            row for cycle in cycles for row in (cycle.get("verification") or {}).get("outcomes") or []
        ]
        verified = [row for row in outcomes if row.get("status") == "verified_success"]
        record(
            "the closed loop completes the objective and says so honestly",
            run.get("phase") == AutonomyPhase.COMPLETED.value
            and run.get("outcome") == AutonomyOutcome.OBJECTIVE_COMPLETED.value
            and run.get("evidence_source") == "simulated"
            and bool(run.get("game", {}).get("completed")),
            {"phase": run.get("phase"), "outcome": run.get("outcome"),
             "evidence_source": run.get("evidence_source"),
             "objective_id": run.get("objective_id"),
             "objective": run.get("objective"),
             "completion_criteria": run.get("completion_criteria")},
        )
        record(
            "every dispatched action carries a verified outcome with evidence",
            bool(verified)
            and len(verified) == run.get("actions_dispatched")
            and all(row.get("evidence") for row in verified)
            and not run.get("effect_verified_unchecked"),
            {"dispatched": run.get("actions_dispatched"), "verified": len(verified),
             "outcomes": [row.get("status") for row in outcomes],
             "evidence": [row.get("evidence") for row in verified]},
        )
        phases = [step.get("phase") for step in run.get("steps") or []]
        required = (
            AutonomyPhase.INITIALIZING.value,
            AutonomyPhase.OBSERVING.value,
            AutonomyPhase.UNDERSTANDING_STATE.value,
            AutonomyPhase.IDENTIFYING_OBJECTIVE.value,
            AutonomyPhase.PLANNING.value,
            AutonomyPhase.EXECUTING.value,
            AutonomyPhase.VERIFYING.value,
            AutonomyPhase.COMPLETED.value,
        )
        record(
            "the session trace records the whole orchestration, in order",
            all(phase in phases for phase in required),
            {"missing": [phase for phase in required if phase not in phases],
             "phases": phases},
        )
        record(
            "the session ends and its input is released at the end",
            bool(run.get("session_ended"))
            and bool((run.get("session_ended") or {}).get("cleanup", {}).get("ok")),
            {"session_ended": run.get("session_ended")},
        )
        record(
            "the run is never described as live evidence",
            run.get("evidence_source") == "simulated"
            and "simulated" not in ("", None)
            and run.get("simulated") is True,
            run.get("evidence_source"),
        )
        return run
    finally:
        await forge.shutdown()


async def bounds_stop_a_run() -> list[bool]:
    """The declared budgets stop a run; they are not advice, and never widened."""
    checks: list[bool] = []

    # The world needs six presses; the run is approved for three. It must spend
    # exactly three and stop — a run that kept going would be widening its own
    # budget to finish the objective.
    forge = SimulatedNovaForge(game=SimulatedGame(target_value=6))
    try:
        limits = simulated_limits(max_actions=3, max_actions_per_plan=3)
        run = await forge.run(limits=limits)
        ok = (
            run.get("outcome") == AutonomyOutcome.RESOURCE_LIMIT_REACHED.value
            and run.get("actions_dispatched") == limits.max_actions
            and forge.game.presses == limits.max_actions
            and run.get("phase") != AutonomyPhase.COMPLETED.value
        )
        record("the action budget stops the run after exactly its bound", ok,
               {"outcome": run.get("outcome"), "reason": run.get("reason"),
                "dispatched": run.get("actions_dispatched"), "presses": forge.game.presses,
                "budget": limits.max_actions, "objective_met": run.get("game", {}).get("completed")})
        checks.append(ok)
    finally:
        await forge.shutdown()

    # A plan that needs more steps than the operator approved is REFUSED, not
    # trimmed and not run: nothing is dispatched at all.
    forge = SimulatedNovaForge(game=SimulatedGame(target_value=6))
    try:
        limits = simulated_limits(max_actions=3, max_actions_per_plan=1)
        run = await forge.run(limits=limits)
        reason = str(run.get("reason") or "")
        ok = (
            run.get("outcome") == AutonomyOutcome.RESOURCE_LIMIT_REACHED.value
            and run.get("actions_dispatched") == 0
            and forge.game.presses == 0
            and "per-plan budget" in reason
        )
        record("a plan above the approved per-plan budget is refused, not widened", ok,
               {"outcome": run.get("outcome"), "reason": reason,
                "dispatched": run.get("actions_dispatched"), "presses": forge.game.presses})
        checks.append(ok)
    finally:
        await forge.shutdown()

    return checks


async def safety_stops() -> list[bool]:
    """A world that cannot be read or driven ends the run with NO input."""
    checks: list[bool] = []

    forge = SimulatedNovaForge(game=SimulatedGame(target_responsive=False))
    try:
        run = await forge.run()
        ok = (
            run.get("outcome") == AutonomyOutcome.SAFETY_STOP.value
            and run.get("actions_dispatched") == 0
            and forge.game.presses == 0
        )
        record("an unresponsive target stops the run before any input", ok,
               {"outcome": run.get("outcome"), "dispatched": run.get("actions_dispatched"),
                "presses": forge.game.presses})
        checks.append(ok)
    finally:
        await forge.shutdown()

    forge = SimulatedNovaForge(game=SimulatedGame(accepts_input=False))
    try:
        run = await forge.run()
        ok = (
            run.get("outcome") != AutonomyOutcome.OBJECTIVE_COMPLETED.value
            and forge.game.presses == 0
            and forge.game.refused_presses > 0
        )
        record("an input backend that refuses is never reported as progress", ok,
               {"outcome": run.get("outcome"), "phase": run.get("phase"),
                "refused_presses": forge.game.refused_presses,
                "presses": forge.game.presses})
        checks.append(ok)
    finally:
        await forge.shutdown()

    forge = SimulatedNovaForge(game=SimulatedGame(counter_readable=False))
    try:
        run = await forge.run()
        statuses = [
            row.get("status")
            for cycle in run.get("cycles") or []
            for row in (cycle.get("verification") or {}).get("outcomes") or []
        ]
        ok = (
            forge.game.presses == 0
            and run.get("outcome") != AutonomyOutcome.OBJECTIVE_COMPLETED.value
            and "verified_success" not in statuses
        )
        record("an unreadable counter is not a verified effect", ok,
               {"outcome": run.get("outcome"), "presses": forge.game.presses,
                "statuses": statuses})
        checks.append(ok)
    finally:
        await forge.shutdown()

    forge = SimulatedNovaForge(game=SimulatedGame(failing=True))
    try:
        run = await forge.run()
        ok = run.get("outcome") == AutonomyOutcome.OBJECTIVE_FAILED.value
        record("a verified objective failure ends the run as failed", ok,
               {"outcome": run.get("outcome"), "game": run.get("game")})
        checks.append(ok)
    finally:
        await forge.shutdown()

    return checks


async def pause_resume_and_cancel() -> list[bool]:
    """Pause, resume and cancellation stay the operator's, and hold no input."""
    checks: list[bool] = []
    forge = SimulatedNovaForge()
    try:
        await forge.pause(SIM_SESSION_ID, reason="operator")
        held = await forge.observe_frame_once()
        await forge.resume(SIM_SESSION_ID)
        back = await forge.observe_frame_once()
        ok = (
            held.get("status") == "not_observing"
            and held.get("reason_code") == "paused_by_user"
            and back.get("status") == "observed"
        )
        record("a paused run stops reading and resumes on the operator's word", ok,
               {"paused_read": held.get("reason_code"), "resumed_read": back.get("status")})
        checks.append(ok)
    finally:
        await forge.shutdown()

    forge = SimulatedNovaForge()
    try:
        controller = forge.controller()
        started = await controller.start(SIM_SESSION_ID, approved=True, note="acceptance")
        cancelled = await controller.cancel(SIM_SESSION_ID, reason="operator")
        run = await controller.wait(SIM_SESSION_ID, timeout=20.0)
        payload = controller.run_for(SIM_SESSION_ID)
        terminal = (payload.to_dict() if payload is not None else {}).get("outcome")
        # A cancelled run never restarts: the same call again must not start one.
        again = await controller.resume(SIM_SESSION_ID)
        ok = (
            started.get("started") is True
            and cancelled.get("cancelled") is True
            and terminal == AutonomyOutcome.SESSION_CANCELLED.value
            and again.get("resumed") is not True
        )
        record("cancellation ends the run and it is never restarted", ok,
               {"started": started.get("started"), "cancelled": cancelled.get("cancelled"),
                "outcome": terminal, "restart": again.get("code") or again.get("reason"),
                "run": run.get("phase")})
        checks.append(ok)
        await controller.shutdown()
    finally:
        await forge.shutdown()

    return checks


async def main() -> int:
    checks: list[bool] = []
    record("the acceptance runs on a supported host", sys.version_info >= (3, 11),
           {"python": sys.version.split()[0], "platform": sys.platform})
    checks.append(dependency_matrix())
    checks.append(await approval_gate())
    run = await happy_path()
    checks.append(bool(run.get("started")))
    checks.extend(await bounds_stop_a_run())
    checks.extend(await safety_stops())
    checks.extend(await pause_resume_and_cancel())

    # ── live gameplay: reported, never inferred from the simulator ────────
    live_reason = (
        "No game was validated for autonomous control in this session. NF-22 requires a "
        "game whose launch/embedding compatibility, capture reliability, observable state, "
        "control surface, objective and success/failure signal have all been verified — and "
        "requires that permission to launch a game is never read as permission to control "
        "it. None of the installed titles has that evidence, so no session was started and "
        "no input was sent to a real game by this script."
    )
    print(json.dumps({
        "live_gameplay": "NOT_TESTED",
        "fixture_scenario": SIM_GAME,
        "evidence_source_tested": "simulated",
        "why": live_reason,
        "what_a_live_run_would_need": [
            "an installed game with a validated objective the system can observe (NF-16/NF-17)",
            "a fresh, target-validated observation and an available resource gate",
            "the operator's explicit approval for THIS run, with its plan and budget visible",
            "a reversible objective or a safe termination path before the first input",
            "verified pause and cancellation ahead of the run",
        ],
    }, indent=2), flush=True)

    failures = [step["step"] for step in STEPS if not step["ok"]]
    if all(checks) and not failures:
        # Everything this build can prove offline passed. Live gameplay is the
        # phase's remaining gap, and it is a limitation rather than a pass.
        verdict = "PASS WITH LIMITATIONS"
    elif not checks:
        verdict = "BLOCKED"
    else:
        verdict = "FAIL"
    print(json.dumps({
        "verdict": verdict,
        "steps": len(STEPS),
        "failed": len(failures),
        "failed_steps": failures,
        "live_gameplay": "NOT_TESTED",
        "note": (
            "The simulated end-to-end pass is real orchestration over simulated evidence. "
            "It is not live-game verification."
        ),
    }, indent=2), flush=True)
    return 0 if verdict != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
