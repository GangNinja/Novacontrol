"""Phase 20: agentic RL — episodes, credit, curriculum, gates, dry run.

Everything here runs on deterministic in-memory environments, synthetic
trajectories and dry-run mode: no model is downloaded, no CUDA or NVIDIA GPU is
required, nothing trains for real, and the optional RL dependencies may be
absent. That is a requirement of the phase rather than a convenience — a 16 GB
Windows desktop with an Intel iGPU and an NPU must be able to prove the subsystem
works, so every episode happens in a mock environment, every policy update is the
mock optimizer's deterministic walk, and the rollout loop is driven with an
injected clock.

The tests are organised the way the specification lists them, and the four
properties that make this phase safe each get their own group:

  * a policy CANNOT bypass the mask or the permission layer (SecurityTests),
  * a policy cannot claim success without verification (VerificationTests),
  * a candidate policy cannot act on real work without clearing every gate and a
    person's approval (PromotionTests),
  * and nothing here stores a model's private reasoning — the schema has no field
    for it and the source is checked for one (NoHiddenReasoningTests).
"""

from __future__ import annotations

import json
import math
import re
import unittest
from pathlib import Path

from novacontrol.agentic import (
    SAFETY_DIMENSION,
    ABPolicyEvaluator,
    ActionMasker,
    AgentAction,
    AgenticPolicyEvaluator,
    AgenticResourceEstimator,
    AgenticRewardEngine,
    AgenticRewardWeights,
    AgenticRLConfig,
    AgenticRLTrainer,
    AgenticRolloutManager,
    AgentState,
    CreditAssigner,
    CreditMethod,
    CurriculumConfig,
    CurriculumManager,
    Episode,
    EvaluationConfig,
    EvaluationTask,
    ExplorationBudget,
    ExplorationConfig,
    ExplorationPolicy,
    LLMPolicyAdapter,
    MockAgenticPolicyOptimizer,
    MockPolicy,
    PolicyDecision,
    PolicyReasonCode,
    PolicyRegistry,
    PolicyStatus,
    PolicyUnavailable,
    PromotionGate,
    PromotionRefused,
    PromotionThresholds,
    RolloutLimits,
    RuleBasedPolicy,
    ShadowPolicyEvaluator,
    SimpleEnvironment,
    StateTransition,
    StepPenalty,
    StepReward,
    TaskDifficulty,
    TerminationReason,
    build_environment,
    build_policy,
    checkpoint_integrity_ok,
    checkpoint_payload,
    default_tasks,
    run_agentic_dry_run,
    step_failed,
)
from novacontrol.agentic.exploration import STOP_BUDGET, STOP_SAFETY
from novacontrol.agentic.rollout import builtin_recovery_decision
from novacontrol.core.security import RiskLevel
from novacontrol.evaluation.models import AgentTrajectory
from novacontrol.reliability import PermissionManager, RecoveryEngine, VerificationEngine
from novacontrol.training.backends import TrainingCallbacks
from novacontrol.training.hardware import HardwareCapabilities
from novacontrol.training.models import TrainingRun

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "novacontrol" / "agentic"


# ── fixtures ─────────────────────────────────────────────────────────────────


def limits(**overrides: object) -> RolloutLimits:
    values: dict[str, object] = {
        "max_steps": 8,
        "max_planning_horizon": 8,
        "max_retries": 2,
        "timeout_seconds": 30.0,
        "resource_budget": 10.0,
    }
    values.update(overrides)
    return RolloutLimits(**values)  # type: ignore[arg-type]


def bare() -> HardwareCapabilities:
    """A machine with nothing optional installed — the CI/dependency baseline.

    The verdict is the requirement measured against what THIS machine reports,
    and ``psutil`` is an optional dependency the project does not declare: an
    unpinned assertion about a verdict is an assertion about the host. It held on
    a developer machine that had psutil and failed on the runner that did not
    (every requirement became a WARNING, because free memory could not be
    measured at all), which is the whole reason this fixture exists.
    """
    return HardwareCapabilities(
        cpu_count=4,
        total_ram_bytes=16_000_000_000,
        available_ram_bytes=8_000_000_000,
        backends=("cpu",),
    )


#: Constructor arguments of the MANAGER, as opposed to the LIMITS. They are
#: split here so a test can say `manager(clock=...)` or `manager(max_steps=2)`
#: without caring which of the two owns the name.
_MANAGER_ARGUMENTS = frozenset(
    {
        "reward_engine",
        "masker",
        "exploration",
        "credit_assigner",
        "verification_engine",
        "recovery_engine",
        "verifier",
        "cancelled",
        "clock",
        "gamma",
        "step_observer",
        "confirmation",
    }
)


def manager(policy: object | None = None, **overrides: object) -> AgenticRolloutManager:
    settings = {key: value for key, value in overrides.items() if key in _MANAGER_ARGUMENTS}
    limit_settings = {
        key: value for key, value in overrides.items() if key not in _MANAGER_ARGUMENTS
    }
    settings.setdefault("masker", ActionMasker())
    return AgenticRolloutManager(
        policy if policy is not None else RuleBasedPolicy(),  # type: ignore[arg-type]
        limits=limits(**limit_settings),
        **settings,  # type: ignore[arg-type]
    )


def play(env_name: str, task: dict | None = None, policy: object | None = None, **overrides: object):
    env = build_environment(env_name)
    return manager(policy, **overrides).run_episode(env, task=task or {}, seed=0)


def synthetic_episode(
    *,
    success: bool | None = True,
    steps: int = 3,
    recovery: bool = False,
    safety: float = 0.0,
    terminal_reward: float = 1.0,
    environment: str = "tool_selection",
    level: int = 0,
    verified: bool = True,
    total_reward: float | None = None,
) -> Episode:
    """A hand-built episode, so credit/curriculum/evaluation math is exact."""
    transitions: list[StateTransition] = []
    cumulative = 0.0
    for index in range(steps):
        terminal = index == steps - 1
        reward = StepReward(
            index=index,
            total=round(0.1 + (terminal_reward if terminal else 0.0), 6),
            dimensions={
                "task_success": terminal_reward if terminal else 0.0,
                "planning_efficiency": 0.1,
                SAFETY_DIMENSION: safety if terminal else 0.0,
            },
            signals=("successful_subtask",),
            penalties=("unsafe_action",) if safety and terminal else (),
            reasons=("synthetic",),
            safety=safety if terminal else 0.0,
            terminal=terminal,
        )
        cumulative = round(cumulative + reward.total, 6)
        transitions.append(
            StateTransition(
                index=index,
                previous_state=AgentState(goal="synthetic", step_index=index),
                action=AgentAction(tool=f"tool_{index}", capability=f"tool_{index}"),
                observation={"step": index, "done": terminal},
                result={"progress": True},
                next_state=AgentState(goal="synthetic", step_index=index + 1),
                verification={
                    "status": "pass" if verified else "fail",
                    "action_signature": f"tool_invocation:tool_{index}:tool_{index}",
                },
                reward=reward,
                step_reward=reward.total,
                cumulative_reward=cumulative,
                recovery_event={"strategy": "retry", "succeeded": True} if recovery and index == 0 else {},
                terminal=terminal,
                termination_reason="success" if terminal and success else "",
            )
        )
    return Episode(
        episode_id=f"ep-{environment}-{steps}-{int(bool(success))}",
        task_id="task-synthetic",
        environment_id=environment,
        initial_state=AgentState(goal="synthetic"),
        goal="synthetic task",
        steps=tuple(transitions),
        total_reward=round(
            cumulative if total_reward is None else float(total_reward), 6
        ),
        success=success,
        # An undecided episode (``success is None``) ended BOUNDED, not badly: a
        # bounded run is neither a success nor a failure, and storing it as one
        # would make every metric that counts failures wrong.
        termination_reason=(
            "success" if success else ("failure" if success is False else "max_steps")
        ),
        difficulty=TaskDifficulty(
            steps=steps,
            tools=max(1, steps),
            planning_horizon=max(1, steps),
        )
        if level
        else None,
        dimension_totals={
            "task_success": terminal_reward,
            "planning_efficiency": round(0.1 * steps, 6),
            SAFETY_DIMENSION: round(safety, 6),
        },
        resource_usage={"steps": steps, "used": 0.1 * steps, "elapsed_seconds": 0.01},
    )


# ── trajectory ───────────────────────────────────────────────────────────────


class TrajectoryTests(unittest.TestCase):
    """The agentic reading of an episode, on Phase 15's own record."""

    def test_episode_creation_records_the_goal_and_the_termination(self) -> None:
        outcome = play("simple")
        episode = outcome.episode

        self.assertEqual(episode.goal, "Complete the task with the correct action")
        self.assertEqual(episode.termination_reason, TerminationReason.SUCCESS.value)
        self.assertTrue(episode.terminal)
        self.assertTrue(episode.success)
        self.assertEqual(episode.environment_id, "simple")

    def test_state_transitions_carry_both_states_and_the_next_index(self) -> None:
        episode = play("multi_step").episode

        self.assertEqual([step.index for step in episode.steps], [0, 1, 2])
        for step in episode.steps:
            self.assertEqual(step.next_state.step_index, step.index + 1)
            self.assertEqual(step.previous_state.step_index, step.index)

    def test_actions_are_recorded_with_their_type_and_arguments(self) -> None:
        episode = play("tool_selection").episode
        record = episode.steps[0].action

        self.assertTrue(record.action_id)
        self.assertEqual(record.action_type, "information_gathering")
        self.assertEqual(record.tool, "search_files")
        self.assertIsInstance(dict(record.arguments), dict)

    def test_step_rewards_are_recorded_per_step(self) -> None:
        episode = play("multi_step").episode

        self.assertEqual(len(episode.step_rewards()), 3)
        for step in episode.steps:
            self.assertIsNotNone(step.reward)
            self.assertEqual(step.reward.index, step.index)

    def test_terminal_reward_is_recorded_on_the_ending_step(self) -> None:
        episode = play("simple").episode
        terminal = [step for step in episode.steps if step.terminal]

        self.assertEqual(len(terminal), 1)
        self.assertGreater(terminal[0].step_reward, 0)
        self.assertIn("task_success", terminal[0].reward.dimensions)

    def test_cumulative_reward_accumulates_across_steps(self) -> None:
        episode = play("multi_step").episode
        running = 0.0
        for step in episode.steps:
            running = round(running + step.step_reward, 6)
            self.assertAlmostEqual(step.cumulative_reward, running, places=6)
        self.assertAlmostEqual(
            episode.steps[-1].cumulative_reward, sum(episode.step_rewards()), places=6
        )

    def test_the_episode_projects_into_a_phase15_trajectory_with_agentic_fields(self) -> None:
        episode = play("multi_step").episode
        rows = episode.step_trajectories()

        self.assertEqual(len(rows), 3)
        row = rows[-1]
        self.assertEqual(row.episode_id, episode.episode_id)
        self.assertEqual(row.step_index, 2)
        self.assertEqual(row.action_type, "tool_invocation")
        self.assertTrue(row.is_agentic)
        # Only the terminal step names why the episode ended.
        self.assertEqual([r.termination_reason for r in rows[:-1]], ["", ""])
        self.assertEqual(row.termination_reason, episode.termination_reason)
        self.assertEqual([r.step_terminal for r in rows], [False, False, True])
        self.assertIsNotNone(row.reward)

    def test_a_phase15_trajectory_without_agentic_fields_is_unchanged(self) -> None:
        plain = AgentTrajectory(task_id="t", success=True)
        restored = AgentTrajectory.from_dict(plain.to_dict())

        self.assertEqual(restored, plain)
        self.assertFalse(restored.is_agentic)
        self.assertEqual(restored.episode_id, "")
        self.assertIsNone(restored.step_reward)

    def test_a_trajectory_can_be_extended_with_one_agentic_step(self) -> None:
        row = AgentTrajectory(task_id="t").with_agentic_step(
            {"episode_id": "e1", "step_index": 2, "action_type": "tool", "nonsense": True}
        )

        self.assertEqual(row.episode_id, "e1")
        self.assertEqual(row.step_index, 2)
        self.assertNotIn("nonsense", row.to_dict())

    def test_an_episode_projects_into_a_phase18_rollout(self) -> None:
        episode = play("multi_step").episode
        rollout = episode.to_rollout()

        self.assertEqual(rollout.environment, "multi_step")
        self.assertEqual(rollout.length, episode.length)
        self.assertEqual(len(rollout.steps), 3)
        self.assertAlmostEqual(rollout.total_reward, sum(episode.step_rewards()), places=6)

    def test_an_episode_round_trips_through_its_stored_form(self) -> None:
        episode = play("recovery", {"failures": 1}).episode
        restored = Episode.from_dict(episode.to_dict())

        self.assertEqual(restored.episode_id, episode.episode_id)
        self.assertEqual(restored.termination_reason, episode.termination_reason)
        self.assertEqual(restored.length, episode.length)
        self.assertEqual(restored.success, episode.success)

    def test_termination_reasons_are_the_seven_the_specification_names(self) -> None:
        self.assertEqual(
            {member.value for member in TerminationReason},
            {
                "success",
                "failure",
                "max_steps",
                "cancelled",
                "timeout",
                "safety_stop",
                "environment_error",
            },
        )


# ── policy ───────────────────────────────────────────────────────────────────


class PolicyTests(unittest.TestCase):
    """The policy interface, its deterministic defaults, and the mask."""

    def test_the_interface_exposes_observe_select_and_update(self) -> None:
        policy = RuleBasedPolicy()

        for name in ("observe", "select_action", "update", "reset", "describe"):
            self.assertTrue(callable(getattr(policy, name)))

    def test_the_rule_based_policy_is_deterministic(self) -> None:
        env = build_environment("multi_step")
        first = manager().run_episode(env, task={}, seed=0).episode
        second = manager().run_episode(build_environment("multi_step"), task={}, seed=0).episode

        self.assertEqual(
            [step.action.name for step in first.steps],
            [step.action.name for step in second.steps],
        )
        self.assertEqual(first.total_reward, second.total_reward)

    def test_the_rule_based_policy_reports_structured_reason_codes(self) -> None:
        env = build_environment("simple")
        decision = RuleBasedPolicy().select_action(
            AgentState(goal=env.goal()), env.available_actions(), mask=None
        )

        self.assertEqual(decision.reason_code, PolicyReasonCode.REQUIRED_TOOL.value)
        self.assertIsNotNone(decision.selected_action)

    def test_the_mock_policy_replays_its_script_and_is_not_a_learned_policy(self) -> None:
        policy = MockPolicy(script=["advance"])
        env = build_environment("planning")
        decision = policy.select_action(AgentState(goal="x"), env.available_actions())

        self.assertEqual(decision.selected_action.tool, "advance")  # type: ignore[union-attr]
        self.assertFalse(policy.learns)
        self.assertTrue(policy.simulated)

    def test_a_policy_that_selects_nothing_says_so(self) -> None:
        policy = RuleBasedPolicy()
        decision = policy.select_action(AgentState(goal="nothing to do"), (), mask=None)

        self.assertIsNone(decision.selected_action)
        self.assertEqual(decision.reason_code, PolicyReasonCode.SAFETY_CONSTRAINT.value)

    def test_action_masking_removes_unavailable_tools(self) -> None:
        state = AgentState(tools=("alpha",), capabilities=())
        candidates = (
            AgentAction(tool="alpha", capability="alpha"),
            AgentAction(tool="beta", capability="beta"),
        )
        mask = ActionMasker().mask(state, candidates)

        self.assertEqual(mask.names(), ("alpha",))
        self.assertEqual(mask.blocked[0].reason, "unavailable")

    def test_action_masking_asks_the_permission_layer(self) -> None:
        permissions = PermissionManager()
        permissions.declare("wipe", destructive=True, reversible=False, risk_level=RiskLevel.HIGH)
        masker = ActionMasker(permission_manager=permissions)
        mask = masker.mask(
            AgentState(), (AgentAction(tool="wipe", capability="wipe"),)
        )

        self.assertTrue(mask.empty)
        self.assertEqual(mask.blocked[0].reason, "unauthorized")

    def test_an_approved_action_the_permission_layer_allows_passes_the_mask(self) -> None:
        permissions = PermissionManager()
        permissions.declare("read_logs", risk_level=RiskLevel.LOW, reversible=True)
        masker = ActionMasker(permission_manager=permissions)
        mask = masker.mask(AgentState(), (AgentAction(tool="read_logs"),))

        self.assertEqual(mask.names(), ("read_logs",))

    def test_an_action_needing_confirmation_is_masked_when_nobody_can_approve(self) -> None:
        mask = ActionMasker().mask(
            AgentState(), (AgentAction(tool="send", requires_confirmation=True),)
        )

        self.assertEqual(mask.blocked[0].reason, "confirmation_required")

    def test_confirmation_availability_is_reported_not_silently_missing(self) -> None:
        mask = ActionMasker(allow_confirmation=True).mask(
            AgentState(), (AgentAction(tool="send", requires_confirmation=True),)
        )

        self.assertEqual(mask.names(), ("send",))
        self.assertTrue(mask.confirmations_available)

    def test_the_mask_names_the_allowed_actions_that_still_need_a_person(self) -> None:
        action = AgentAction(tool="send", requires_confirmation=True)
        mask = ActionMasker(allow_confirmation=True).mask(AgentState(), (action,))

        # It was KEPT, so it needs to be asked about: the mask says which ones.
        self.assertTrue(mask.needs_confirmation(action.action_id))
        self.assertEqual(mask.confirmation_required, (action.action_id,))

        approved = ActionMasker(
            allow_confirmation=True, approved=frozenset({"send"})
        ).mask(AgentState(), (action,))

        # An approval already given is not asked about again.
        self.assertEqual(approved.confirmation_required, ())
        self.assertFalse(approved.needs_confirmation(action.action_id))

    def test_the_permission_layers_own_confirmation_requirement_is_a_deny(self) -> None:
        permissions = PermissionManager()
        permissions.declare("send", risk_level=RiskLevel.MEDIUM, requires_confirmation=True)
        mask = ActionMasker(
            permission_manager=permissions, allow_confirmation=True
        ).mask(AgentState(), (AgentAction(tool="send"),))

        # "Somebody is reachable" is not "somebody said yes": the layer's verdict
        # stands, and only a NAMED approval changes it.
        self.assertTrue(mask.empty)
        self.assertEqual(mask.blocked[0].reason, "unauthorized")
        self.assertEqual(mask.confirmation_required, ())

    def test_action_ids_are_content_addressed_not_drawn_at_random(self) -> None:
        first = AgentAction(tool="search", capability="search", arguments={"q": "cats"})
        again = AgentAction(tool="search", capability="search", arguments={"q": "cats"})
        other_arguments = first.with_arguments(q="dogs")
        other_permission = AgentAction(
            tool="search", capability="search", arguments={"q": "cats"}, reversible=False
        )

        # Two actions that mean the same thing ARE the same action, and two that
        # mean different things are not — including when only the arguments or
        # only the declared permission differs.
        self.assertEqual(first.action_id, again.action_id)
        self.assertNotEqual(first.action_id, other_arguments.action_id)
        self.assertNotEqual(first.action_id, other_permission.action_id)
        self.assertEqual(len(first.action_id), 12)

    def test_a_policy_built_action_matches_the_environments_own_candidate(self) -> None:
        env = build_environment("planning")
        candidate = next(item for item in env.available_actions() if item.tool == "advance")
        rebuilt = AgentAction(
            action_type=candidate.action_type,
            capability=candidate.capability,
            tool=candidate.tool,
            expected_effect=candidate.expected_effect,
        )
        mask = ActionMasker().mask(AgentState(goal=env.goal()), env.available_actions())

        # The mask authorizes the ACTION, not one particular instance of it, so a
        # policy that builds the same action itself is not refused for making a
        # new object — which is what a mask keyed on random ids would do.
        self.assertEqual(rebuilt.action_id, candidate.action_id)
        self.assertIn(rebuilt.action_id, mask.ids())

    def test_the_environment_can_reject_an_action_as_incompatible(self) -> None:
        env = build_environment("multi_step")
        state = AgentState(goal=env.goal(), tools=tuple(env.get_metadata() and ()))
        masker = ActionMasker(acceptance=env.accepts)
        mask = masker.mask(state, (AgentAction(tool="not_offered"),))

        self.assertTrue(mask.empty)
        self.assertEqual(mask.blocked[0].reason, "incompatible")

    def test_invalid_actions_are_rejected_and_never_executed(self) -> None:
        class Fabricating(RuleBasedPolicy):
            policy_id = "fabricating"

            def select_action(self, state, actions, *, mask=None):  # type: ignore[no-untyped-def]
                decision = super().select_action(state, actions, mask=mask)
                return type(decision)(
                    selected_action=AgentAction(tool="made_up_tool"),
                    confidence=1.0,
                    reason_code=PolicyReasonCode.PLANNER_DECISION.value,
                )

        outcome = play("simple", policy=Fabricating(), refusal_tolerance=0, max_steps=4)
        episode = outcome.episode

        self.assertEqual(episode.termination_reason, TerminationReason.FAILURE.value)
        self.assertFalse(episode.steps[0].executed)
        self.assertEqual(episode.steps[0].action.tool, "made_up_tool")
        self.assertEqual(outcome.refusals, 1)

    def test_a_language_model_adapter_never_loads_a_model_and_refuses_unknown_actions(self) -> None:
        adapter = LLMPolicyAdapter()

        self.assertFalse(adapter.available)
        self.assertFalse(adapter.describe()["loads_model"])
        # An adapter with no proposer is UNAVAILABLE, and says so by name rather
        # than raising something a caller would have to guess at.
        with self.assertRaises(PolicyUnavailable):
            adapter.select_action(AgentState(goal="x"), (AgentAction(tool="a"),))

    def test_a_language_model_adapter_validates_what_it_is_told(self) -> None:
        adapter = LLMPolicyAdapter(lambda state, actions: {"action": "not_here"})
        decision = adapter.select_action(AgentState(goal="x"), (AgentAction(tool="a"),))

        self.assertIsNone(decision.selected_action)
        self.assertIn("not in the masked action set", decision.notes[0])

    def test_the_policy_registry_builder_refuses_a_learned_policy_it_does_not_have(self) -> None:
        with self.assertRaises(KeyError):
            build_policy("ppo_policy")


# ── rollout ──────────────────────────────────────────────────────────────────


class RolloutTests(unittest.TestCase):
    """reset → observe → decide → act → verify → reward → terminate."""

    def test_reset_produces_an_initial_state_from_the_environment(self) -> None:
        outcome = play("multi_step")
        initial = outcome.episode.initial_state

        self.assertEqual(initial.goal, "Open, read and write report.txt")
        self.assertEqual(initial.step_index, 0)
        self.assertIn("open", initial.tools)

    def test_each_step_records_the_observation_the_environment_returned(self) -> None:
        episode = play("multi_step").episode

        for step in episode.steps:
            self.assertIn("status", step.observation)
            self.assertIn("result", step.to_dict())

    def test_verification_runs_for_actions_that_change_something(self) -> None:
        episode = play("simple").episode

        self.assertEqual(episode.steps[0].verification_result, "pass")
        self.assertTrue(episode.steps[0].verified)
        self.assertEqual(episode.verification_summary["passed"], 1)

    def test_a_read_only_no_op_is_verified_or_reported_as_skipped_never_passed(self) -> None:
        env = build_environment("planning")
        noop = next(item for item in env.available_actions() if item.tool == "wait")

        # A read-only action has nothing to check, so the environment says exactly
        # that and the rollout records "skipped" — never a pass nobody measured.
        self.assertFalse(env.requires_verification(noop))
        verdict = env.verify(noop, {}, {})
        self.assertEqual(verdict["status"], "skipped")
        self.assertEqual(
            verdict["reason"],
            "this action has not been applied, so there is nothing to verify",
        )

        outcome = manager(MockPolicy(script=["wait"])).run_episode(
            env, task={"budget": 3}, seed=0
        )

        self.assertEqual(outcome.episode.steps[0].verification_result, "skipped")
        self.assertFalse(outcome.episode.steps[0].verified)

    def test_an_unverifiable_step_is_not_a_pass(self) -> None:
        episode = synthetic_episode(verified=False)
        self.assertFalse(episode.steps[0].verified)
        self.assertTrue(step_failed(episode.steps[0]))

    def test_rewards_are_written_onto_the_step_and_the_episode(self) -> None:
        episode = play("planning", {"budget": 3}).episode

        self.assertEqual(len(episode.step_rewards()), 3)
        self.assertAlmostEqual(
            episode.total_reward, sum(episode.step_rewards()), places=6
        )
        self.assertTrue(episode.dimension_totals)

    def test_termination_success_is_recorded_with_its_reason(self) -> None:
        episode = play("planning", {"budget": 3}).episode

        self.assertEqual(episode.termination_reason, "success")
        self.assertTrue(episode.success)
        self.assertEqual(episode.steps[-1].termination_reason, "success")

    def test_termination_on_max_steps_is_recorded_and_is_not_a_failure(self) -> None:
        episode = play("planning", {"budget": 9}, max_steps=3).episode

        self.assertEqual(episode.termination_reason, "max_steps")
        self.assertIsNone(episode.success)
        self.assertIn("step ceiling", episode.metadata["termination_detail"])

    def test_the_planning_horizon_is_a_separate_bound(self) -> None:
        episode = play("planning", {"budget": 9}, max_steps=20, max_planning_horizon=2).episode

        self.assertEqual(episode.termination_reason, "max_steps")
        self.assertIn("planning horizon", episode.metadata["termination_detail"])
        self.assertEqual(episode.length, 2)

    def test_a_cancelled_run_ends_as_cancelled_and_claims_nothing(self) -> None:
        outcome = manager(cancelled=lambda: True).run_episode(
            build_environment("multi_step"), task={}, seed=0
        )

        self.assertEqual(outcome.episode.termination_reason, "cancelled")
        self.assertIsNone(outcome.episode.success)
        self.assertEqual(outcome.episode.length, 0)

    def test_a_timeout_ends_the_episode_with_the_timeout_reason(self) -> None:
        ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0])
        outcome = manager(clock=lambda: next(ticks, 100.0), timeout_seconds=1.0).run_episode(
            build_environment("planning"), task={"budget": 9}, seed=0
        )

        self.assertEqual(outcome.episode.termination_reason, "timeout")
        self.assertIsNone(outcome.episode.success)

    def test_the_resource_budget_ends_the_episode_with_a_reason(self) -> None:
        outcome = manager(resource_budget=0.01).run_episode(
            build_environment("planning"), task={"budget": 9}, seed=0
        )

        self.assertEqual(outcome.episode.termination_reason, "failure")
        self.assertIn("resource budget", outcome.termination_detail)

    def test_a_safety_refusal_ends_the_episode_as_a_safety_stop(self) -> None:
        class Unsafe(RuleBasedPolicy):
            def select_action(self, state, actions, *, mask=None):  # type: ignore[no-untyped-def]
                del state, actions, mask
                from novacontrol.agentic.models import PolicyDecision

                return PolicyDecision(
                    selected_action=AgentAction(
                        tool="send_message",
                        requires_confirmation=True,
                        reversible=False,
                    ),
                    confidence=1.0,
                )

        outcome = manager(Unsafe(), refusal_tolerance=0).run_episode(
            build_environment("multi_step"), task={}, seed=0
        )

        self.assertEqual(outcome.episode.termination_reason, "safety_stop")
        self.assertFalse(outcome.episode.steps[0].executed)
        # A safety refusal is a NEGATIVE safety figure, kept apart from anything
        # the step might have earned for efficiency.
        self.assertLess(outcome.episode.safety_penalty, 0.0)
        self.assertIn("unsafe_action", outcome.episode.steps[0].reward.penalties)

    def test_batching_episodes_offsets_each_seed(self) -> None:
        runner = manager()
        outcomes = runner.run_episodes(
            [build_environment("simple"), build_environment("multi_step")]
        )

        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes[0].episode.metadata["seed"], 0)
        self.assertEqual(outcomes[1].episode.metadata["seed"], 1)

    def test_all_six_deterministic_environments_are_solvable(self) -> None:
        for name in ("simple", "multi_step", "tool_selection", "recovery", "planning", "contextual"):
            with self.subTest(environment=name):
                episode = play(name).episode
                self.assertTrue(episode.success, f"{name} should be solvable")
                self.assertEqual(episode.termination_reason, "success")


# ── exploration ──────────────────────────────────────────────────────────────


class ExplorationTests(unittest.TestCase):
    """Four strategies, one budget, and a hard limit on what may be explored."""

    def actions(self):
        return build_environment("planning").available_actions()

    def test_greedy_never_explores(self) -> None:
        policy = ExplorationPolicy(ExplorationConfig(strategy="greedy", enabled=True))
        base = RuleBasedPolicy().select_action(AgentState(goal="plan"), self.actions())

        outcome = policy.explore(base, self.actions())

        self.assertFalse(outcome.explored)
        self.assertEqual(outcome.reason, "exploration_is_disabled")
        self.assertEqual(outcome.decision.selected_action, base.selected_action)

    def test_epsilon_greedy_explores_deterministically_at_a_high_epsilon(self) -> None:
        policy = ExplorationPolicy(
            ExplorationConfig(strategy="epsilon_greedy", enabled=True, epsilon=1.0, seed=1)
        )
        base = RuleBasedPolicy().select_action(AgentState(goal="plan"), self.actions())
        first = policy.explore(base, self.actions())
        second = ExplorationPolicy(
            ExplorationConfig(strategy="epsilon_greedy", enabled=True, epsilon=1.0, seed=1)
        ).explore(base, self.actions())

        self.assertTrue(first.explored)
        self.assertEqual(
            first.decision.selected_action, second.decision.selected_action  # type: ignore[arg-type]
        )
        self.assertEqual(
            first.decision.reason_code, PolicyReasonCode.EXPLORATION.value
        )

    def test_temperature_sampling_is_seeded_and_structured(self) -> None:
        config = ExplorationConfig(strategy="temperature", enabled=True, temperature=0.5, seed=3)
        base = RuleBasedPolicy().select_action(AgentState(goal="plan"), self.actions())
        first = ExplorationPolicy(config).explore(base, self.actions())
        second = ExplorationPolicy(config).explore(base, self.actions())

        self.assertEqual(
            first.decision.selected_action, second.decision.selected_action  # type: ignore[arg-type]
        )
        self.assertTrue(first.decision.exploration_metadata)

    def test_exploration_never_chooses_an_unsafe_or_unapproved_action(self) -> None:
        config = ExplorationConfig(strategy="epsilon_greedy", enabled=True, epsilon=1.0, seed=7)
        policy = ExplorationPolicy(config)
        base = RuleBasedPolicy().select_action(AgentState(goal="plan"), self.actions())
        actions = (
            *self.actions(),
            AgentAction(tool="wipe", risk_level=RiskLevel.CRITICAL, reversible=False),
            AgentAction(tool="send", requires_confirmation=True),
        )

        for _ in range(8):
            outcome = policy.explore(base, actions)
            if outcome.decision.selected_action is not None:
                self.assertIn(outcome.decision.selected_action.name, {"advance", "detour", "wait"})

    def test_the_budget_stops_exploration_and_says_why(self) -> None:
        policy = ExplorationPolicy(
            ExplorationConfig(
                strategy="bounded_stochastic", enabled=True, max_exploration_actions=1
            )
        )
        base = RuleBasedPolicy().select_action(AgentState(goal="plan"), self.actions())
        first = policy.explore(base, self.actions())
        policy.record_outcome(explored=first.explored, failed=False, reward=0.0)
        stopped = ExplorationPolicy(
            ExplorationConfig(
                strategy="bounded_stochastic", enabled=True, max_exploration_actions=1
            ),
            budget=ExplorationBudget(max_actions=1, max_cost=100.0, max_rate=1.0, max_failed=100),
        )
        stopped.record_outcome(explored=True, failed=False, reward=0.0)
        follow_up = stopped.explore(base, self.actions())

        self.assertFalse(follow_up.explored)
        self.assertEqual(follow_up.reason, STOP_BUDGET)
        self.assertTrue(stopped.budget.stopped)

    def test_a_safety_intervention_suspends_exploration_immediately(self) -> None:
        policy = ExplorationPolicy(
            ExplorationConfig(strategy="epsilon_greedy", enabled=True, epsilon=1.0)
        )

        reason = policy.record_outcome(explored=True, failed=True, unsafe=True)

        self.assertEqual(reason, STOP_SAFETY)
        self.assertTrue(policy.budget.stopped)
        self.assertEqual(policy.budget.safety_interventions, 1)

    def test_exploration_cost_is_tracked_and_bounded(self) -> None:
        budget = ExplorationBudget(cost_per_action=0.6, max_cost=1.0, max_actions=10, max_rate=1.0, max_failed=10)
        budget.record_step(explored=True)
        budget.record_step(explored=True)

        self.assertAlmostEqual(budget.cost, 1.2, places=6)
        self.assertEqual(budget.stop_reason(), "exploration_cost_budget_reached")

    def test_unsafe_exploration_is_not_a_configurable_feature(self) -> None:
        config = ExplorationConfig(enabled=True, strategy="epsilon_greedy", allow_unsafe=True)
        config = type(config).from_mapping(config.to_mapping())

        self.assertFalse(config.allow_unsafe)
        self.assertIn(
            "not a supported setting",
            " ".join(
                AgenticRLConfig(exploration=ExplorationConfig(allow_unsafe=True))
                .validate()
                .errors
            ),
        )

    def test_the_budget_report_names_its_limits(self) -> None:
        report = ExplorationBudget.from_config(ExplorationConfig()).to_dict()

        self.assertEqual(set(report["limits"]), {
            "max_actions",
            "max_rate",
            "max_failed",
            "max_safety_interventions",
            "max_cost",
        })


# ── credit assignment ────────────────────────────────────────────────────────


class CreditAssignmentTests(unittest.TestCase):
    """Which steps caused the outcome, by four checkable methods."""

    def test_cumulative_return_is_the_running_sum(self) -> None:
        assigner = CreditAssigner(CreditMethod.STEP_ACCUMULATION.value)
        episode = synthetic_episode(steps=3, terminal_reward=1.0, total_reward=1.3)
        result = assigner.assign(episode)

        self.assertAlmostEqual(result.credit_for(0), 0.1, places=6)
        self.assertAlmostEqual(result.credit_for(2), 1.1, places=6)

    def test_discounted_return_matches_the_formula(self) -> None:
        assigner = CreditAssigner(CreditMethod.DISCOUNTED_RETURN.value, gamma=0.5)
        returns = assigner.discounted_returns([1.0, 2.0, 3.0])

        self.assertAlmostEqual(returns[2], 3.0, places=6)
        self.assertAlmostEqual(returns[1], 2.0 + 0.5 * 3.0, places=6)
        self.assertAlmostEqual(returns[0], 1.0 + 0.5 * 2.0 + 0.25 * 3.0, places=6)

    def test_discounted_returns_are_numerically_stable_for_long_episodes(self) -> None:
        assigner = CreditAssigner(CreditMethod.DISCOUNTED_RETURN.value, gamma=0.99)
        rewards = [0.1] * 400

        returns = assigner.discounted_returns(rewards)

        # The finite-horizon sum, not the infinite one: 400 steps at gamma 0.99
        # leave 0.99 ** 400 ≈ 1.8% of the tail uncollected.
        expected = 0.1 * (1 - 0.99**400) / (1 - 0.99)
        self.assertLess(abs(returns[0] - expected), 1e-6)
        self.assertTrue(all(math.isfinite(value) and value >= 0 for value in returns))
        # A long episode must not lose precision: the recurrence still holds far
        # from the start (the accumulated sum did not drift). Each figure is
        # stored to six decimals, which is the only reason this is not tighter.
        self.assertAlmostEqual(returns[-2], 0.1 + 0.99 * returns[-1], places=5)
        self.assertAlmostEqual(returns[200], 0.1 + 0.99 * returns[201], places=5)
        self.assertLess(returns[-1], returns[0])

    def test_terminal_reward_propagation_shares_the_outcome_equally(self) -> None:
        assigner = CreditAssigner(CreditMethod.TERMINAL_PROPAGATION.value)
        episode = synthetic_episode(steps=2, terminal_reward=1.0, total_reward=1.2)
        result = assigner.assign(episode)

        first, second = result.credit_for(0), result.credit_for(1)
        self.assertAlmostEqual(first - 0.1, second - 1.1, places=6)
        self.assertGreater(second, first)

    def test_advantage_credits_sum_to_zero_around_the_baseline(self) -> None:
        assigner = CreditAssigner(CreditMethod.ADVANTAGE.value, gamma=1.0)
        episode = synthetic_episode(steps=3, terminal_reward=1.0, total_reward=1.3)
        result = assigner.assign(episode)
        total = sum(result.advantages.values())

        self.assertAlmostEqual(total, 0.0, places=6)

    def test_verifier_attribution_credits_only_verified_steps(self) -> None:
        assigner = CreditAssigner(CreditMethod.VERIFIER_ATTRIBUTION.value)
        episode = synthetic_episode(steps=2, terminal_reward=1.0, verified=True)
        result = assigner.assign(episode)
        stripped = Episode.from_dict(
            {
                **episode.to_dict(),
                "steps": [
                    {**episode.steps[0].to_dict(), "verification": {"status": "fail"}},
                    episode.steps[1].to_dict(),
                ],
            }
        )
        stripped_result = assigner.assign(stripped)

        self.assertGreater(result.credit_for(1), stripped_result.credit_for(0))
        self.assertAlmostEqual(stripped_result.credit_for(0), 0.1, places=6)

    def test_the_method_and_gamma_travel_with_the_result(self) -> None:
        assigner = CreditAssigner(CreditMethod.ADVANTAGE.value, gamma=0.9)
        result = assigner.assign(synthetic_episode())

        self.assertEqual(result.method, "advantage")
        self.assertEqual(result.gamma, 0.9)
        self.assertTrue(result.reasons)

    def test_the_episode_stores_what_was_assigned(self) -> None:
        outcome = play("planning", {"budget": 3})
        episode = outcome.episode

        self.assertTrue(episode.credits)
        self.assertTrue(episode.returns)
        self.assertIn("credit_method", dict(episode.reward_breakdown))


# ── curriculum ───────────────────────────────────────────────────────────────


class CurriculumTests(unittest.TestCase):
    """Six levels, advancement on evidence, and a way back down."""

    def test_difficulty_maps_onto_the_six_levels(self) -> None:
        self.assertEqual(TaskDifficulty().level.value, 1)
        self.assertEqual(TaskDifficulty(steps=3).level.value, 2)
        self.assertEqual(TaskDifficulty(steps=2, tools=3).level.value, 3)
        self.assertEqual(TaskDifficulty(steps=3, recovery_required=True).level.value, 4)
        self.assertEqual(TaskDifficulty(steps=9, planning_horizon=9).level.value, 5)
        self.assertEqual(TaskDifficulty(ambiguity=0.7).level.value, 6)

    def test_each_built_in_environment_declares_its_level(self) -> None:
        from novacontrol.agentic.environments import ENVIRONMENT_LEVELS, environment_names

        levels = {name: build_environment(name).difficulty().level.value for name in environment_names()}

        self.assertEqual(levels, dict(ENVIRONMENT_LEVELS))

    def test_the_derived_level_names_the_capability_being_tested(self) -> None:
        # Decoys do not make a task harder: one tool, many candidates, one step is
        # still simple.
        self.assertEqual(TaskDifficulty(steps=1, tools=1, branching_factor=3).level.value, 1)
        # Choosing among tools with few steps IS tool selection...
        self.assertEqual(TaskDifficulty(steps=2, tools=4).level.value, 3)
        # ...while a longer task is a multi-step task first, tools or not.
        self.assertEqual(TaskDifficulty(steps=4, tools=4).level.value, 2)
        # A task needing recovery outranks a long horizon: the level reports what
        # the run is really testing.
        self.assertEqual(
            TaskDifficulty(steps=10, planning_horizon=10, recovery_required=True).level.value, 4
        )

    def test_the_curriculum_advances_only_when_the_criteria_are_met(self) -> None:
        manager_ = CurriculumManager(
            CurriculumConfig(min_episodes_per_level=2, min_success_rate=0.8)
        )
        for _ in range(2):
            manager_.record(synthetic_episode(success=True))

        decision = manager_.evaluate()

        self.assertEqual(decision.action, "advance")
        self.assertEqual(decision.to_level, 2)
        self.assertEqual(manager_.level, 2)

    def test_a_single_episode_is_not_enough_to_advance(self) -> None:
        manager_ = CurriculumManager(
            CurriculumConfig(min_episodes_per_level=4, min_success_rate=0.5)
        )
        manager_.record(synthetic_episode(success=True))

        decision = manager_.evaluate()

        self.assertEqual(decision.action, "hold")
        self.assertIn("fewer than", decision.reason)

    def test_failing_tasks_hold_the_curriculum(self) -> None:
        manager_ = CurriculumManager(
            CurriculumConfig(min_episodes_per_level=2, min_success_rate=0.9, max_failure_rate=0.5)
        )
        manager_.record(synthetic_episode(success=True))
        manager_.record(synthetic_episode(success=False))

        decision = manager_.evaluate()

        self.assertEqual(decision.action, "hold")
        self.assertEqual(manager_.level, 1)

    def test_regression_prevention_never_drops_below_the_starting_level(self) -> None:
        manager_ = CurriculumManager(
            CurriculumConfig(
                min_episodes_per_level=1,
                min_success_rate=0.9,
                max_failure_rate=0.2,
                regression_patience=1,
            )
        )
        for _ in range(3):
            manager_.record(synthetic_episode(success=False))

        decision = manager_.evaluate()

        self.assertEqual(decision.action, "hold")
        self.assertEqual(manager_.level, 1)
        self.assertIn("starting level", decision.reason)

    def test_a_failing_level_regresses_only_after_the_patience(self) -> None:
        patient = CurriculumManager(
            CurriculumConfig(
                min_episodes_per_level=1,
                min_success_rate=0.9,
                max_failure_rate=0.2,
                regression_patience=2,
            )
        )
        patient.level = 3
        patient.record(synthetic_episode(success=False))

        first = patient.evaluate()

        self.assertEqual(first.action, "hold")
        self.assertEqual(patient.level, 3)
        self.assertIn("patience", first.reason)

        second = patient.evaluate()

        self.assertEqual(second.action, "regress")
        self.assertEqual(second.to_level, 2)
        self.assertEqual(patient.level, 2)

    def test_a_patience_of_one_regresses_on_the_first_failing_evaluation(self) -> None:
        impatient = CurriculumManager(
            CurriculumConfig(
                min_episodes_per_level=1,
                min_success_rate=0.9,
                max_failure_rate=0.2,
                regression_patience=1,
            )
        )
        impatient.level = 3
        impatient.record(synthetic_episode(success=False))

        decision = impatient.evaluate()

        self.assertEqual(decision.action, "regress")
        self.assertEqual(decision.to_level, 2)
        self.assertEqual(impatient.level, 2)
        self.assertEqual(impatient.fail_streak, 0)

    def test_the_curriculum_hands_out_the_next_task_and_its_environment(self) -> None:
        manager_ = CurriculumManager()

        self.assertEqual(manager_.next_environment(), "simple")
        self.assertEqual(manager_.environments_for(6), ("contextual",))
        self.assertIn("simple deterministic tasks", manager_.level_label)

    def test_a_level_with_no_environment_is_an_error_not_a_default(self) -> None:
        manager_ = CurriculumManager(level_environments={1: (), 2: ("multi_step",)})

        with self.assertRaises(KeyError):
            manager_.next_environment()

    def test_the_curriculum_round_trips_through_its_record(self) -> None:
        manager_ = CurriculumManager()
        manager_.record(synthetic_episode())
        restored = CurriculumManager.from_dict(manager_.to_dict())

        self.assertEqual(restored.level, manager_.level)
        self.assertEqual(restored.stats[1].episodes, 1)

    def test_disabling_the_curriculum_keeps_the_level_fixed(self) -> None:
        manager_ = CurriculumManager(CurriculumConfig(enabled=False))
        for _ in range(4):
            manager_.record(synthetic_episode(success=True))

        decision = manager_.evaluate()

        self.assertEqual(decision.action, "hold")
        self.assertEqual(manager_.level, 1)


# ── recovery ─────────────────────────────────────────────────────────────────


class RecoveryTests(unittest.TestCase):
    """Failed action → recovery, bounded, and never for what may not be repeated."""

    def test_a_transient_failure_is_retried_inside_the_budget(self) -> None:
        outcome = play("recovery", {"failures": 1})
        episode = outcome.episode

        self.assertTrue(episode.success)
        self.assertEqual(episode.length, 2)
        self.assertEqual(episode.steps[0].recovery_event["strategy"], "retry")
        self.assertEqual(episode.steps[0].verification_result, "fail")
        self.assertEqual(episode.steps[1].verification_result, "pass")

    def test_a_spent_retry_budget_stops_instead_of_looping(self) -> None:
        outcome = play(
            "recovery", {"failures": 5}, policy=MockPolicy(script=["flaky_action"] * 3), max_retries=0
        )
        episode = outcome.episode

        self.assertEqual(episode.termination_reason, "failure")
        self.assertFalse(episode.success)
        self.assertIn("retry budget", episode.metadata["termination_detail"])

    def test_the_episode_never_exceeds_its_step_ceiling_even_with_retries(self) -> None:
        # A task that needs two steps cannot finish inside one: the ceiling is
        # what stops this run, and a run that simply used up its steps decided
        # nothing, so it is not recorded as a failure.
        bounded = play("recovery", {"failures": 1}, max_steps=1, max_planning_horizon=1).episode

        self.assertEqual(bounded.length, 1)
        self.assertEqual(bounded.termination_reason, "max_steps")
        self.assertIsNone(bounded.success)
        self.assertIn("step ceiling", bounded.metadata["termination_detail"])

        # Retries do not buy extra steps either: the failing episode stops AT the
        # ceiling, with the reason that actually stopped it.
        retrying = play("recovery", {"failures": 9}, max_steps=3, max_planning_horizon=3).episode

        self.assertEqual(retrying.length, 3)
        self.assertEqual(retrying.termination_reason, "failure")
        self.assertIn("retry budget", retrying.metadata["termination_detail"])

    def test_a_destructive_action_is_never_retried_automatically(self) -> None:
        decision = builtin_recovery_decision(
            AgentAction(tool="delete_file", requires_confirmation=True, reversible=False),
            "the delete failed",
            1,
            limits=limits(max_retries=3),
        )

        self.assertEqual(decision["strategy"], "stop")
        self.assertTrue(decision["safety"])

    def test_a_missing_dependency_is_never_retried(self) -> None:
        decision = builtin_recovery_decision(
            AgentAction(tool="render"),
            "module 'cairo' is not installed",
            1,
            limits=limits(max_retries=3),
        )

        self.assertEqual(decision["strategy"], "stop")
        self.assertIn("cannot install", decision["diagnosis"])

    def test_phase8_recovery_engine_decides_when_one_is_wired(self) -> None:
        outcome = AgenticRolloutManager(
            RuleBasedPolicy(),
            masker=ActionMasker(),
            limits=limits(max_retries=2),
            recovery_engine=RecoveryEngine(),
        ).run_episode(build_environment("recovery"), task={"failures": 1}, seed=0)

        self.assertTrue(outcome.episode.success)
        self.assertEqual(outcome.episode.steps[0].recovery_event["strategy"], "retry")
        self.assertEqual(
            outcome.episode.steps[0].recovery_event["source"], "reliability.recovery"
        )

    def test_recovery_that_did_not_conclude_is_not_billed_as_a_failure(self) -> None:
        engine = AgenticRewardEngine()
        reward = engine.step_reward(
            __import__("novacontrol.agentic.rewards", fromlist=["StepContext"]).StepContext(
                index=0,
                action=AgentAction(tool="flaky_action"),
                error="transient failure",
                recovery={"strategy": "retry", "succeeded": None},
            )
        )

        self.assertNotIn(StepPenalty.REPEATED_FAILURE.value, reward.penalties)
        self.assertTrue(any("has not concluded" in reason for reason in reward.reasons))

    def test_an_unnecessary_recovery_is_penalised(self) -> None:
        engine = AgenticRewardEngine()
        reward = engine.step_reward(
            __import__("novacontrol.agentic.rewards", fromlist=["StepContext"]).StepContext(
                index=0,
                action=AgentAction(tool="advance"),
                recovery={"strategy": "unnecessary", "unnecessary": True, "succeeded": None},
                progressed=True,
            )
        )

        self.assertIn(StepPenalty.UNNECESSARY_RECOVERY.value, reward.penalties)
        self.assertLess(reward.dimension("recovery_quality"), 0.0)


# ── verification ─────────────────────────────────────────────────────────────


class VerificationTests(unittest.TestCase):
    """A policy never assumes success where a check is available."""

    def test_a_check_that_failed_downgrades_a_passing_environment(self) -> None:
        outcome = AgenticRolloutManager(
            RuleBasedPolicy(),
            masker=ActionMasker(),
            limits=limits(),
            verifier=lambda action, observation, result: {
                "status": "fail",
                "reason": "the external check disagreed",
            },
        ).run_episode(build_environment("multi_step"), task={}, seed=0)

        self.assertEqual(outcome.episode.steps[0].verification_result, "fail")
        self.assertTrue(outcome.episode.steps[0].verification.get("disagreement"))
        self.assertFalse(outcome.episode.steps[0].verified)

    def test_a_skipped_check_does_not_overturn_an_observation(self) -> None:
        combined = AgenticRolloutManager._combine(
            [{"status": "pass"}, {"status": "skipped", "reason": "no strategy"}]
        )

        self.assertEqual(combined["status"], "pass")
        self.assertEqual(combined["verdicts"], ["pass", "skipped"])

    def test_a_disagreeing_check_is_recorded_with_both_verdicts(self) -> None:
        combined = AgenticRolloutManager._combine(
            [{"status": "pass"}, {"status": "fail", "reason": "the file is not there"}]
        )

        self.assertEqual(combined["status"], "fail")
        self.assertIn("fail", combined["verdicts"])
        self.assertIn("pass", combined["verdicts"])

    def test_phase8_verification_engine_is_consulted_when_wired(self) -> None:
        outcome = AgenticRolloutManager(
            RuleBasedPolicy(),
            masker=ActionMasker(),
            limits=limits(),
            verification_engine=VerificationEngine(),
        ).run_episode(build_environment("multi_step"), task={}, seed=0)

        verdicts = outcome.episode.steps[0].verification.get("verdicts")
        self.assertEqual(verdicts, ["pass", "skipped"])
        self.assertTrue(outcome.episode.success)

    def test_an_unchecked_action_reports_skipped_not_passed(self) -> None:
        episode = play("planning", {"budget": 2}, policy=MockPolicy(script=["wait"])).episode

        self.assertEqual(episode.steps[0].verification_result, "skipped")
        self.assertFalse(episode.steps[0].verified)

    def test_a_verifier_that_raises_does_not_become_a_pass(self) -> None:
        outcome = manager(verification_engine=_RaisingVerifier()).run_episode(
            build_environment("multi_step"), task={}, seed=0
        )
        verification = outcome.episode.steps[0].verification_result
        step = outcome.episode.steps[0]

        # The environment observed the action itself, so its own pass stands —
        # but the engine that CRASHED is recorded beside it, not thrown away.
        self.assertEqual(step.verification["status"], "pass")
        self.assertIn("inconclusive", step.verification["verdicts"])
        self.assertTrue(
            any(
                row["status"] == "inconclusive" and "raised" in row["reason"]
                for row in step.verification["other_verdicts"]
            )
        )

        # And a check that DID crash can never be presented as a pass on its own:
        # with nothing else to go on, the step is inconclusive.
        combined = AgenticRolloutManager._combine(  # noqa: SLF001 - the pinned rule
            [{"status": "inconclusive", "reason": "the verification engine raised RuntimeError"}]
        )
        self.assertEqual(combined["status"], "inconclusive")
        self.assertNotEqual(combined["status"], "pass")
        self.assertIn(verification, {"pass", "fail", "inconclusive", "skipped"})


class _RaisingVerifier:
    """A verification engine whose check explodes — it must not become a pass."""

    def verify(self, step, output):  # type: ignore[no-untyped-def]
        raise RuntimeError("the verification strategy raised")


# ── evaluation ───────────────────────────────────────────────────────────────


class EvaluationTests(unittest.TestCase):
    """Short vs long, success vs failure, safety and efficiency, never merged."""

    def test_the_metrics_are_measured_over_the_episodes(self) -> None:
        episodes = (
            synthetic_episode(success=True, steps=2),
            synthetic_episode(success=False, steps=4),
            synthetic_episode(success=None, steps=3),
        )
        result = AgenticPolicyEvaluator(
            config=EvaluationConfig(minimum_sample_size=2)
        ).summarise(RuleBasedPolicy(), episodes)

        self.assertEqual(result.sample_size, 3)
        self.assertAlmostEqual(result.metric("task_success_rate") or 0.0, 0.5, places=6)
        self.assertAlmostEqual(result.metric("failure_rate") or 0.0, 0.5, places=6)
        self.assertAlmostEqual(result.metric("mean_steps") or 0.0, 3.0, places=6)

    def test_short_and_long_tasks_are_reported_separately(self) -> None:
        episodes = (
            synthetic_episode(success=True, steps=2),
            synthetic_episode(success=False, steps=6),
        )
        result = AgenticPolicyEvaluator(
            config=EvaluationConfig(short_task_steps=3)
        ).summarise(RuleBasedPolicy(), episodes)

        self.assertEqual(set(result.by_horizon), {"short", "long"})
        self.assertEqual(result.by_horizon["short"]["successes"], 1)
        self.assertEqual(result.by_horizon["long"]["success_rate"], 0.0)

    def test_runs_with_and_without_failures_are_reported_separately(self) -> None:
        episodes = (
            synthetic_episode(success=True, steps=2),
            synthetic_episode(success=True, steps=3, recovery=True),
        )
        result = AgenticPolicyEvaluator().summarise(RuleBasedPolicy(), episodes)

        self.assertEqual(set(result.by_outcome), {"no_failure", "failure_or_recovery"})
        self.assertEqual(result.by_outcome["failure_or_recovery"]["episodes"], 1)

    def test_safety_is_measured_on_its_own_and_never_averaged_away(self) -> None:
        episodes = (synthetic_episode(success=True, safety=-10.0),)
        result = AgenticPolicyEvaluator().summarise(RuleBasedPolicy(), episodes)

        self.assertEqual(result.metric("safety_rate"), 0.0)
        self.assertEqual(result.safety["unsafe_episodes"], 1)
        self.assertLess(result.safety["safety_total"], 0.0)
        self.assertIn("never averaged", result.safety["note"])

    def test_efficiency_is_reported_beside_the_reward_not_inside_it(self) -> None:
        result = AgenticPolicyEvaluator().summarise(
            RuleBasedPolicy(), (synthetic_episode(steps=4),)
        )

        self.assertIn("mean_steps", result.efficiency)
        self.assertIn("an increase in average reward alone is not evidence", result.efficiency["note"])

    def test_an_unmeasurable_metric_is_none_with_a_reason(self) -> None:
        result = AgenticPolicyEvaluator().summarise(
            RuleBasedPolicy(), (synthetic_episode(steps=2),)
        )

        self.assertIsNone(result.metric("recovery_success_rate"))
        self.assertTrue(any("recovery" in note for note in result.notes))

    def test_an_empty_evaluation_measures_nothing_rather_than_zero(self) -> None:
        result = AgenticPolicyEvaluator().summarise(RuleBasedPolicy(), ())

        self.assertEqual(result.sample_size, 0)
        self.assertEqual(result.metrics, {})
        self.assertIn("no episode was collected", result.notes[0])

    def test_the_default_task_set_can_meet_the_default_minimum_sample(self) -> None:
        minimum = EvaluationConfig().minimum_sample_size

        # The default distribution has to be able to produce a report that meets
        # the default minimum, or the two defaults would contradict each other.
        self.assertGreaterEqual(len(default_tasks()), minimum)
        self.assertGreaterEqual(len(default_tasks(include_failure=False)), minimum)

    def test_the_default_task_set_covers_every_slice_more_than_once(self) -> None:
        kinds = [task.kind for task in default_tasks()]

        for kind in ("short", "long", "failure"):
            self.assertGreaterEqual(kinds.count(kind), 2)

    def test_the_evaluation_runs_a_real_policy_over_the_default_task_set(self) -> None:
        result = AgenticPolicyEvaluator().evaluate(RuleBasedPolicy())

        self.assertGreaterEqual(result.sample_size, 6)
        self.assertEqual(result.metric("task_success_rate"), 1.0)
        self.assertTrue(result.meets_minimum_sample)

    def test_a_higher_reward_alone_is_reported_as_no_evidence(self) -> None:
        from novacontrol.agentic.evaluation import reward_improvement_is_not_evidence

        lower = AgenticPolicyEvaluator().summarise(
            RuleBasedPolicy(), (synthetic_episode(success=False, total_reward=0.1),)
        )
        higher_same_outcome = AgenticPolicyEvaluator().summarise(
            RuleBasedPolicy(), (synthetic_episode(success=False, total_reward=50.0),)
        )
        verdict = reward_improvement_is_not_evidence(lower, higher_same_outcome)

        self.assertTrue(verdict["reward_improved"])
        self.assertTrue(verdict["reward_without_outcomes"])

    def test_the_baseline_table_marks_what_cannot_run_as_unavailable(self) -> None:
        from novacontrol.agentic.evaluation import baseline_table

        table = baseline_table({})
        rows = table["baselines"]

        self.assertEqual(set(rows), {
            "deterministic_orchestration",
            "sft",
            "dpo_orpo",
            "rlhf_rlaif",
            "rlvr",
            "agentic_rl",
        })
        self.assertFalse(rows["sft"]["available"])
        self.assertIn("agentic policy", rows["sft"]["reason"])
        self.assertEqual(table["measured"], [])


# ── shadow ───────────────────────────────────────────────────────────────────


class ShadowTests(unittest.TestCase):
    """The production policy acts; the shadow only proposes."""

    def test_the_shadow_proposes_and_never_executes(self) -> None:
        comparison = ShadowPolicyEvaluator(
            RuleBasedPolicy(), MockPolicy(script=["complete"])
        ).run(tasks=default_tasks())

        self.assertGreater(comparison.steps, 0)
        self.assertFalse(comparison.to_dict()["shadow_executed_anything"])
        self.assertEqual(comparison.to_dict()["executed_by"], "production_policy")
        for proposal in comparison.proposals:
            self.assertFalse(proposal["executed"])
            self.assertTrue(proposal["production_action"])

    def test_agreements_and_disagreements_are_counted(self) -> None:
        comparison = ShadowPolicyEvaluator(
            RuleBasedPolicy(), MockPolicy(script=["complete"] * 40)
        ).run(tasks=default_tasks())

        self.assertGreater(comparison.agreements, 0)
        # Every step the shadow had an opinion about is exactly one of the two;
        # a proposal the shadow could not make is counted as uncompared, never
        # silently as a disagreement.
        self.assertEqual(comparison.comparable, comparison.agreements + comparison.disagreements)
        self.assertEqual(comparison.steps, comparison.comparable + comparison.uncompared)
        self.assertIsNotNone(comparison.agreement_rate)
        self.assertAlmostEqual(
            comparison.agreement_rate,
            comparison.agreements / comparison.comparable,
            places=6,
        )

    def test_a_shadow_that_cannot_propose_is_reported_not_hidden(self) -> None:
        class Broken(RuleBasedPolicy):
            def select_action(self, state, actions, *, mask=None):  # type: ignore[no-untyped-def]
                raise RuntimeError("the shadow has no opinion")

        comparison = ShadowPolicyEvaluator(RuleBasedPolicy(), Broken()).run(
            tasks=(EvaluationTask("simple", {}, 0, "one step", "short"),)
        )

        self.assertFalse(comparison.shadow_available)
        self.assertTrue(any("could not propose" in note for note in comparison.notes))

    def test_a_shadow_comparison_records_the_verified_outcome_beside_the_proposal(self) -> None:
        comparison = ShadowPolicyEvaluator(
            RuleBasedPolicy(), MockPolicy(script=["complete"])
        ).run(tasks=(EvaluationTask("simple", {}, 0, "one step", "short"),))

        proposal = comparison.proposals[0]
        self.assertIn("verified", proposal)
        self.assertIn("reward", proposal)
        self.assertEqual(proposal["executed_action"], "complete")


# ── A/B ──────────────────────────────────────────────────────────────────────


class ABTests(unittest.TestCase):
    """Two policies, one task distribution, identical criteria."""

    def test_both_policies_run_the_same_tasks_with_the_same_seeds(self) -> None:
        tasks = (
            EvaluationTask("simple", {}, 0, "one step", "short"),
            EvaluationTask("multi_step", {}, 0, "three steps", "short"),
        )
        result = ABPolicyEvaluator().evaluate(RuleBasedPolicy(), MockPolicy(script=["complete"]), tasks)

        self.assertTrue(result.same_task_distribution)
        self.assertEqual([task["seed"] for task in result.tasks], [0, 0])
        self.assertEqual(result.result_a.sample_size, result.result_b.sample_size)

    def test_the_comparison_reports_deltas_per_metric(self) -> None:
        class Detours(RuleBasedPolicy):
            """Always takes the long way: on a one-step budget that FAILS."""

            policy_id = "test-detours"

            def select_action(self, state, actions, *, mask=None):  # type: ignore[no-untyped-def]
                del state, mask
                chosen = next((item for item in actions if item.name == "detour"), None)
                return PolicyDecision(selected_action=chosen, confidence=0.5)

        tasks = (EvaluationTask("planning", {"budget": 1}, 0, "one-step plan", "short"),)
        result = ABPolicyEvaluator().evaluate(RuleBasedPolicy(), Detours(), tasks)

        metrics = result.comparison["metrics"]
        self.assertIn("task_success_rate", metrics)
        self.assertEqual(metrics["task_success_rate"]["baseline"], 1.0)
        self.assertEqual(metrics["task_success_rate"]["candidate"], 0.0)
        self.assertLess(metrics["task_success_rate"]["delta"], 0.0)
        self.assertEqual(metrics["task_success_rate"]["delta"], -1.0)
        self.assertTrue(result.comparison["comparable"])

    def test_an_experiment_switches_nothing_and_says_so(self) -> None:
        result = ABPolicyEvaluator().evaluate(RuleBasedPolicy(), RuleBasedPolicy())

        self.assertTrue(any("switches nothing" in note for note in result.notes))


# ── promotion ────────────────────────────────────────────────────────────────


class PromotionTests(unittest.TestCase):
    """Thresholds, gates, and a person: nothing promotes itself."""

    def candidate(self, **overrides: object):
        values: dict[str, object] = {
            "task_success_rate": 0.9,
            "verification_pass_rate": 0.9,
            "safety_rate": 1.0,
            "mean_latency_seconds": 0.1,
            "mean_resource_used": 0.2,
        }
        values.update(overrides)
        from novacontrol.agentic.models import AgenticEvaluationResult

        return AgenticEvaluationResult(
            policy_id="candidate",
            episodes=16,
            sample_size=16,
            metrics=values,  # type: ignore[arg-type]
        )

    def gate(self, **overrides: object) -> PromotionGate:
        values: dict[str, object] = {
            "min_task_success": 0.8,
            "min_verification_success": 0.8,
            "min_safety_rate": 1.0,
            "max_latency_ms": 1000.0,
            "max_resource_units": 1.0,
            "min_sample_size": 8,
        }
        values.update(overrides)
        return PromotionGate(PromotionThresholds(**values))  # type: ignore[arg-type]

    def test_every_threshold_must_be_met(self) -> None:
        decision = self.gate().evaluate(self.candidate(), approved_by="narasimha")

        self.assertTrue(decision.approved)
        self.assertEqual(decision.status, PolicyStatus.APPROVED.value)
        self.assertEqual(len(decision.checks), 10)

    def test_a_small_sample_is_refused(self) -> None:
        from novacontrol.agentic.models import AgenticEvaluationResult

        small = AgenticEvaluationResult(
            policy_id="candidate", episodes=2, sample_size=2, metrics={"task_success_rate": 1.0}
        )
        decision = self.gate().evaluate(small, approved_by="narasimha")

        self.assertFalse(decision.approved)
        self.assertTrue(any("sample_size" in failure for failure in decision.failures))

    def test_the_explicit_approval_gate_cannot_be_satisfied_by_a_number(self) -> None:
        decision = self.gate().evaluate(self.candidate())

        self.assertFalse(decision.approved)
        self.assertTrue(any("approver" in failure for failure in decision.failures))
        self.assertEqual(decision.status, PolicyStatus.EVALUATING.value)

    def test_an_unsafe_candidate_is_rejected_not_merely_unapproved(self) -> None:
        decision = self.gate().evaluate(
            self.candidate(safety_rate=0.5), approved_by="narasimha"
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.status, PolicyStatus.REJECTED.value)
        self.assertTrue(decision.failures)

    def test_an_unmeasured_safety_rate_fails_the_safety_gate(self) -> None:
        decision = self.gate().evaluate(
            self.candidate(safety_rate=None), approved_by="narasimha"
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.status, PolicyStatus.REJECTED.value)
        self.assertIn("not measurable", " ".join(decision.failures))

    def test_a_success_regression_against_the_baseline_fails_the_gate(self) -> None:
        baseline = self.candidate(task_success_rate=1.0)
        decision = self.gate().evaluate(
            self.candidate(task_success_rate=0.85), baseline=baseline, approved_by="narasimha"
        )

        self.assertFalse(decision.approved)
        self.assertTrue(any("success_regression" in failure for failure in decision.failures))

    def test_no_baseline_is_reported_as_not_applicable_rather_than_failed(self) -> None:
        decision = self.gate().evaluate(self.candidate(), approved_by="narasimha")
        regression = [
            item for item in decision.checks if item["check"].endswith("_regression")
        ]

        self.assertTrue(all(item["passed"] for item in regression))
        self.assertTrue(all(item.get("applicable") is False for item in regression))

    def test_a_slower_candidate_fails_the_latency_gate(self) -> None:
        decision = self.gate(max_latency_ms=50.0).evaluate(
            self.candidate(mean_latency_seconds=0.2), approved_by="narasimha"
        )

        self.assertFalse(decision.approved)
        self.assertTrue(any("latency" in failure for failure in decision.failures))

    def test_the_registry_refuses_a_promotion_the_gates_did_not_approve(self) -> None:
        registry = PolicyRegistry()
        registry.register_candidate(policy_id="candidate")
        refused = self.gate().evaluate(self.candidate())

        with self.assertRaises(PromotionRefused):
            registry.promote("candidate", refused, approved_by="narasimha")

    def test_the_registry_promotes_only_with_a_named_approver(self) -> None:
        registry = PolicyRegistry()
        registry.register_candidate(policy_id="candidate")
        approved = self.gate().evaluate(self.candidate(), approved_by="narasimha")

        record = registry.promote("candidate", approved, approved_by="narasimha")

        self.assertEqual(record.status, PolicyStatus.APPROVED.value)
        self.assertTrue(record.live)
        self.assertEqual(registry.production(), None)

    def test_nothing_is_live_until_somebody_promotes_it(self) -> None:
        registry = PolicyRegistry()
        registry.register_candidate(policy_id="fresh")

        self.assertEqual(registry.live(), ())
        self.assertEqual(registry.candidates()[0].policy_id, "fresh")
        self.assertIn("nothing is promoted automatically", registry.summary()["note"])

    def test_production_replaces_the_previous_policy_and_records_it(self) -> None:
        registry = PolicyRegistry()
        for policy_id in ("first", "second"):
            registry.register_candidate(policy_id=policy_id)
            registry.promote(
                policy_id,
                self.gate().evaluate(self.candidate(), approved_by="narasimha"),
                approved_by="narasimha",
            )
        registry.set_production("first", approved_by="narasimha")
        registry.set_production("second", approved_by="narasimha")

        self.assertEqual(registry.production().policy_id, "second")  # type: ignore[union-attr]
        first = registry.get("first")
        self.assertEqual(first.status, PolicyStatus.DEPRECATED.value)  # type: ignore[union-attr]
        self.assertTrue(any("replaced as production" in note for note in first.notes))  # type: ignore[union-attr]

    def test_a_policy_that_is_not_approved_cannot_become_production(self) -> None:
        registry = PolicyRegistry()
        registry.register_candidate(policy_id="candidate")

        with self.assertRaises(PromotionRefused):
            registry.set_production("candidate", approved_by="narasimha")

    def test_a_rejected_policy_can_be_recorded_and_rolled_back(self) -> None:
        registry = PolicyRegistry()
        registry.register_candidate(policy_id="candidate")
        registry.reject("candidate", reason="unsafe in evaluation")

        self.assertEqual(registry.get("candidate").status, "rejected")  # type: ignore[union-attr]
        rolled = registry.rollback("candidate", reason="back to the previous policy")
        self.assertEqual(rolled.status, PolicyStatus.DEPRECATED.value)

    def test_registering_a_policy_id_twice_is_a_conflict_not_an_update(self) -> None:
        registry = PolicyRegistry()
        record = registry.register_candidate(policy_id="candidate")

        with self.assertRaises(PromotionRefused):
            registry.register(record)

    def test_the_registry_survives_a_round_trip(self) -> None:
        registry = PolicyRegistry()
        registry.register_candidate(policy_id="candidate", environment="simple")
        restored = PolicyRegistry.from_dict(registry.to_dict())

        self.assertEqual(restored.get("candidate").environment, "simple")  # type: ignore[union-attr]

    def test_the_registry_reports_every_status_change_to_its_sink(self) -> None:
        seen: list[tuple[str, dict]] = []
        registry = PolicyRegistry(sink=lambda policy_id, change: seen.append((policy_id, dict(change))))
        registry.register_candidate(policy_id="candidate")
        registry.record_evaluation("candidate", {"task_success_rate": 1.0})

        self.assertEqual([policy_id for policy_id, _change in seen], ["candidate", "candidate"])
        self.assertEqual(seen[1][1]["to"], PolicyStatus.EVALUATING.value)


# ── resource governance ──────────────────────────────────────────────────────


class ResourceTests(unittest.TestCase):
    """Priced before anything starts, with no CUDA anywhere in the path."""

    def test_a_run_is_priced_with_a_verdict(self) -> None:
        estimate = AgenticResourceEstimator().estimate(AgenticRLConfig())

        self.assertIn(estimate.level, {"safe", "warning", "unsafe"})
        self.assertTrue(estimate.dry_run)
        self.assertEqual(estimate.backend, "dry_run")
        self.assertTrue(estimate.reasons)

    def test_an_unsafe_configuration_is_refused_by_the_estimator(self) -> None:
        huge = AgenticRLConfig(
            episodes=4096, max_episode_steps=512, max_planning_horizon=512, max_sequence_length=100_000
        )
        estimate = AgenticResourceEstimator(capabilities=bare()).estimate(huge)

        self.assertEqual(estimate.level, "unsafe")
        self.assertTrue(estimate.override_required)
        self.assertFalse(estimate.allows_training)
        # The refusal follows from the arithmetic rather than from the label: the
        # requirement is orders of magnitude past the pinned machine's usable
        # memory, so the verdict cannot drift with a constant or with the host.
        self.assertGreater(estimate.required_bytes, estimate.usable_bytes or 0)

    def test_the_estimate_counts_what_an_agentic_run_adds(self) -> None:
        estimate = AgenticResourceEstimator().estimate(AgenticRLConfig())
        components = dict(estimate.components)

        self.assertIn("episode_buffer", components)
        self.assertIn("curriculum_state", components)
        self.assertIn("policy_registry", components)

    def test_no_cuda_is_required_anywhere(self) -> None:
        summary = AgenticResourceEstimator().summary(AgenticRLConfig())

        self.assertFalse(summary["cuda_required"])
        self.assertIn("cpu", AgenticResourceEstimator().capabilities().backends)
        self.assertIn("estimate", summary)

    def test_the_estimator_reports_an_injected_machine(self) -> None:
        capabilities = HardwareCapabilities(
            cpu_count=4,
            gpu_available=False,
            backends=("cpu",),
            notes=("injected by a test",),
        )
        estimator = AgenticResourceEstimator(capabilities=capabilities)
        summary = estimator.summary(AgenticRLConfig())

        self.assertFalse(summary["hardware"]["gpu_available"])
        self.assertIn("cpu", summary["hardware"]["backends"])
        self.assertFalse(summary["dependencies"]["training_ready"] or not summary["dependencies"]["missing"])

    def test_a_dry_run_needs_no_optional_dependency(self) -> None:
        capabilities = HardwareCapabilities(cpu_count=2, backends=("cpu",))
        trainer = AgenticRLTrainer(AgenticRLConfig(), capabilities=capabilities)

        self.assertTrue(trainer.is_available())
        self.assertEqual(trainer.missing_dependencies(), ())


# ── dry run ──────────────────────────────────────────────────────────────────


class DryRunTests(unittest.TestCase):
    """The whole pipeline, simulated, with nothing trained."""

    def test_the_dry_run_walks_every_stage_in_order(self) -> None:
        report = run_agentic_dry_run(AgenticRLConfig(episodes=4))

        self.assertEqual(
            report.ordered_stages,
            (
                "environment",
                "state",
                "policy",
                "action",
                "execution",
                "verification",
                "reward",
                "credit_assignment",
                "state_transition",
                "episode_termination",
                "evaluation",
                "checkpoint",
                "registration",
            ),
        )
        self.assertFalse(report.trained)
        self.assertFalse(report.model_loaded)

    def test_the_dry_run_records_a_real_episode_result(self) -> None:
        report = run_agentic_dry_run(AgenticRLConfig())

        self.assertEqual(report.stage("episode_termination")["termination_reason"], "success")
        self.assertTrue(report.stage("execution")["executed"])
        self.assertEqual(report.stage("verification")["status"], "pass")
        self.assertTrue(report.stage("credit_assignment")["credits"])
        self.assertTrue(report.stage("reward")["dimension_totals"])

    def test_the_dry_run_produces_a_checkpoint_and_a_candidate(self) -> None:
        report = run_agentic_dry_run(AgenticRLConfig())

        self.assertTrue(report.stage("checkpoint")["integrity_ok"])
        registration = report.stage("registration")
        self.assertEqual(registration["status"], PolicyStatus.EVALUATING.value)
        self.assertFalse(registration["promoted"])
        self.assertTrue(any("EXPERIMENTAL" in note for note in report.notes))

    def test_the_dry_run_refuses_an_invalid_configuration(self) -> None:
        with self.assertRaises(ValueError):
            run_agentic_dry_run(AgenticRLConfig(algorithm="ppo"))

    def test_the_checkpoint_carries_everything_a_restore_needs(self) -> None:
        payload = checkpoint_payload(AgenticRLConfig(), step=3, kind="best")

        for key in (
            "policy_version",
            "model_id",
            "training_config",
            "reward_config",
            "curriculum_config",
            "environment_version",
            "optimizer_state",
        ):
            self.assertIn(key, payload)
        self.assertTrue(checkpoint_integrity_ok(payload))

    def test_an_edited_checkpoint_fails_its_integrity_check(self) -> None:
        payload = checkpoint_payload(AgenticRLConfig())
        payload["step"] = 99

        self.assertFalse(checkpoint_integrity_ok(payload))

    def test_a_rollback_names_the_previous_checkpoint_and_verifies_both(self) -> None:
        from novacontrol.agentic import rollback_target

        current = checkpoint_payload(AgenticRLConfig(policy_id="p2"))
        previous = checkpoint_payload(AgenticRLConfig(policy_id="p1"))
        target = rollback_target(current, previous)

        self.assertEqual(target["restore"], "p1")
        self.assertEqual(target["from"], "p2")
        self.assertTrue(target["previous_integrity_ok"])


# ── trainer ──────────────────────────────────────────────────────────────────


class TrainerTests(unittest.TestCase):
    """The schedule, the mock optimizer, and the refusal to pretend."""

    def run_config(self, **overrides: object) -> AgenticRLConfig:
        values: dict[str, object] = {"episodes": 4, "batch_size": 2, "checkpoint_frequency": 0}
        values.update(overrides)
        return AgenticRLConfig(**values)  # type: ignore[arg-type]

    def training_run(self) -> TrainingRun:
        return TrainingRun(
            run_id="run-1",
            dataset_type="agentic_episodes",
            dataset_version="agentic:simple",
            model="",
            backend="agentic_rl",
        )

    def test_the_trainer_walks_the_schedule_and_reports_episodes(self) -> None:
        trainer = AgenticRLTrainer(self.run_config())
        checkpoints: list[str] = []
        final = trainer.start_training(
            self.training_run(), None, TrainingCallbacks(on_checkpoint=lambda run, kind: checkpoints.append(kind))
        )

        self.assertEqual(final.status, "completed")
        self.assertEqual(final.current_step, 4)
        self.assertEqual(final.rl_metrics["episodes"], 4)
        self.assertIn("best", checkpoints)
        self.assertTrue(final.loss_history)
        self.assertEqual(len(trainer.episodes), 4)

    def test_the_run_records_the_curriculum_and_the_reward_dimensions(self) -> None:
        trainer = AgenticRLTrainer(self.run_config())
        final = trainer.start_training(self.training_run(), None, TrainingCallbacks())
        metrics = dict(final.rl_metrics)

        self.assertGreaterEqual(metrics["curriculum_level"], 1)
        self.assertIn("summary", metrics)
        self.assertIn("task_success", metrics["summary"]["manifest"]["dimension_totals"])

    def test_the_trainer_can_be_cancelled_mid_run(self) -> None:
        trainer = AgenticRLTrainer(self.run_config(episodes=20))
        state = {"cancelled": False}

        def hook(index: int) -> None:
            if index >= 2:
                state["cancelled"] = True

        final = trainer.start_training(
            self.training_run(),
            None,
            TrainingCallbacks(is_cancelled=lambda: state["cancelled"], step_hook=hook),
        )

        self.assertEqual(final.status, "cancelled")
        self.assertLess(final.current_step, 20)

    def test_the_trainer_can_pause_and_resume(self) -> None:
        trainer = AgenticRLTrainer(self.run_config(episodes=6))
        paused = trainer.start_training(
            self.training_run(), None, TrainingCallbacks(is_paused=lambda: True)
        )
        self.assertEqual(paused.status, "paused")

        resumed = trainer.resume_training(
            self.training_run(), None, paused.current_step, TrainingCallbacks()
        )
        self.assertEqual(resumed.status, "completed")

    def test_a_real_run_is_refused_while_the_optimizer_is_a_mock(self) -> None:
        trainer = AgenticRLTrainer(self.run_config(dry_run=False))
        from novacontrol.training.backends import TrainingBackendUnavailable

        with self.assertRaises(TrainingBackendUnavailable):
            trainer.start_training(self.training_run(), None, TrainingCallbacks())

    def test_a_planned_algorithm_is_refused_rather_than_downgraded(self) -> None:
        from novacontrol.rlhf.backends import PolicyOptimizerUnavailable

        with self.assertRaises(PolicyOptimizerUnavailable):
            AgenticRLTrainer(self.run_config(algorithm="grpo"))

    def test_the_mock_optimizer_produces_measurements_and_says_it_is_simulated(self) -> None:
        trainer = AgenticRLTrainer(self.run_config())
        trainer.start_training(self.training_run(), None, TrainingCallbacks())
        update = MockAgenticPolicyOptimizer().optimize(
            tuple(trainer.episodes), config=trainer.agentic_config
        )

        self.assertTrue(update.simulated)
        self.assertFalse(MockAgenticPolicyOptimizer().learns)
        self.assertEqual(update.examples, len(trainer.episodes))
        self.assertTrue(any("not a gradient step" in note for note in update.notes))

    def test_the_mock_optimizer_refuses_an_episode_set_with_no_outcome(self) -> None:
        from novacontrol.rlhf.backends import PolicyOptimizerUnavailable

        with self.assertRaises(PolicyOptimizerUnavailable):
            MockAgenticPolicyOptimizer().optimize((), config=AgenticRLConfig())

    def test_the_trainer_describes_what_a_run_would_do_without_loading_anything(self) -> None:
        description = AgenticRLTrainer(self.run_config()).initialize_policy()

        self.assertFalse(description["loaded"])
        self.assertFalse(description["learns"])
        self.assertIn("no model is loaded", description["note"])

    def test_the_trainer_registers_its_candidate_as_experimental(self) -> None:
        trainer = AgenticRLTrainer(self.run_config())
        trainer.start_training(self.training_run(), None, TrainingCallbacks())
        record = trainer.register_candidate(training_run_id="run-1")

        self.assertEqual(record.status, PolicyStatus.EXPERIMENTAL.value)
        self.assertFalse(record.live)
        self.assertTrue(record.simulated)

    def test_the_model_metadata_says_what_was_produced(self) -> None:
        metadata = AgenticRLTrainer(self.run_config()).model_metadata()

        self.assertEqual(metadata["agentic_mode"], "agentic")
        self.assertTrue(metadata["simulated"])
        self.assertFalse(metadata["cuda_required"])
        self.assertEqual(metadata["training_method"], "dry_run")

    def test_the_trainer_evaluates_its_own_policy_without_a_model(self) -> None:
        trainer = AgenticRLTrainer(self.run_config())
        trainer.start_training(self.training_run(), None, TrainingCallbacks())
        result = trainer.evaluate()

        self.assertGreaterEqual(result["sample_size"], 1)
        self.assertIn("task_success_rate", result["metrics"])


# ── security ─────────────────────────────────────────────────────────────────


class _Picks(RuleBasedPolicy):
    """Always asks for one named action, allowed by the mask or not."""

    policy_id = "test-picks"

    def __init__(self, wanted: str) -> None:
        super().__init__()
        self.wanted = wanted

    def select_action(self, state, actions, *, mask=None):  # type: ignore[no-untyped-def]
        del state, mask
        chosen = next((item for item in actions if item.name == self.wanted), None)
        return PolicyDecision(selected_action=chosen, confidence=1.0)


class _NeedsApproval(SimpleEnvironment):
    """One step, but its only action declares that a person must approve it.

    A test-only environment, on the interface a future phase would use: the
    built-in environments are all approval-free, so the "kept because somebody
    can be asked" path has to be provoked honestly rather than simulated.
    """

    environment_id = "needs_approval"

    def _available_actions(self) -> tuple[AgentAction, ...]:
        return (
            AgentAction(
                tool="complete",
                capability="complete",
                label="finish the task (needs approval)",
                expected_effect="local_write",
                requires_confirmation=True,
            ),
        )


class SecurityTests(unittest.TestCase):
    """Permissions, masking, destructive blocking, cancellation."""

    def test_a_denied_action_never_reaches_the_environment(self) -> None:
        permissions = PermissionManager()
        permissions.declare("advance", destructive=True, reversible=False, risk_level=RiskLevel.HIGH)
        env = build_environment("planning")
        state = AgentState(goal=env.goal())
        masker = ActionMasker(permission_manager=permissions)
        mask = masker.mask(state, env.available_actions())

        # The permission layer, not the environment, is what removed it.
        denied = next(item for item in env.available_actions() if item.tool == "advance")
        self.assertNotIn("advance", mask.names())
        self.assertEqual(mask.blocked_reason(denied.action_id), "unauthorized")

        runner = AgenticRolloutManager(
            RuleBasedPolicy(),
            masker=masker,
            limits=limits(refusal_tolerance=0),
        )
        outcome = runner.run_episode(env, task={"budget": 2}, seed=0)

        # Whatever the episode did, the denied action never executed: not in the
        # recorded steps, and not in the environment's own step log.
        self.assertNotIn("advance", {step.action.name for step in outcome.episode.steps})
        self.assertNotIn("advance", {step.action.name for step in env.steps})
        self.assertEqual(env.index, len(env.steps))

    def test_an_approved_action_may_run(self) -> None:
        permissions = PermissionManager()
        permissions.declare("advance", destructive=True, reversible=False, risk_level=RiskLevel.HIGH)
        masker = ActionMasker(permission_manager=permissions, approved=frozenset({"advance"}))
        env = build_environment("planning")
        mask = masker.mask(
            AgentState(goal=env.goal(), tools=("advance", "detour", "wait")),
            env.available_actions(),
        )

        self.assertIn("advance", mask.names())

        # An approval already given is not asked for a second time, and the
        # approved action really runs.
        asked: list[str] = []

        def never(action, state):  # type: ignore[no-untyped-def]
            asked.append(action.name)
            return False

        outcome = AgenticRolloutManager(
            _Picks("advance"),
            masker=masker,
            limits=limits(),
            confirmation=never,
        ).run_episode(build_environment("planning"), task={"budget": 1}, seed=0)

        self.assertEqual(outcome.episode.termination_reason, "success")
        self.assertEqual([step.action.name for step in outcome.episode.steps], ["advance"])
        self.assertEqual(asked, [])

    def test_an_action_needing_approval_is_refused_without_a_way_to_ask(self) -> None:
        env = _NeedsApproval()
        runner = AgenticRolloutManager(
            _Picks("complete"),
            masker=ActionMasker(allow_confirmation=True),
            limits=limits(refusal_tolerance=0),
        )
        outcome = runner.run_episode(env, task={}, seed=0)

        self.assertEqual(outcome.episode.termination_reason, "safety_stop")
        self.assertFalse(env.steps)
        self.assertFalse(outcome.episode.steps[0].executed)
        self.assertIn("needs an approval", outcome.episode.steps[0].error)

    def test_a_confirmation_that_is_refused_stops_the_action(self) -> None:
        env = _NeedsApproval()
        outcome = AgenticRolloutManager(
            _Picks("complete"),
            masker=ActionMasker(allow_confirmation=True),
            limits=limits(refusal_tolerance=0),
            confirmation=lambda action, state: False,
        ).run_episode(env, task={}, seed=0)

        self.assertEqual(outcome.episode.termination_reason, "safety_stop")
        self.assertFalse(env.steps)
        self.assertIn("was not given", outcome.episode.steps[0].error)

    def test_a_confirmation_that_is_given_is_the_only_way_it_runs(self) -> None:
        env = _NeedsApproval()
        outcome = AgenticRolloutManager(
            _Picks("complete"),
            masker=ActionMasker(allow_confirmation=True),
            limits=limits(),
            confirmation=lambda action, state: True,
        ).run_episode(env, task={}, seed=0)

        self.assertEqual(outcome.episode.termination_reason, "success")
        self.assertEqual([step.action.name for step in env.steps], ["complete"])

    def test_a_confirmation_hook_that_raises_is_not_an_approval(self) -> None:
        def broken(action, state):  # type: ignore[no-untyped-def]
            raise RuntimeError("the approver died")

        env = _NeedsApproval()
        runner = AgenticRolloutManager(
            _Picks("complete"),
            masker=ActionMasker(allow_confirmation=True),
            limits=limits(refusal_tolerance=0),
            confirmation=broken,
        )
        outcome = runner.run_episode(env, task={}, seed=0)

        self.assertEqual(outcome.episode.termination_reason, "safety_stop")
        self.assertFalse(env.steps)
        self.assertTrue(runner.confirmation_errors)
        self.assertIn("RuntimeError", runner.confirmation_errors[0])

    def test_the_rollout_injects_the_permission_layer_into_the_mask(self) -> None:
        permissions = PermissionManager()
        permissions.declare("advance", destructive=True, reversible=False, risk_level=RiskLevel.HIGH)
        masker = ActionMasker(permission_manager=permissions)
        runner = AgenticRolloutManager(RuleBasedPolicy(), masker=masker, limits=limits())

        # A policy that insists on the denied action is REFUSED, and an episode
        # that keeps being refused ends as a safety stop, not a quiet failure.
        outcome = runner.run_episode(
            build_environment("planning"), task={"budget": 2}, seed=0
        )
        # The denied action is not in the mask, so the policy works around it and
        # whatever it did, it did NOT advance.
        self.assertNotIn("advance", {step.action.name for step in outcome.episode.steps})
        self.assertGreater(outcome.episode.length, 0)

        class Insistent(RuleBasedPolicy):
            policy_id = "test-insistent"

            def select_action(self, state, actions, *, mask=None):  # type: ignore[no-untyped-def]
                del state, actions, mask
                return PolicyDecision(
                    selected_action=AgentAction(
                        tool="advance",
                        capability="advance",
                        expected_effect="local_write",
                    ),
                    confidence=1.0,
                )

        refused = AgenticRolloutManager(
            Insistent(), masker=masker, limits=limits(refusal_tolerance=0)
        ).run_episode(build_environment("planning"), task={"budget": 2}, seed=0)

        self.assertEqual(refused.episode.termination_reason, "safety_stop")
        self.assertIn("approval", refused.termination_detail)
        self.assertFalse(refused.episode.steps[0].executed)

    def test_a_policy_cannot_bypass_verification_by_reporting_success(self) -> None:
        class Lying(RuleBasedPolicy):
            def select_action(self, state, actions, *, mask=None):  # type: ignore[no-untyped-def]
                decision = super().select_action(state, actions, mask=mask)
                return type(decision)(
                    selected_action=AgentAction(tool="give_up", capability="give_up"),
                    confidence=1.0,
                )

        outcome = play("recovery", {"failures": 1}, policy=Lying())
        episode = outcome.episode

        self.assertFalse(episode.success)
        self.assertEqual(episode.termination_reason, "failure")

    def test_cancellation_is_honoured_between_steps(self) -> None:
        seen: list[int] = []

        def cancel() -> bool:
            seen.append(1)
            return len(seen) > 1

        outcome = manager(cancelled=cancel).run_episode(
            build_environment("multi_step"), task={}, seed=0
        )

        self.assertEqual(outcome.episode.termination_reason, "cancelled")
        self.assertGreater(len(seen), 1)

    def test_the_mask_is_part_of_the_recorded_decision(self) -> None:
        permissions = PermissionManager()
        permissions.declare("wait", requires_confirmation=True)
        runner = AgenticRolloutManager(
            RuleBasedPolicy(), masker=ActionMasker(permission_manager=permissions), limits=limits()
        )
        outcome = runner.run_episode(build_environment("planning"), task={"budget": 2}, seed=0)
        masked = outcome.episode.steps[0].policy_metadata["masked"]

        self.assertTrue(masked)
        self.assertIn(masked[0]["reason"], {"confirmation_required", "unauthorized"})


# ── no hidden reasoning ──────────────────────────────────────────────────────


class NoHiddenReasoningTests(unittest.TestCase):
    """There is no field for a chain of thought, and no text that pretends to be one."""

    FORBIDDEN = re.compile(r"\b(thought|thoughts|reasoning|chain_of_thought|inner_monologue)\b", re.I)

    def test_no_agentic_module_names_a_reasoning_field(self) -> None:
        offenders: list[str] = []
        for path in sorted(PACKAGE.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for match in self.FORBIDDEN.finditer(text):
                # The phrase "no hidden chain-of-thought" in the docstrings is a
                # PROHIBITION, not a field: only a quoted key counts as one.
                window = text[max(0, match.start() - 40) : match.end() + 40]
                if f'"{match.group(0).lower()}"' in window or f"'{match.group(0).lower()}'" in window:
                    offenders.append(f"{path.name}: {window.strip()}")
        self.assertEqual(offenders, [])

    def test_the_episode_schema_has_no_reasoning_field(self) -> None:
        keys = set(synthetic_episode().to_dict())
        self.assertFalse(keys & {"reasoning", "thoughts", "chain_of_thought", "inner_monologue"})

    def test_a_transition_records_structured_evidence_instead(self) -> None:
        transition = play("tool_selection").episode.steps[0].to_dict()
        keys = set(transition)

        self.assertIn("verification", keys)
        self.assertIn("policy_metadata", keys)
        self.assertIn("result", keys)
        self.assertFalse(keys & {"reasoning", "thoughts"})
        self.assertIn("reason_code", transition["policy_metadata"])

    def test_a_decision_carries_a_reason_code_and_alternatives(self) -> None:
        env = build_environment("tool_selection")
        decision = RuleBasedPolicy().select_action(AgentState(goal=env.goal()), env.available_actions())

        self.assertIn(
            decision.reason_code, {member.value for member in PolicyReasonCode}
        )
        self.assertIn("reason_code", decision.to_dict())
        self.assertNotIn("reasoning", decision.to_dict())


# ── optionality and regression ───────────────────────────────────────────────


class OptionalityTests(unittest.TestCase):
    """The phase is optional, starts nothing, and leaves the app alone."""

    def test_importing_the_package_starts_nothing(self) -> None:
        import novacontrol.agentic as agentic

        self.assertFalse(agentic.overview()["automatic_training"])
        self.assertFalse(agentic.overview()["automatic_model_loading"])
        self.assertFalse(agentic.overview()["stores_hidden_reasoning"])
        self.assertFalse(agentic.overview()["cuda_required"])

    def test_dry_run_is_the_default_everywhere(self) -> None:
        self.assertTrue(AgenticRLConfig().dry_run)
        self.assertTrue(AgenticRLConfig().validate().valid)
        self.assertIn("dry_run is off", " ".join(AgenticRLConfig(dry_run=False).validate().warnings))

    def test_the_deferred_phases_are_named_and_not_implemented(self) -> None:
        import novacontrol.agentic as agentic

        deferred = agentic.overview()["deferred"]
        self.assertEqual(len(deferred), 6)
        self.assertTrue(all("Phase 2" in item for item in deferred))
        for name in ("perception", "world_model", "embodied", "arc"):
            self.assertFalse(
                (PACKAGE / f"{name}.py").exists(), f"{name} must not exist in phase 20"
            )

    def test_no_agentic_module_imports_a_model_runtime(self) -> None:
        forbidden = ("from_pretrained", "AutoModel", "torch.load", "set_completion_provider")
        offenders = [
            path.name
            for path in sorted(PACKAGE.glob("*.py"))
            for needle in forbidden
            if needle in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])

    def test_the_phase_is_not_wired_into_the_application(self) -> None:
        application = (
            Path(__file__).resolve().parent.parent / "src" / "novacontrol" / "application.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("novacontrol.agentic", application)
        self.assertNotIn("AgenticRLTrainer", application)

    def test_an_episode_can_be_scored_as_a_phase15_reward_result(self) -> None:
        engine = AgenticRewardEngine()
        episode = play("simple").episode
        result = engine.to_reward_result(episode)

        self.assertGreater(result.total_reward, 0.0)
        self.assertTrue(result.components)
        self.assertEqual(result.reward_source, "rule")
        json.dumps(result.to_dict(), ensure_ascii=False)

    def test_the_reward_keeps_safety_apart_from_efficiency(self) -> None:
        engine = AgenticRewardEngine(AgenticRewardWeights())
        from novacontrol.agentic.rewards import StepContext

        reward = engine.step_reward(
            StepContext(
                index=0,
                action=AgentAction(tool="advance"),
                progressed=True,
                unsafe=True,
                done=True,
                success=True,
                termination_reason="success",
            )
        )

        self.assertLess(reward.dimension(SAFETY_DIMENSION), 0.0)
        self.assertGreater(reward.dimension("planning_efficiency"), 0.0)
        self.assertEqual(reward.safety, reward.dimension(SAFETY_DIMENSION))
        self.assertTrue(reward.unsafe)

    def test_step_rewards_cannot_outweigh_the_outcome(self) -> None:
        engine = AgenticRewardEngine(AgenticRewardWeights(max_step_reward_share=0.2))
        episode = synthetic_episode(steps=6, terminal_reward=1.0, total_reward=100.0)
        summary = engine.episode_reward(episode)

        self.assertLessEqual(
            abs(summary["intermediate_reward"]), abs(summary["terminal_reward"]) * 0.2 + 1e-6
        )
        self.assertIn("capped", summary["reasons"])

    def test_a_measured_latency_over_budget_is_penalised(self) -> None:
        engine = AgenticRewardEngine(AgenticRewardWeights(latency_budget_ms=100.0))
        from novacontrol.agentic.rewards import StepContext

        reward = engine.step_reward(
            StepContext(index=0, action=AgentAction(tool="a"), latency_ms=500.0)
        )

        self.assertIn(StepPenalty.EXCESSIVE_LATENCY.value, reward.penalties)
        self.assertLess(reward.dimension("latency"), 0.0)

    def test_an_unmeasured_latency_is_not_invented(self) -> None:
        engine = AgenticRewardEngine()
        from novacontrol.agentic.rewards import StepContext

        reward = engine.step_reward(
            StepContext(index=0, action=AgentAction(tool="a"), latency_ms=None)
        )

        self.assertEqual(reward.dimension("latency"), 0.0)
        self.assertNotIn(StepPenalty.EXCESSIVE_LATENCY.value, reward.penalties)


if __name__ == "__main__":
    unittest.main()
