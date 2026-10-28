"""NF-22 — the deterministic simulated game, and the session that drives it.

NF-22's first autonomous gameplay task needs a game whose objective is
*observable*. No such verified lightweight target exists in this environment, so
this module provides one and labels every result that comes out of it
``evidence_source="simulated"``.

The important part is what is **real** here. The simulator is only the world and
the input backend; everything around it is the shipped pipeline, driven through
its own injected seams:

* observation is an NF-15-shaped observer record (the only simulated part of the
  reading chain: the pixels are rendered from the simulated world, not captured);
* state understanding is the real :class:`~novacontrol.novaforge.state_service.GameStateService`
  reading a real :class:`~novacontrol.novaforge.state.GameState` through
  :func:`~novacontrol.novaforge.state.build_game_state`;
* the objective is the real :class:`~novacontrol.novaforge.objective_service.ObjectiveService`
  interpreting that state through a **declared** objective profile;
* planning is the real :func:`~novacontrol.novaforge.planner.GamePlanService` over
  a **declared** plan rule, and its "input backend" is the simulated world;
* outcome verification is the real
  :class:`~novacontrol.novaforge.verification.GameVerificationService` with a
  **declared** observable, reading a fresh state after the action.

So a successful simulated run is evidence that the orchestration, the evidence
rules, the outcome vocabulary and the bounds work together — and it is **not**
evidence that NovaForge can play any real game. The phase's own words: a
simulated end-to-end pass does not count as a live-game verification.

The simulated world is deliberately small and honest about itself:

    OBJECTIVE: Collect the audit tokens      progress: 1/3
    ... press SPACE ...                       progress: 2/3
    ... press SPACE ...                       progress: 3/3
    OBJECTIVE COMPLETE

Every switch a test needs (an unreadable counter, a target that stops answering,
an input backend that refuses, a game that fails its objective) is a declared
attribute, not a hidden branch.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Mapping
from uuid import uuid4

from novacontrol.perception.models import BBox, OcrText, SceneRepresentation

from .autonomy import (
    AutonomyLimits,
    AutonomyPolicy,
    AutonomousSessionController,
)
from .control import ControlAction, ControlKind, ControlTarget
from .objective_service import ObjectiveService
from .objectives import default_objective_registry
from .planner import GamePlanService, PlanPolicy, default_plan_rules
from .state import default_registry
from .state_service import GameStateService
from .verification import (
    GameVerificationService,
    ObservationEvidence,
    VerificationContext,
    VerificationPolicy,
    default_verification_rules,
)

__all__ = [
    "MAX_SIM_PRESSES",
    "SIM_COMPLETION_TEXT",
    "SIM_FAILURE_TEXT",
    "SIM_GAME",
    "SIM_OBJECTIVE_TEXT",
    "SIM_PROFILE_ID",
    "SIM_SESSION_ID",
    "SIM_TARGET",
    "SIM_TARGET_VALUE",
    "SIM_TITLE",
    "SimulatedGame",
    "SimulatedNovaForge",
    "simulated_limits",
    "simulated_policy",
]

#: One session id for the whole simulated world: this is a fixture, and a
#: second one would only make the evidence harder to attribute.
SIM_SESSION_ID = "nf22-sim-session"
#: The simulator presents the SHIPPED fixture scenario (``nc-forge-mission@1``):
#: its perception layout, its objective rule, its plan rule and its verification
#: rule are all declared by this build, so the simulated run adds no success
#: criteria of its own. The title carries the profile's tokens.
SIM_PROFILE_ID = "nc-forge-mission@1"
SIM_GAME = "NC FORGE MISSION (fixture scenario)"
SIM_TITLE = "NC FORGE MISSION - BLOCK RUN"
SIM_TARGET: dict[str, Any] = {"title": SIM_TITLE, "handle": 41207, "pid": 6120}
SIM_OBJECTIVE_TEXT = "Collect the audit tokens"
SIM_COMPLETION_TEXT = "MISSION COMPLETE"
SIM_FAILURE_TEXT = "MISSION FAILED"
SIM_TARGET_VALUE = 3
#: The declared control the fixture plan rule uses: hold W for 120 ms.
SIM_KEY = "w"
SIM_HOLD_MS = 120
#: The simulated game's own bound on accepted input, so a runaway loop hits the
#: GAME's limit too and cannot look like progress.
MAX_SIM_PRESSES = 24

#: The shipped fixture profile's own frame size and field boxes (state.py).
SIM_WIDTH = 1280
SIM_HEIGHT = 720
_OBJECTIVE_BOX = BBox(x=40, y=60, width=800, height=60)
_PROGRESS_BOX = BBox(x=40, y=140, width=300, height=60)
_NOTIFICATION_BOX = BBox(x=40, y=620, width=900, height=60)


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


def simulated_limits(**overrides: Any) -> AutonomyLimits:
    """The bounds the simulated acceptance run uses (small, so a debt is visible)."""
    values: dict[str, Any] = {
        # The fixture objective needs three inputs, and the fixture plan rule
        # expands one objective into three steps; the run is given exactly that
        # much room, so a fourth action is a limit rather than a guess.
        "max_cycles": 4,
        "max_actions": 4,
        "max_actions_per_plan": 3,
        "session_seconds": 60.0,
        "observation_attempts": 2,
        "recovery_attempts": 2,
        "replans": 1,
    }
    values.update(overrides)
    return AutonomyLimits(**values)


def simulated_policy(**overrides: Any) -> AutonomyPolicy:
    values: dict[str, Any] = {"require_approval": True, "plan_approval": "run"}
    values.update(overrides)
    return AutonomyPolicy(**values)


@dataclass(slots=True)
class SimulatedGame:
    """A deterministic world with one measurable objective and a declared vocabulary.

    Every attribute is a documented switch so a test can produce a specific
    failure without patching anything: an unreadable counter, a window that stops
    answering, an input backend that refuses, a game that reaches its failure
    state. Nothing here is random.
    """

    target_value: int = SIM_TARGET_VALUE
    objective_text: str = SIM_OBJECTIVE_TEXT
    #: Switches.
    counter_readable: bool = True
    target_responsive: bool = True
    accepts_input: bool = True
    failing: bool = False
    #: Observed facts.
    value: int = 0
    completed: bool = False
    failed: bool = False
    presses: int = 0
    refused_presses: int = 0
    observations: int = 0
    revision: int = 0

    def scene(self, *, sequence: int, frame_id: str) -> SceneRepresentation:
        """The declared layout, rendered as a perception scene."""
        text: list[OcrText] = [
            OcrText(
                text=self.objective_text,
                bbox=_OBJECTIVE_BOX,
                reading_order=0,
                source="simulated",
            )
        ]
        if self.counter_readable:
            text.append(
                OcrText(
                    text=str(self.value),
                    bbox=_PROGRESS_BOX,
                    reading_order=1,
                    source="simulated",
                )
            )
        if self.completed:
            text.append(
                OcrText(
                    text=SIM_COMPLETION_TEXT,
                    bbox=_NOTIFICATION_BOX,
                    reading_order=2,
                    source="simulated",
                )
            )
        if self.failed:
            text.append(
                OcrText(
                    text=SIM_FAILURE_TEXT,
                    bbox=_NOTIFICATION_BOX,
                    reading_order=2,
                    source="simulated",
                )
            )
        return SceneRepresentation(
            width=SIM_WIDTH,
            height=SIM_HEIGHT,
            text=tuple(text),
            source_id=f"simulated:{frame_id}",
            frame_id=frame_id,
        )

    def press(self, key: str) -> tuple[bool, str]:
        """One input operation. Returns ``(ok, code)`` — the world's own answer."""
        if not self.accepts_input:
            self.refused_presses += 1
            return False, "input_refused"
        if key != SIM_KEY:
            self.refused_presses += 1
            return False, "unsupported_key"
        if self.presses >= MAX_SIM_PRESSES:
            self.refused_presses += 1
            return False, "sim_budget_exhausted"
        self.presses += 1
        self.revision += 1
        if self.completed or self.failed:
            return True, "completed"
        if self.failing:
            # The declared failure state. The mission ends HERE rather than at the
            # counter's target: a screen showing "MISSION FAILED" beside a counter
            # that reached its target is two contradictory pieces of evidence, and
            # NF-17 rightly refuses a verdict on a conflict. A failing game gives
            # one verdict, so the failure the rule declares is the one that is read.
            self.failed = True
            return True, "failed"
        self.value += 1
        if self.value >= self.target_value:
            self.completed = True
        return True, "completed"

    def snapshot(self) -> dict[str, Any]:
        return {
            "game": SIM_GAME,
            "objective_text": self.objective_text,
            "target_value": self.target_value,
            "value": self.value,
            "completed": self.completed,
            "failed": self.failed,
            "presses": self.presses,
            "refused_presses": self.refused_presses,
            "observations": self.observations,
            "counter_readable": self.counter_readable,
            "target_responsive": self.target_responsive,
            "accepts_input": self.accepts_input,
        }


class _SimulatedObserver:
    """The NF-15 surface of the simulated game (the only simulated reading link).

    It answers exactly what the real observer answers, so the state service cannot
    tell the difference between "no validated frame" and "the simulator is off".
    """

    def __init__(self, gateway: "SimulatedNovaForge") -> None:
        self._gateway = gateway

    def status(self) -> dict[str, Any]:
        return self._gateway.observer_status()

    def frame_path(self) -> Path | None:
        return self._gateway.frame_path()


class SimulatedNovaForge:
    """The simulated game plus the REAL NF-16…NF-21 stack over it.

    The instance satisfies :class:`~novacontrol.novaforge.autonomy.AutonomyGateway`,
    so the shipped :class:`~novacontrol.novaforge.autonomy.AutonomousSessionController`
    drives it exactly as it drives the manager.
    """

    def __init__(self, *, game: SimulatedGame | None = None) -> None:
        self.game = game or SimulatedGame()
        self.session_id = SIM_SESSION_ID
        self.started_at = _stamp()
        self.paused = False
        self.stopped = False
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._observations = 0
        self._sequence = 0
        self._captured: SceneRepresentation | None = None
        self._pinned = True
        self._tmp = TemporaryDirectory(prefix="nf22-sim-")
        self._frame = Path(self._tmp.name) / "000001.png"
        self._frame.write_bytes(b"\x89PNG\r\n\x1a\nnf22-simulated-frame")
        self._input_released = 0
        # ── the REAL services, wired through their own injected seams ──────
        # Every registry here is the SHIPPED one: the fixture profile, the
        # fixture objective rule, its plan rule and its verification rule already
        # exist for offline evaluation, so this module declares no criteria of
        # its own and cannot invent one for the simulator's convenience.
        self.objectives = ObjectiveService(
            registry=default_objective_registry(), publish=self._publish
        )
        self.state = GameStateService(
            observer=_SimulatedObserver(self),
            perceive=self._perceive,
            on_state=self.objectives.observe,
            registry=default_registry(),
        )
        self.planner = GamePlanService(
            policy=PlanPolicy(),
            context_reader=self._planning_context,
            submit=self._submit,
            rules=default_plan_rules(),
            publish=self._publish,
        )
        self.verification = GameVerificationService(
            policy=VerificationPolicy(),
            context_reader=self._verification_context,
            observe=self._observe_evidence,
            rules=default_verification_rules(),
            record=self._record_outcomes,
            invalidate=self._invalidate,
            replan=self._replan,
            pause=self._pause_from_recovery,
            stop=self._stop_from_recovery,
            notify=self._notify_operator,
            publish=self._publish,
        )

    # ── lifecycle used by tests ───────────────────────────────────────────
    async def shutdown(self) -> None:
        await self.state.shutdown()
        await self.objectives.shutdown()
        await self.verification.shutdown()
        await self.planner.shutdown()
        self._tmp.cleanup()

    # ── NF-15: the simulated reading link ─────────────────────────────────
    async def observe_frame_once(self) -> dict[str, Any]:
        """One immediate read of the simulated world, or its refusal."""
        if self.stopped:
            return {
                "status": "unavailable",
                "reason_code": "stopped",
                "reason": "The simulated session was stopped; no read was attempted.",
            }
        if self.paused:
            return {
                "status": "not_observing",
                "reason_code": "paused_by_user",
                "reason": "Observation is paused; no read was attempted.",
            }
        if not self.game.target_responsive:
            self.game.observations += 1
            return {
                "status": "unavailable",
                "reason_code": "target_not_foreground",
                "reason": "The simulated window no longer answers its own probe.",
                "session_id": self.session_id,
                "target": dict(SIM_TARGET),
            }
        self._sequence += 1
        self._observations += 1
        self.game.observations += 1
        self._frame.write_bytes(
            b"\x89PNG\r\n\x1a\nnf22-simulated-frame-" + str(self._sequence).encode()
        )
        self._captured = self.game.scene(sequence=self._sequence, frame_id=self._frame.name)
        return {
            "observation_id": f"{self.session_id}:{self._sequence:04d}",
            "session_id": self.session_id,
            "seq": self._sequence,
            "game": SIM_GAME,
            "captured_at": _stamp(),
            "status": "observed",
            "target": dict(SIM_TARGET),
            "width": SIM_WIDTH,
            "height": SIM_HEIGHT,
            "size_bytes": self._frame.stat().st_size,
            "backend": "simulated",
            "source": "simulation.py",
            "fresh": True,
            "target_valid": True,
            "reason_code": "ok",
            "reason": "",
        }

    def observer_status(self) -> dict[str, Any]:
        """The observer shape the state service reads before it reads a state."""
        success = (
            {
                "observation_id": f"{self.session_id}:{self._sequence:04d}",
                "seq": self._sequence,
                "at": _stamp(),
                "width": SIM_WIDTH,
                "height": SIM_HEIGHT,
                "backend": "simulated",
            }
            if self._sequence
            else None
        )
        return {
            "state": "paused" if self.paused else ("stopped" if self.stopped else "observing"),
            "session_id": self.session_id,
            "game": SIM_GAME,
            "target": dict(SIM_TARGET),
            "last_success": success,
        }

    def frame_path(self) -> Path | None:
        return self._frame

    async def _perceive(self, _frame: Any) -> Any:
        """The perception seam over the simulated scene (no engine involved)."""
        scene = self._captured or self.game.scene(sequence=1, frame_id="frame")
        return SimpleNamespace(
            status="success",
            scene=scene,
            latency={"total": 1.0, "simulated": True},
            capability=lambda name: SimpleNamespace(available=True, reason=""),
        )

    # ── NF-16 / NF-17 seams ───────────────────────────────────────────────
    async def read_state(self) -> dict[str, Any]:
        """NF-16's reading of the newest validated simulated frame."""
        if self._sequence == 0:
            return {
                "analyzed": False,
                "reason_code": "no_validated_frame",
                "reason": "No simulated frame has been validated yet.",
            }
        outcome = await self.state.analyze()
        return outcome.to_dict()

    def objective_picture(self, session_id: str = "") -> dict[str, Any]:
        """NF-17's newest interpretation of that reading."""
        current = self.objectives.current
        return current.to_dict() if current is not None else {}

    # ── the gateway surface the autonomous supervisor drives ──────────────
    def get(self, session_id: str) -> Any | None:
        if session_id != self.session_id or self.stopped:
            return None
        return SimpleNamespace(
            session_id=self.session_id,
            target=SimpleNamespace(**SIM_TARGET),
            target_validated=self._pinned,
            state="stopped" if self.stopped else ("paused" if self.paused else "running"),
        )

    async def plan_actions(self, session_id: str) -> dict[str, Any]:
        self._require(session_id)
        plan = await self.planner.plan(session_id)
        return {"session_id": session_id, "plan": plan.to_dict(), "effect_verified": False}

    async def execute_plan(
        self, session_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(session_id)
        plan_id = str(payload.get("plan_id") or "")
        plan = await self.planner.execute(
            session_id, plan_id, approved=payload.get("approved") is True
        )
        return {"session_id": session_id, "plan": plan.to_dict(), "effect_verified": False}

    async def verify_outcomes(
        self, session_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(session_id)
        plan_id = str(payload.get("plan_id") or "")
        current = self.planner.current_for(session_id)
        if not plan_id and current is not None:
            plan_id = current.plan_id
        result = await self.verification.verify(session_id, plan_id)
        return {
            "session_id": session_id,
            "plan_id": plan_id,
            "verification": result.to_dict(),
            "effect_verified": result.effect_verified,
        }

    async def recover_outcome(
        self, session_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(session_id)
        return await self.verification.recover(
            session_id,
            str(payload.get("outcome_id") or ""),
            approve=payload.get("approve") is True,
        )

    async def pause(self, session_id: str, *, reason: str = "") -> dict[str, Any]:
        self._require(session_id)
        self.paused = True
        return {
            "session_id": session_id,
            "paused": True,
            "reason": reason or "operator",
            "note": "The simulated world is paused; no further input is sent.",
        }

    async def resume(self, session_id: str) -> dict[str, Any]:
        self._require(session_id)
        self.paused = False
        return {"session_id": session_id, "resumed": True}

    async def stop(self, session_id: str, *, reason: str = "") -> dict[str, Any]:
        self._require(session_id)
        self.stopped = True
        # Cleanup is reported as what actually happened, never as a claim: the
        # simulated backend holds one key at most, and it is released here.
        self._input_released += 1
        return {
            "session_id": session_id,
            "stopped": True,
            "reason": reason or "operator",
            "cleanup": {
                "released": self._input_released,
                "held": [],
                "ok": True,
                "note": "The simulated input backend held no key; the release is a no-op.",
            },
        }

    # ── the seams NF-20 and NF-21 are constructed with ────────────────────
    async def _planning_context(self, session_id: str, refresh: bool) -> Any:
        from .planner import PlanningContext

        if refresh:
            await self._refresh_state()
        state = self.state.current
        return PlanningContext(
            session_id=session_id,
            game=state.game if state is not None else SIM_GAME,
            target=self._control_target(),
            state=state,
            objective_state=self.objectives.current,
            knowledge=(),
            gate={"allow": True, "state": "open", "reason": ""},
            budget_remaining=None,
            state_age_seconds=0.0,
            control_policy=self._control_policy(),
        )

    async def _verification_context(self, session_id: str, plan_id: str) -> VerificationContext:
        plan = self.planner.get(plan_id)
        state = self.state.current
        baseline: dict[str, Mapping[str, Any]] = {}
        if state is not None:
            for row in state.hud:
                baseline[row.element_id] = row.to_dict()
        return VerificationContext(
            session_id=session_id,
            game=state.game if state is not None else SIM_GAME,
            plan=plan,
            target=dict(SIM_TARGET),
            gate={"allow": True, "state": "open", "reason": ""},
            objective_state=self.objectives.current,
            baseline_hud=baseline,
            baseline_state_id=state.state_id if state is not None else "",
            baseline_observation_id=(
                state.observation.observation_id if state is not None else ""
            ),
            baseline_sequence=state.sequence if state is not None else 0,
        )

    async def _observe_evidence(self, session_id: str) -> ObservationEvidence:
        """NF-21's post-action reading: a NEW simulated frame, read for real."""
        await self.observe_frame_once()
        outcome = await self.state.analyze()
        payload = outcome.to_dict()
        state = self.state.current
        if not payload.get("analyzed") or state is None:
            return ObservationEvidence(
                observation_id="",
                session_id=session_id,
                status=str(payload.get("status") or "unavailable"),
                accepted=False,
                code=str(payload.get("reason_code") or "no_state"),
                reason=str(payload.get("reason") or "The simulated reading produced no state."),
                target=dict(SIM_TARGET),
            )
        readings: dict[str, Mapping[str, Any]] = {}
        for row in state.hud:
            readings[row.element_id] = row.to_dict()
        return ObservationEvidence(
            observation_id=state.observation.observation_id,
            state_id=state.state_id,
            session_id=session_id,
            game=state.game,
            sequence=state.sequence,
            at=state.timestamp,
            age_seconds=0.0,
            status=state.status.value,
            accepted=True,
            readings=readings,
            changes=tuple(row.to_dict() for row in state.changes),
            capture={"backend": "simulated"},
            target=dict(SIM_TARGET),
            state=state,
        )

    async def _record_outcomes(
        self, session_id: str, plan_id: str, outcomes: Any
    ) -> Any:
        """The planner's own record of NF-21's verdicts (NF-20's seam)."""
        plan = self.planner.get(plan_id)
        if plan is None:
            return None
        rows = [dict(row) for row in outcomes]
        verified = bool(rows) and all(
            str(row.get("status") or "") == "verified_success" for row in rows
        )
        plan.execution["outcomes"] = rows
        plan.execution["effect_verified"] = verified
        plan.execution["effect_note"] = (
            "Every dispatched step's declared observable was observed; no wider claim "
            "is made about the simulated world."
            if verified
            else "At least one dispatched step's effect was not established."
        )
        return plan.to_dict()

    async def _invalidate(self, session_id: str, plan_id: str) -> Any:
        plan = self.planner.get(plan_id)
        if plan is None:
            return {"invalidated": False, "reason": "no such plan"}
        plan.invalidation = {
            "code": "recovery_invalidated",
            "reason": "NF-21's recovery invalidated this plan.",
            "at": _stamp(),
        }
        return {"invalidated": True, "plan_id": plan_id}

    async def _replan(self, session_id: str, plan_id: str, reason: str) -> Any:
        plan = await self.planner.replan(session_id, plan_id, reason=reason or "recovery")
        return {"plan_id": plan.plan_id, "status": plan.status.value, "code": plan.code}

    async def _pause_from_recovery(self, session_id: str, reason: str) -> Any:
        return await self.pause(session_id, reason=reason)

    async def _stop_from_recovery(self, session_id: str, reason: str) -> Any:
        return await self.stop(session_id, reason=reason)

    async def _notify_operator(self, session_id: str, payload: Mapping[str, Any]) -> Any:
        self.events.append(("novaforge.sim.operator_notified", dict(payload)))
        return {"notified": True, "simulated": True}

    # ── internals ─────────────────────────────────────────────────────────
    async def _refresh_state(self) -> None:
        await self.observe_frame_once()
        await self.state.analyze()

    def _control_target(self) -> ControlTarget:
        return ControlTarget(
            session_id=self.session_id,
            title=SIM_TITLE,
            handle=int(SIM_TARGET["handle"]),
            pid=int(SIM_TARGET["pid"]),
            process="novaforge-sim",
            start_time=self.started_at,
            game=SIM_GAME,
        )

    @staticmethod
    def _control_policy() -> Any:
        """The interface's own declared bounds (defaults, nothing widened)."""
        from .control import ControlPolicy

        return ControlPolicy()

    async def _submit(self, session_id: str, action: ControlAction) -> dict[str, Any]:
        """The 'input backend' of the simulated game, shaped like NF-19's envelope."""
        started = _stamp()
        ok, code = self.game.press(action.key or SIM_KEY)
        if not ok:
            return {
                "ok": False,
                "status": "rejected",
                "code": code,
                "reason": f"The simulated input backend refused {action.kind.value} {action.key!r}.",
                "result": {"action_id": action.action_id, "status": "rejected"},
            }
        self.events.append(
            (
                "novaforge.sim.input",
                {
                    "action_id": action.action_id,
                    "kind": action.kind.value,
                    "key": action.key,
                    "simulated": True,
                },
            )
        )
        return {
            "ok": True,
            "status": "completed",
            "code": "",
            "reason": "",
            "result": {
                "action_id": action.action_id,
                "status": "completed",
                "kind": action.kind.value,
                "dispatched_at": started,
                "finished_at": _stamp(),
                "duration_ms": 1.0,
                "cleanup": {"released": 0, "held": [], "ok": True},
            },
        }

    async def _publish(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.events.append((event_type, dict(payload)))

    def _require(self, session_id: str) -> None:
        if session_id != self.session_id or self.stopped:
            raise KeyError(session_id)

    # ── one call to run the whole thing ───────────────────────────────────
    def controller(
        self,
        *,
        limits: AutonomyLimits | None = None,
        policy: AutonomyPolicy | None = None,
    ) -> AutonomousSessionController:
        """The shipped supervisor, pointed at this simulated world."""
        return AutonomousSessionController(
            gateway=self,
            limits=limits or simulated_limits(),
            policy=policy or simulated_policy(),
            evidence_source="simulated",
            publish=self._publish_autonomy,
        )

    async def run(
        self,
        *,
        limits: AutonomyLimits | None = None,
        policy: AutonomyPolicy | None = None,
        approved: bool = True,
    ) -> dict[str, Any]:
        """Drive the autonomous loop over the simulated world and report the run."""
        controller = self.controller(limits=limits, policy=policy)
        started = await controller.start(
            self.session_id, approved=approved, note="NF-22 simulated acceptance run"
        )
        if not started.get("started"):
            return {
                "started": False,
                "code": started.get("code"),
                "reason": started.get("reason"),
                "game": self.game.snapshot(),
            }
        await controller.wait(self.session_id)
        run = controller.run_for(self.session_id)
        payload = run.to_dict(cycles=20) if run is not None else {}
        payload["started"] = True
        payload["game"] = self.game.snapshot()
        payload["simulated"] = True
        payload["sim_events"] = [row[0] for row in self.events]
        await controller.shutdown()
        return payload

    async def _publish_autonomy(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.events.append((event_type, dict(payload)))


async def main(*, limits: AutonomyLimits | None = None) -> dict[str, Any]:
    """Run one simulated autonomous session and return its report."""
    forge = SimulatedNovaForge()
    try:
        return await forge.run(limits=limits)
    finally:
        await forge.shutdown()


if __name__ == "__main__":  # pragma: no cover - a manual probe
    import json

    print(json.dumps(asyncio.run(main()), indent=2, default=str))
