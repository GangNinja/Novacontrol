"""NF-25 — the bounded task layer: preflight gates, judging, and the adapter.

Every test here drives the real `tasks` module over fakes for the services it
delegates to. Nothing launches a game, focuses a window, or sends input: the
supervisor is replaced by a scripted gateway, and every run payload is a fixture.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
from unittest import IsolatedAsyncioTestCase

from novacontrol.novaforge.autonomy import MAX_ACTIONS, MAX_CYCLES, AutonomyLimits
from novacontrol.novaforge.control import CONTROL_KINDS
from novacontrol.novaforge.models import MissionConfig
from novacontrol.novaforge.tasks import (
    TASK_SCHEMA_VERSION,
    AutonomyTaskGateway,
    TaskRunner,
    TaskSpec,
    TaskVerdict,
    dispatched_kinds,
    judge_task,
    task_progress,
    task_stages,
)

SESSION = "session-nf25"


# ── fixtures ───────────────────────────────────────────────────────────────


def mission(**overrides: Any) -> MissionConfig:
    payload: dict[str, Any] = {
        "name": "open the options menu",
        "objective": "Open the options menu and return to the main menu.",
        "target_title": "Sim Game",
        "max_actions": 4,
    }
    payload.update(overrides)
    return MissionConfig(**payload)


def spec(**overrides: Any) -> TaskSpec:
    payload: dict[str, Any] = {
        "task_id": "task-1",
        "mission": mission(),
        "game": "Sim Game",
        "success_conditions": ("the main menu is visible again after the options screen",),
        "permitted_actions": ("key_press", "key_release"),
        "initial_scenes": ("menu",),
        "required_evidence": ("scene:menu",),
        "max_seconds": 60.0,
        "max_cycles": 3,
        "observation_max_age_seconds": 5.0,
        "recovery_limit": 1,
    }
    payload.update(overrides)
    return TaskSpec(**payload)


def stamp(age_seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()


def state_payload(
    *,
    kind: str = "menu",
    status: str = "observed",
    age: float = 0.5,
    analyzed: bool = True,
    state_id: str = "state-1",
    observation_id: str = "obs-1",
    timestamp: str | None = None,
    observation_age: float | None = None,
) -> dict[str, Any]:
    if not analyzed:
        return {"analyzed": False, "reason": "no validated frame", "state": None}
    return {
        "analyzed": True,
        "reason": "",
        "state": {
            "game": "Sim Game",
            "state_id": state_id,
            "sequence": 1,
            "timestamp": timestamp if timestamp is not None else stamp(age),
            "scene": {"kind": kind, "status": status, "evidence": ["GAME MENU"]},
            "observation": {
                "observation_id": observation_id,
                "at": stamp(observation_age if observation_age is not None else age),
            },
        },
    }


def run_payload(
    *,
    phase: str = "COMPLETED",
    outcome: str = "objective_completed",
    evidence: Sequence[str] = ("state:state-1", "scene:menu", "objective:obj-1"),
    dispatched: int = 2,
    outcomes: Sequence[Mapping[str, Any]] = (),
    reason: str = "The objective completed.",
    source: str = "live",
    recoveries: int = 0,
    cycles: int = 1,
) -> dict[str, Any]:
    rows = [
        {
            "index": index + 1,
            "state": {"state_id": "state-1", "scene": {"kind": "menu"}},
            "verification": {"outcomes": [dict(item) for item in outcomes]},
            "objective": {
                "objective_id": "obj-1",
                "evidence": [{"reference": "objective-evidence-1"}],
            },
        }
        for index in range(cycles)
    ]
    return {
        "run_id": "run-1",
        "session_id": SESSION,
        "phase": phase,
        "outcome": outcome,
        "reason": reason,
        "game": "Sim Game",
        "target": {"title": "Sim Game — Main Menu"},
        "objective": "Open the options menu and return to the main menu.",
        "objective_id": "obj-1",
        "evidence_source": source,
        "started_at": stamp(12),
        "ended_at": stamp(0),
        "limits": {"max_actions": 4, "max_cycles": 3},
        "actions_dispatched": dispatched,
        "recoveries": recoveries,
        "replans": 0,
        "verification_evidence": list(evidence),
        "cycles": rows,
    }


def staged_run(**overrides: Any) -> dict[str, Any]:
    """A run whose final cycle actually planned, was approved and dispatched."""
    payload = run_payload(**overrides)
    cycle = payload["cycles"][-1]
    cycle["plan"] = {"plan_id": "plan-1", "steps": [{"index": 1, "kind": "key_press"}]}
    cycle["approval"] = {"approved": True}
    cycle["execution"] = {
        "status": "executed",
        "steps": [{"index": 1, "kind": "key_press", "ok": True}],
    }
    return payload


def verified_outcome(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "outcome_id": "outcome-1",
        "status": "verified_success",
        "expected_effect": "the options screen is visible",
        "observed_effect": "the options screen is visible",
        "evidence": [
            {"ref": "state:state-2", "observation_id": "obs-2", "state_id": "state-2", "accepted": True}
        ],
    }
    payload.update(overrides)
    return payload


class FakeGateway:
    """A scripted `TaskGateway`: every answer is written by the test."""

    def __init__(
        self,
        *,
        exists: bool = True,
        title: str = "Sim Game — Main Menu",
        state: Mapping[str, Any] | None = None,
        allowed: Sequence[str] = tuple(sorted(CONTROL_KINDS)),
        resources: Mapping[str, Any] | None = None,
        start_answer: Mapping[str, Any] | None = None,
        wait_answer: Mapping[str, Any] | None = None,
        run_value: Mapping[str, Any] | None = None,
        cancel_answer: Mapping[str, Any] | None = None,
        cancel_error: Exception | None = None,
        held: Sequence[Mapping[str, Any]] | None = (),
    ) -> None:
        self.exists = exists
        self.title = title
        self.state = state if state is not None else state_payload()
        self.allowed = list(allowed)
        self.resources = dict(resources or {})
        self.start_answer = dict(start_answer or {"started": True, "run_id": "run-1"})
        self.wait_answer = (
            dict(wait_answer)
            if wait_answer is not None
            else {"finished": True, "status": run_payload()}
        )
        self.run_value = (
            dict(run_value) if run_value is not None else run_payload()
        )
        self.cancel_answer = dict(
            cancel_answer
            or {
                "cancelled": True,
                "code": "cancelled",
                "reason": "The operator cancelled the run.",
                "status": {"phase": "CANCELLED", "outcome": "session_cancelled"},
            }
        )
        self.cancel_error = cancel_error
        self.held = held
        self.started: list[dict[str, Any]] = []
        self.waited: list[float | None] = []
        self.state_reads = 0
        self.cancelled: list[str] = []

    def session(self, session_id: str) -> Any | None:
        return SimpleNamespace(session_id=session_id) if self.exists else None

    def target_title(self, session_id: str) -> str:
        return self.title

    async def read_state(self) -> Mapping[str, Any]:
        self.state_reads += 1
        if isinstance(self.state, Exception):
            raise self.state
        return self.state

    def objective_picture(self, session_id: str) -> Mapping[str, Any]:
        return {"objective_id": "obj-1"}

    def allowed_actions(self) -> Sequence[str]:
        return tuple(self.allowed)

    def resource_gate(self) -> Mapping[str, Any]:
        return dict(self.resources)

    async def start_run(
        self, session_id: str, spec: TaskSpec, *, approved: bool, note: str
    ) -> Mapping[str, Any]:
        self.started.append(
            {"session_id": session_id, "spec": spec, "approved": approved, "note": note}
        )
        return dict(self.start_answer)

    async def wait_run(
        self, session_id: str, *, timeout: float | None
    ) -> Mapping[str, Any]:
        self.waited.append(timeout)
        return dict(self.wait_answer)

    def run(self, session_id: str) -> Mapping[str, Any] | None:
        return dict(self.run_value) if self.run_value else None

    async def cancel(self, session_id: str, *, reason: str) -> Mapping[str, Any]:
        self.cancelled.append(reason)
        if self.cancel_error is not None:
            raise self.cancel_error
        return dict(self.cancel_answer)

    def held_inputs(self) -> Sequence[Mapping[str, Any]] | None:
        return self.held


# ── spec validation ────────────────────────────────────────────────────────


class TaskSpecTests(unittest.TestCase):
    """A task that cannot be verified is refused before anything runs."""

    def test_a_bounded_task_is_runnable(self) -> None:
        task = spec()
        self.assertEqual(task.errors(), [])
        self.assertTrue(task.runnable)
        self.assertEqual(task.task_id, "task-1")

    def test_a_task_must_name_its_game(self) -> None:
        problems = spec(game="").errors()
        self.assertTrue(any("name the game" in item for item in problems))

    def test_a_task_without_success_conditions_is_refused(self) -> None:
        problems = spec(success_conditions=()).errors()
        self.assertTrue(any("at least one success condition" in item for item in problems))

    def test_a_vague_goal_is_refused(self) -> None:
        problems = spec(success_conditions=("play well",)).errors()
        self.assertTrue(any("states no observable outcome" in item for item in problems))
        # The same words with a visible outcome are acceptable.
        self.assertEqual(
            spec(
                success_conditions=("play well enough that the victory banner is visible",)
            ).errors(),
            [],
        )

    def test_a_task_without_evidence_is_refused(self) -> None:
        problems = spec(required_evidence=()).errors()
        self.assertTrue(any("name the evidence" in item for item in problems))

    def test_an_unknown_action_kind_is_refused(self) -> None:
        problems = spec(permitted_actions=("key_press", "cast spell")).errors()
        self.assertTrue(any("cast spell" in item for item in problems))

    def test_a_duplicate_action_kind_is_refused(self) -> None:
        problems = spec(permitted_actions=("key_press", "key_press")).errors()
        self.assertTrue(any("declared twice" in item for item in problems))

    def test_a_task_without_an_action_budget_is_refused(self) -> None:
        problems = spec(mission=mission(max_actions=None)).errors()
        self.assertTrue(any("action budget" in item for item in problems))

    def test_a_budget_above_the_ceiling_is_refused_not_clamped(self) -> None:
        problems = spec(mission=mission(max_actions=MAX_ACTIONS + 1)).errors()
        self.assertTrue(any("refused, never clamped" in item for item in problems))
        self.assertIn(
            "max_actions_per_plan must not exceed the task's action budget.",
            spec(mission=mission(max_actions=2), max_actions_per_plan=3).errors(),
        )

    def test_a_cycle_budget_above_the_supervisor_ceiling_is_refused(self) -> None:
        problems = spec(max_cycles=MAX_CYCLES + 1).errors()
        self.assertTrue(any("max_cycles" in item for item in problems))

    def test_an_unknown_initial_scene_is_refused(self) -> None:
        problems = spec(initial_scenes=("credits",)).errors()
        self.assertTrue(any("not a scene this build can name" in item for item in problems))

    def test_unknown_cannot_be_declared_an_allowed_initial_scene(self) -> None:
        problems = spec(initial_scenes=("menu", "unknown")).errors()
        self.assertTrue(any("cannot be an allowed initial scene" in item for item in problems))

    def test_a_too_short_evidence_tag_is_refused(self) -> None:
        problems = spec(required_evidence=("a",)).errors()
        self.assertTrue(any("would match almost any evidence" in item for item in problems))

    def test_a_task_longer_than_its_mission_time_limit_is_refused(self) -> None:
        problems = spec(mission=mission(time_limit_seconds=30), max_seconds=60.0).errors()
        self.assertTrue(any("cannot outlive its session's budget" in item for item in problems))
        # A task inside the mission's limit is fine.
        self.assertEqual(
            spec(mission=mission(time_limit_seconds=120), max_seconds=60.0).errors(), []
        )

    def test_an_impossible_freshness_bound_is_refused(self) -> None:
        self.assertTrue(
            any(
                "observation_max_age_seconds" in item
                for item in spec(observation_max_age_seconds=0.0).errors()
            )
        )
        self.assertTrue(
            any(
                "observation_max_age_seconds" in item
                for item in spec(observation_max_age_seconds=90.0).errors()
            )
        )

    def test_the_task_limits_translate_to_the_supervisors_own_limits(self) -> None:
        limits = spec().autonomy_limits()
        self.assertIsInstance(limits, AutonomyLimits)
        self.assertEqual(limits.errors(), [])
        self.assertEqual(limits.max_actions, 4)
        self.assertEqual(limits.max_cycles, 3)
        self.assertEqual(limits.session_seconds, 60.0)
        self.assertEqual(limits.recovery_attempts, 1)

    def test_the_payload_states_the_limits_and_the_evidence(self) -> None:
        payload = spec().to_dict()
        self.assertEqual(payload["limits"]["max_actions"], 4)
        self.assertEqual(payload["required_evidence"], ["scene:menu"])
        self.assertTrue(payload["requires_approval"])


# ── preflight gates ────────────────────────────────────────────────────────


class TaskPreflightTests(IsolatedAsyncioTestCase):
    """Every gate refuses by name, and nothing reaches the supervisor."""

    async def test_an_invalid_spec_never_reaches_the_supervisor(self) -> None:
        gateway = FakeGateway()
        report = await TaskRunner(gateway).start(
            spec(success_conditions=()), SESSION, approved=True
        )
        self.assertEqual(report.status, "refused")
        self.assertEqual(report.code, "invalid_task")
        self.assertEqual(gateway.started, [])

    async def test_an_unapproved_task_never_reaches_the_supervisor(self) -> None:
        gateway = FakeGateway()
        report = await TaskRunner(gateway).start(spec(), SESSION)
        self.assertEqual(report.code, "approval_required")
        self.assertEqual(gateway.started, [])
        self.assertIn("explicit approval", report.reason)

    async def test_an_unknown_session_is_refused(self) -> None:
        report = await TaskRunner(FakeGateway(exists=False)).start(
            spec(), SESSION, approved=True
        )
        self.assertEqual(report.code, "unknown_session")

    async def test_an_unpinned_target_is_refused(self) -> None:
        report = await TaskRunner(FakeGateway(title="")).start(
            spec(), SESSION, approved=True
        )
        self.assertEqual(report.code, "target_unverified")

    async def test_a_different_window_is_refused(self) -> None:
        report = await TaskRunner(FakeGateway(title="Another Game")).start(
            spec(), SESSION, approved=True
        )
        self.assertEqual(report.code, "target_mismatch")
        self.assertIn("Another Game", report.reason)

    async def test_an_action_the_build_cannot_perform_is_refused(self) -> None:
        gateway = FakeGateway(allowed=("key_press",))
        report = await TaskRunner(gateway).start(spec(), SESSION, approved=True)
        self.assertEqual(report.code, "unsupported_action")
        self.assertEqual(gateway.started, [])

    async def test_an_unavailable_control_layer_is_refused_by_name(self) -> None:
        report = await TaskRunner(FakeGateway(allowed=())).start(
            spec(), SESSION, approved=True
        )
        self.assertEqual(report.code, "unsupported_action")
        self.assertIn("No input path", report.reason)

    async def test_an_unreadable_state_is_refused(self) -> None:
        gateway = FakeGateway(state=RuntimeError("observer exploded"))
        report = await TaskRunner(gateway).start(spec(), SESSION, approved=True)
        self.assertEqual(report.code, "initial_state_unavailable")
        self.assertIn("RuntimeError", report.reason)
        self.assertEqual(gateway.started, [])

    async def test_a_refused_analysis_is_refused(self) -> None:
        report = await TaskRunner(
            FakeGateway(state=state_payload(analyzed=False))
        ).start(spec(), SESSION, approved=True)
        self.assertEqual(report.code, "initial_state_unavailable")
        self.assertIn("no validated frame", report.reason)

    async def test_an_unnamed_screen_is_refused(self) -> None:
        report = await TaskRunner(
            FakeGateway(state=state_payload(kind="unknown", status="unknown"))
        ).start(spec(), SESSION, approved=True)
        self.assertEqual(report.code, "initial_state_unknown")

    async def test_a_stale_state_is_refused(self) -> None:
        report = await TaskRunner(
            FakeGateway(state=state_payload(age=30.0))
        ).start(spec(), SESSION, approved=True)
        self.assertEqual(report.code, "initial_state_stale")
        self.assertIn("30.0 s old", report.reason)

    async def test_freshness_is_the_frames_age_not_the_analysiss_age(self) -> None:
        # The state was built a moment ago (fresh timestamp) from an old frame:
        # the task's bound is about the frame, so this must be refused.
        report = await TaskRunner(
            FakeGateway(state=state_payload(age=0.2, observation_age=30.0))
        ).start(spec(), SESSION, approved=True)
        self.assertEqual(report.code, "initial_state_stale")
        self.assertIn("30.0 s old", report.reason)

    async def test_an_observation_without_a_readable_time_is_refused(self) -> None:
        payload = state_payload()
        payload["state"]["observation"]["at"] = "not-a-time"
        report = await TaskRunner(FakeGateway(state=payload)).start(
            spec(), SESSION, approved=True
        )
        self.assertEqual(report.code, "initial_state_stale")
        self.assertIn("no readable timestamp", report.reason)

    async def test_a_different_game_in_the_state_is_refused(self) -> None:
        payload = state_payload()
        payload["state"]["game"] = "A Different Game"
        report = await TaskRunner(FakeGateway(state=payload)).start(
            spec(), SESSION, approved=True
        )
        self.assertEqual(report.code, "game_mismatch")
        self.assertIn("A Different Game", report.reason)

    async def test_the_wrong_initial_scene_is_refused(self) -> None:
        report = await TaskRunner(
            FakeGateway(state=state_payload(kind="death"))
        ).start(spec(), SESSION, approved=True)
        self.assertEqual(report.code, "initial_state_mismatch")
        self.assertIn("death", report.reason)

    async def test_a_refusing_resource_gate_stops_the_task(self) -> None:
        gateway = FakeGateway(
            resources={"allow": False, "reason": "only 1.2 GB free", "state": "critical"}
        )
        report = await TaskRunner(gateway).start(spec(), SESSION, approved=True)
        self.assertEqual(report.code, "resource_gate_refused")
        self.assertEqual(report.resource_gate["state"], "critical")
        self.assertEqual(gateway.started, [])

    async def test_a_supervisor_refusal_is_reported_with_its_own_code(self) -> None:
        gateway = FakeGateway(
            start_answer={"started": False, "code": "already_running", "reason": "busy"}
        )
        report = await TaskRunner(gateway).start(spec(), SESSION, approved=True)
        self.assertEqual(report.status, "refused")
        self.assertEqual(report.code, "already_running")
        self.assertEqual(report.reason, "busy")

    async def test_a_broken_target_reader_is_a_refusal_not_a_crash(self) -> None:
        class Broken(FakeGateway):
            def target_title(self, session_id: str) -> str:  # type: ignore[override]
                raise RuntimeError("window list gone")

        report = await TaskRunner(Broken()).start(spec(), SESSION, approved=True)
        self.assertEqual(report.code, "target_unverified")
        self.assertIn("window list gone", report.reason)

    async def test_a_broken_control_policy_is_a_refusal_not_a_crash(self) -> None:
        class Broken(FakeGateway):
            def allowed_actions(self) -> Sequence[str]:  # type: ignore[override]
                raise RuntimeError("control layer gone")

        report = await TaskRunner(Broken()).start(spec(), SESSION, approved=True)
        self.assertEqual(report.code, "unsupported_action")
        self.assertIn("control layer gone", report.reason)

    async def test_an_unreadable_resource_gate_is_not_measured_not_refused(self) -> None:
        class Broken(FakeGateway):
            def resource_gate(self) -> Mapping[str, Any]:  # type: ignore[override]
                raise RuntimeError("governor missing")

        report = await TaskRunner(Broken()).start(spec(), SESSION, approved=True)
        self.assertEqual(report.status, "running")
        self.assertIsNone(report.resource_gate.get("allow"))
        self.assertIn("could not be read", report.resource_gate.get("reason", ""))

    async def test_a_start_fault_is_a_refusal_not_a_crash(self) -> None:
        class Broken(FakeGateway):
            async def start_run(self, session_id, spec, *, approved, note):  # type: ignore[override]
                raise RuntimeError("gateway blew up")

        report = await TaskRunner(Broken()).start(spec(), SESSION, approved=True)
        self.assertEqual(report.code, "start_failed")
        self.assertIn("RuntimeError", report.reason)

    async def test_a_started_task_pins_its_opening_evidence(self) -> None:
        gateway = FakeGateway(
            start_answer={
                "started": True,
                "run_id": "run-9",
                "objective_id": "obj-1",
                "limits": {"max_actions": 4},
            }
        )
        report = await TaskRunner(gateway).start(spec(), SESSION, approved=True, note="ok")
        self.assertEqual(report.status, "running")
        self.assertEqual(report.code, "started")
        self.assertEqual(report.initial["scene"], "menu")
        self.assertEqual(report.initial["scene_status"], "observed")
        self.assertEqual(report.initial["state_id"], "state-1")
        self.assertEqual(report.initial["observation_id"], "obs-1")
        self.assertIsNotNone(report.initial["age_seconds"])
        self.assertEqual(report.run["run_id"], "run-9")
        self.assertEqual(gateway.started[0]["approved"], True)
        self.assertEqual(gateway.started[0]["note"], "ok")

    async def test_two_tasks_cannot_run_on_one_session(self) -> None:
        gateway = FakeGateway(
            start_answer={"started": True, "run_id": "run-1"},
            wait_answer={"finished": False, "reason": "still running", "status": run_payload(phase="EXECUTING", outcome="")},
        )
        runner = TaskRunner(gateway)
        first = await runner.start(spec(), SESSION, approved=True)
        self.assertEqual(first.status, "running")
        second = await runner.start(spec(task_id="task-2"), SESSION, approved=True)
        self.assertEqual(second.code, "already_running")
        self.assertEqual(len(gateway.started), 1)


# ── judging ────────────────────────────────────────────────────────────────


class JudgeTaskTests(unittest.TestCase):
    """The judge's job is to refuse a completion it cannot evidence."""

    def test_a_completed_run_with_its_evidence_is_verified(self) -> None:
        report = judge_task(spec(), run_payload(outcomes=(verified_outcome(),)))
        self.assertEqual(report.verdict, TaskVerdict.VERIFIED_COMPLETION.value)
        self.assertTrue(report.completed)
        self.assertEqual(report.verification["basis"], "nf21_verified_effect")
        self.assertEqual(report.missing_evidence, [])
        self.assertFalse(report.simulated)

    def test_completion_from_nf17_objective_evidence_is_verified_but_named(self) -> None:
        report = judge_task(spec(), run_payload())
        self.assertEqual(report.verdict, TaskVerdict.VERIFIED_COMPLETION.value)
        self.assertEqual(report.verification["basis"], "nf17_objective_evidence")

    def test_a_missing_required_tag_keeps_completion_unverified(self) -> None:
        report = judge_task(
            spec(required_evidence=("scene:menu", "health:full")),
            run_payload(outcomes=(verified_outcome(),)),
        )
        self.assertEqual(report.verdict, TaskVerdict.OUTCOME_UNKNOWN.value)
        self.assertEqual(report.code, "evidence_missing")
        self.assertEqual(report.missing_evidence, ["health:full"])
        self.assertFalse(report.completed)
        self.assertIn("ending alone", report.reason)

    def test_the_basis_names_the_last_cycles_verified_effect(self) -> None:
        payload = run_payload(outcomes=(), cycles=2)
        payload["cycles"][0]["verification"]["outcomes"] = [verified_outcome()]
        report = judge_task(spec(), payload)
        self.assertEqual(report.verdict, TaskVerdict.VERIFIED_COMPLETION.value)
        self.assertEqual(report.verification["basis"], "nf17_objective_evidence")

    def test_evidence_without_an_acceptance_flag_is_not_accepted(self) -> None:
        outcome = verified_outcome(
            evidence=[{"ref": "state:state-2", "observation_id": "obs-2"}]
        )
        report = judge_task(
            spec(required_evidence=("state:state-2",)),
            run_payload(evidence=(), outcomes=(outcome,)),
        )
        self.assertEqual(report.verdict, TaskVerdict.OUTCOME_UNKNOWN.value)
        self.assertEqual(report.missing_evidence, ["state:state-2"])

    def test_an_objective_id_alone_is_not_evidence(self) -> None:
        payload = run_payload(evidence=(), outcomes=(), cycles=1)
        payload["cycles"][-1]["objective"] = {"objective_id": "obj-1", "evidence": []}
        report = judge_task(spec(required_evidence=("objective:obj-1",)), payload)
        self.assertEqual(report.verdict, TaskVerdict.OUTCOME_UNKNOWN.value)
        self.assertEqual(report.missing_evidence, ["objective:obj-1"])

    def test_completion_without_any_evidence_is_not_claimed(self) -> None:
        payload = run_payload(evidence=(), outcomes=(), cycles=0)
        report = judge_task(spec(required_evidence=()), payload)
        self.assertEqual(report.verdict, TaskVerdict.OUTCOME_UNKNOWN.value)
        self.assertEqual(report.code, "no_evidence")

    def test_only_the_final_cycle_supplies_the_screen_evidence(self) -> None:
        # An earlier cycle saw the menu; the cycle the completion was read from
        # shows a death screen. The earlier token must not prove the final one.
        payload = run_payload(evidence=(), outcomes=(verified_outcome(),), cycles=2)
        payload["cycles"][-1]["state"]["scene"]["kind"] = "death"
        report = judge_task(spec(required_evidence=("scene:menu",)), payload)
        self.assertEqual(report.verdict, TaskVerdict.OUTCOME_UNKNOWN.value)
        self.assertEqual(report.missing_evidence, ["scene:menu"])
        self.assertNotIn("scene:menu", report.evidence_found)
        self.assertIn("scene:death", report.evidence_found)

    def test_a_dispatched_input_is_not_a_response(self) -> None:
        payload = run_payload(
            phase="VERIFYING",
            outcome="outcome_unknown",
            evidence=(),
            dispatched=3,
            outcomes=(
                {
                    "outcome_id": "o-1",
                    "status": "outcome_unknown",
                    "observed_effect": "",
                    "evidence": [],
                },
            ),
            reason="nothing was readable afterwards",
        )
        report = judge_task(spec(), payload)
        self.assertEqual(report.verdict, TaskVerdict.OUTCOME_UNKNOWN.value)
        self.assertEqual(report.progress["actions_dispatched"], 3)
        self.assertEqual(report.progress["actions_responded"], 0)
        self.assertEqual(report.progress["unverified_outcomes"], 1)

    def test_a_verified_failure_is_reported_as_failure(self) -> None:
        report = judge_task(
            spec(),
            run_payload(
                phase="FAILED",
                outcome="objective_failed",
                evidence=(),
                outcomes=(verified_outcome(status="verified_failure", observed_effect="no change"),),
                reason="the marker never appeared",
            ),
        )
        self.assertEqual(report.verdict, TaskVerdict.OBJECTIVE_FAILED.value)
        self.assertEqual(report.progress["verified_failures"], 1)

    def test_cancellation_is_not_failure(self) -> None:
        report = judge_task(
            spec(),
            run_payload(phase="CANCELLED", outcome="session_cancelled", evidence=(), reason="operator stop"),
        )
        self.assertEqual(report.verdict, TaskVerdict.CANCELLED.value)

    def test_a_safety_stop_is_named(self) -> None:
        report = judge_task(
            spec(),
            run_payload(phase="FAILED", outcome="safety_stop", evidence=(), reason="focus lost"),
        )
        self.assertEqual(report.verdict, TaskVerdict.SAFETY_STOP.value)

    def test_a_dependency_failure_is_not_an_objective_failure(self) -> None:
        report = judge_task(
            spec(),
            run_payload(phase="FAILED", outcome="dependency_failure", evidence=(), reason="no plan stage"),
        )
        self.assertEqual(report.verdict, TaskVerdict.DEPENDENCY_UNAVAILABLE.value)

    def test_lost_observation_ends_interrupted(self) -> None:
        report = judge_task(
            spec(),
            run_payload(phase="FAILED", outcome="observation_unavailable", evidence=(), reason="capture stopped"),
        )
        self.assertEqual(report.verdict, TaskVerdict.INTERRUPTED.value)

    def test_a_spent_clock_with_progress_is_progress_only(self) -> None:
        report = judge_task(
            spec(),
            run_payload(
                phase="FAILED",
                outcome="session_timed_out",
                evidence=("state:state-1",),
                outcomes=(verified_outcome(),),
                reason="time budget spent",
            ),
        )
        self.assertEqual(report.verdict, TaskVerdict.PROGRESS_ONLY.value)
        self.assertIn("no verified completion", report.reason)

    def test_a_spent_clock_without_progress_is_neither(self) -> None:
        report = judge_task(
            spec(),
            run_payload(phase="FAILED", outcome="session_timed_out", evidence=(), outcomes=(), dispatched=1),
        )
        self.assertEqual(report.verdict, TaskVerdict.INTERRUPTED.value)

    def test_a_resource_limit_with_progress_is_progress_only(self) -> None:
        report = judge_task(
            spec(),
            run_payload(
                phase="FAILED",
                outcome="resource_limit_reached",
                evidence=("state:state-1",),
                outcomes=(verified_outcome(),),
            ),
        )
        self.assertEqual(report.verdict, TaskVerdict.PROGRESS_ONLY.value)

    def test_a_resource_limit_without_progress_is_a_refusal(self) -> None:
        report = judge_task(
            spec(),
            run_payload(phase="FAILED", outcome="resource_limit_reached", evidence=(), outcomes=()),
        )
        self.assertEqual(report.verdict, TaskVerdict.RESOURCE_REFUSED.value)

    def test_an_unstated_ending_is_unknown(self) -> None:
        report = judge_task(spec(), run_payload(phase="", outcome="", evidence=()))
        self.assertEqual(report.verdict, TaskVerdict.OUTCOME_UNKNOWN.value)

    def test_a_simulated_run_says_so(self) -> None:
        report = judge_task(spec(), run_payload(source="simulated"))
        self.assertTrue(report.simulated)
        self.assertTrue(any("live-game result" in item for item in report.limitations))
        live = judge_task(spec(), run_payload(source="live"))
        self.assertFalse(live.simulated)

    def test_progress_counts_the_three_different_things(self) -> None:
        payload = run_payload(
            outcomes=(
                verified_outcome(),
                verified_outcome(outcome_id="o-2", status="verified_success", evidence=[]),
            ),
            dispatched=2,
        )
        progress = task_progress(payload)
        self.assertEqual(progress["actions_dispatched"], 2)
        self.assertEqual(progress["actions_responded"], 2)
        self.assertEqual(progress["verified_successes"], 2)
        self.assertEqual(progress["cycles"], 1)
        self.assertEqual(progress["recoveries"], 0)

    def test_every_objective_stage_is_answered_separately(self) -> None:
        payload = staged_run()
        del payload["cycles"][-1]["approval"]
        stages = task_stages(payload)
        self.assertTrue(stages["objective_identified"]["reached"])
        # No preflight reading was supplied, so the first stage is NOT inferred
        # from the run's own later observations.
        self.assertFalse(stages["initial_state_verified"]["reached"])
        self.assertTrue(stages["plan_generated"]["reached"])
        self.assertFalse(stages["plan_approved"]["reached"])
        self.assertTrue(stages["action_requested"]["reached"])
        self.assertTrue(stages["input_dispatched"]["reached"])
        self.assertFalse(stages["game_response_observed"]["reached"])
        self.assertFalse(stages["progress_evidence_recorded"]["reached"])
        self.assertFalse(stages["completion_verified"]["reached"])
        with_initial = task_stages(payload, initial={"state_id": "state-1", "scene": "menu"})
        self.assertTrue(with_initial["initial_state_verified"]["reached"])

    def test_a_dispatch_only_run_reports_its_ladder_without_a_response(self) -> None:
        payload = staged_run(phase="VERIFYING", outcome="outcome_unknown", evidence=(), outcomes=())
        report = judge_task(spec(), payload, initial={"state_id": "state-1", "scene": "menu"})
        self.assertTrue(report.stages["plan_approved"]["reached"])
        self.assertTrue(report.stages["input_dispatched"]["reached"])
        self.assertFalse(report.stages["game_response_observed"]["reached"])
        self.assertFalse(report.stages["progress_evidence_recorded"]["reached"])
        self.assertFalse(report.stages["completion_verified"]["reached"])
        self.assertIn("stages", report.to_dict())

    def test_verified_completion_marks_the_final_stage_then(self) -> None:
        report = judge_task(spec(), staged_run(outcomes=(verified_outcome(),)))
        self.assertEqual(report.verdict, TaskVerdict.VERIFIED_COMPLETION.value)
        self.assertTrue(report.stages["completion_verified"]["reached"])
        unverified = judge_task(spec(), run_payload(outcomes=(verified_outcome(),)))
        self.assertEqual(unverified.verdict, TaskVerdict.VERIFIED_COMPLETION.value)
        self.assertTrue(unverified.stages["completion_verified"]["reached"])

    def test_an_action_outside_the_tasks_policy_voids_the_completion(self) -> None:
        payload = staged_run(outcomes=(verified_outcome(),))
        payload["cycles"][-1]["execution"]["steps"].append(
            {"index": 2, "kind": "mouse_move", "ok": True}
        )
        report = judge_task(spec(), payload)
        self.assertEqual(report.verdict, TaskVerdict.SAFETY_STOP.value)
        self.assertEqual(report.code, "action_policy_exceeded")
        self.assertFalse(report.completed)
        policy = report.verification["action_policy"]
        self.assertEqual(policy["violations"], ["mouse_move"])
        self.assertEqual(policy["dispatched"], ["key_press", "mouse_move"])
        self.assertIn("mouse_move", report.reason)

    def test_a_refused_step_is_not_an_action_the_task_performed(self) -> None:
        payload = staged_run(outcomes=(verified_outcome(),))
        payload["cycles"][-1]["execution"]["steps"].append(
            {"index": 2, "kind": "mouse_move", "ok": False}
        )
        report = judge_task(spec(), payload)
        self.assertEqual(report.verdict, TaskVerdict.VERIFIED_COMPLETION.value)
        self.assertEqual(report.verification["action_policy"]["violations"], [])
        self.assertEqual(dispatched_kinds(payload), (["key_press"], 0))

    def test_a_dispatched_step_without_a_kind_keeps_completion_unproven(self) -> None:
        payload = staged_run(outcomes=(verified_outcome(),))
        payload["cycles"][-1]["execution"]["steps"].append({"index": 2, "ok": True})
        report = judge_task(spec(), payload)
        self.assertEqual(report.verdict, TaskVerdict.SAFETY_STOP.value)
        self.assertEqual(report.code, "action_policy_exceeded")
        self.assertEqual(report.verification["action_policy"]["unlabelled"], 1)
        self.assertTrue(any("no action kind" in item for item in report.limitations))

    def test_the_report_carries_the_task_contract(self) -> None:
        report = judge_task(spec(), run_payload())
        payload = report.to_dict()
        self.assertEqual(payload["schema_version"], TASK_SCHEMA_VERSION)
        self.assertEqual(payload["success_conditions"], list(spec().success_conditions))
        self.assertEqual(payload["required_evidence"], ["scene:menu"])
        self.assertTrue(payload["completed"])
        self.assertIn("never a game response", payload["note"])


# ── finish / run / cancel ──────────────────────────────────────────────────


class TaskRunnerLifecycleTests(IsolatedAsyncioTestCase):
    async def test_a_run_starts_then_is_judged(self) -> None:
        gateway = FakeGateway(
            start_answer={"started": True, "run_id": "run-1"},
            wait_answer={"finished": True, "status": run_payload(outcomes=(verified_outcome(),))},
        )
        report = await TaskRunner(gateway).run(spec(), SESSION, approved=True)
        self.assertEqual(report.status, "finished")
        self.assertEqual(report.verdict, TaskVerdict.VERIFIED_COMPLETION.value)
        self.assertEqual(gateway.waited, [70.0])

    async def test_held_input_after_the_run_is_measured(self) -> None:
        gateway = FakeGateway(held=(({"key": "space"},)))
        report = await TaskRunner(gateway).run(spec(), SESSION, approved=True)
        self.assertEqual(report.verification["held_inputs_after"], 1)
        self.assertTrue(any("still held" in item for item in report.limitations))

    async def test_an_unreadable_control_layer_does_not_assume_zero_held(self) -> None:
        gateway = FakeGateway(held=None)
        report = await TaskRunner(gateway).run(spec(), SESSION, approved=True)
        self.assertNotIn("held_inputs_after", report.verification)

    async def test_a_refused_start_is_returned_without_waiting(self) -> None:
        gateway = FakeGateway()
        report = await TaskRunner(gateway).run(spec(), SESSION)
        self.assertEqual(report.status, "refused")
        self.assertEqual(gateway.waited, [])

    async def test_finishing_another_task_on_the_session_is_refused(self) -> None:
        gateway = FakeGateway(
            wait_answer={"finished": False, "reason": "still running", "status": run_payload(phase="EXECUTING", outcome="")}
        )
        runner = TaskRunner(gateway)
        await runner.start(spec(), SESSION, approved=True)
        report = await runner.finish(spec(task_id="task-else"), SESSION)
        self.assertEqual(report.code, "wrong_task")

    async def test_an_unfinished_run_stays_running_and_claims_nothing(self) -> None:
        gateway = FakeGateway(
            wait_answer={
                "finished": False,
                "reason": "still running",
                "status": run_payload(phase="EXECUTING", outcome="", evidence=()),
            }
        )
        runner = TaskRunner(gateway)
        await runner.start(spec(), SESSION, approved=True)
        report = await runner.finish(spec(), SESSION)
        self.assertEqual(report.status, "running")
        self.assertEqual(report.code, "still_running")
        self.assertEqual(report.verdict, "")

    async def test_a_wait_fault_is_unknown_not_completion(self) -> None:
        class Broken(FakeGateway):
            async def wait_run(self, session_id, *, timeout):  # type: ignore[override]
                raise RuntimeError("task vanished")

        runner = TaskRunner(Broken())
        await runner.start(spec(), SESSION, approved=True)
        report = await runner.finish(spec(), SESSION)
        self.assertEqual(report.verdict, TaskVerdict.OUTCOME_UNKNOWN.value)
        self.assertEqual(report.code, "wait_failed")
        self.assertIn("RuntimeError", report.reason)

    async def test_the_same_task_id_with_a_different_objective_is_refused(self) -> None:
        gateway = FakeGateway(
            wait_answer={"finished": False, "reason": "still running", "status": run_payload(phase="EXECUTING", outcome="")}
        )
        runner = TaskRunner(gateway)
        await runner.start(spec(), SESSION, approved=True)
        other = spec(mission=mission(objective="A different objective entirely."))
        report = await runner.finish(other, SESSION)
        self.assertEqual(report.code, "wrong_task")

    async def test_a_replaced_run_is_not_judged_as_this_task(self) -> None:
        gateway = FakeGateway(
            start_answer={"started": True, "run_id": "run-9"},
            wait_answer={"finished": True, "status": run_payload()},
        )
        runner = TaskRunner(gateway)
        await runner.start(spec(), SESSION, approved=True)
        report = await runner.finish(spec(), SESSION)
        self.assertEqual(report.code, "wrong_run")
        self.assertIn("run-9", report.reason)

    async def test_a_run_for_another_objective_is_not_judged_as_this_task(self) -> None:
        payload = run_payload()
        payload["task"] = "Collect ten coins."
        runner = TaskRunner(FakeGateway(wait_answer={"finished": True, "status": payload}))
        report = await runner.finish(spec(), SESSION)
        self.assertEqual(report.status, "refused")
        self.assertEqual(report.code, "wrong_task")
        self.assertIn("Collect ten coins.", report.reason)

    async def test_a_missing_run_record_is_unknown(self) -> None:
        runner = TaskRunner(FakeGateway(wait_answer={"finished": True, "status": None}, run_value={}))
        await runner.start(spec(), SESSION, approved=True)
        report = await runner.finish(spec(), SESSION)
        self.assertEqual(report.code, "no_run_record")
        self.assertEqual(report.verdict, TaskVerdict.OUTCOME_UNKNOWN.value)

    async def test_cancelling_without_a_task_says_so(self) -> None:
        answer = await TaskRunner(FakeGateway()).cancel(SESSION)
        self.assertFalse(answer["cancelled"])
        self.assertEqual(answer["code"], "no_active_task")

    async def test_cancelling_a_running_task_measures_held_input(self) -> None:
        gateway = FakeGateway(
            wait_answer={"finished": False, "reason": "still running", "status": run_payload(phase="EXECUTING", outcome="")},
            held=(({"key": "space"},)),
        )
        runner = TaskRunner(gateway)
        await runner.start(spec(), SESSION, approved=True)
        answer = await runner.cancel(SESSION, reason="operator")
        self.assertTrue(answer["cancelled"])
        self.assertEqual(answer["held_inputs"], 1)
        self.assertTrue(answer["input_release_measured"])
        self.assertEqual(gateway.cancelled, ["operator"])
        self.assertTrue(any("1 held input" in item for item in answer["task"]["limitations"]))

    async def test_cancellation_never_invents_a_release_record(self) -> None:
        gateway = FakeGateway(
            wait_answer={"finished": False, "reason": "still running", "status": run_payload(phase="EXECUTING", outcome="")},
            held=None,
        )
        runner = TaskRunner(gateway)
        await runner.start(spec(), SESSION, approved=True)
        answer = await runner.cancel(SESSION, reason="operator")
        self.assertIsNone(answer["held_inputs"])
        self.assertFalse(answer["input_release_measured"])
        self.assertTrue(
            any("was not measured" in item for item in answer["task"]["limitations"])
        )

    async def test_a_confirmed_cancel_frees_the_session_for_another_task(self) -> None:
        gateway = FakeGateway(
            wait_answer={"finished": False, "reason": "still running", "status": run_payload(phase="EXECUTING", outcome="")}
        )
        runner = TaskRunner(gateway)
        await runner.start(spec(), SESSION, approved=True)
        self.assertEqual(runner.active, {SESSION: "task-1"})
        answer = await runner.cancel(SESSION, reason="operator")
        self.assertTrue(answer["cancelled"])
        self.assertEqual(runner.active, {})
        self.assertEqual(answer["task"]["verdict"], TaskVerdict.CANCELLED.value)
        self.assertEqual(answer["task"]["status"], "finished")
        # A second task on the same session is now allowed; the ledger no longer
        # claims an activity that stopped.
        started = await runner.start(spec(task_id="task-2"), SESSION, approved=True)
        self.assertEqual(started.status, "running")
        self.assertEqual(started.code, "started")

    async def test_a_cancel_after_completion_claims_no_completion(self) -> None:
        gateway = FakeGateway(
            wait_answer={"finished": False, "reason": "still running", "status": run_payload(phase="EXECUTING", outcome="")},
            cancel_answer={
                "cancelled": True,
                "code": "already_finished",
                "reason": "The run had already ended as objective_completed.",
                "status": {"phase": "COMPLETED", "outcome": "objective_completed"},
            },
        )
        runner = TaskRunner(gateway)
        await runner.start(spec(), SESSION, approved=True)
        answer = await runner.cancel(SESSION)
        self.assertEqual(answer["task"]["verdict"], TaskVerdict.OUTCOME_UNKNOWN.value)
        self.assertEqual(answer["task"]["code"], "cancelled_after_completion")
        self.assertFalse(answer["task"]["completed"])

    async def test_a_cancel_after_a_safety_stop_does_not_rewrite_the_ending(self) -> None:
        gateway = FakeGateway(
            wait_answer={"finished": False, "reason": "still running", "status": run_payload(phase="EXECUTING", outcome="")},
            cancel_answer={
                "cancelled": True,
                "code": "already_finished",
                "reason": "The run had already ended as safety_stop.",
                "status": {"phase": "FAILED", "outcome": "safety_stop"},
            },
        )
        runner = TaskRunner(gateway)
        await runner.start(spec(), SESSION, approved=True)
        answer = await runner.cancel(SESSION)
        self.assertEqual(answer["task"]["verdict"], TaskVerdict.OUTCOME_UNKNOWN.value)
        self.assertEqual(answer["task"]["code"], "cancelled_after_ending")
        self.assertIn("safety_stop", answer["task"]["reason"])

    async def test_an_unconfirmed_cancel_deactivates_without_claiming_a_stop(self) -> None:
        gateway = FakeGateway(
            wait_answer={"finished": False, "reason": "still running", "status": run_payload(phase="EXECUTING", outcome="")},
            cancel_answer={"cancelled": False, "code": "no_run", "reason": "There is no run to cancel."},
        )
        runner = TaskRunner(gateway)
        await runner.start(spec(), SESSION, approved=True)
        answer = await runner.cancel(SESSION)
        self.assertFalse(answer["cancelled"])
        self.assertEqual(answer["code"], "no_run")
        self.assertEqual(answer["task"]["verdict"], TaskVerdict.OUTCOME_UNKNOWN.value)
        self.assertEqual(runner.active, {})

    async def test_a_cancel_that_cannot_be_delivered_leaves_the_task_active(self) -> None:
        gateway = FakeGateway(
            wait_answer={"finished": False, "reason": "still running", "status": run_payload(phase="EXECUTING", outcome="")},
            cancel_error=RuntimeError("supervisor wedged"),
        )
        runner = TaskRunner(gateway)
        await runner.start(spec(), SESSION, approved=True)
        answer = await runner.cancel(SESSION)
        self.assertFalse(answer["cancelled"])
        self.assertEqual(answer["code"], "cancel_failed")
        self.assertIn("RuntimeError", answer["reason"])
        self.assertEqual(runner.active, {SESSION: "task-1"})

    async def test_the_runner_keeps_one_ledger_and_a_bounded_history(self) -> None:
        gateway = FakeGateway(start_answer={"started": False, "code": "declined", "reason": "no"})
        runner = TaskRunner(gateway, max_reports=3)
        for index in range(5):
            await runner.start(spec(task_id=f"task-{index}"), SESSION, approved=True)
        status = runner.status()
        self.assertEqual(len(status["reports"]), 3)
        self.assertEqual(status["reports"][-1]["task_id"], "task-4")
        self.assertEqual(status["counts"]["refused"], 5)


# ── the adapter to the real supervisor ─────────────────────────────────────


class AutonomyTaskGatewayTests(IsolatedAsyncioTestCase):
    async def test_the_adapter_passes_the_translated_limits_and_the_approval(self) -> None:
        calls: list[dict[str, Any]] = []

        class Controller:
            runs: dict[str, Any] = {}

            async def start(self, session_id, *, approved, note, task, limits):
                calls.append(
                    {
                        "session_id": session_id,
                        "approved": approved,
                        "note": note,
                        "task": task,
                        "limits": limits,
                    }
                )
                return {"started": True, "run_id": "run-1", "limits": limits.to_dict()}

            async def wait(self, session_id, *, timeout):
                return {"finished": True, "status": run_payload()}

            async def cancel(self, session_id, *, reason):
                return {"status": "cancelled", "cleanup": {"held": []}}

        adapter = AutonomyTaskGateway(
            autonomy=Controller(),
            sessions=lambda session_id: SimpleNamespace(session_id=session_id),
            target_title=lambda session_id: "Sim Game — Main Menu",
            read_state=lambda: _async(state_payload()),
            objective_picture=lambda session_id: {"objective_id": "obj-1"},
            allowed_actions=lambda: tuple(sorted(CONTROL_KINDS)),
            resource_gate=lambda: {"allow": True},
        )
        task = spec()
        self.assertIsNotNone(adapter.session(SESSION))
        self.assertEqual(adapter.target_title(SESSION), "Sim Game — Main Menu")
        self.assertEqual((await adapter.read_state())["analyzed"], True)
        answer = await adapter.start_run(SESSION, task, approved=True, note="ok")
        self.assertTrue(answer["started"])
        self.assertEqual(calls[0]["task"], task.mission.objective)
        self.assertEqual(calls[0]["limits"].max_actions, 4)
        self.assertEqual(calls[0]["approved"], True)
        waited = await adapter.wait_run(SESSION, timeout=5.0)
        self.assertTrue(waited["finished"])
        self.assertIsNone(adapter.run(SESSION))
        cancelled = await adapter.cancel(SESSION, reason="stop")
        self.assertEqual(cancelled["status"], "cancelled")

    async def test_the_adapter_reads_a_run_record_when_the_controller_has_one(self) -> None:
        class Record:
            def to_dict(self) -> dict[str, Any]:
                return run_payload()

        class Controller:
            runs = {SESSION: Record()}

            async def start(self, session_id, **kwargs):
                return {"started": True}

            async def wait(self, session_id, *, timeout):
                return {"finished": True}

            async def cancel(self, session_id, *, reason):
                return {}

        adapter = AutonomyTaskGateway(
            autonomy=Controller(),
            sessions=lambda session_id: object(),
            target_title=lambda session_id: "Sim Game",
            read_state=lambda: _async(state_payload()),
            objective_picture=lambda session_id: {},
            allowed_actions=lambda: (),
            resource_gate=lambda: {},
        )
        record = adapter.run(SESSION)
        self.assertEqual(record["run_id"] if record else None, "run-1")


class ManagerWiringTests(IsolatedAsyncioTestCase):
    """The product path: one runner, built from the manager's own readers."""

    async def test_the_manager_builds_one_runner_and_refuses_an_unknown_session(self) -> None:
        from tempfile import TemporaryDirectory

        from novacontrol.novaforge.manager import NovaForgeManager

        async def capture(title: str) -> Mapping[str, Any]:
            return {}

        async def geometry(handle: int) -> Any | None:
            return None

        async def window_lines() -> Sequence[Mapping[str, Any]]:
            return ()

        with TemporaryDirectory() as data_dir:
            manager = NovaForgeManager(
                data_dir=data_dir,
                capture=capture,
                geometry=geometry,
                window_lines=window_lines,
            )
            try:
                runner = manager.tasks()
                self.assertIs(runner, manager.tasks())
                self.assertIsInstance(runner, TaskRunner)
                answer = await manager.run_task(spec(), "no-such-session", approved=True)
                self.assertEqual(answer["status"], "refused")
                self.assertEqual(answer["code"], "unknown_session")
                # The approval gate is the operator's, even through the manager.
                unapproved = await manager.run_task(spec(), "no-such-session")
                self.assertEqual(unapproved["code"], "approval_required")
            finally:
                await manager.shutdown()


async def _async(value: Any) -> Any:
    return value


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
