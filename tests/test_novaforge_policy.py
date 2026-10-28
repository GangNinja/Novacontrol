"""NF-10: the state-dependent policy, its baseline, and the trial record.

The policy is tested on its own (a decision is a function of the observation)
and through the REAL closed loop with a stub session, so the guarded path is
exercised rather than described. The disposable window is used by the
acceptance script, not here: these tests must be deterministic on CI.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any

from novacontrol.novaforge import (
    ClosedLoopRunner,
    FixedKeyPolicy,
    GameObservation,
    HarnessStagesPolicy,
    LoopLimits,
    LoopResult,
    LoopStep,
    OcrReading,
    PolicyTrial,
    classify_runs,
    compare_policies,
    outcome_of,
    summarise_trials,
    trial_rows,
)

HANDLE = 0x1234
PID = 4242


def observation(
    stage: str | None = "IDLE",
    *,
    status: str = "observed",
    vision: dict[str, Any] | None = None,
    conflicts: tuple[str, ...] = (),
    text: str = "STAGE",
) -> GameObservation:
    title = f"NC FORGE HARNESS - STAGE {stage}" if stage else "NC FORGE HARNESS"
    return GameObservation(
        observed_at="2026-10-22T10:00:00+00:00",
        status=status,
        target={"handle": HANDLE, "pid": PID, "title": title},
        ocr=OcrReading(text=text, words=({"text": text, "line": 0},) if text else ()),
        vision=vision,
        conflicts=conflicts,
        reason=f"status {status}",
    )


def vision(description: str, *, state: str = "interpreted") -> dict[str, Any]:
    return {
        "status": state,
        "description": description,
        "provenance": "model_interpretation",
        "conflicts": [],
    }


class StagePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_stage_selects_its_own_action(self) -> None:
        policy = HarnessStagesPolicy()
        for stage, key in (("IDLE", "a"), ("ARMED", "b"), ("FAULT", "r")):
            with self.subTest(stage=stage):
                proposal = await policy.propose(observation(stage))
                self.assertIsNotNone(proposal, policy.last_reason)
                assert proposal is not None
                self.assertEqual(proposal.action["key"], key)
                self.assertEqual(proposal.policy, "HarnessStagesPolicy")
                self.assertEqual(proposal.policy_version, 1)
                self.assertFalse(proposal.model_backed)
                self.assertTrue(proposal.evidence)

    async def test_the_next_decision_changes_after_the_observed_transition(self) -> None:
        """The same policy, the same history length, a different OBSERVATION."""
        policy = HarnessStagesPolicy()
        idle = observation("IDLE")
        armed = observation("ARMED")
        first = await policy.propose(idle)
        second = await policy.propose(armed, history=(idle,))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first.action["key"], "a")
        self.assertEqual(second.action["key"], "b")
        self.assertEqual(second.expects, {"title_contains": "STAGE COMPLETE"})

    async def test_complete_is_left_to_the_verifier(self) -> None:
        policy = HarnessStagesPolicy()
        proposal = await policy.propose(observation("COMPLETE"))
        self.assertIsNone(proposal)
        self.assertIn("verifier", policy.last_reason.lower())

    async def test_a_stale_observation_cannot_trigger_an_action(self) -> None:
        policy = HarnessStagesPolicy()
        proposal = await policy.propose(observation("IDLE", status="stale"))
        self.assertIsNone(proposal)
        self.assertIn("stale", policy.last_reason)

    async def test_an_unreadable_stage_is_refused_rather_than_guessed(self) -> None:
        policy = HarnessStagesPolicy()
        proposal = await policy.propose(observation(None))
        self.assertIsNone(proposal)
        self.assertIn("refuses to guess", policy.last_reason)

    async def test_a_model_claim_can_supply_a_stage_and_is_labelled(self) -> None:
        policy = HarnessStagesPolicy()
        proposal = await policy.propose(
            observation(None, vision=vision("The window reports stage armed."))
        )
        self.assertIsNotNone(proposal, policy.last_reason)
        assert proposal is not None
        self.assertEqual(proposal.action["key"], "b")
        self.assertTrue(proposal.model_backed)
        self.assertTrue(proposal.uncertainty)
        self.assertIn("claim, not measured evidence", " ".join(proposal.uncertainty))

    async def test_a_model_conflict_or_prose_cannot_choose_an_action(self) -> None:
        policy = HarnessStagesPolicy()
        for candidate in (
            observation(
                None,
                vision=vision("The window reports stage armed."),
                conflicts=("The model reported no visible text, but OCR read text.",),
            ),
            observation(None, vision=vision("It looks like the game is ready.", state="degraded")),
        ):
            with self.subTest(candidate=candidate.vision):
                self.assertIsNone(await policy.propose(candidate), policy.last_reason)

    async def test_repeating_a_stalled_stage_stops_the_policy(self) -> None:
        policy = HarnessStagesPolicy(stall_limit=2)
        idle = observation("IDLE")
        self.assertIsNotNone(await policy.propose(idle, history=(idle,)))
        # Two identical history entries plus the current one is a stall.
        self.assertIsNone(await policy.propose(idle, history=(idle, idle)))
        self.assertIn("not a decision", policy.last_reason)

    def test_capability_is_honest_about_model_backing(self) -> None:
        capability = HarnessStagesPolicy().capability()
        self.assertTrue(capability["available"])
        self.assertFalse(capability["model_backed"])
        self.assertEqual(capability["version"], 1)
        self.assertEqual(capability["stages"]["idle"], "a")
        self.assertIn("not a learned", capability["reason"])


class BaselinePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_baseline_presses_one_key_whatever_the_stage(self) -> None:
        policy = FixedKeyPolicy(key="space")
        keys = []
        for stage in ("IDLE", "ARMED", "FAULT"):
            proposal = await policy.propose(observation(stage))
            self.assertIsNotNone(proposal)
            assert proposal is not None
            keys.append(proposal.action["key"])
            self.assertFalse(proposal.model_backed)
            self.assertEqual(proposal.expects, {})
        self.assertEqual(keys, ["space", "space", "space"])

    async def test_the_baseline_still_refuses_an_untrusted_observation(self) -> None:
        policy = FixedKeyPolicy()
        self.assertIsNone(await policy.propose(observation("IDLE", status="unavailable")))
        self.assertIn("cannot trust", policy.last_reason)

    def test_the_baseline_says_it_is_a_baseline(self) -> None:
        capability = FixedKeyPolicy().capability()
        self.assertIn("baseline", capability["kind"])
        self.assertFalse(capability["model_backed"])
        self.assertIn("does not read the state", capability["reason"])


class ScriptedGateway:
    """The session, as the loop uses it: plan/execute/verify, recorded."""

    def __init__(self, *, verdicts: list[dict[str, Any]] | None = None,
                 plan: dict[str, Any] | None = None,
                 execute: dict[str, Any] | None = None) -> None:
        self.verdicts = list(verdicts or [])
        self.plan = plan or {
            "status": "proposed",
            "proposal": {"proposal_id": "p1", "digest": "d1"},
            "remaining_actions": 3,
        }
        self.execute = execute or {"status": "executed"}
        self.plan_calls: list[dict[str, Any]] = []
        self.execute_calls: list[dict[str, Any]] = []
        self.verify_calls: list[str] = []

    def get(self, session_id: str) -> Any:
        return SimpleNamespace(
            target=SimpleNamespace(handle=HANDLE, pid=PID, title="NC FORGE HARNESS"),
            objective=None,
        )

    def plan_action(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.plan_calls.append(payload)
        return self.plan

    async def execute_action(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.execute_calls.append(payload)
        return self.execute

    async def verify_objective(self, session_id: str, *, reason: str = "manual",
                               action_id: str = "") -> dict[str, Any]:
        self.verify_calls.append(reason)
        if self.verdicts:
            return self.verdicts.pop(0)
        return {"outcome": "verified_failure", "reason": "not yet"}


class StubObservations:
    def __init__(self, *items: GameObservation) -> None:
        self.queue = list(items)
        self.calls: list[dict[str, Any]] = []

    def capability(self) -> dict[str, Any]:
        return {"observation": {"available": True, "capture": True},
                "interpretation": {"available": True, "vision_model": False}}

    async def observe(self, handle: int, **kwargs: Any) -> GameObservation:
        self.calls.append({"handle": handle, **kwargs})
        if not self.queue:
            return observation(None, status="unavailable", text="")
        return self.queue.pop(0)


def loop(gateway: ScriptedGateway, observations: StubObservations, policy: Any,
         *, limits: LoopLimits | None = None, cancel: asyncio.Event | None = None) -> ClosedLoopRunner:
    return ClosedLoopRunner(
        gateway=gateway,
        observations=observations,
        policy=policy,
        limits=limits or LoopLimits(max_actions=4, deadline_seconds=30),
        cancel=cancel,
    )


class StageLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_transitions_complete_only_when_the_verifier_agrees(self) -> None:
        """A verified_failure BETWEEN stages is not a failed attempt.

        NF-08's rule (failure after an action ends the run) is right for a
        one-action policy and wrong for a staged objective. The stage policy
        opts out; the verdict is still the verifier's, at the top of the loop.
        """
        gateway = ScriptedGateway(
            verdicts=[
                {"outcome": "verified_failure", "reason": "not yet"},  # precheck
                {"outcome": "verified_failure", "reason": "not yet"},  # idle
                {"outcome": "verified_failure", "reason": "not yet"},  # armed
                {"outcome": "verified_success", "reason": "COMPLETE in the title"},
            ]
        )
        observations = StubObservations(
            observation("IDLE"),
            observation("ARMED"),
            observation("ARMED"),
            observation("COMPLETE"),
            observation("COMPLETE"),
        )
        result = await loop(gateway, observations, HarnessStagesPolicy()).run("s1")
        self.assertEqual(result.status, "completed", result.reason)
        self.assertTrue(result.completed)
        self.assertEqual(result.actions_executed, 2)
        self.assertEqual([call["key"] for call in gateway.plan_calls], ["a", "b"])
        self.assertEqual(result.policy_version, 1)
        self.assertFalse(result.model_backed)
        keys = [step.proposal["action"]["key"] for step in result.steps if step.proposal]
        self.assertEqual(keys, ["a", "b"])

    async def test_a_stage_that_never_verifies_ends_failed_not_stopped(self) -> None:
        gateway = ScriptedGateway(verdicts=[
            {"outcome": "verified_failure", "reason": "not yet"},
            {"outcome": "verified_failure", "reason": "not yet"},
            {"outcome": "verified_failure", "reason": "marker never appeared"},
        ])
        observations = StubObservations(
            observation("IDLE"), observation("ARMED"), observation("COMPLETE"),
        )
        result = await loop(gateway, observations, HarnessStagesPolicy()).run("s1")
        self.assertEqual(result.status, "failed", result.reason)
        self.assertIn("marker never appeared", result.reason)

    async def test_a_stale_first_observation_stops_before_any_plan(self) -> None:
        gateway = ScriptedGateway()
        result = await loop(
            gateway, StubObservations(observation("IDLE", status="stale")),
            HarnessStagesPolicy(),
        ).run("s1")
        self.assertEqual(result.status, "stopped", result.reason)
        self.assertEqual(gateway.plan_calls, [])
        self.assertEqual(result.actions_executed, 0)

    async def test_a_refused_plan_stops_and_is_counted(self) -> None:
        gateway = ScriptedGateway(plan={"status": "denied", "outcome": {"reason": "denied by policy"}})
        result = await loop(
            gateway, StubObservations(observation("IDLE")), HarnessStagesPolicy(),
        ).run("s1")
        self.assertEqual(result.status, "refused", result.reason)
        self.assertEqual(result.actions_refused, 1)
        self.assertEqual(gateway.execute_calls, [])

    async def test_a_denied_action_is_a_refusal_not_a_retry(self) -> None:
        gateway = ScriptedGateway(execute={"status": "denied", "outcome": {"reason": "guardian"}})
        observations = StubObservations(observation("IDLE"), observation("IDLE"))
        result = await loop(gateway, observations, HarnessStagesPolicy()).run("s1")
        self.assertEqual(result.status, "refused", result.reason)
        self.assertEqual(len(gateway.plan_calls), 1)
        self.assertEqual(len(observations.calls), 1)

    async def test_cancellation_stops_before_the_next_action(self) -> None:
        gateway = ScriptedGateway()
        cancel = asyncio.Event()
        cancel.set()
        result = await loop(
            gateway, StubObservations(observation("IDLE")),
            HarnessStagesPolicy(), cancel=cancel,
        ).run("s1")
        self.assertEqual(result.status, "canceled", result.reason)
        self.assertEqual(gateway.plan_calls, [])

    async def test_a_policy_that_never_decides_stops_at_its_bound(self) -> None:
        """A hung policy is a dependency fault, not permission to wait for ever."""

        class HangingPolicy:
            name = "HangingPolicy"
            available = True

            async def propose(self, observation: Any, *, history: Any = ()) -> Any:
                await asyncio.sleep(5)
                return None

        gateway = ScriptedGateway()
        limits = LoopLimits(
            max_actions=2, deadline_seconds=30, decision_timeout_seconds=0.05
        )
        result = await loop(
            gateway, StubObservations(observation("IDLE")), HangingPolicy(), limits=limits
        ).run("s1")
        self.assertEqual(result.status, "stopped", result.reason)
        self.assertIn("did not decide within", result.reason)
        self.assertEqual(gateway.plan_calls, [])
        self.assertEqual(result.actions_executed, 0)

    async def test_a_cancel_during_a_hung_decision_is_reported_as_cancelled(self) -> None:
        class HangingPolicy:
            name = "HangingPolicy"
            available = True

            async def propose(self, observation: Any, *, history: Any = ()) -> Any:
                await asyncio.sleep(5)
                return None

        cancel = asyncio.Event()
        gateway = ScriptedGateway()
        limits = LoopLimits(
            max_actions=2, deadline_seconds=30, decision_timeout_seconds=0.05
        )

        async def press_cancel() -> None:
            await asyncio.sleep(0.01)
            cancel.set()

        task = asyncio.ensure_future(press_cancel())
        result = await loop(
            gateway,
            StubObservations(observation("IDLE")),
            HangingPolicy(),
            limits=limits,
            cancel=cancel,
        ).run("s1")
        await task
        self.assertEqual(result.status, "canceled", result.reason)
        self.assertEqual(gateway.plan_calls, [])

    async def test_a_policy_that_raises_is_reported_and_never_executed(self) -> None:
        class ExplodingPolicy:
            name = "ExplodingPolicy"
            available = True

            async def propose(self, observation: Any, *, history: Any = ()) -> Any:
                raise RuntimeError("the model answered with prose")

        gateway = ScriptedGateway()
        result = await loop(
            gateway, StubObservations(observation("IDLE")), ExplodingPolicy()
        ).run("s1")
        self.assertEqual(result.status, "error", result.reason)
        self.assertIn("RuntimeError", result.reason)
        self.assertIn("prose", result.reason)
        self.assertEqual(gateway.plan_calls, [])

    async def test_a_malformed_proposal_is_refused_before_planning(self) -> None:
        """A policy is a plugin point: its answer is checked, never trusted."""

        class MalformedPolicy:
            name = "MalformedPolicy"
            available = True

            def __init__(self, proposal: Any) -> None:
                self.proposal = proposal

            async def propose(self, observation: Any, *, history: Any = ()) -> Any:
                return self.proposal

        shapes = {
            "no action": SimpleNamespace(action=None, reason="because"),
            "no kind": SimpleNamespace(action={}, reason="because"),
            "no reason": SimpleNamespace(action={"kind": "key", "key": "a"}, reason=""),
            "a dict": {"action": {"kind": "key"}, "reason": "because"},
        }
        for label, proposal in shapes.items():
            with self.subTest(shape=label):
                gateway = ScriptedGateway()
                result = await loop(
                    gateway,
                    StubObservations(observation("IDLE")),
                    MalformedPolicy(proposal),
                ).run("s1")
                self.assertEqual(result.status, "error", result.reason)
                self.assertIn("malformed proposal", result.reason)
                self.assertEqual(gateway.plan_calls, [])
                self.assertEqual(result.actions_executed, 0)

    async def test_target_loss_after_an_action_stops_before_another_decision(self) -> None:
        """The window vanishing is a stop, not a reason to press into the void."""
        gateway = ScriptedGateway()
        observations = StubObservations(
            observation("IDLE"),
            observation(None, status="unavailable", text=""),
        )
        result = await loop(gateway, observations, HarnessStagesPolicy()).run("s1")
        self.assertEqual(result.status, "stopped", result.reason)
        self.assertEqual(len(gateway.plan_calls), 1)
        self.assertEqual(result.actions_executed, 1)
        self.assertIn("After the action the observation is unavailable", result.reason)

    async def test_a_planner_fault_is_recorded_not_raised(self) -> None:
        class FaultyPlanner(ScriptedGateway):
            def plan_action(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
                raise RuntimeError("the session store is gone")

        result = await loop(
            FaultyPlanner(), StubObservations(observation("IDLE")), HarnessStagesPolicy()
        ).run("s1")
        self.assertEqual(result.status, "error", result.reason)
        self.assertIn("planner raised RuntimeError", result.reason)
        self.assertEqual(result.actions_executed, 0)

    def test_a_non_positive_decision_bound_is_refused(self) -> None:
        problems = LoopLimits(decision_timeout_seconds=0).errors()
        self.assertTrue(any("decision_timeout_seconds" in problem for problem in problems))

    async def test_an_unknown_policy_name_is_refused_by_the_application(self) -> None:
        # The application owns the registry; this pins the names themselves so a
        # rename cannot leave the panel offering a policy the server rejects.
        from novacontrol.application import NovaControlApplication

        self.assertEqual(
            NovaControlApplication.novaforge_policy_names(),
            ("ready", "stages", "fixed"),
        )


class TrialRecordTests(unittest.TestCase):
    def _result(self, status: str, verdict: str, *, actions: int = 1,
                policy: str = "HarnessStagesPolicy") -> LoopResult:
        return LoopResult(
            status=status,
            reason=f"{status}: {verdict}",
            policy=policy,
            policy_version=1,
            actions_executed=actions,
            elapsed_seconds=1.5,
            verification={"outcome": verdict},
            steps=[
                LoopStep(
                    index=0,
                    at="2026-10-22T10:00:00+00:00",
                    observation={"title": "NC FORGE HARNESS - STAGE IDLE"},
                    proposal={"action": {"kind": "key", "key": "a"}, "model_backed": False},
                    observation_age_seconds=0.2,
                    decision_latency_ms=1,
                    evidence_count=3,
                ),
                LoopStep(
                    index=1,
                    at="2026-10-22T10:00:01+00:00",
                    observation={"title": "NC FORGE HARNESS - STAGE ARMED"},
                    proposal={"action": {"kind": "key", "key": "b"}, "model_backed": False},
                    observation_age_seconds=0.3,
                    decision_latency_ms=1,
                    evidence_count=3,
                ),
            ],
        )

    def test_a_completed_run_without_a_verifier_success_is_inconclusive(self) -> None:
        self.assertEqual(
            outcome_of(self._result("completed", "unknown")), "inconclusive"
        )
        self.assertEqual(
            outcome_of(self._result("completed", "verification_error")),
            "verification_error",
        )
        self.assertEqual(
            outcome_of(self._result("completed", "verified_success")),
            "verified_success",
        )

    def test_failures_and_refusals_are_kept_and_counted(self) -> None:
        trials = [
            PolicyTrial.from_loop(self._result("completed", "verified_success"), trial=1),
            PolicyTrial.from_loop(self._result("failed", "verified_failure", actions=2), trial=2),
            PolicyTrial.from_loop(self._result("refused", "unknown", actions=0), trial=3),
            PolicyTrial.from_loop(self._result("canceled", "unknown", actions=0), trial=4),
        ]
        summary = summarise_trials(trials).to_dict()
        self.assertEqual(summary["trials"], 4)
        self.assertEqual(summary["outcomes"]["verified_success"], 1)
        self.assertEqual(summary["outcomes"]["verified_failure"], 1)
        self.assertEqual(summary["outcomes"]["refused"], 1)
        self.assertEqual(summary["outcomes"]["aborted"], 1)
        self.assertEqual(summary["success_rate"], 0.25)
        self.assertEqual(summary["responded_to_transition_trials"], 4)
        self.assertEqual(len(summary["raw"]), 4, "a raw row was dropped")

    def test_a_state_change_without_a_changed_decision_is_not_a_response(self) -> None:
        """The baseline moves the harness and then repeats itself; that is not adapting.

        ``responded_to_transition_trials`` used to count any observed stage
        change, which credited a fixed key press that walked IDLE -> FAULT with
        having responded to it.
        """
        def run(second_proposal: dict[str, Any] | None) -> LoopResult:
            return LoopResult(
                status="completed",
                reason="ran",
                policy="FixedKeyPolicy",
                policy_version=1,
                actions_executed=1,
                steps=[
                    LoopStep(
                        index=0,
                        at="2026-10-22T10:00:00+00:00",
                        observation={"title": "NC FORGE HARNESS - STAGE IDLE"},
                        proposal={"action": {"kind": "key", "key": "space"}},
                    ),
                    LoopStep(
                        index=1,
                        at="2026-10-22T10:00:01+00:00",
                        observation={"title": "NC FORGE HARNESS - STAGE FAULT"},
                        proposal=second_proposal,
                    ),
                ],
                verification={"outcome": "verified_failure"},
            )

        state_changed_only = PolicyTrial.from_loop(run(None), trial=1)
        repeated = PolicyTrial.from_loop(
            run({"action": {"kind": "key", "key": "space"}}), trial=2
        )
        changed = PolicyTrial.from_loop(
            run({"action": {"kind": "key", "key": "r"}}), trial=3
        )

        self.assertEqual(state_changed_only.observed_transitions, 1)
        self.assertFalse(state_changed_only.responded_to_transition)
        self.assertFalse(repeated.responded_to_transition)
        self.assertTrue(changed.responded_to_transition)
        summary = summarise_trials([state_changed_only, repeated, changed]).to_dict()
        self.assertEqual(summary["responded_to_transition_trials"], 1)

    def test_a_metric_with_no_samples_is_none_and_named(self) -> None:
        bare = LoopResult(status="error", reason="no run", policy="FixedKeyPolicy")
        summary = summarise_trials([PolicyTrial.from_loop(bare, trial=1)]).to_dict()
        self.assertIsNone(summary["elapsed_seconds_mean"])
        self.assertIsNone(summary["decision_latency_ms_mean"])
        self.assertIsNone(summary["evidence_coverage_mean"])
        self.assertIn("elapsed_seconds", summary["unavailable_metrics"])
        self.assertIn("decision_latency_ms", summary["unavailable_metrics"])

    def test_the_comparison_reports_only_measured_deltas(self) -> None:
        baseline = [PolicyTrial.from_loop(
            self._result("failed", "verified_failure", policy="FixedKeyPolicy"), trial=1
        )]
        candidate = [PolicyTrial.from_loop(
            self._result("completed", "verified_success"), trial=1
        )]
        data = compare_policies(baseline, candidate)
        self.assertEqual(data["baseline"]["success_rate"], 0.0)
        self.assertEqual(data["candidate"]["success_rate"], 1.0)
        self.assertEqual(data["delta"]["success_rate"], 1.0)
        self.assertIn("No significance", data["note"])

    def test_classification_keeps_every_outcome(self) -> None:
        counts = classify_runs([
            self._result("completed", "verified_success"),
            self._result("stopped", "unknown"),
            self._result("error", "unknown"),
        ])
        self.assertEqual(counts["verified_success"], 1)
        self.assertEqual(counts["inconclusive"], 1)
        self.assertEqual(counts["error"], 1)

    def test_trial_rows_are_one_row_per_run(self) -> None:
        rows = trial_rows([PolicyTrial.from_loop(
            self._result("completed", "verified_success"), trial=7
        )])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trial"], 7)
        self.assertEqual(rows[0]["outcome"], "verified_success")


if __name__ == "__main__":
    unittest.main()
