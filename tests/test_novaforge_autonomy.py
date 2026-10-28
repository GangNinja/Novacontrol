"""NF-22 — the autonomous session supervisor and the simulated closed loop.

Two layers, tested for their own reasons:

* **The supervisor** over a *scripted* gateway: every branch of the phase's
  vocabulary (lifecycle, orchestration, limits, recovery, cancellation) is driven
  with hand-written answers, so a failure names one rule rather than a puzzle.
* **The simulated acceptance loop**, over the shipped NF-16 → NF-21 services and
  the build's declared fixture scenario. That run is real orchestration over
  simulated evidence; it is deliberately NOT presented as live gameplay.

No test launches a game, focuses a window, or sends a real key: the simulated
input backend is a Python method, and every verdict about it says ``simulated``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import unittest
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Mapping
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest import mock

from novacontrol.novaforge.autonomy import (
    ACTIVE_PHASES,
    AUTONOMY_TERMINAL_PHASES,
    AUTONOMY_TRANSITIONS,
    AutonomyConflictError,
    AutonomyLimits,
    AutonomyOutcome,
    AutonomyPhase,
    AutonomyPolicy,
    AutonomyRun,
    AutonomyTransitionError,
    AutonomousSessionController,
    PHASES,
    objective_from_picture,
    terminal_phase_for,
)
from novacontrol.novaforge.simulation import (
    SIM_GAME,
    SIM_SESSION_ID,
    SIM_TITLE,
    MAX_SIM_PRESSES,
    SimulatedGame,
    SimulatedNovaForge,
    simulated_limits,
    simulated_policy,
)
from novacontrol.optimization.models import ResourceAssessment, ResourceLevel

# The HTTP fixture (a real application, a real manager, only the desktop seams
# replaced). Imported lazily-so-named because the web suite's own test case is
# what provides the isolated app and the session cookie.
try:  # pragma: no cover - httpx is an optional dev dependency
    from test_novaforge import FakeDesktop, _mission
    from test_novaforge_api import _ForgeApiFixture
    from test_web_api import (  # noqa: F401 - the fixture's base
        NoopBrowserRunner,
        NoopDesktopRunner,
        _IsolatedApiTestCase,
    )

except Exception:  # pragma: no cover - the HTTP suite is skipped without httpx
    _ForgeApiFixture = None  # type: ignore[assignment,misc]
    NoopBrowserRunner = None  # type: ignore[assignment,misc]
    NoopDesktopRunner = None  # type: ignore[assignment,misc]

SESSION = "nf22-script"

ROOT = Path(__file__).resolve().parents[1]
PANEL_JS = ROOT / "src/novacontrol/web/static/js/novaforge.js"
INDEX = ROOT / "src/novacontrol/web/static/index.html"


def objective_row(
    *,
    objective_id: str = "obj-1",
    status: str = "in_progress",
    description: str = "Collect the audit tokens",
    value: float = 0.0,
    target: float = 3.0,
) -> dict[str, Any]:
    return {
        "objective_id": objective_id,
        "description": description,
        "session_id": SESSION,
        "status": status,
        "explicit": True,
        "progress": {
            "kind": "numeric",
            "value": value,
            "target": target,
            "start": 0.0,
            "fraction": 0.0,
        },
        "completion_conditions": ["reach 3 on progress"],
        "failure_conditions": ["read 'mission failed'"],
        "evidence": [{"kind": "progress", "summary": "counter read", "observation_id": "o1"}],
    }


def picture(objective: Mapping[str, Any] | None = None, *, key: str = "objectives") -> dict[str, Any]:
    rows = [dict(objective)] if objective else []
    return {key: rows, "session_id": SESSION}


def observation_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "observation_id": f"{SESSION}:0001",
        "session_id": SESSION,
        "seq": 1,
        "game": "SIM",
        "captured_at": "2026-10-26T00:00:00+00:00",
        "status": "observed",
        "target": {"title": "SIM", "handle": 11, "pid": 22},
        "width": 640,
        "height": 360,
        "backend": "simulated",
        "fresh": True,
        "target_valid": True,
        "reason_code": "ok",
        "reason": "",
    }
    row.update(overrides)
    return row


def plan_row(
    *,
    steps: int = 1,
    status: str = "awaiting_approval",
    code: str = "",
    reason: str = "",
    plan_id: str = "plan-1",
) -> dict[str, Any]:
    return {
        "plan_id": plan_id,
        "session_id": SESSION,
        "status": status,
        "code": code,
        "reason": reason,
        "objective": {"objective_id": "obj-1", "description": "Collect the audit tokens"},
        "steps": [
            {
                "index": index + 1,
                "action_id": f"action-{index + 1}",
                "kind": "key_hold",
                "parameters": {"key": "w"},
                "description": "Hold W",
                "expected_effect": "the counter advances",
                "risk": "low",
                "reversible": True,
                "source": "declared rule",
            }
            for index in range(steps)
        ],
    }


def executed_plan_row(
    *,
    dispatched: int = 1,
    status: str = "executed",
    code: str = "all_steps_submitted",
    reason: str = "",
    plan_id: str = "plan-1",
) -> dict[str, Any]:
    row = plan_row(steps=max(1, dispatched), status=status, code=code, reason=reason, plan_id=plan_id)
    row["execution"] = {
        "steps": [
            {
                "index": index + 1,
                "action_id": f"action-{index + 1}",
                "kind": "key_hold",
                "ok": index < dispatched,
                "status": "completed" if index < dispatched else "rejected",
                "code": "" if index < dispatched else code or "gate_refused",
                "reason": "" if index < dispatched else reason,
            }
            for index in range(max(1, dispatched))
        ],
        "effect_verified": False,
    }
    return row


def verification_row(
    *,
    outcome_status: str = "verified_success",
    effect_verified: bool = True,
    recovery: str = "none",
    recovery_allowed: bool = True,
    code: str = "effect_observed",
    reason: str = "Every declared observable was satisfied.",
) -> dict[str, Any]:
    return {
        "status": "verified" if effect_verified else "unknown",
        "code": "",
        "reason": "",
        "effect_verified": effect_verified,
        "outcomes": [
            {
                "outcome_id": "outcome-1",
                "action_id": "action-1",
                "status": outcome_status,
                "code": code,
                "reason": reason,
                "expected_effect": "the counter advances by one",
                "observed_effect": "the counter advanced" if effect_verified else "",
                "confidence": 1.0 if effect_verified else None,
                "confidence_basis": "one accepted reading",
                "method": "declared observable",
                "observations": 1,
                "evidence": [{"ref": "state:s1", "observation_id": "o2", "accepted": True}],
                "rejected": [],
                "recovery": {
                    "action": recovery,
                    "allowed": recovery_allowed,
                    "reason": "recorded by NF-21",
                },
            }
        ],
    }


class DictStore:
    """The smallest persistence seam the controller accepts."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def read(self, namespace: str) -> dict[str, Any]:
        return dict(self.data.get(namespace) or {})

    def write(self, namespace: str, payload: Mapping[str, Any]) -> None:
        self.data[namespace] = dict(payload)


class ScriptedGateway:
    """A scripted session: hand-written answers for every stage of the loop."""

    def __init__(self, **options: Any) -> None:
        self.session_id = SESSION
        #: Every session id this gateway answers for. Two entries make the
        #: "two runs, one window" conflict reachable.
        self.sessions = tuple(options.get("sessions") or (SESSION,))
        self.objective = options.get("objective", objective_row())
        self.pictures: deque[dict[str, Any]] = deque(options.get("pictures") or [])
        #: One objective status per READING of the objective picture, so the
        #: objective progresses the way a game's would and no answer depends on how
        #: many times the loop happened to observe: the script says what each read
        #: sees, not how the supervisor is wired.
        self.statuses: list[str] = list(options.get("statuses") or [])
        self.picture_reads = 0
        self.objective_id = options.get("objective_id", "obj-1")
        self.observations: deque[dict[str, Any]] = deque(options.get("observations") or [])
        self.plan = options.get("plan", plan_row())
        #: The scene the state layer reports for this run. ``None`` keeps the
        #: original scene-less payload (a reading that named no scene), which is a
        #: different fact from a run whose scene was understood and then dropped.
        self.state_scene = options.get("state_scene")
        self.plan_after_execution = options.get("plan_after_execution")
        self.execution = options.get("execution", executed_plan_row())
        self.verification = options.get("verification", verification_row())
        self.verifications: deque[dict[str, Any]] = deque(options.get("verifications") or [])
        self.recovery = options.get(
            "recovery",
            {"applied": True, "code": "observed_again", "reason": "fresh reading taken", "result": None},
        )
        self.target_validated = options.get("target_validated", True)
        self.target = SimpleNamespace(
            title="SIM", handle=options.get("handle", 11), pid=options.get("pid", 22)
        )
        self.session_gone = options.get("session_gone", False)
        #: The session record answers this many times, then it is gone — how the
        #: "the game closed between approval and the first reading" case is driven.
        self.session_gone_after = options.get("session_gone_after")
        self.session_reads = 0
        #: When set, the first read waits until the test releases it — how the
        #: cancellation and pause paths get a run that is genuinely mid-cycle.
        self.block_reads = bool(options.get("block_reads", False))
        self.calls: list[str] = []
        self.executed_plans: list[dict[str, Any]] = []
        self.recovered: list[dict[str, Any]] = []
        self.stopped_with: str = ""
        self.paused = 0
        self.resumed = 0
        self.observations_taken = 0
        self._block = asyncio.Event()

    # ── read surface ──────────────────────────────────────────────────────
    def get(self, session_id: str) -> Any | None:
        self.session_reads += 1
        if self.session_gone or session_id not in self.sessions:
            return None
        if self.session_gone_after is not None and self.session_reads > self.session_gone_after:
            return None
        return SimpleNamespace(
            session_id=session_id,
            target=self.target,
            target_validated=self.target_validated,
            state="running",
        )

    async def observe_frame_once(self) -> dict[str, Any]:
        self.calls.append("observe")
        if self.block_reads:
            await self._block.wait()
        answer = self.observations.popleft() if self.observations else None
        if answer is not None and str(answer.get("status") or "") != "observed":
            return answer
        self.observations_taken += 1
        if answer is not None:
            return answer
        return observation_row(
            seq=self.observations_taken,
            observation_id=f"{SESSION}:{self.observations_taken:04d}",
        )

    async def read_state(self) -> dict[str, Any]:
        self.calls.append("read_state")
        state: dict[str, Any] = {"state_id": "s1", "status": "ok", "game": "SIM", "hud": []}
        if self.state_scene is not None:
            state["scene"] = dict(self.state_scene)
        return {"analyzed": True, "state": state}

    def objective_picture(self, session_id: str = "") -> dict[str, Any]:
        if self.pictures:
            return self.pictures.popleft()
        if self.statuses:
            index = max(0, min(len(self.statuses) - 1, self.picture_reads))
            self.picture_reads += 1
            return picture(
                objective_row(status=self.statuses[index], objective_id=self.objective_id)
            )
        return picture(self.objective)

    async def plan_actions(self, session_id: str) -> dict[str, Any]:
        self.calls.append("plan")
        plan = dict(self.plan)
        if self.plan_after_execution is not None and self.executed_plans:
            plan = dict(self.plan_after_execution)
        return {"session_id": SESSION, "plan": plan, "effect_verified": False}

    async def execute_plan(self, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append("execute")
        self.executed_plans.append(dict(payload))
        return {"session_id": SESSION, "plan": dict(self.execution), "effect_verified": False}

    async def verify_outcomes(self, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append("verify")
        verification = self.verifications.popleft() if self.verifications else self.verification
        return {
            "session_id": SESSION,
            "plan_id": str(payload.get("plan_id") or ""),
            "verification": dict(verification),
            "effect_verified": bool(dict(verification).get("effect_verified")),
        }

    async def recover_outcome(self, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append("recover")
        self.recovered.append(dict(payload))
        return dict(self.recovery)

    async def pause(self, session_id: str, *, reason: str = "") -> dict[str, Any]:
        self.paused += 1
        return {"session_id": SESSION, "paused": True, "reason": reason}

    async def resume(self, session_id: str) -> dict[str, Any]:
        self.resumed += 1
        return {"session_id": SESSION, "resumed": True}

    async def stop(self, session_id: str, *, reason: str = "") -> dict[str, Any]:
        self.stopped_with = reason
        return {"session_id": SESSION, "stopped": True, "cleanup": {"released": 1, "ok": True}}


def limits(**overrides: Any) -> AutonomyLimits:
    values: dict[str, Any] = {
        "max_cycles": 3,
        "max_actions": 3,
        "max_actions_per_plan": 3,
        "session_seconds": 30.0,
        "observation_attempts": 1,
        "recovery_attempts": 1,
        "replans": 1,
    }
    values.update(overrides)
    # A per-plan budget above the whole run's action budget is refused by design;
    # a test that tightens the action budget means the per-plan one with it.
    values["max_actions_per_plan"] = min(values["max_actions_per_plan"], values["max_actions"])
    return AutonomyLimits(**values)


def policy(**overrides: Any) -> AutonomyPolicy:
    values: dict[str, Any] = {"require_approval": True, "plan_approval": "run"}
    values.update(overrides)
    return AutonomyPolicy(**values)


async def settle(times: int = 8) -> None:
    """Let the run's background tasks (the session stop) finish."""
    for _ in range(times):
        await asyncio.sleep(0)


class VocabularyTests(TestCase):
    """The phase's vocabulary: every phase, one legal map, no exit from an ending."""

    def test_every_phase_is_a_key_and_every_target_is_a_phase(self) -> None:
        self.assertEqual(sorted(AUTONOMY_TRANSITIONS), sorted(AutonomyPhase))
        for source, targets in AUTONOMY_TRANSITIONS.items():
            for target in targets:
                self.assertIn(target, AutonomyPhase, f"{source} -> {target}")

    def test_the_terminal_phases_have_no_exit(self) -> None:
        for phase in AUTONOMY_TERMINAL_PHASES:
            self.assertEqual(AUTONOMY_TRANSITIONS[phase], frozenset(), phase)

    def test_the_paused_phase_reenters_at_observation(self) -> None:
        self.assertEqual(
            AUTONOMY_TRANSITIONS[AutonomyPhase.PAUSED],
            frozenset({AutonomyPhase.OBSERVING, AutonomyPhase.CANCELLED, AutonomyPhase.FAILED}),
        )

    def test_active_and_terminal_phases_partition_the_vocabulary(self) -> None:
        self.assertEqual(ACTIVE_PHASES & AUTONOMY_TERMINAL_PHASES, frozenset())
        self.assertEqual(ACTIVE_PHASES | AUTONOMY_TERMINAL_PHASES | {AutonomyPhase.IDLE, AutonomyPhase.PAUSED},
                         frozenset(AutonomyPhase))
        self.assertEqual(set(PHASES), {item.value for item in AutonomyPhase})

    def test_every_outcome_names_a_terminal_phase(self) -> None:
        for outcome in AutonomyOutcome:
            self.assertIn(terminal_phase_for(outcome), AUTONOMY_TERMINAL_PHASES)

    def test_a_limit_is_not_reported_as_a_failed_objective(self) -> None:
        self.assertIs(terminal_phase_for(AutonomyOutcome.RESOURCE_LIMIT_REACHED), AutonomyPhase.FAILED)
        self.assertIs(terminal_phase_for(AutonomyOutcome.OBJECTIVE_COMPLETED), AutonomyPhase.COMPLETED)
        self.assertIs(terminal_phase_for(AutonomyOutcome.SESSION_CANCELLED), AutonomyPhase.CANCELLED)

    def test_an_illegal_move_raises_and_leaves_the_state_alone(self) -> None:
        controller = AutonomousSessionController(gateway=ScriptedGateway())
        run = AutonomyRun(run_id="r", session_id=SESSION, phase=AutonomyPhase.IDLE, evidence_source="simulated")
        with self.assertRaises(AutonomyTransitionError):
            controller._record(run, AutonomyPhase.EXECUTING)
        self.assertIs(run.phase, AutonomyPhase.IDLE)
        controller._record(run, AutonomyPhase.INITIALIZING)
        self.assertIs(run.phase, AutonomyPhase.INITIALIZING)

    def test_limits_refuse_a_widened_bound_rather_than_clamping_it(self) -> None:
        problems = limits(max_actions=99).errors()
        self.assertTrue(any("max_actions" in problem for problem in problems))
        with self.assertRaises(ValueError):
            AutonomousSessionController(gateway=ScriptedGateway(), limits=limits(max_actions=99))

    def test_a_plan_budget_above_the_action_budget_is_refused(self) -> None:
        # Built directly: the ``limits`` helper clamps this pair for the other
        # tests, so the rule under test has to be stated without its help.
        problems = AutonomyLimits(max_actions=1, max_actions_per_plan=2).errors()
        self.assertTrue(any("must not exceed" in problem for problem in problems))

    def test_the_objective_reader_accepts_both_picture_spellings(self) -> None:
        row = objective_row()
        self.assertEqual(objective_from_picture(picture(row))["objective_id"], "obj-1")
        self.assertEqual(
            objective_from_picture({"current": picture(row)})["objective_id"], "obj-1"
        )
        self.assertEqual(objective_from_picture({}), {})


class LifecycleTests(IsolatedAsyncioTestCase):
    """Starting, pausing, resuming, cancelling and shutting a run down."""

    async def controller(self, gateway: ScriptedGateway | None = None, **options: Any):
        gateway = gateway or ScriptedGateway()
        controller = AutonomousSessionController(
            gateway=gateway,
            limits=options.pop("limits", limits()),
            policy=options.pop("policy", policy()),
            **options,
        )
        self.addAsyncCleanup(controller.shutdown)
        return controller, gateway

    async def test_an_unapproved_run_does_not_start(self) -> None:
        controller, gateway = await self.controller()
        answer = await controller.start(SESSION, approved=False)
        self.assertFalse(answer["started"])
        self.assertEqual(answer["code"], "approval_required")
        self.assertEqual(gateway.calls, [])
        self.assertEqual(controller.status(SESSION)["runs"], [])

    async def test_an_unknown_session_is_refused(self) -> None:
        controller, _ = await self.controller()
        with self.assertRaises(ValueError):
            await controller.start("somebody-else", approved=True)

    async def test_a_second_run_on_the_same_session_is_refused(self) -> None:
        gateway = ScriptedGateway(block_reads=True)
        controller, _ = await self.controller(gateway)
        first = await controller.start(SESSION, approved=True)
        self.assertTrue(first["started"])
        second = await controller.start(SESSION, approved=True)
        self.assertFalse(second["started"])
        self.assertEqual(second["code"], "already_running")
        await controller.cancel(SESSION)

    async def test_two_runs_may_not_drive_the_same_window(self) -> None:
        gateway = ScriptedGateway(block_reads=True, sessions=(SESSION, "nf22-other"))
        controller, _ = await self.controller(gateway)
        await controller.start(SESSION, approved=True)
        await settle(2)
        # Both sessions pin handle 11 / pid 22: one window, one driver.
        with self.assertRaises(AutonomyConflictError):
            await controller.start("nf22-other", approved=True)
        await controller.cancel(SESSION)

    async def test_a_second_window_of_the_same_game_is_refused_too(self) -> None:
        # Two windows of the same game is still one game to play: a second run
        # would be acting on the same objective, so the title alone is enough.
        gateway = ScriptedGateway(block_reads=True, sessions=(SESSION, "nf22-other"))
        controller, _ = await self.controller(gateway)
        await controller.start(SESSION, approved=True)
        await settle(2)
        gateway.target = SimpleNamespace(title="SIM", handle=99, pid=88)
        with self.assertRaises(AutonomyConflictError):
            await controller.start("nf22-other", approved=True)
        await controller.cancel(SESSION)

    async def test_a_cancelled_run_ends_with_its_own_outcome(self) -> None:
        gateway = ScriptedGateway(block_reads=True)
        controller, _ = await self.controller(gateway)
        await controller.start(SESSION, approved=True)
        await settle(2)
        await controller.cancel(SESSION, reason="the operator changed their mind")
        run = controller.run_for(SESSION)
        self.assertIsNotNone(run)
        self.assertIs(run.phase, AutonomyPhase.CANCELLED)
        self.assertEqual(run.outcome, AutonomyOutcome.SESSION_CANCELLED.value)
        self.assertEqual(gateway.executed_plans, [])

    async def test_a_cancelled_run_never_dispatches_afterwards(self) -> None:
        gateway = ScriptedGateway(block_reads=True)
        controller, _ = await self.controller(gateway)
        await controller.start(SESSION, approved=True)
        await settle(2)
        await controller.cancel(SESSION)
        calls = list(gateway.calls)
        await settle(6)
        self.assertEqual(gateway.calls, calls)

    async def test_a_finished_run_is_never_restarted(self) -> None:
        controller, _ = await self.controller()
        await controller.start(SESSION, approved=True)
        await controller.wait(SESSION)
        answer = await controller.resume(SESSION)
        self.assertFalse(answer["resumed"])
        self.assertEqual(answer["code"], "run_finished")

    async def test_pause_and_resume_go_through_the_session(self) -> None:
        gateway = ScriptedGateway(block_reads=True)
        controller, _ = await self.controller(gateway)
        await controller.start(SESSION, approved=True)
        await settle(2)
        paused = await controller.pause(SESSION, reason="checking the screen")
        self.assertTrue(paused["paused"])
        self.assertEqual(gateway.paused, 1)
        self.assertIs(controller.run_for(SESSION).phase, AutonomyPhase.PAUSED)
        resumed = await controller.resume(SESSION)
        self.assertTrue(resumed["resumed"])
        self.assertEqual(gateway.resumed, 1)
        await controller.cancel(SESSION)

    async def test_the_session_is_stopped_when_the_run_ends(self) -> None:
        gateway = ScriptedGateway()
        controller, _ = await self.controller(gateway)
        await controller.start(SESSION, approved=True)
        await controller.wait(SESSION)
        await settle(10)
        self.assertIn("autonomous run ended", gateway.stopped_with)
        run = controller.run_for(SESSION)
        self.assertEqual(run.session_ended["cleanup"]["ok"], True)

    async def test_shutdown_stops_every_run_it_owns(self) -> None:
        gateway = ScriptedGateway(block_reads=True)
        controller, _ = await self.controller(gateway, cancel_grace_seconds=0.05)
        await controller.start(SESSION, approved=True)
        await settle(2)
        answer = await controller.shutdown()
        self.assertEqual(answer["stopped"], [SESSION])
        self.assertIs(controller.run_for(SESSION).phase, AutonomyPhase.CANCELLED)
        # The release is not merely QUEUED: shutdown returns only once the
        # session's own stop ran, and says so. A pending stop task that the
        # process exits underneath is exactly how held input outlives a shutdown.
        self.assertTrue(answer["released"])
        self.assertIn("session_cancelled", gateway.stopped_with)

    async def test_shutdown_reports_a_stop_that_never_answered(self) -> None:
        class HangingStop(ScriptedGateway):
            async def stop(self, session_id: str, *, reason: str = "") -> dict[str, Any]:
                await asyncio.sleep(30)
                raise AssertionError("the bound should have fired first")

        controller, _ = await self.controller(
            HangingStop(block_reads=True), cancel_grace_seconds=0.05
        )
        await controller.start(SESSION, approved=True)
        await settle(2)
        answer = await controller.shutdown(timeout=0.05)
        # A dependency that never answers is reported, never hidden, and never
        # allowed to turn the shutdown into a hang.
        self.assertFalse(answer["released"])
        self.assertEqual(answer["stopped"], [SESSION])

    async def test_the_clock_bound_ends_a_run_that_overstays_it(self) -> None:
        ticks = {"now": 0.0}

        def clock() -> float:
            ticks["now"] += 5.0
            return ticks["now"]

        gateway = ScriptedGateway(block_reads=True)
        controller, _ = await self.controller(gateway, clock=clock, limits=limits(session_seconds=6.0))
        await controller.start(SESSION, approved=True)
        gateway._block.set()
        await controller.wait(SESSION)
        run = controller.run_for(SESSION)
        self.assertEqual(run.outcome, AutonomyOutcome.SESSION_TIMED_OUT.value)
        self.assertIn("session bound", run.ended_reason)

    async def test_a_session_that_closes_before_the_first_reading_fails_the_check(self) -> None:
        # The session answers once (the approval), then the game is gone.
        gateway = ScriptedGateway(session_gone_after=1)
        controller, _ = await self.controller(gateway)
        await controller.start(SESSION, approved=True)
        await controller.wait(SESSION)
        run = controller.run_for(SESSION)
        self.assertEqual(run.outcome, AutonomyOutcome.DEPENDENCY_FAILURE.value)
        self.assertEqual(gateway.executed_plans, [])

    async def test_an_unvalidated_target_ends_the_run_before_it_acts(self) -> None:
        gateway = ScriptedGateway(target_validated=False)
        controller, _ = await self.controller(gateway)
        await controller.start(SESSION, approved=True)
        await controller.wait(SESSION)
        run = controller.run_for(SESSION)
        self.assertEqual(run.outcome, AutonomyOutcome.DEPENDENCY_FAILURE.value)
        self.assertIn("validated a target", run.ended_reason)
        self.assertEqual(gateway.executed_plans, [])

    async def test_the_run_records_an_approval_note_and_its_evidence_source(self) -> None:
        controller, _ = await self.controller(evidence_source="simulated")
        answer = await controller.start(SESSION, approved=True, note="NF-22 acceptance")
        self.assertEqual(answer["evidence_source"], "simulated")
        run = controller.run_for(SESSION)
        self.assertTrue(run.approved)
        self.assertEqual(run.approved_note, "NF-22 acceptance")
        self.assertTrue(run.approved_at)


class OrchestrationTests(IsolatedAsyncioTestCase):
    """One cycle: observe, understand, identify, plan, approve, act, verify, decide."""

    async def run_once(self, gateway: ScriptedGateway, **options: Any):
        controller = AutonomousSessionController(
            gateway=gateway,
            limits=options.pop("limits", limits()),
            policy=options.pop("policy", policy()),
            **options,
        )
        self.addAsyncCleanup(controller.shutdown)
        started = await controller.start(SESSION, approved=True)
        await controller.wait(SESSION)
        return controller, controller.run_for(SESSION), started

    async def test_a_verified_success_continues_and_completion_ends_the_run(self) -> None:
        gateway = ScriptedGateway(statuses=["in_progress", "in_progress", "completed"])
        controller, run, _ = await self.run_once(gateway)
        self.assertEqual(run.outcome, AutonomyOutcome.OBJECTIVE_COMPLETED.value)
        self.assertIs(run.phase, AutonomyPhase.COMPLETED)
        self.assertEqual(len(run.cycles), 2)
        first = run.cycles[0]
        self.assertEqual(first.verification["outcomes"][0]["status"], "verified_success")
        self.assertEqual(run.actions_dispatched, 1)
        self.assertEqual(run.verification_evidence, ["state:s1", "o2"])

    async def test_the_cycle_records_the_scene_the_state_layer_understood(self) -> None:
        """NF-25's `scene:` evidence and NF-26's transitions read this exact row.

        The scene used to be dropped when the cycle's state row was written, so a
        task whose required evidence named a screen could never satisfy it on a
        real run — and the learning layer recorded every transition's scene as
        "unknown" although the state layer had named it.
        """
        gateway = ScriptedGateway(
            statuses=["completed"],
            state_scene={"kind": "menu", "status": "observed", "evidence": ["GAME MENU"]},
        )
        _, run, _ = await self.run_once(gateway)
        scene = run.cycles[0].state["scene"]
        self.assertEqual(scene["kind"], "menu")
        self.assertEqual(scene["status"], "observed")
        self.assertEqual(scene["evidence"], ["GAME MENU"])
        # A payload without a scene still records none, rather than inventing one.
        plain = ScriptedGateway(statuses=["completed"])
        _, bare, _ = await self.run_once(plain)
        self.assertNotIn("scene", bare.cycles[0].state)

    async def test_the_cycle_records_the_knowledge_the_plan_used(self) -> None:
        """NF-18's retrieved records travel from the plan into the session trace.

        The field once held the objective's own evidence rows, stringified into
        blank entries — an audit trail that named no knowledge at all. It is the
        PLAN's own ``knowledge_refs`` now, and a blank is dropped rather than
        recorded as if it were a reference.
        """
        gateway = ScriptedGateway(
            plan={**plan_row(), "knowledge_refs": ["knowledge:k1", ""]},
            statuses=["in_progress", "in_progress", "completed"],
        )
        _, run, _ = await self.run_once(gateway)
        self.assertEqual(run.cycles[0].knowledge_refs, ["knowledge:k1"])
        self.assertEqual(run.cycles[0].to_dict()["knowledge_refs"], ["knowledge:k1"])

    async def test_every_stage_is_recorded_in_order(self) -> None:
        controller, run, _ = await self.run_once(
            ScriptedGateway(statuses=["in_progress", "in_progress", "completed"])
        )
        phases = [step.phase for step in run.steps]
        self.assertEqual(phases[0], AutonomyPhase.INITIALIZING.value)
        for expected in (
            AutonomyPhase.IDENTIFYING_OBJECTIVE.value,
            AutonomyPhase.OBSERVING.value,
            AutonomyPhase.UNDERSTANDING_STATE.value,
            AutonomyPhase.PLANNING.value,
            AutonomyPhase.EXECUTING.value,
            AutonomyPhase.VERIFYING.value,
        ):
            self.assertIn(expected, phases, phases)

    async def test_the_execution_always_carries_the_approval(self) -> None:
        gateway = ScriptedGateway(statuses=["in_progress", "in_progress", "completed"])
        _, run, _ = await self.run_once(gateway)
        self.assertTrue(gateway.executed_plans)
        self.assertIs(gateway.executed_plans[0]["approved"], True)
        self.assertEqual(run.cycles[0].approval["mode"], "run")
        self.assertTrue(run.cycles[0].approval["approved"])

    async def test_a_plan_at_the_approved_scope_runs_and_a_bigger_one_stops_the_run(self) -> None:
        gateway = ScriptedGateway(plan=plan_row(steps=2))
        controller, run, _ = await self.run_once(gateway, limits=limits(max_actions_per_plan=1))
        self.assertEqual(run.outcome, AutonomyOutcome.RESOURCE_LIMIT_REACHED.value)
        self.assertIn("approved per-plan budget", run.ended_reason)
        self.assertEqual(gateway.executed_plans, [])

    async def test_a_missing_observation_ends_the_run_as_observation_unavailable(self) -> None:
        gateway = ScriptedGateway(
            observations=deque(
                [
                    observation_row(status="unavailable", reason_code="capture_failed", reason="no frame"),
                ]
            )
        )
        controller, run, _ = await self.run_once(gateway)
        self.assertEqual(run.outcome, AutonomyOutcome.OBSERVATION_UNAVAILABLE.value)
        self.assertIn("capture_failed", run.ended_reason)

    async def test_a_frame_from_another_session_stops_the_run(self) -> None:
        gateway = ScriptedGateway(
            observations=deque([observation_row(session_id="someone-else")])
        )
        _, run, _ = await self.run_once(gateway)
        self.assertEqual(run.outcome, AutonomyOutcome.SAFETY_STOP.value)
        self.assertIn("another session's evidence", run.ended_reason)

    async def test_a_frame_from_a_replaced_window_stops_the_run(self) -> None:
        gateway = ScriptedGateway(
            observations=deque(
                [observation_row(target={"title": "SIM", "handle": 99, "pid": 22})]
            )
        )
        _, run, _ = await self.run_once(gateway)
        self.assertEqual(run.outcome, AutonomyOutcome.SAFETY_STOP.value)
        self.assertIn("another window", run.ended_reason)

    async def test_a_target_that_cannot_be_seen_stops_the_run(self) -> None:
        gateway = ScriptedGateway(
            observations=deque(
                [
                    observation_row(
                        status="rejected",
                        reason_code="target_minimized",
                        reason="the window is minimized",
                    )
                ]
            )
        )
        _, run, _ = await self.run_once(gateway)
        self.assertEqual(run.outcome, AutonomyOutcome.SAFETY_STOP.value)
        self.assertIn("cannot be seen", run.ended_reason)

    async def test_a_busy_observation_is_waited_for_not_counted_as_a_failure(self) -> None:
        # The dependency check reads once; the cycle's first read is busy, and the
        # retry gets a real frame. A busy answer is a wait, never a failure.
        gateway = ScriptedGateway(
            observations=deque(
                [
                    observation_row(),
                    {"status": "busy", "reason_code": "capture_in_flight", "reason": "a capture is running"},
                    observation_row(seq=3, observation_id=f"{SESSION}:0003"),
                ]
            ),
            statuses=["in_progress", "in_progress", "completed"],
        )
        _, run, _ = await self.run_once(gateway, limits=limits(observation_attempts=2))
        self.assertEqual(run.outcome, AutonomyOutcome.OBJECTIVE_COMPLETED.value)

    async def test_an_unreadable_state_ends_the_run_rather_than_looping(self) -> None:
        class Unreadable(ScriptedGateway):
            async def read_state(self) -> dict[str, Any]:
                return {"analyzed": False, "reason_code": "observer_unavailable", "reason": "the observer is gone"}

        controller, run, _ = await self.run_once(Unreadable())
        self.assertEqual(run.outcome, AutonomyOutcome.DEPENDENCY_FAILURE.value)
        self.assertIn("observer is gone", run.ended_reason)

    async def test_a_stale_state_ends_the_run_as_observation_unavailable(self) -> None:
        class Stale(ScriptedGateway):
            async def read_state(self) -> dict[str, Any]:
                return {"analyzed": False, "reason_code": "observation_stale", "reason": "the frame is old"}

        _, run, _ = await self.run_once(Stale())
        self.assertEqual(run.outcome, AutonomyOutcome.OBSERVATION_UNAVAILABLE.value)

    async def test_a_game_with_no_interpretable_objective_is_refused_before_any_action(self) -> None:
        gateway = ScriptedGateway(objective={})
        controller, run, _ = await self.run_once(gateway)
        self.assertEqual(run.outcome, AutonomyOutcome.DEPENDENCY_FAILURE.value)
        self.assertIn("No interpretable objective", run.ended_reason)
        self.assertEqual(gateway.executed_plans, [])
        self.assertEqual(run.objective_id, "")

    async def test_the_completion_criteria_are_pinned_before_the_first_cycle(self) -> None:
        gateway = ScriptedGateway(
            pictures=deque([picture(objective_row(status="completed"))])
        )
        _, run, _ = await self.run_once(gateway)
        self.assertEqual(run.objective_id, "obj-1")
        self.assertEqual(run.completion_criteria, ["reach 3 on progress"])
        self.assertEqual(run.failure_criteria, ["read 'mission failed'"])

    async def test_a_different_objective_ends_the_run(self) -> None:
        gateway = ScriptedGateway(
            pictures=deque(
                [
                    picture(objective_row(objective_id="obj-1")),
                    picture(objective_row(objective_id="obj-2", description="Something else")),
                ]
            )
        )
        _, run, _ = await self.run_once(gateway)
        self.assertEqual(run.outcome, AutonomyOutcome.OUTCOME_UNKNOWN.value)
        self.assertIn("objective changed", run.ended_reason)

    async def test_a_planner_refusal_is_mapped_to_its_own_outcome(self) -> None:
        cases = {
            "stale_state": AutonomyOutcome.OBSERVATION_UNAVAILABLE,
            "gate_closed": AutonomyOutcome.SAFETY_STOP,
            "no_action_budget": AutonomyOutcome.RESOURCE_LIMIT_REACHED,
            "no_plan_rule": AutonomyOutcome.OUTCOME_UNKNOWN,
            "planning_timeout": AutonomyOutcome.DEPENDENCY_FAILURE,
            # NF-20's own counter reading is not a verdict: NF-17 owns completion,
            # so a plan refused for "progress is at target" is an unknown outcome.
            "objective_progress_complete": AutonomyOutcome.OUTCOME_UNKNOWN,
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                gateway = ScriptedGateway(plan=plan_row(status="insufficient_information", code=code, reason=f"{code} happened"))
                controller, run, _ = await self.run_once(gateway)
                self.assertEqual(run.outcome, expected.value, run.ended_reason)

    async def test_a_plan_with_no_steps_is_not_executed(self) -> None:
        gateway = ScriptedGateway(plan=plan_row(status="no_action", code="no_feasible_action", steps=0))
        _, run, _ = await self.run_once(gateway)
        self.assertEqual(run.outcome, AutonomyOutcome.OUTCOME_UNKNOWN.value)
        self.assertEqual(gateway.executed_plans, [])

    async def test_a_refused_execution_stops_without_claiming_progress(self) -> None:
        gateway = ScriptedGateway(
            execution=executed_plan_row(dispatched=0, status="stopped", code="gate_refused", reason="the gate refused")
        )
        _, run, _ = await self.run_once(gateway)
        self.assertEqual(run.outcome, AutonomyOutcome.SAFETY_STOP.value)
        self.assertEqual(run.actions_dispatched, 0)

    async def test_a_plan_that_claims_to_be_runnable_and_has_no_steps_is_only_unknown(self) -> None:
        """An empty plan is not evidence that the objective failed.

        A plan that answers "planned" with no steps proposed nothing and sent
        nothing, so nothing about the objective was established. Reporting
        OBJECTIVE_FAILED here would claim a failure no reading ever showed.
        """
        gateway = ScriptedGateway(plan=plan_row(status="planned", steps=0))
        _, run, _ = await self.run_once(gateway)
        self.assertEqual(run.outcome, AutonomyOutcome.OUTCOME_UNKNOWN.value)
        self.assertIsNot(run.phase, AutonomyPhase.COMPLETED)
        self.assertEqual(gateway.executed_plans, [])

    async def test_a_plan_the_control_layer_does_not_know_is_never_an_objective_failure(self) -> None:
        """A stage mismatch is not evidence about the game.

        NF-19 answers ``unknown_plan`` when it does not hold the plan this run
        picked up, and ``no_steps`` when the plan it was given has nothing to run.
        Neither says anything about the objective: one is a dependency that is not
        where the run believed it was, the other is an unknown outcome. Nothing was
        sent in either case, and no verdict on the objective was earned.
        """
        cases = {
            "unknown_plan": AutonomyOutcome.DEPENDENCY_FAILURE,
            "no_steps": AutonomyOutcome.OUTCOME_UNKNOWN,
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                gateway = ScriptedGateway(
                    plan=plan_row(status="planned"),
                    execution=executed_plan_row(
                        dispatched=0, status="rejected", code=code, reason=f"{code} happened"
                    ),
                )
                _, run, _ = await self.run_once(gateway)
                self.assertEqual(run.outcome, expected.value, run.ended_reason)
                self.assertNotEqual(run.outcome, AutonomyOutcome.OBJECTIVE_FAILED.value)
                self.assertEqual(run.actions_dispatched, 0)
                self.assertEqual(run.verification_evidence, [])

    async def test_a_verified_failure_is_not_a_success(self) -> None:
        gateway = ScriptedGateway(
            verification=verification_row(
                outcome_status="verified_failure",
                effect_verified=False,
                recovery="invalidate_plan",
                code="effect_absent",
                reason="the counter did not advance",
            ),
            statuses=["in_progress", "in_progress", "completed"],
        )
        _, run, _ = await self.run_once(gateway, limits=limits(recovery_attempts=0))
        self.assertNotEqual(run.outcome, AutonomyOutcome.OBJECTIVE_COMPLETED.value)
        self.assertIn("recovery budget", run.ended_reason)

    async def test_an_unknown_outcome_never_repeats_the_action(self) -> None:
        gateway = ScriptedGateway(
            verification=verification_row(
                outcome_status="outcome_unknown",
                effect_verified=False,
                recovery="none",
                code="evidence_insufficient",
                reason="nothing established the effect",
            )
        )
        _, run, _ = await self.run_once(gateway)
        self.assertEqual(run.outcome, AutonomyOutcome.OUTCOME_UNKNOWN.value)
        self.assertEqual(len(gateway.executed_plans), 1)
        self.assertEqual(gateway.recovered, [])

    async def test_a_stop_decision_ends_the_run_instead_of_retrying(self) -> None:
        gateway = ScriptedGateway(
            verification=verification_row(
                outcome_status="verified_failure",
                effect_verified=False,
                recovery="stop_operation",
                code="unsafe",
                reason="the target is not the one the plan was made for",
            )
        )
        _, run, _ = await self.run_once(gateway)
        self.assertEqual(run.outcome, AutonomyOutcome.SAFETY_STOP.value)
        self.assertEqual(len(gateway.executed_plans), 1)
        self.assertEqual(gateway.recovered, [])

    async def test_an_escalation_ends_the_run_and_names_the_operator(self) -> None:
        gateway = ScriptedGateway(
            verification=verification_row(
                outcome_status="outcome_unknown",
                effect_verified=False,
                recovery="request_operator",
                code="needs_a_person",
                reason="the reading is ambiguous",
            )
        )
        _, run, _ = await self.run_once(gateway)
        self.assertEqual(run.outcome, AutonomyOutcome.OUTCOME_UNKNOWN.value)
        self.assertIn("escalated", run.ended_reason)
        self.assertEqual(len(gateway.executed_plans), 1)

    async def test_an_unchanged_reading_never_repeats_an_unknown_action(self) -> None:
        """The phase's rule: never repeat an action whose outcome is unknown.

        The recorded recovery IS applied — a fresh read was asked for — but the
        world answers with the reading it already gave: same observation, same
        state. Nothing new arrived to act on, so the run ends as an unknown
        outcome with ONE dispatch instead of pressing the same key again. The
        counterpart is ``test_a_recorded_recovery_is_applied_and_the_run_
        continues`` below: when the re-read DOES bring a new reading, the attempt
        is earned and the run acts again.
        """
        gateway = ScriptedGateway(
            observations=deque([observation_row() for _ in range(3)]),
            verification=verification_row(
                outcome_status="outcome_unknown",
                effect_verified=False,
                recovery="refresh_observation",
                code="stale_reading",
                reason="the post-action frame was stale",
            ),
            statuses=["in_progress", "in_progress", "in_progress"],
        )
        controller, run, _ = await self.run_once(gateway, limits=limits(recovery_attempts=2))
        self.assertEqual(run.outcome, AutonomyOutcome.OUTCOME_UNKNOWN.value)
        self.assertEqual(len(gateway.executed_plans), 1)
        self.assertEqual(run.actions_dispatched, 1)
        # The recovery itself was carried out before the repeat was refused.
        self.assertEqual(run.recoveries, 1)
        self.assertEqual(gateway.recovered[0]["outcome_id"], "outcome-1")
        self.assertIn("same reading", run.ended_reason)
        self.assertEqual(
            run.unresolved_evidence, [f"{SESSION}:0001", "s1"]
        )

    async def test_a_recorded_recovery_is_applied_and_the_run_continues(self) -> None:
        gateway = ScriptedGateway(
            verification=verification_row(
                outcome_status="outcome_unknown",
                effect_verified=False,
                recovery="refresh_observation",
                code="stale_reading",
                reason="the post-action frame was stale",
            ),
            # The first verdict is unknown and NF-21 asks for a fresh reading; the
            # re-read confirms the effect. The recovered cycle plans and acts AGAIN
            # before the objective completes: recovery continues the run, it does
            # not end it.
            verifications=deque(
                [
                    verification_row(
                        outcome_status="outcome_unknown",
                        effect_verified=False,
                        recovery="refresh_observation",
                        code="stale_reading",
                        reason="the post-action frame was stale",
                    ),
                    verification_row(),
                ]
            ),
            statuses=["in_progress", "in_progress", "in_progress", "completed"],
        )
        controller, run, _ = await self.run_once(gateway, limits=limits(recovery_attempts=1, max_cycles=3))
        self.assertEqual(run.recoveries, 1)
        self.assertEqual(gateway.recovered[0]["outcome_id"], "outcome-1")
        self.assertEqual(run.outcome, AutonomyOutcome.OBJECTIVE_COMPLETED.value)
        self.assertEqual(len(gateway.executed_plans), 2)

    async def test_a_recovery_that_was_not_allowed_is_not_carried_out(self) -> None:
        gateway = ScriptedGateway(
            verification=verification_row(
                outcome_status="outcome_unknown",
                effect_verified=False,
                recovery="replan",
                recovery_allowed=False,
                code="bound_reached",
                reason="the replan bound was already spent",
            )
        )
        _, run, _ = await self.run_once(gateway)
        self.assertEqual(run.outcome, AutonomyOutcome.OUTCOME_UNKNOWN.value)
        self.assertEqual(gateway.recovered, [])

    async def test_a_recovery_that_fails_is_reported_rather_than_retried(self) -> None:
        gateway = ScriptedGateway(
            verification=verification_row(
                outcome_status="outcome_unknown",
                effect_verified=False,
                recovery="refresh_observation",
                code="stale_reading",
                reason="the post-action frame was stale",
            ),
            recovery={"applied": False, "code": "recovery_not_allowed", "reason": "its own bound was reached"},
        )
        _, run, _ = await self.run_once(gateway)
        self.assertEqual(run.outcome, AutonomyOutcome.OUTCOME_UNKNOWN.value)
        self.assertIn("could not be carried out", run.ended_reason)
        self.assertEqual(len(gateway.recovered), 1)

    async def test_the_recovery_budget_is_enforced_across_cycles(self) -> None:
        gateway = ScriptedGateway(
            verification=verification_row(
                outcome_status="outcome_unknown",
                effect_verified=False,
                recovery="refresh_observation",
                code="stale_reading",
                reason="the post-action frame was stale",
            )
        )
        _, run, _ = await self.run_once(gateway, limits=limits(recovery_attempts=1, max_cycles=4))
        self.assertEqual(run.recoveries, 1)
        self.assertEqual(run.outcome, AutonomyOutcome.OUTCOME_UNKNOWN.value)
        self.assertIn("recovery budget", run.ended_reason)

    async def await_phase(self, controller: AutonomousSessionController, *phases: AutonomyPhase) -> AutonomyPhase:
        """Poll (yielding to the loop) until the run reaches one of ``phases``."""
        seen = AutonomyPhase.IDLE
        for _ in range(1000):
            run = controller.run_for(SESSION)
            self.assertIsNotNone(run)
            seen = run.phase
            if seen in phases:
                return seen
            await asyncio.sleep(0.01)
        self.fail(f"the run never reached {[row.value for row in phases]}; it is in {seen.value}")

    async def test_a_per_plan_policy_parks_the_run_until_it_is_answered(self) -> None:
        gateway = ScriptedGateway(statuses=["in_progress", "in_progress", "completed"])
        controller = AutonomousSessionController(
            gateway=gateway,
            limits=limits(),
            policy=policy(plan_approval="per_plan"),
        )
        self.addAsyncCleanup(controller.shutdown)
        await controller.start(SESSION, approved=True)
        await self.await_phase(controller, AutonomyPhase.WAITING_FOR_APPROVAL)
        # The approval is the gate: nothing is dispatched while it is unanswered.
        self.assertEqual(gateway.executed_plans, [])
        # One answer covers ONE plan. The loop answers each park the way an
        # operator would, up to the point where the run reaches its own ending —
        # an approval that silently covered the next plan would never park again.
        parks = 0
        for _ in range(4):
            answer = await controller.approve_plan(SESSION, approved=True, note="looks right")
            self.assertTrue(answer["approved"])
            phase = await self.await_phase(
                controller, AutonomyPhase.WAITING_FOR_APPROVAL, *AUTONOMY_TERMINAL_PHASES
            )
            if phase is AutonomyPhase.WAITING_FOR_APPROVAL:
                parks += 1
                # A second park is a second request, never a second dispatch.
                self.assertLessEqual(len(gateway.executed_plans), parks)
                continue
            break
        finished = await controller.wait(SESSION, timeout=10)
        self.assertTrue(finished["finished"], finished)
        self.assertTrue(gateway.executed_plans)

    async def test_a_refused_plan_is_not_executed(self) -> None:
        gateway = ScriptedGateway(statuses=["in_progress", "in_progress", "completed"])
        controller = AutonomousSessionController(
            gateway=gateway,
            limits=limits(),
            policy=policy(plan_approval="per_plan"),
        )
        self.addAsyncCleanup(controller.shutdown)
        await controller.start(SESSION, approved=True)
        for _ in range(20):
            await asyncio.sleep(0.01)
            if controller.run_for(SESSION).phase is AutonomyPhase.WAITING_FOR_APPROVAL:
                break
        await controller.approve_plan(SESSION, approved=False, note="not this one")
        await controller.wait(SESSION)
        self.assertEqual(gateway.executed_plans, [])
        run = controller.run_for(SESSION)
        self.assertIs(run.phase, AutonomyPhase.CANCELLED)
        self.assertEqual(run.outcome, AutonomyOutcome.SESSION_CANCELLED.value)


class LimitTests(IsolatedAsyncioTestCase):
    """The bounds: they are enforced, and reaching one is reported as itself."""

    async def drive(self, gateway: ScriptedGateway, **options: Any):
        controller = AutonomousSessionController(
            gateway=gateway,
            limits=options.pop("limits", limits()),
            policy=options.pop("policy", policy()),
            **options,
        )
        self.addAsyncCleanup(controller.shutdown)
        await controller.start(SESSION, approved=True)
        await controller.wait(SESSION)
        return controller, controller.run_for(SESSION)

    async def test_the_action_budget_stops_a_run_and_says_so(self) -> None:
        gateway = ScriptedGateway(
            verification=verification_row(
                outcome_status="outcome_unknown",
                effect_verified=False,
                recovery="refresh_observation",
                code="stale_reading",
                reason="the reading was stale",
            )
        )
        _, run = await self.drive(gateway, limits=limits(max_actions=2, recovery_attempts=5, max_cycles=8))
        self.assertEqual(run.outcome, AutonomyOutcome.RESOURCE_LIMIT_REACHED.value)
        self.assertIn("action budget", run.ended_reason)
        self.assertLessEqual(run.actions_dispatched, 2)

    async def test_the_cycle_budget_stops_a_run_that_never_concludes(self) -> None:
        gateway = ScriptedGateway(
            verification=verification_row(
                outcome_status="outcome_unknown",
                effect_verified=False,
                recovery="refresh_observation",
                code="stale_reading",
                reason="the reading was stale",
            )
        )
        _, run = await self.drive(gateway, limits=limits(max_cycles=3, recovery_attempts=5, max_actions=6))
        self.assertEqual(run.outcome, AutonomyOutcome.RESOURCE_LIMIT_REACHED.value)
        self.assertIn("cycle budget", run.ended_reason)

    async def test_a_wired_resource_gate_can_refuse_the_run(self) -> None:
        gateway = ScriptedGateway()
        controller = AutonomousSessionController(
            gateway=gateway,
            limits=limits(),
            policy=policy(require_resource_gate=True),
            resource_probe=lambda: {"allow": False, "state": "critical", "reason": "Only 0.4 GB free."},
        )
        self.addAsyncCleanup(controller.shutdown)
        answer = await controller.start(SESSION, approved=True)
        self.assertFalse(answer["started"])
        self.assertEqual(answer["code"], "resource_gate_refused")
        self.assertIn("0.4 GB", answer["reason"])
        self.assertEqual(gateway.calls, [])

    async def test_an_unwired_gate_is_reported_as_unmeasured_not_as_permission(self) -> None:
        gateway = ScriptedGateway()
        controller = AutonomousSessionController(
            gateway=gateway, limits=limits(), policy=policy(require_resource_gate=True)
        )
        self.addAsyncCleanup(controller.shutdown)
        answer = await controller.start(SESSION, approved=True)
        self.assertTrue(answer["started"])
        self.assertIsNone(answer["resources"]["allow"])
        self.assertFalse(answer["resources"]["available"])
        await controller.wait(SESSION)

    async def test_a_gate_that_raises_does_not_break_the_run(self) -> None:
        def broken() -> Mapping[str, Any]:
            raise RuntimeError("no probe here")

        gateway = ScriptedGateway(statuses=["in_progress", "completed"])
        controller = AutonomousSessionController(
            gateway=gateway, limits=limits(), policy=policy(), resource_probe=broken
        )
        self.addAsyncCleanup(controller.shutdown)
        answer = await controller.start(SESSION, approved=True)
        self.assertTrue(answer["started"])
        self.assertIn("resource probe raised", answer["resources"]["reason"])
        await controller.wait(SESSION)
        self.assertEqual(controller.run_for(SESSION).outcome, AutonomyOutcome.OBJECTIVE_COMPLETED.value)

    async def test_a_dependency_that_never_answers_times_out_instead_of_hanging(self) -> None:
        class Hanging(ScriptedGateway):
            async def plan_actions(self, session_id: str) -> dict[str, Any]:
                await asyncio.sleep(30)
                return {}

        controller, run = await self.drive(Hanging(), limits=limits(planning_timeout_seconds=0.05))
        self.assertEqual(run.outcome, AutonomyOutcome.DEPENDENCY_FAILURE.value)
        self.assertIn("planning stage did not answer", run.ended_reason)

    async def test_the_run_never_exceeds_the_actions_it_was_approved_for(self) -> None:
        gateway = ScriptedGateway(
            verification=verification_row(
                outcome_status="outcome_unknown",
                effect_verified=False,
                recovery="refresh_observation",
                code="stale_reading",
                reason="the reading was stale",
            )
        )
        _, run = await self.drive(gateway, limits=limits(max_actions=1, recovery_attempts=4, max_cycles=5))
        self.assertEqual(len(gateway.executed_plans), 1)
        self.assertEqual(run.actions_dispatched, 1)


class PersistenceAndObservabilityTests(IsolatedAsyncioTestCase):
    """The trace survives the process, and every phase move is announced."""

    async def test_the_run_history_survives_a_restart(self) -> None:
        store = DictStore()
        gateway = ScriptedGateway(statuses=["in_progress", "completed"])
        first = AutonomousSessionController(gateway=gateway, limits=limits(), store=store)
        await first.start(SESSION, approved=True)
        await first.wait(SESSION)
        await first.shutdown()
        second = AutonomousSessionController(gateway=ScriptedGateway(), limits=limits(), store=store)
        self.addAsyncCleanup(second.shutdown)
        restored = second.run_for(SESSION)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.outcome, AutonomyOutcome.OBJECTIVE_COMPLETED.value)
        self.assertEqual(restored.objective_id, "obj-1")
        self.assertTrue(second.status(SESSION)["runs"])

    async def test_a_broken_store_never_fails_the_run(self) -> None:
        class Broken:
            def read(self, namespace: str) -> Any:
                raise OSError("unreadable")

            def write(self, namespace: str, payload: Mapping[str, Any]) -> None:
                raise OSError("unwritable")

        gateway = ScriptedGateway(statuses=["in_progress", "completed"])
        controller = AutonomousSessionController(gateway=gateway, limits=limits(), store=Broken())
        self.addAsyncCleanup(controller.shutdown)
        answer = await controller.start(SESSION, approved=True)
        self.assertTrue(answer["started"])
        await controller.wait(SESSION)
        self.assertEqual(controller.run_for(SESSION).outcome, AutonomyOutcome.OBJECTIVE_COMPLETED.value)

    async def test_every_phase_move_is_published_with_its_reason(self) -> None:
        seen: list[tuple[str, dict[str, Any]]] = []

        async def publish(event_type: str, payload: Mapping[str, Any]) -> None:
            seen.append((event_type, dict(payload)))

        gateway = ScriptedGateway(statuses=["in_progress", "in_progress", "completed"])
        controller = AutonomousSessionController(
            gateway=gateway, limits=limits(), publish=publish, evidence_source="live"
        )
        self.addAsyncCleanup(controller.shutdown)
        await controller.start(SESSION, approved=True)
        await controller.wait(SESSION)
        await settle(6)
        events = [row for row in seen if row[0] == "novaforge.autonomy.phase"]
        self.assertTrue(events)
        payloads = [row[1] for row in events]
        self.assertTrue(all(row["session_id"] == SESSION for row in payloads))
        self.assertTrue(all(row["evidence_source"] == "live" for row in payloads))
        self.assertIn(AutonomyPhase.EXECUTING.value, [row["phase"] for row in payloads])
        self.assertIn(AutonomyPhase.COMPLETED.value, [row["phase"] for row in payloads])
        # The ending step is announced WITH its outcome, so a subscriber can tell
        # how the run finished without reading the record.
        self.assertIn(
            AutonomyOutcome.OBJECTIVE_COMPLETED.value, [row["outcome"] for row in payloads]
        )

    async def test_a_publisher_that_raises_never_fails_the_run(self) -> None:
        async def broken(event_type: str, payload: Mapping[str, Any]) -> None:
            raise RuntimeError("the bus is gone")

        gateway = ScriptedGateway(statuses=["in_progress", "completed"])
        controller = AutonomousSessionController(gateway=gateway, limits=limits(), publish=broken)
        self.addAsyncCleanup(controller.shutdown)
        await controller.start(SESSION, approved=True)
        await controller.wait(SESSION)
        self.assertEqual(controller.run_for(SESSION).outcome, AutonomyOutcome.OBJECTIVE_COMPLETED.value)

    async def test_the_trace_holds_the_evidence_and_the_rejected_readings(self) -> None:
        verification = verification_row()
        verification["outcomes"][0]["rejected"] = [
            {"observation_id": "o3", "code": "stale_reading", "reason": "the frame was 8 s old"}
        ]
        gateway = ScriptedGateway(verification=verification, statuses=["in_progress", "in_progress", "completed"])
        controller, run, _ = await self.run_once(gateway)
        outcome = run.cycles[0].verification["outcomes"][0]
        self.assertEqual(outcome["rejected"][0]["code"], "stale_reading")
        self.assertEqual(outcome["evidence"][0]["ref"], "state:s1")
        self.assertIn("state:s1", run.verification_evidence)

    async def run_once(self, gateway: ScriptedGateway):
        controller = AutonomousSessionController(gateway=gateway, limits=limits())
        self.addAsyncCleanup(controller.shutdown)
        await controller.start(SESSION, approved=True)
        await controller.wait(SESSION)
        return controller, controller.run_for(SESSION), None

    async def test_the_status_surface_reports_the_bounds_and_the_runs(self) -> None:
        controller = AutonomousSessionController(
            gateway=ScriptedGateway(), limits=limits(), evidence_source="live"
        )
        self.addAsyncCleanup(controller.shutdown)
        status = controller.status(SESSION)
        self.assertTrue(status["available"])
        self.assertEqual(status["limits"]["max_cycles"], 3)
        self.assertEqual(status["evidence_source"], "live")
        self.assertTrue(any("never captures a frame" in row for row in status["limitations"]))

    async def test_undeclared_provenance_is_never_reported_as_live(self) -> None:
        # An unlabelled controller and a stored row that lost its label are both
        # the unproven class: reporting either as live would let evidence nobody
        # captured be counted as real-game evidence downstream (NF-26's rule).
        undeclared = AutonomousSessionController(gateway=ScriptedGateway(), limits=limits())
        self.addAsyncCleanup(undeclared.shutdown)
        self.assertEqual(undeclared.evidence_source, "simulated")
        self.assertEqual(undeclared.status(SESSION)["evidence_source"], "simulated")

        store = DictStore()
        store.data["novaforge_autonomy"] = {
            "runs": [
                {
                    "run_id": "r-legacy",
                    "session_id": SESSION,
                    "phase": AutonomyPhase.COMPLETED.value,
                    "outcome": AutonomyOutcome.OBJECTIVE_COMPLETED.value,
                }
            ]
        }
        restored = AutonomousSessionController(
            gateway=ScriptedGateway(), limits=limits(), store=store
        )
        self.addAsyncCleanup(restored.shutdown)
        run = restored.run_for(SESSION)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run.evidence_source, "simulated")


class SimulatedGameplayTests(IsolatedAsyncioTestCase):
    """The closed loop over the shipped services and the declared fixture game."""

    async def forge(self, game: SimulatedGame | None = None):
        forge = SimulatedNovaForge(game=game)
        self.addAsyncCleanup(forge.shutdown)
        return forge

    async def test_the_simulated_run_completes_its_objective_with_verified_actions(self) -> None:
        forge = await self.forge()
        run = await forge.run()
        self.assertEqual(run["phase"], AutonomyPhase.COMPLETED.value)
        self.assertEqual(run["outcome"], AutonomyOutcome.OBJECTIVE_COMPLETED.value)
        self.assertEqual(run["evidence_source"], "simulated")
        self.assertEqual(run["game"]["value"], 3)
        self.assertTrue(run["game"]["completed"])
        self.assertEqual(run["actions_dispatched"], 3)
        self.assertGreaterEqual(len(run["cycles"]), 2)
        # The verdict is NF-21's, taken from a fresh reading after the input.
        first = run["cycles"][0]
        self.assertEqual(first["verification"]["status"], "verified")
        self.assertTrue(
            all(row["status"] == "verified_success" for row in first["verification"]["outcomes"])
        )
        self.assertTrue(all(row["evidence"] for row in first["verification"]["outcomes"]))

    async def test_the_completion_is_read_from_the_objective_not_from_the_plan(self) -> None:
        forge = await self.forge()
        run = await forge.run()
        # The cycle that ends the run is the one that OBSERVES completion; the
        # cycle before it only established the effect of its own input.
        final = run["cycles"][-1]
        self.assertEqual(final["objective"]["status"], "completed")
        self.assertEqual(final["outcome"], AutonomyOutcome.OBJECTIVE_COMPLETED.value)
        self.assertIsNone(final["plan"].get("plan_id"))

    async def test_no_cycle_records_a_blank_knowledge_reference(self) -> None:
        forge = await self.forge()
        run = await forge.run()
        for cycle in run["cycles"]:
            plan = cycle.get("plan") or {}
            self.assertEqual(
                cycle["knowledge_refs"],
                [item for item in plan.get("knowledge_refs") or [] if str(item)],
                cycle["index"],
            )
            for ref in cycle["knowledge_refs"]:
                self.assertTrue(str(ref).strip(), cycle["index"])

    async def test_the_trace_records_the_whole_orchestration(self) -> None:
        forge = await self.forge()
        run = await forge.run()
        phases = [step["phase"] for step in run["steps"]]
        for expected in (
            AutonomyPhase.INITIALIZING.value,
            AutonomyPhase.IDENTIFYING_OBJECTIVE.value,
            AutonomyPhase.PLANNING.value,
            AutonomyPhase.EXECUTING.value,
            AutonomyPhase.VERIFYING.value,
            AutonomyPhase.COMPLETED.value,
        ):
            self.assertIn(expected, phases, phases)
        first = run["cycles"][0]
        self.assertTrue(first["observation"]["observation_id"])
        self.assertEqual(first["observation"]["status"], "observed")
        self.assertTrue(first["state"]["state_id"])
        self.assertEqual(first["objective"]["objective_id"], run["objective_id"])
        self.assertTrue(run["verification_evidence"])
        self.assertIn("novaforge.autonomy.phase", run["sim_events"])

    async def test_the_run_asks_for_approval_before_it_starts(self) -> None:
        forge = await self.forge()
        answer = await forge.controller().start(SIM_SESSION_ID, approved=False)
        self.assertFalse(answer["started"])
        self.assertEqual(answer["code"], "approval_required")
        self.assertEqual(forge.game.presses, 0)

    async def test_the_session_is_stopped_and_its_inputs_released_at_the_end(self) -> None:
        forge = await self.forge()
        run = await forge.run()
        self.assertTrue(run["session_ended"])
        self.assertTrue(run["session_ended"]["cleanup"]["ok"])
        self.assertTrue(forge.stopped)

    async def test_a_game_that_stops_answering_ends_the_run_with_no_input(self) -> None:
        forge = await self.forge(SimulatedGame(target_responsive=False))
        run = await forge.run()
        self.assertEqual(run["outcome"], AutonomyOutcome.SAFETY_STOP.value)
        self.assertEqual(run["actions_dispatched"], 0)
        self.assertEqual(forge.game.presses, 0)

    async def test_an_input_backend_that_refuses_is_not_reported_as_progress(self) -> None:
        forge = await self.forge(SimulatedGame(accepts_input=False))
        run = await forge.run()
        self.assertNotEqual(run["outcome"], AutonomyOutcome.OBJECTIVE_COMPLETED.value)
        self.assertEqual(forge.game.presses, 0)
        self.assertGreater(forge.game.refused_presses, 0)
        self.assertNotEqual(run["phase"], AutonomyPhase.COMPLETED.value)

    async def test_a_counter_that_cannot_be_read_is_not_a_verified_effect(self) -> None:
        forge = await self.forge(SimulatedGame(counter_readable=False))
        run = await forge.run()
        # With no readable counter the world offers no measurable effect, so the
        # run refuses to act blind rather than pressing and calling it progress.
        self.assertEqual(forge.game.presses, 0)
        self.assertNotEqual(run["outcome"], AutonomyOutcome.OBJECTIVE_COMPLETED.value)
        self.assertEqual(run["verification_evidence"], [])
        statuses = [
            row["status"]
            for cycle in run["cycles"]
            for row in (cycle.get("verification") or {}).get("outcomes") or []
        ]
        self.assertNotIn("verified_success", statuses)

    async def test_an_objective_that_fails_ends_the_run_as_failed(self) -> None:
        forge = await self.forge(SimulatedGame(failing=True))
        run = await forge.run()
        self.assertEqual(run["outcome"], AutonomyOutcome.OBJECTIVE_FAILED.value)
        self.assertTrue(forge.game.failed)

    async def test_the_run_stops_at_its_action_budget(self) -> None:
        forge = await self.forge(SimulatedGame(target_value=6))
        run = await forge.run(limits=simulated_limits(max_actions=3, max_actions_per_plan=1))
        self.assertEqual(run["outcome"], AutonomyOutcome.RESOURCE_LIMIT_REACHED.value)
        self.assertLessEqual(forge.game.presses, 3)

    async def test_the_simulated_run_is_never_described_as_live(self) -> None:
        controller = (await self.forge()).controller()
        self.assertEqual(controller.evidence_source, "simulated")
        self.assertEqual(controller.status(SIM_SESSION_ID)["evidence_source"], "simulated")

    async def test_pausing_the_simulated_session_stops_the_reading(self) -> None:
        forge = await self.forge()
        await forge.pause(SIM_SESSION_ID, reason="operator")
        answer = await forge.observe_frame_once()
        self.assertEqual(answer["status"], "not_observing")
        self.assertEqual(answer["reason_code"], "paused_by_user")
        await forge.resume(SIM_SESSION_ID)
        answer = await forge.observe_frame_once()
        self.assertEqual(answer["status"], "observed")

    async def test_the_simulated_game_has_its_own_input_bound(self) -> None:
        game = SimulatedGame(target_value=MAX_SIM_PRESSES + 5)
        for _ in range(MAX_SIM_PRESSES):
            self.assertTrue(game.press("w")[0])
        ok, code = game.press("w")
        self.assertFalse(ok)
        self.assertEqual(code, "sim_budget_exhausted")

    async def test_the_simulated_world_only_answers_its_declared_key(self) -> None:
        game = SimulatedGame()
        ok, code = game.press("q")
        self.assertFalse(ok)
        self.assertEqual(code, "unsupported_key")
        self.assertEqual(game.value, 0)

    async def test_the_simulated_policy_is_still_a_real_policy(self) -> None:
        forge = await self.forge()
        answer = await forge.controller(policy=simulated_policy(plan_approval="per_plan")).start(
            SIM_SESSION_ID, approved=True
        )
        self.assertTrue(answer["started"])
        self.assertEqual(answer["limits"]["max_actions"], simulated_limits().max_actions)
        # The run is pinned to the WINDOW it drives, not to the profile's name.
        self.assertEqual(SIM_TITLE, answer["status"]["game"])


class AutonomyApiTests(_ForgeApiFixture):
    """The HTTP surface over the REAL manager, its services and its supervisor.

    Nothing here is a double except the desktop seams: the routes drive the real
    session, the real observer/state/objective services and the real supervisor,
    which is what makes the refusal below evidence about the wiring.
    """

    def _session(self) -> str:
        described = self._start()
        session_id = str(described["session_id"])
        self._wait(lambda: bool(self.manager.get(session_id).target_validated))
        return session_id

    def _autonomy(self, session_id: str) -> dict[str, Any]:
        response = self._client.get(f"/novaforge/sessions/{session_id}/autonomy")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_the_status_route_reports_the_bounds_and_the_runs(self) -> None:
        session_id = self._session()
        body = self._autonomy(session_id)
        self.assertTrue(body["available"])
        self.assertEqual(body["evidence_source"], "live")
        self.assertIn("max_cycles", body["limits"])
        self.assertTrue(body["approval_required"])
        self.assertEqual([row["session_id"] for row in body["sessions"]], [session_id])
        self.assertEqual(body["runs"], [])

    def test_starting_without_approval_starts_nothing(self) -> None:
        session_id = self._session()
        response = self._client.post(
            f"/novaforge/sessions/{session_id}/autonomy/start", json={}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["started"])
        self.assertEqual(body["code"], "approval_required")
        self.assertEqual(self._autonomy(session_id)["runs"], [])

    def test_an_approved_run_over_a_target_with_no_objective_ends_honestly(self) -> None:
        """A real run against a real window refuses what it cannot interpret.

        The fake window has no declared objective profile and no readable mission
        panel, so there is nothing to work towards: the run must end with the
        reason that says so, instead of pressing keys at a window nobody can read.
        """
        session_id = self._session()
        response = self._client.post(
            f"/novaforge/sessions/{session_id}/autonomy/start",
            json={"approved": True, "task": "Reach the checkpoint."},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["started"])
        self._wait(lambda: bool(self._autonomy(session_id)["runs"][0]["terminal"]))
        run = self._autonomy(session_id)["runs"][0]
        self.assertEqual(run["phase"], AutonomyPhase.FAILED.value)
        self.assertIn(
            run["outcome"],
            {
                AutonomyOutcome.DEPENDENCY_FAILURE.value,
                AutonomyOutcome.OBSERVATION_UNAVAILABLE.value,
            },
        )
        self.assertTrue(run["reason"], "a terminal run always says why it ended")
        self.assertEqual(run["actions_dispatched"], 0)
        # The cleanup is its own task (it stops the session), so it is polled for.
        self._wait(lambda: bool(self._autonomy(session_id)["runs"][0]["session_ended"]))
        # The supervisor is not a session: the session it drove was stopped by
        # the run's own cleanup (which is what releases held input).
        self._wait(
            lambda: self.manager.get(session_id).state.value not in {"running", "starting"}
        )

    def test_the_trace_route_carries_every_phase_move(self) -> None:
        session_id = self._session()
        self._client.post(
            f"/novaforge/sessions/{session_id}/autonomy/start", json={"approved": True}
        )
        self._wait(lambda: bool(self._autonomy(session_id)["runs"][0]["terminal"]))
        response = self._client.get(
            "/novaforge/autonomy/trace", params={"session_id": session_id}
        )
        self.assertEqual(response.status_code, 200, response.text)
        trace = response.json()
        self.assertTrue(trace["runs"])
        phases = [step["phase"] for step in trace["runs"][0]["steps"]]
        self.assertEqual(phases[0], AutonomyPhase.INITIALIZING.value)
        self.assertIn(AutonomyPhase.FAILED.value, phases)
        self.assertTrue(all(step["at"] for step in trace["runs"][0]["steps"]))
        # The run's own timeline carries what the supervisor announced: a missing
        # entry in EVENT_SPECS would have made these vanish silently.
        events = self._client.get(
            f"/novaforge/sessions/{session_id}/timeline"
        ).json()
        types = [row["type"] for row in events["events"]]
        self.assertIn("autonomy.started", types)
        self.assertIn("novaforge.autonomy.phase", types)
        autonomous = [row for row in events["events"] if row["type"].startswith("autonomy.") or row["type"] == "novaforge.autonomy.phase"]
        self.assertTrue(all(row["source"] == "autonomy" for row in autonomous))

    def test_pause_resume_and_cancel_answer_instead_of_failing(self) -> None:
        session_id = self._session()
        for action in ("pause", "resume", "cancel"):
            response = self._client.post(
                f"/novaforge/sessions/{session_id}/autonomy/{action}", json={}
            )
            self.assertEqual(response.status_code, 200, response.text)
        # A run that was never started is answered with its own code, not a 500.
        self.assertFalse((
            self._client.post(
                f"/novaforge/sessions/{session_id}/autonomy/approve", json={"approved": True}
            ).json()["approved"]
        ))


class AutonomyCardContractTests(TestCase):
    """The panel contract: every control the card shows is wired to its own route.

    A card whose button posts somewhere else is worse than a missing button, so
    the ids in the markup, the handlers in the module and the routes in the app
    are pinned together. The rules themselves (Start only when no run holds the
    session, approval only while the run is parked, never a live badge over
    simulated evidence) live in ``autonomyView`` and are driven against the real
    module by :class:`AutonomyCardDomTests` below.
    """

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.html = (self.root / "src/novacontrol/web/static/index.html").read_text(
            encoding="utf-8"
        )
        self.js = (self.root / "src/novacontrol/web/static/js/novaforge.js").read_text(
            encoding="utf-8"
        )

    def test_the_card_renders_every_control_the_phase_requires(self) -> None:
        # The card ROOT is markup: it anchors the stylesheet and the card's
        # placement, and no script needs to name it (the NF-19/NF-20 contract
        # tests pin their own roots the same way). Every CONTROL below must
        # exist in the markup AND be addressed by name in the module, because a
        # control the panel never looks up is a button that does nothing.
        self.assertIn('id="forgeAutonomyCard"', self.html, "forgeAutonomyCard")
        for element_id in (
            "forgeAutonomyBadge",
            "forgeAutonomyHint",
            "forgeAutonomyFacts",
            "forgeAutonomyOutcome",
            "forgeAutonomyCycles",
            "forgeAutonomyApproval",
            "forgeAutonomyStartButton",
            "forgeAutonomyPauseButton",
            "forgeAutonomyResumeButton",
            "forgeAutonomyCancelButton",
            "forgeAutonomyApproveButton",
            "forgeAutonomyRejectButton",
            "forgeAutonomyRefreshButton",
        ):
            with self.subTest(element=element_id):
                self.assertIn(f'id="{element_id}"', self.html, f"missing #{element_id}")
                self.assertIn(f'"{element_id}"', self.js, f"{element_id} is never looked up")

    def test_the_card_posts_only_the_supervisors_own_routes(self) -> None:
        for suffix in ("/start", "/pause", "/resume", "/cancel", "/approve"):
            self.assertIn(f'postAutonomy("{suffix}"', self.js, suffix)
        self.assertIn('autonomyPath("")', self.js)
        # The card names the autonomy routes and nothing else: a control that
        # posted to the desktop/browser command routes would drive a different
        # path than the one the supervisor verified.
        self.assertNotIn('autonomyPath("/execute"', self.js)
        # The route is session-scoped and absolute, exactly like the control and
        # plan cards' own paths: a relative path would resolve against whatever
        # page the panel is on.
        self.assertIn(
            '"/novaforge/sessions/" + encodeURIComponent(forge.sessionId) + "/autonomy"',
            self.js,
        )

    def test_the_card_says_which_evidence_it_is_reporting(self) -> None:
        self.assertIn("Evidence source", self.js)
        self.assertIn("evidence_source", self.js)
        # The autonomous label is only used for a run the supervisor reports; a
        # session with no run shows its own honest idle badge.
        self.assertIn('"untested"', self.js)
        self.assertIn("No autonomous run for this session.", self.js)

    def test_the_card_never_claims_success_from_a_plan_or_a_still_open_game(self) -> None:
        hint = self.html[self.html.index("forgeAutonomyCard") :]
        hint = hint[: hint.index("</article>")]
        self.assertIn("not success", " ".join(hint.split()))
        self.assertIn("never restarted", " ".join(hint.split()))


class _CriticalGovernor:
    """A governor reading a machine with no room, for the wiring test."""

    def assess(self) -> ResourceAssessment:
        return ResourceAssessment(
            level=ResourceLevel.CRITICAL,
            reasons=("Only 0.4 GB free.",),
            available_ram_bytes=400 * 1024 * 1024,
        )


@unittest.skipIf(NoopDesktopRunner is None, "the API fixture is unavailable")
class AutonomyResourceGateWiringTests(unittest.TestCase):
    """The SHIPPED application hands the governor's reading to the run's gate.

    The supervisor could always be refused by a wired probe, and the manager
    could always accept one — but nothing passed it, so the real application
    reported "not measured" forever while the governor's reading sat one
    attribute away. An unwired safety gate looks exactly like a satisfied one
    from the outside, which is why the wiring itself is pinned here.
    """

    def _application(self):
        from novacontrol.application import NovaControlApplication

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner), \
                mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner):
            return NovaControlApplication(data_dir=tmp.name)

    def test_the_application_reads_the_shared_governor_through_the_gate(self) -> None:
        app = self._application()
        self.assertIsNotNone(app.novaforge.autonomy_resources)
        self.assertTrue(app.novaforge.autonomy().policy.require_resource_gate)
        # The same governor every other load decision asks, not a second reading.
        reading = app.novaforge.autonomy_status()["resources"]
        self.assertTrue(reading["available"])
        self.assertEqual(reading["level"], app.governor.assess().level.value)
        self.assertIsInstance(reading["allow"], bool)

    def test_a_critical_machine_is_refused_by_the_shipped_wiring(self) -> None:
        app = self._application()
        with mock.patch.object(app, "governor", _CriticalGovernor()):
            reading = app.novaforge.autonomy_status()["resources"]
        self.assertTrue(reading["available"])
        self.assertFalse(reading["allow"])
        self.assertEqual(reading["level"], "critical")
        self.assertIn("0.4 GB", reading["reason"])


AUTONOMY_CARD_DOM = r"""
const fs = require('fs');
function makeNode(tag, id) {
  const node = {
    tagName: String(tag || 'div').toUpperCase(), id: id || '', className: '',
    _text: '', _html: '', dataset: {}, children: [], disabled: false, hidden: false,
    appendChild: function (child) { this.children.push(child); return child; },
  };
  // Setting textContent REPLACES the children, as the real DOM does. A shim that
  // kept them made every re-render look cumulative, so a row a later render was
  // supposed to drop still read as present.
  Object.defineProperty(node, 'textContent', {
    get: function () { return this._text; },
    set: function (value) {
      this._text = value === undefined || value === null ? '' : String(value);
      this.children = [];
    },
  });
  Object.defineProperty(node, 'innerHTML', {
    get: function () { return this._html; },
    set: function (value) { this._html = value; if (value === '') this.children = []; },
  });
  return node;
}
const nodes = {};
[
  'forgeAutonomyBadge', 'forgeAutonomyHint', 'forgeAutonomyFacts', 'forgeAutonomyOutcome',
  'forgeAutonomyCycles', 'forgeAutonomyActions',
  'forgeAutonomyApproval', 'forgeAutonomyStartButton',
  'forgeAutonomyPauseButton', 'forgeAutonomyResumeButton', 'forgeAutonomyCancelButton',
  'forgeAutonomyApproveButton', 'forgeAutonomyRejectButton', 'forgeAutonomyRefreshButton',
].forEach(function (id) { nodes[id] = makeNode('div', id); });
global.document = { getElementById: function (id) { return nodes[id] || null; } };
global.el = function (tag, className, text) {
  const node = makeNode(tag);
  node.className = className || '';
  if (text !== undefined) node.textContent = String(text);
  return node;
};
global.window = {};
eval(fs.readFileSync(process.argv[1], 'utf8'));
const L = global.window.NovaForgeLogic;
function text(node) { return (node.textContent || '') + node.children.map(text).join(' '); }
function snapshot() {
  return {
    badge: nodes.forgeAutonomyBadge.textContent,
    badgeState: nodes.forgeAutonomyBadge.dataset.state,
    hint: nodes.forgeAutonomyHint.textContent,
    outcome: nodes.forgeAutonomyOutcome.textContent,
    facts: text(nodes.forgeAutonomyFacts),
    cycles: text(nodes.forgeAutonomyCycles),
    actions: text(nodes.forgeAutonomyActions),
    approvalHidden: nodes.forgeAutonomyApproval.hidden,
    buttons: ['forgeAutonomyStartButton', 'forgeAutonomyPauseButton', 'forgeAutonomyResumeButton',
      'forgeAutonomyCancelButton', 'forgeAutonomyApproveButton', 'forgeAutonomyRejectButton']
      .map(function (id) { return nodes[id].disabled; }),
  };
}
const limits = { max_cycles: 4, max_actions: 6, max_actions_per_plan: 2, session_seconds: 60,
  recovery_attempts: 1, replans: 1 };
function run(phase, extra) {
  return Object.assign({
    run_id: 'r1', session_id: 's1', phase: phase, active: false, terminal: false,
    evidence_source: 'live', game: 'SIM GAME', objective: 'Collect the audit tokens',
    completion_criteria: ['MISSION COMPLETE visible'], outcome: '', reason: 'why',
    actions_dispatched: 0, recoveries: 0, replans: 0, elapsed_seconds: 1.5,
    cycles: [], steps: [],
  }, extra || {});
}
function body(entry, canStart) {
  return {
    available: true, can_start: Boolean(canStart), evidence_source: 'live',
    approval_required: true, limits: limits, policy: { plan_approval: 'run' },
    resources: { available: true, level: 'ok', reason: '' },
    runs: entry ? [entry] : [],
  };
}
const planned = run('EXECUTING', {
  active: true,
  cycles: [{
    index: 1, phase: 'VERIFYING', reason: 'checking', actions_dispatched: 1,
    plan: { plan_id: 'p1', status: 'planned', steps: [
      { index: 1, action_id: 'a1', kind: 'key_hold', description: 'Hold W' },
      { index: 2, action_id: 'a2', kind: 'click', description: 'Click the token' },
    ] },
    approval: { mode: 'run', approved: true },
    execution: { plan_id: 'p1', status: 'executed', steps: [
      { index: 1, action_id: 'a1', kind: 'key_hold', ok: true, status: 'completed', code: '' },
      { index: 2, action_id: 'a2', kind: 'click', ok: false, status: 'rejected', code: 'gate_refused' },
    ] },
  }],
});
const completed = run('COMPLETED', {
  terminal: true, outcome: 'objective_completed', actions_dispatched: 1,
  cycles: [
    { index: 1, phase: 'VERIFYING', reason: 'checking', actions_dispatched: 1,
      plan: { plan_id: 'p1', status: 'planned', steps: [
        { index: 1, action_id: 'a1', kind: 'key_hold', description: 'Hold W' }] },
      approval: { mode: 'run', approved: true },
      execution: { plan_id: 'p1', status: 'executed', steps: [
        { index: 1, action_id: 'a1', kind: 'key_hold', ok: true, status: 'completed', code: '' }] } },
    { index: 2, phase: 'OBSERVING', reason: 'the objective is complete', plan: {} },
  ],
});
const out = {};
L.renderAutonomy(null);
out.none = snapshot();
L.renderAutonomy(body(null, true));
out.idle = snapshot();
L.renderAutonomy(body(run('EXECUTING', { active: true })));
out.active = snapshot();
L.renderAutonomy(body(planned));
out.planned = snapshot();
L.renderAutonomy(body(run('PAUSED', { active: true })));
out.paused = snapshot();
L.renderAutonomy(body(run('WAITING_FOR_APPROVAL', { active: true })));
out.parked = snapshot();
L.renderAutonomy(body(run('COMPLETED', { terminal: true, outcome: 'objective_completed' })));
out.ended = snapshot();
L.renderAutonomy(body(completed));
out.completed = snapshot();
L.renderAutonomy(body(run('EXECUTING', { active: true, evidence_source: 'simulated' })));
out.simulated = snapshot();
if (process.argv[2]) {
  // The payload a REAL run leaves behind, handed in by AutonomyCardRealPayloadTests:
  // the hand-written cases above can only prove the renderer against the field names
  // a test author believed in.
  L.renderAutonomy(JSON.parse(fs.readFileSync(process.argv[2], 'utf8')));
  out.real = snapshot();
}
console.log(JSON.stringify(out));
"""


async def _simulated_status_payload() -> dict[str, Any]:
    """One real simulated run, in the shape the autonomy status route serves.

    The two keys the route adds on top of the controller's own status are added
    the way ``NovaForgeManager.autonomy_status`` adds them, so the card is fed the
    payload it really receives rather than the controller's smaller one.
    """
    forge = SimulatedNovaForge(game=SimulatedGame())
    controller: AutonomousSessionController | None = None
    try:
        controller = forge.controller()
        started = await controller.start(SIM_SESSION_ID, approved=True)
        if not started.get("started"):  # pragma: no cover - the fixture always starts
            raise AssertionError(f"the simulated run did not start: {started}")
        await controller.wait(SIM_SESSION_ID)
        status = controller.status(SIM_SESSION_ID)
        await controller.shutdown()
    finally:
        await forge.shutdown()
    status["approval_required"] = bool(controller.policy.require_approval)
    status["sessions"] = []
    return status


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class AutonomyCardRealPayloadTests(unittest.TestCase):
    """The card over the payload the SHIPPED supervisor really produces.

    ``AutonomyCardDomTests`` drives hand-written payloads, which can only prove
    the renderer against the field names a test author believed in. This one
    drives the status a real simulated run leaves behind, so a field the card
    reads under a different name than the supervisor writes it fails here instead
    of rendering as a blank in front of an operator.
    """

    @classmethod
    def setUpClass(cls) -> None:
        payload = asyncio.run(_simulated_status_payload())
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "autonomy-status.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            completed = subprocess.run(
                [
                    shutil.which("node") or "node",
                    "-e",
                    AUTONOMY_CARD_DOM,
                    str(PANEL_JS),
                    str(path),
                ],
                capture_output=True,
                timeout=60,
                check=False,
            )
        stdout = completed.stdout.decode("utf-8", errors="replace").strip()
        stderr = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            raise AssertionError(f"node failed: {stderr[:800]}")
        try:
            case = json.loads(stdout.splitlines()[-1])["real"]
        except (IndexError, KeyError, json.JSONDecodeError) as exc:  # pragma: no cover
            raise AssertionError(
                f"the real-payload driver produced no JSON: {stdout[:400]!r}"
            ) from exc
        cls.case = case

    def test_a_real_simulated_run_never_wears_the_autonomous_label(self) -> None:
        """An ended run is badged ``ended``; a simulated one is never ``autonomous``.

        The ending outranks the evidence source for the badge — and the evidence
        source is still named in the facts below it, so "simulated" is disclosed
        rather than implied by the absence of the word.
        """
        self.assertEqual(self.case["badge"], "ended")
        self.assertEqual(self.case["badgeState"], "done")
        self.assertNotEqual(self.case["badge"], "autonomous")
        self.assertIn("Evidence source simulated", self.case["facts"])

    def test_the_real_run_reports_its_ending_and_its_objective(self) -> None:
        self.assertTrue(self.case["outcome"].startswith("Ended: objective_completed"))
        self.assertIn("Collect the audit tokens", self.case["facts"])
        self.assertIn("reach 3 on progress", self.case["facts"])
        self.assertIn("covered by the run's own approval", self.case["facts"])
        # Only Start: the finished run is never restarted (its own controls are
        # all gone), while a NEW run is allowed — and it will be asked for its own
        # approval, which is why the server's own ``can_start`` says yes here.
        self.assertEqual(self.case["buttons"], [False, True, True, True, True, True])

    def test_the_real_plans_actions_are_listed_with_their_own_records(self) -> None:
        """The run's three dispatched inputs, from the plan and NF-19's record.

        They come from cycle 1 — the cycle that ended the run observed completion
        and holds no plan — so this is the fallback doing its job on real data.
        """
        actions = self.case["actions"]
        self.assertNotIn("The last cycle built no plan.", actions)
        self.assertIn("cycle 1 \u00b7 1. key_hold", actions)
        self.assertIn("3. key_hold", actions)
        self.assertEqual(actions.count("(completed)"), 3)

    def test_the_real_run_reports_what_it_executed_against_its_budget(self) -> None:
        self.assertIn("3 of 4", self.case["facts"])
        self.assertIn("cycles 4", self.case["facts"])
        self.assertIn("0 / 0", self.case["facts"])  # recoveries / replans
        self.assertIn("cycle 2", self.case["cycles"])


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class AutonomyCardDomTests(unittest.TestCase):
    """The card's own rules, driven against the real module in a DOM shim.

    What the API tests cannot see is the OPERATOR's side: which control is
    enabled, what the badge says and whether the card asks for an answer. Those
    are the phase's user-control guarantees, so they are asserted on the nodes
    the real ``renderAutonomy`` builds rather than on the source that builds
    them. The button order in every assertion is:

        start, pause, resume, cancel, approve, reject
    """

    @classmethod
    def setUpClass(cls) -> None:
        completed = subprocess.run(
            [shutil.which("node") or "node", "-e", AUTONOMY_CARD_DOM, str(PANEL_JS)],
            capture_output=True, timeout=60, check=False,
        )
        stdout = completed.stdout.decode("utf-8", errors="replace").strip()
        stderr = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            raise AssertionError(f"node failed: {stderr[:800]}")
        try:
            cls.data = json.loads(stdout.splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:  # pragma: no cover - harness failure
            raise AssertionError(f"autonomy driver produced no JSON: {stdout[:400]!r}") from exc

    def test_without_a_run_nothing_can_be_pressed_and_the_card_says_so(self) -> None:
        case = self.data["none"]
        self.assertEqual(case["buttons"], [True] * 6)
        self.assertEqual(case["badge"], "untested")
        self.assertTrue(case["approvalHidden"])
        self.assertEqual(case["outcome"], "No autonomous run for this session.")

    def test_a_live_session_offers_start_only_and_names_its_budgets(self) -> None:
        case = self.data["idle"]
        self.assertEqual(case["badge"], "idle")
        self.assertEqual(case["buttons"], [False, True, True, True, True, True])
        self.assertIn("cycles 4", case["facts"])
        self.assertIn("actions 6", case["facts"])
        self.assertIn("required before anything is sent", case["facts"])

    def test_a_live_run_offers_pause_and_cancel_but_never_start(self) -> None:
        case = self.data["active"]
        self.assertEqual(case["badge"], "autonomous")
        self.assertEqual(case["badgeState"], "active")
        self.assertEqual(case["buttons"], [True, False, True, False, True, True])
        self.assertTrue(case["approvalHidden"])

    def test_a_paused_run_offers_resume_and_holds_everything_else(self) -> None:
        case = self.data["paused"]
        self.assertEqual(case["buttons"], [True, True, False, False, True, True])
        self.assertIn("PAUSED", case["hint"])

    def test_only_a_parked_plan_asks_the_operator_for_an_answer(self) -> None:
        case = self.data["parked"]
        self.assertFalse(case["approvalHidden"])
        # Approve/refuse/cancel are the answers to a parked plan; Pause is not
        # offered because nothing is running to pause.
        self.assertEqual(case["buttons"], [True, True, True, False, False, False])
        self.assertIn("WAITING_FOR_APPROVAL", case["hint"])

    def test_an_ended_run_enables_nothing_at_all(self) -> None:
        case = self.data["ended"]
        self.assertEqual(case["buttons"], [True] * 6)
        self.assertEqual(case["badge"], "ended")
        self.assertEqual(case["badgeState"], "done")
        self.assertTrue(case["outcome"].startswith("Ended: objective_completed"))

    def test_the_newest_plan_is_still_shown_after_the_cycle_that_ended_the_run(self) -> None:
        """The cycle that reads completion holds no plan, so the last cycle alone
        would answer "no plan" for a run that dispatched actions. The newest plan
        the run built is the current plan, and the row says which cycle it is.
        """
        case = self.data["completed"]
        self.assertIn("cycle 1 · 1. key_hold — Hold W (completed)", case["actions"])
        self.assertNotIn("The last cycle built no plan.", case["actions"])
        self.assertIn("p1", case["facts"])
        self.assertIn("(cycle 1)", case["facts"])

    def test_the_planned_and_executed_actions_are_listed_not_just_counted(self) -> None:
        """Scope H: the operator sees WHICH actions ran, and what became of each.

        The plan's own steps are listed, matched against NF-19's record by action
        id: a dispatched step shows its own status, and a step with no record is
        reported as not dispatched rather than as if it had run.
        """
        case = self.data["planned"]
        self.assertIn("1. key_hold — Hold W (completed)", case["actions"])
        self.assertIn("2. click — Click the token (rejected: gate_refused)", case["actions"])

    def test_without_a_plan_the_card_says_there_is_none(self) -> None:
        self.assertIn("No plan has been built for this session yet.", self.data["none"]["actions"])
        self.assertIn("No plan has been built for this session yet.", self.data["idle"]["actions"])
        self.assertIn("The last cycle built no plan.", self.data["active"]["actions"])

    def test_a_simulated_run_is_never_badged_autonomous(self) -> None:
        case = self.data["simulated"]
        self.assertEqual(case["badge"], "simulated")
        self.assertEqual(case["badgeState"], "simulated")
        self.assertNotEqual(case["badge"], "autonomous")
        self.assertIn("simulated", case["facts"])

    def test_the_card_is_reachable_from_mission_control(self) -> None:
        html = INDEX.read_text(encoding="utf-8")
        mission = html.index('id="forgeMissionView"')
        card = html.index('id="forgeAutonomyCard"')
        guardian = html.index('id="forgeGuardianView"')
        self.assertGreater(card, mission, "the autonomy card must be in Mission Control")
        self.assertLess(card, guardian, "the autonomy card must not be in Guardian")


if __name__ == "__main__":  # pragma: no cover - direct execution
    import unittest

    unittest.main()
