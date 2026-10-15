"""Phase 24 — planning, reasoning and action policy: contracts and workflows.

Two halves, both required.

The first half exercises the planning contracts directly: goal representation,
decomposition validation, candidate comparison, outcome estimation with its
basis, revalidation, completion, replanning and the capability table. These
tests pin the RULES — a cycle is reported, a retired rule is not borrowed, a
condition that cannot be observed is not rounded up.

The second half drives the REAL application over HTTP (``create_app()`` against
an isolated data directory), which is the interface a user or a CLI reaches:
``POST /planning/goals`` to create and plan, ``POST /planning/goals/{id}/run``
to execute, ``GET /planning/goals/{id}`` to read the record back. That is the
only thing that proves the endpoints, the application methods, the policy
wiring, the fresh-state gate and the persistence agree with each other — an
internal call could pass while the route 422s.

Every workflow that claims a run COMPLETED also proves the success condition was
observed by the world model (through ``POST /world/observe``); a run whose
conditions nothing observed must report ``needs_observation``, never completion.
"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

try:  # pragma: no cover - import guard mirrors the rest of the suite
    from fastapi.testclient import TestClient

    from novacontrol.api.app import create_app
    from novacontrol.application import NovaControlApplication
except Exception:  # pragma: no cover - httpx/fastapi missing
    TestClient = None  # type: ignore[assignment]
    create_app = None  # type: ignore[assignment]
    NovaControlApplication = None  # type: ignore[assignment]

from novacontrol.browser import NoopBrowserRunner
from novacontrol.core.config import GoalPlanningSettings, NovaControlConfig
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.planning.candidates import (
    BaselineOrderPlanner,
    CostAwarePlanner,
    PlanningBudget,
)
from novacontrol.planning.capabilities import (
    CAPABILITY_TABLE,
    capability_rows,
    register_planning_capabilities,
)
from novacontrol.planning.estimation import EstimateBasis, OutcomeEstimator
from novacontrol.planning.executor import StepContext, WorkflowExecutor
from novacontrol.planning.goal_manager import GoalManager, JsonGoalRepository
from novacontrol.planning.goals import (
    Goal,
    GoalBudget,
    GoalStatus,
    IssueKind,
    SuccessCondition,
    Task,
    coerce_value,
    decompose,
)
from novacontrol.planning.grounding import StateGroundingService
from novacontrol.planning.models import (
    Plan,
    PlanStep,
    StepEffect,
    VerificationMethod,
    VerificationSpec,
)
from novacontrol.planning.monitor import PlanMonitor
from novacontrol.planning.policy import ActionPolicy
from novacontrol.planning.ranking import PlanRanker
from novacontrol.reliability import VerificationEngine

# --------------------------------------------------------------------------- #
# Small doubles
# --------------------------------------------------------------------------- #


class _MemoryStore:
    """The two methods the goal repository uses from the JSON state store."""

    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}
        self.fail_writes = False

    def read(self, name: str) -> dict[str, Any]:
        payload = self.values.get(name)
        return dict(payload) if isinstance(payload, dict) else {}

    def write(self, name: str, payload: dict[str, Any]) -> None:
        if self.fail_writes:
            raise OSError("the store is unavailable")
        self.values[name] = dict(payload)


class _Rule:
    """A Phase 23 learned rule, structurally: id, scope, status, prediction."""

    def __init__(
        self,
        rule_id: str,
        *,
        action: str,
        outcome: dict[str, Any],
        scope: str = "sim",
        status: str = "provisional",
    ) -> None:
        self.rule_id = rule_id
        self.scope = scope
        self.status = status
        self._action = action
        self._outcome = dict(outcome)

    def applies_to(self, scope: str) -> bool:
        return not self.scope or self.scope == scope

    def predicts(self, action: str) -> dict[str, Any] | None:
        return dict(self._outcome) if action == self._action else None


class _RuleBook:
    """The subset of ``RuleBook`` the estimator is documented to use."""

    def __init__(self, rules: tuple[_Rule, ...] = ()) -> None:
        self._rules = rules

    @property
    def rules(self) -> tuple[_Rule, ...]:
        return tuple(self._rules)

    def predict(self, action: str, *, scope: str = "") -> tuple[dict[str, Any], _Rule] | None:
        best: tuple[dict[str, Any], _Rule] | None = None
        for rule in self._rules:
            if rule.status == "retired":
                continue
            if scope and rule.scope and rule.scope != scope:
                continue
            outcome = rule.predicts(action)
            if outcome:
                best = (outcome, rule)
        return best


def _task(task_id: str, **overrides: Any) -> Task:
    base: dict[str, Any] = {
        "id": task_id,
        "title": task_id.replace("_", " ").title(),
        "action": f"do_{task_id}",
        "estimated_seconds": 0.01,
    }
    base.update(overrides)
    return Task(**base)


def _goal(**overrides: Any) -> Goal:
    base: dict[str, Any] = {
        "request": "finish the release checklist",
        "success_conditions": (
            SuccessCondition(key="release.ready", expected=True, description="ready"),
        ),
    }
    base.update(overrides)
    return Goal(**base)


# --------------------------------------------------------------------------- #
# The contracts
# --------------------------------------------------------------------------- #


class GoalRepresentationTests(unittest.TestCase):
    """A goal keeps what it was given and reports what it was not."""

    def test_a_goal_without_conditions_reports_unmeasurable(self) -> None:
        goal = Goal(request="make it better")
        self.assertFalse(goal.measurable)
        result = decompose(goal, [_task("only")])
        kinds = {issue.kind for issue in result.issues}
        self.assertIn(IssueKind.UNMEASURABLE, kinds)
        self.assertFalse(result.measurable)

    def test_a_condition_without_an_expected_value_is_not_measurable(self) -> None:
        condition = SuccessCondition(key="release.ready")
        self.assertFalse(condition.measurable, "no expected value cannot be checked")
        self.assertIsNone(condition.satisfied_by({"release.ready": True}))

    def test_stated_fields_survive_a_round_trip_and_unstated_ones_stay_empty(self) -> None:
        goal = _goal(
            constraints=("no network",),
            exclusions=("destructive",),
            initial_state={"disk.free": 10},
            provenance="test",
            correlation_id="corr-1",
            deadline_seconds=30.0,
        )
        restored = Goal.from_mapping(goal.to_dict())
        self.assertEqual(restored.constraints, ("no network",))
        self.assertEqual(restored.exclusions, ("destructive",))
        self.assertEqual(restored.initial_state, {"disk.free": 10})
        self.assertEqual(restored.provenance, "test")
        self.assertEqual(restored.correlation_id, "corr-1")
        self.assertEqual(restored.deadline_seconds, 30.0)
        # Nothing invented: a goal states no approval conditions, so it has none.
        self.assertEqual(restored.approval_conditions, ())
        self.assertEqual(restored.failure_conditions, ())

    def test_an_asserted_initial_state_is_never_an_observation(self) -> None:
        service = StateGroundingService(world=None)
        grounding = service.ground(["release.ready"], asserted={"release.ready": True})
        fact = grounding.fact("release.ready")
        self.assertIsNotNone(fact)
        self.assertFalse(fact.usable, "what the caller said is not what the machine did")
        self.assertEqual(fact.source, "caller_assertion")
        self.assertIsNone(fact.holds(True))
        self.assertTrue(grounding.limitations)


class DecompositionValidationTests(unittest.TestCase):
    """Cycles, missing prerequisites, duplicate ids and impossible budgets."""

    def test_a_dependency_cycle_is_reported(self) -> None:
        goal = _goal()
        tasks = [
            _task("a", depends_on=("b",)),
            _task("b", depends_on=("a",)),
        ]
        result = decompose(goal, tasks)
        cycles = result.issues_of(IssueKind.CYCLE)
        self.assertEqual(len(cycles), 1)
        self.assertIn("a", cycles[0].detail)
        self.assertFalse(result.ok)

    def test_a_prerequisite_no_task_produces_is_reported(self) -> None:
        goal = _goal()
        tasks = [_task("a", preconditions=("tree.clean=True",))]
        kinds = {issue.kind for issue in decompose(goal, tasks).issues}
        self.assertIn(IssueKind.MISSING_PREREQUISITE, kinds)

    def test_a_prerequisite_an_earlier_task_produces_is_fine(self) -> None:
        goal = _goal()
        tasks = [
            _task("check", effects=("tree.clean=True",)),
            _task("prepare", depends_on=("check",), preconditions=("tree.clean=True",)),
        ]
        result = decompose(goal, tasks)
        self.assertEqual(
            [issue for issue in result.issues if issue.kind is IssueKind.MISSING_PREREQUISITE],
            [],
        )

    def test_a_prerequisite_the_state_holds_is_fine(self) -> None:
        goal = _goal()
        tasks = [_task("a", preconditions=("tree.clean=True",))]
        result = decompose(goal, tasks, held_facts={"tree.clean": True})
        self.assertEqual(
            [issue for issue in result.issues if issue.kind is IssueKind.MISSING_PREREQUISITE],
            [],
        )

    def test_a_capability_this_build_does_not_declare_is_reported(self) -> None:
        goal = _goal()
        tasks = [_task("a", capabilities=("teleport",))]
        kinds = {
            issue.kind for issue in decompose(goal, tasks, known_capabilities=("read",)).issues
        }
        self.assertIn(IssueKind.UNKNOWN_CAPABILITY, kinds)

    def test_an_exclusion_the_only_path_violates_is_reported(self) -> None:
        goal = _goal(exclusions=("destructive",))
        tasks = [_task("a", effect=StepEffect.DESTRUCTIVE)]
        kinds = {issue.kind for issue in decompose(goal, tasks).issues}
        self.assertIn(IssueKind.IMPOSSIBLE_CONSTRAINT, kinds)

    def test_a_decomposition_over_the_goal_budget_is_reported(self) -> None:
        goal = _goal(budget=GoalBudget(max_steps=1))
        kinds = {issue.kind for issue in decompose(goal, [_task("a"), _task("b")]).issues}
        self.assertIn(IssueKind.IMPOSSIBLE_CONSTRAINT, kinds)


class CandidateAndRankingTests(unittest.TestCase):
    """Two planners, one documented comparison, and never an infeasible pick."""

    def _outcomes(self, goal: Goal, tasks: list[Task], budget: PlanningBudget | None = None):
        service = StateGroundingService(world=None)
        grounding = service.ground(["release.ready"])
        decomposition = decompose(goal, tasks)
        limits = budget or PlanningBudget()
        return [
            planner.plan(goal, decomposition, grounding, budget=limits)
            for planner in (BaselineOrderPlanner(), CostAwarePlanner())
        ]

    def test_both_planners_produce_a_candidate_and_dependency_order_is_respected(self) -> None:
        goal = _goal()
        tasks = [
            _task("prepare", effects=("tree.clean=True",)),
            _task("publish", depends_on=("prepare",), preconditions=("tree.clean=True",)),
        ]
        outcomes = self._outcomes(goal, tasks)
        for outcome in outcomes:
            self.assertTrue(outcome.candidates, outcome.limitations)
            for candidate in outcome.candidates:
                order = [step.id for step in candidate.plan.steps]
                self.assertLess(order.index("prepare"), order.index("publish"), order)

    def test_ranking_names_one_feasible_candidate_and_never_an_infeasible_one(self) -> None:
        goal = _goal()
        tasks = [_task("a"), _task("b")]
        outcomes = self._outcomes(goal, tasks)
        candidates = [item for outcome in outcomes for item in outcome.candidates]
        ranker = PlanRanker()
        comparison = ranker.compare(candidates)
        selected, score = ranker.select(candidates)
        self.assertIsNotNone(selected)
        self.assertIsNotNone(score)
        self.assertTrue(score.feasible)
        self.assertEqual(comparison["selected"], selected.candidate_id)
        self.assertTrue(all("score" in row for row in comparison["rows"]))
        self.assertIn("not a probability", comparison["label"])

    def test_planning_the_same_input_twice_selects_the_same_plan(self) -> None:
        goal = _goal()
        tasks = [_task("a"), _task("b", depends_on=("a",))]
        first = self._outcomes(goal, tasks)
        second = self._outcomes(goal, tasks)
        # The candidate ID carries a fresh plan id, so it is NOT the thing to
        # compare; the planner that won and the order it produced are.
        self.assertEqual(
            [c.planner for o in first for c in o.candidates],
            [c.planner for o in second for c in o.candidates],
        )
        self.assertEqual(
            [[s.id for s in c.plan.steps] for o in first for c in o.candidates],
            [[s.id for s in c.plan.steps] for o in second for c in o.candidates],
        )
        ranker = PlanRanker()
        first_pick, _ = ranker.select([c for o in first for c in o.candidates])
        second_pick, _ = ranker.select([c for o in second for c in o.candidates])
        self.assertEqual(first_pick.planner, second_pick.planner)
        self.assertEqual(
            [s.id for s in first_pick.plan.steps],
            [s.id for s in second_pick.plan.steps],
            "the documented comparison must pick the same plan for the same goal",
        )

    def test_a_beam_that_hits_its_ceiling_says_so(self) -> None:
        goal = _goal()
        tasks = [_task(f"t{i}") for i in range(6)]
        tight = PlanningBudget(max_expansions=1, max_candidates=1)
        outcome = CostAwarePlanner().plan(
            goal,
            decompose(goal, tasks),
            StateGroundingService(world=None).ground([]),
            budget=tight,
        )
        self.assertEqual(outcome.expansions, 1)
        self.assertTrue(outcome.truncated, "a truncated search must say it was truncated")
        self.assertTrue(outcome.limitations)

    def test_work_already_done_is_skipped_rather_than_repeated(self) -> None:
        goal = _goal()
        tasks = [_task("a", effects=("release.ready=True",))]
        world = {"release.ready": True}
        grounding = StateGroundingService(world=world).ground(["release.ready"])
        outcome = BaselineOrderPlanner().plan(goal, decompose(goal, tasks), grounding)
        candidate = outcome.candidates[0]
        self.assertEqual(candidate.plan.steps, ())
        self.assertTrue(candidate.skipped or candidate.notes)


class OutcomeEstimationTests(unittest.TestCase):
    """Every estimate names its basis; a declaration is never a prediction."""

    def test_a_declared_effect_is_reported_as_a_declaration(self) -> None:
        estimator = OutcomeEstimator()
        estimate = estimator.estimate_task(_task("a", effects=("release.ready=True", "count=3")))
        self.assertIs(estimate.basis, EstimateBasis.RULE_BASED)
        self.assertEqual(estimate.expected, {"release.ready": True, "count": 3})
        self.assertFalse(estimate.predicted)
        self.assertIsNone(estimate.confidence, "nothing measured a confidence")
        self.assertTrue(estimate.limitations)

    def test_nothing_at_all_reports_unavailable_rather_than_a_guess(self) -> None:
        estimate = OutcomeEstimator().estimate_task(_task("a"))
        self.assertIs(estimate.basis, EstimateBasis.UNAVAILABLE)
        self.assertEqual(estimate.expected, {})
        self.assertIn("no provider could estimate", estimate.note)

    def test_a_scoped_learned_rule_is_used_and_names_itself(self) -> None:
        book = _RuleBook((_Rule("rule:1", action="do_a", outcome={"flag": "up"}),))
        estimate = OutcomeEstimator(learned_rules=book).estimate_task(_task("a"), scope="sim")
        self.assertIs(estimate.basis, EstimateBasis.LEARNED_RULE)
        self.assertEqual(estimate.expected, {"flag": "up"})
        self.assertEqual(estimate.source, "rule:1")
        self.assertTrue(estimate.predicted)

    def test_a_rule_from_another_scope_is_not_borrowed(self) -> None:
        book = _RuleBook((_Rule("rule:1", action="do_a", outcome={"flag": "up"}, scope="other"),))
        estimate = OutcomeEstimator(learned_rules=book).estimate_task(_task("a"), scope="sim")
        self.assertIsNot(estimate.basis, EstimateBasis.LEARNED_RULE)

    def test_a_retired_rule_is_never_a_basis(self) -> None:
        book = _RuleBook(
            (
                _Rule(
                    "rule:1",
                    action="do_a",
                    outcome={"flag": "up"},
                    status="retired",
                ),
            )
        )
        estimate = OutcomeEstimator(learned_rules=book).estimate_task(_task("a"), scope="sim")
        self.assertIsNot(estimate.basis, EstimateBasis.LEARNED_RULE)

    def test_the_provider_report_is_honest_about_what_is_wired(self) -> None:
        bare = OutcomeEstimator().providers
        self.assertFalse(bare["world_model"])
        self.assertFalse(bare["learned_rules"])
        self.assertTrue(bare["declared_effects"])
        self.assertEqual(OutcomeEstimator().estimate_plan([_task("a")]).unavailable_steps, ("a",))

    def test_a_world_predictor_that_raises_is_unavailable_not_fatal(self) -> None:
        def boom(action: str) -> dict[str, Any]:
            raise RuntimeError("no model here")

        estimate = OutcomeEstimator(world_predictor=boom).estimate_task(_task("a"))
        self.assertIs(estimate.basis, EstimateBasis.UNAVAILABLE)


class GroundingAndRevalidationTests(unittest.TestCase):
    """Stale is not false, unknown is not yes, and text compares to values."""

    def test_an_observed_fact_makes_a_precondition_hold(self) -> None:
        world = {"tree.clean": True}
        grounding = StateGroundingService(world=world).ground(["tree.clean"])
        report = PlanMonitor().revalidate(_task("a", preconditions=("tree.clean=True",)), grounding)
        self.assertEqual(report.verdict, "proceed", report.reason)
        self.assertEqual(report.checks[0].observed_value, True)

    def test_a_contradicted_precondition_refuses(self) -> None:
        world = {"tree.clean": False}
        grounding = StateGroundingService(world=world).ground(["tree.clean"])
        report = PlanMonitor().revalidate(_task("a", preconditions=("tree.clean=True",)), grounding)
        self.assertEqual(report.verdict, "refuse")
        self.assertFalse(report.proceed)
        self.assertIn("contradicts", report.reason)

    def test_an_unreadable_state_asks_for_an_observation(self) -> None:
        report = PlanMonitor().revalidate(_task("a", preconditions=("x=1",)), None)
        self.assertEqual(report.verdict, "observe")
        self.assertIsNone(report.checks[0].holds)

    def test_a_stale_fact_cannot_satisfy_a_precondition(self) -> None:
        service = StateGroundingService(world={"x": 1}, stale_after_seconds=0.0001)
        grounding = service.ground(["x"])
        # Force the age beyond the ceiling without waiting on a real clock.
        object.__setattr__(grounding.facts[0], "stale", True)
        report = PlanMonitor().revalidate(_task("a", preconditions=("x=1",)), grounding)
        self.assertEqual(report.verdict, "observe")
        self.assertIn("older than", report.reason)

    def test_a_precondition_written_as_text_matches_the_typed_value(self) -> None:
        self.assertIs(coerce_value("True"), True)
        self.assertIs(coerce_value("false"), False)
        self.assertEqual(coerce_value("3"), 3)
        self.assertEqual(coerce_value("2.5"), 2.5)
        self.assertEqual(coerce_value("up"), "up")
        world = {"count": 3, "name": "worker", "flag": True}
        grounding = StateGroundingService(world=world).ground(["count", "name", "flag"])
        report = PlanMonitor().revalidate(
            _task("a", preconditions=("count=3", "name=worker", "flag=True")), grounding
        )
        self.assertEqual(report.verdict, "proceed", report.reason)

    def test_a_fact_value_that_really_is_a_string_still_matches(self) -> None:
        grounding = StateGroundingService(world={"version": "3"}).ground(["version"])
        self.assertIs(grounding.fact("version").holds("3"), True)

    def test_a_conflicting_fact_cannot_carry_a_decision(self) -> None:
        service = StateGroundingService(world={"x": 1})
        grounding = service.ground(["x"], asserted={"x": 2})
        fact = grounding.fact("x")
        self.assertTrue(fact.conflicting)
        self.assertFalse(fact.usable)
        report = PlanMonitor().revalidate(_task("a", preconditions=("x=1",)), grounding)
        self.assertEqual(report.verdict, "observe")


class CompletionTests(unittest.TestCase):
    """Completion is a verified condition, never the end of the steps."""

    def test_a_condition_nothing_observed_is_unknown(self) -> None:
        verdict = PlanMonitor().completion(_goal(), {})
        self.assertEqual(verdict.status, "unknown")
        self.assertTrue(verdict.cannot_tell)
        self.assertFalse(verdict.verified)

    def test_a_condition_that_was_observed_and_held_is_completed(self) -> None:
        verdict = PlanMonitor().completion(_goal(), {"release.ready": True})
        self.assertEqual(verdict.status, "completed")
        self.assertTrue(verdict.verified)
        self.assertEqual(verdict.conditions[0]["answer"], True)

    def test_a_condition_that_was_observed_and_failed_is_failed(self) -> None:
        verdict = PlanMonitor().completion(_goal(), {"release.ready": False})
        self.assertEqual(verdict.status, "failed")

    def test_a_goal_with_no_conditions_can_never_complete(self) -> None:
        verdict = PlanMonitor().completion(Goal(request="do something"), {})
        self.assertEqual(verdict.status, "unknown")
        self.assertIn("states no success condition", verdict.reason)


class ActionPolicyTests(unittest.TestCase):
    """The policy asks the permission layer both ways and reports its source."""

    def test_a_read_only_step_is_allowed_without_a_permission_layer(self) -> None:
        goal = _goal()
        plan = _plan_of(goal, [_task("a")])
        report = ActionPolicy().evaluate_plan(plan, goal=goal)
        self.assertEqual(report.verdicts[0].kind, "allow")
        self.assertFalse(report.has_denials)
        self.assertFalse(ActionPolicy().wired)

    def test_a_destructive_step_needs_approval_and_is_denied_when_approved_wrongly(self) -> None:
        goal = _goal()
        plan = _plan_of(goal, [_task("a", effect=StepEffect.DESTRUCTIVE)])
        waiting = ActionPolicy().evaluate_plan(plan, goal=goal)
        self.assertEqual(waiting.verdicts[0].kind, "needs_approval")
        self.assertEqual(waiting.approval_steps, ("a",))
        approved = ActionPolicy().evaluate_plan(plan, goal=goal, approved=("a",))
        self.assertEqual(approved.verdicts[0].kind, "allow")

    def test_a_goal_exclusion_denies_regardless_of_effect(self) -> None:
        goal = _goal(exclusions=("local write",))
        tasks = [_task("a", effect=StepEffect.LOCAL_WRITE)]
        # Asked of the TASKS, not of a plan: a plan builder refuses to include a
        # step the goal excluded, so there would be no verdict to read.
        report = ActionPolicy().evaluate_tasks(tasks, goal=goal)
        self.assertEqual(report.verdicts[0].kind, "denied")
        self.assertEqual(report.verdicts[0].source, "goal_exclusion")

    def test_a_goal_approval_condition_makes_a_step_ask_first(self) -> None:
        goal = _goal(approval_conditions=("do_a",))
        plan = _plan_of(goal, [_task("a")])
        report = ActionPolicy().evaluate_plan(plan, goal=goal)
        self.assertEqual(report.verdicts[0].kind, "needs_approval")


def _plan_of(goal: Goal, tasks: list[Task]) -> Plan:
    """The baseline planner's plan for these tasks — a real Plan, not a stub."""
    outcome = BaselineOrderPlanner().plan(
        goal, decompose(goal, tasks), StateGroundingService(world=None).ground([])
    )
    return outcome.candidates[0].plan


class ReplanningTests(unittest.TestCase):
    """A deviation is not automatically a replan, and the ceiling is real."""

    def test_a_verified_run_needs_no_replan(self) -> None:
        decision = PlanMonitor(max_replans=2).reassess(workflow=None, goal=_goal())
        self.assertFalse(decision.should_replan)
        self.assertIn("nothing executed", decision.reason)

    def test_the_ceiling_stops_the_decision_and_says_it_is_exhausted(self) -> None:
        monitor = PlanMonitor(max_replans=1)
        monitor.record_replan()
        decision = monitor.reassess(workflow=None, goal=_goal())
        self.assertFalse(decision.should_replan)
        self.assertTrue(decision.exhausted)
        self.assertEqual(decision.remaining, 0)

    def test_the_goal_ceiling_can_be_lower_than_the_monitors(self) -> None:
        monitor = PlanMonitor(max_replans=5)
        decision = monitor.reassess(workflow=None, goal=_goal(budget=GoalBudget(max_replans=0)))
        self.assertTrue(decision.exhausted)
        self.assertEqual(decision.max_replans, 0)


class PersistenceTests(unittest.TestCase):
    """One key per goal, a corrupt record skipped, and a dead store survived."""

    def test_goals_survive_a_restore_and_a_corrupt_record_is_skipped(self) -> None:
        store = _MemoryStore()
        repository = JsonGoalRepository(store)
        manager = GoalManager(repository=repository)
        first = manager.create("first request")
        second = manager.create("second request")
        manager.decompose(first.goal_id, [_task("a")])
        self.assertTrue(manager.save(first.goal_id))
        self.assertTrue(manager.save(second.goal_id))

        # A record nothing can read is skipped, not fatal.
        store.write(repository.key(second.goal_id), {"goal": "not a mapping"})
        restored = GoalManager(repository=JsonGoalRepository(store))
        report = restored.restore()
        self.assertEqual(report["restored"], 1)
        self.assertGreaterEqual(report["skipped"], 1)
        self.assertIsNotNone(restored.get(first.goal_id))

    def test_a_restored_goal_is_unplanned_until_it_is_planned_again(self) -> None:
        store = _MemoryStore()
        repository = JsonGoalRepository(store)
        manager = GoalManager(repository=repository)
        goal = manager.create(
            "plan me",
            success_conditions=(SuccessCondition(key="release.ready", expected=True),),
        )
        manager.decompose(goal.goal_id, [_task("a")])
        manager.plan(goal.goal_id)
        self.assertIsNotNone(manager.selected(goal.goal_id))

        restored = GoalManager(repository=JsonGoalRepository(store))
        restored.restore()
        self.assertIsNone(
            restored.selected(goal.goal_id),
            "a plan is a decision this process did not make, so it is not restored",
        )

    def test_the_restore_report_is_bounded_and_carries_no_request_text(self) -> None:
        """The boot report travels on the status surface, so it records figures only.

        A goal's request text is the user's own words, not an operational figure:
        the restore report says which goals came back, in what state, whether a
        decomposition came with them and what was CHOSEN last time — and nothing
        else. The recorded choice stays a fact about the record: a plan is a
        decision made against state this process never observed.
        """
        store = _MemoryStore()
        manager = GoalManager(repository=JsonGoalRepository(store))
        goal = manager.create(
            "read the private thing and tell me about it",
            success_conditions=(SuccessCondition(key="release.ready", expected=True),),
        )
        manager.decompose(goal.goal_id, [_task("a")])
        manager.plan(goal.goal_id)
        self.assertTrue(manager.selected(goal.goal_id))
        self.assertTrue(manager.save(goal.goal_id))

        restored = GoalManager(repository=JsonGoalRepository(store))
        restored.restore()
        row = restored.status()["restored"][goal.goal_id]
        self.assertEqual(row["goal_id"], goal.goal_id)
        self.assertTrue(row["decomposition"])
        self.assertEqual(row["conditions"], 1)
        self.assertTrue(
            row["previously_selected"],
            "the record says which candidate was chosen last time",
        )
        self.assertNotIn(
            "private thing",
            json.dumps(row),
            "the status surface carries figures, not the user's request text",
        )
        self.assertIsNone(
            restored.selected(goal.goal_id),
            "reporting what was chosen is not restoring the selection",
        )

    def test_a_store_that_refuses_every_write_never_fails_the_work(self) -> None:
        store = _MemoryStore()
        store.fail_writes = True
        manager = GoalManager(repository=JsonGoalRepository(store))
        goal = manager.create("still fine")
        self.assertIsNotNone(manager.get(goal.goal_id))
        self.assertFalse(manager.save(goal.goal_id))


class CapabilityTableTests(unittest.TestCase):
    """Availability is a property of the machine, read live."""

    def test_the_table_declares_the_phase_and_defers_only_phase_26(self) -> None:
        ids = {row.capability_id for row in CAPABILITY_TABLE}
        self.assertIn("planning.goal_representation", ids)
        self.assertIn("planning.completion_verification", ids)
        rows = {row.capability_id: row for row in CAPABILITY_TABLE}
        # The embodied/game-agent placeholder is GONE: Phase 25 shipped that
        # capability as ``embodied.*`` rows on the same registry, so keeping a
        # "future" row here would have this build claim both "not built" and
        # "available" about one thing.
        self.assertNotIn("planning.game_agent", ids)
        self.assertEqual(rows["planning.generalization"].state.value, "future")

    def test_an_unwired_manager_reports_the_providers_as_unavailable(self) -> None:
        rows = {row.name: row for row in capability_rows(GoalManager())}
        self.assertFalse(rows["planning.world_state"].available)
        self.assertFalse(rows["planning.prediction"].available)
        self.assertTrue(rows["planning.goal_representation"].available)
        self.assertIn("assertion", rows["planning.world_state"].reason)

    def test_a_wired_manager_reports_exactly_what_is_wired(self) -> None:
        rules_only = GoalManager(
            grounding=StateGroundingService(world={"x": 1}),
            estimator=OutcomeEstimator(learned_rules=_RuleBook()),
        )
        rows = {row.name: row for row in capability_rows(rules_only)}
        self.assertTrue(rows["planning.world_state"].available)
        self.assertTrue(rows["planning.learned_knowledge"].available)
        self.assertFalse(
            rows["planning.prediction"].available,
            "a learned rule is not a world-model projection, and the table says so",
        )
        with_model = GoalManager(
            grounding=StateGroundingService(world={"x": 1}),
            estimator=OutcomeEstimator(world_predictor=lambda action: {"x": 2}),
        )
        model_rows = {row.name: row for row in capability_rows(with_model)}
        self.assertTrue(model_rows["planning.prediction"].available)

    def test_registration_declares_every_row_on_the_registry(self) -> None:
        class _Registry:
            def __init__(self) -> None:
                self.rows: list[dict[str, Any]] = []

            def register_action(self, name: str, **kwargs: Any) -> None:
                self.rows.append({"name": name, **kwargs})

        registry = _Registry()
        count = register_planning_capabilities(registry, GoalManager())
        self.assertEqual(count, len(CAPABILITY_TABLE))
        self.assertEqual(len(registry.rows), len(CAPABILITY_TABLE))
        self.assertTrue(all(row["category"] == "planning" for row in registry.rows))


class ConfigTests(unittest.TestCase):
    """The section is clamped, never accepted raw."""

    def test_bounds_are_clamped_to_their_ceilings(self) -> None:
        settings = GoalPlanningSettings.from_mapping(
            {
                "max_steps": 10**9,
                "max_replans": 10**6,
                "max_seconds": 10**9,
                "max_expansions": 10**9,
                "search_seconds": 10**9,
                "max_depth": 10**9,
                "enabled": False,
            }
        )
        self.assertEqual(settings.max_steps, 256)
        self.assertEqual(settings.max_replans, 16)
        self.assertEqual(settings.max_seconds, 3600.0)
        self.assertEqual(settings.max_expansions, 4096)
        self.assertEqual(settings.search_seconds, 3600.0)
        self.assertEqual(settings.max_depth, 256)
        self.assertFalse(settings.enabled)

    def test_a_usable_seconds_setting_is_read_rather_than_replaced(self) -> None:
        settings = GoalPlanningSettings.from_mapping({"max_seconds": 45.5, "max_steps": 7})
        self.assertEqual(settings.max_seconds, 45.5)
        self.assertEqual(settings.max_steps, 7)

    def test_a_nonsense_seconds_setting_falls_back_to_the_careful_default(self) -> None:
        # A negative staleness ceiling is refused rather than clamped to zero:
        # zero would make every fact permanently stale, i.e. make the build less
        # careful than the default it replaced.
        settings = GoalPlanningSettings.from_mapping(
            {"stale_after_seconds": -5, "max_seconds": 0, "search_seconds": -1}
        )
        defaults = GoalPlanningSettings()
        self.assertEqual(settings.stale_after_seconds, defaults.stale_after_seconds)
        self.assertEqual(settings.max_seconds, defaults.max_seconds)
        self.assertEqual(settings.search_seconds, defaults.search_seconds)

    def test_the_section_is_read_from_a_config_mapping(self) -> None:
        config = NovaControlConfig.from_mapping({"goal_planning": {"max_steps": 5}})
        self.assertEqual(config.goal_planning.max_steps, 5)
        self.assertTrue(config.goal_planning.enabled)


# --------------------------------------------------------------------------- #
# Workflows through the REAL application
# --------------------------------------------------------------------------- #


def _prepare_body(**overrides: Any) -> dict[str, Any]:
    """A goal whose single step the APPLICATION can really run and verify."""
    body: dict[str, Any] = {
        "request": "read the system status",
        "success_conditions": [
            {
                "key": "workspace.prepared",
                "expected": True,
                "description": "the workspace is ready",
            }
        ],
        "tasks": [
            {
                "id": "read",
                "title": "Read the system status",
                "action": "read_metric",
                "effect": "read_only",
                "parameters": {"metric": "system_status"},
                "effects": ["report.read=True"],
                "expected_outputs": ["report.read=True"],
                "verification": "metric_read",
                "estimated_seconds": 0.01,
            }
        ],
    }
    body.update(overrides)
    return body


@unittest.skipIf(TestClient is None, "fastapi.testclient.TestClient requires httpx")
class _PlanningWorkflowCase(unittest.TestCase):
    """One real application per test, over a temp data dir, with no device runners."""

    def setUp(self) -> None:
        if TestClient is None:
            self.skipTest("httpx not installed")
        self._tmp = TemporaryDirectory()
        self._nova: Any = None
        self._patchers = [
            mock.patch(
                "novacontrol.api.app.NovaControlApplication",
                side_effect=self._isolated_application,
            ),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
        ]
        for patcher in self._patchers:
            patcher.start()
        self._client = TestClient(create_app())
        self._client.__enter__()

    def tearDown(self) -> None:
        self._client.__exit__(None, None, None)
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()

    def _isolated_application(self, **kwargs: object) -> Any:
        kwargs["data_dir"] = self._tmp.name
        app = NovaControlApplication(**kwargs)  # type: ignore[arg-type]
        self._nova = app
        return app

    # ── helpers ──────────────────────────────────────────────────────────
    def _plan(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/planning/goals", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _run(self, goal_id: str, **body: Any) -> dict[str, Any]:
        response = self._client.post(f"/planning/goals/{goal_id}/run", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _view(self, goal_id: str) -> dict[str, Any]:
        response = self._client.get(f"/planning/goals/{goal_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _observe(self, **facts: Any) -> dict[str, Any]:
        """Record a REAL observation, so grounding has something observed."""
        response = self._client.post(
            "/world/observe",
            json={
                "source": "external",
                "source_id": "planning-workflow-test",
                "complete": True,
                "entities": [
                    {
                        "label": "workspace",
                        "attributes": [
                            {"name": name, "value": value, "source": "test"}
                            for name, value in facts.items()
                        ],
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _goal_events(self) -> list[str]:
        """Every planning event the REAL bus recorded, in order."""
        deadline = time.monotonic() + 2.0
        recent = self._nova.event_bus.recent(limit=300)
        while time.monotonic() < deadline:
            time.sleep(0.02)
            current = self._nova.event_bus.recent(limit=300)
            if len(current) == len(recent):
                break
            recent = current
        return [str(event.type) for event in recent if str(event.type).startswith("goal.")]


class WorkflowAGoalRepresentationTests(_PlanningWorkflowCase):
    """A: what the request did not say is reported, not invented."""

    def test_a_goal_with_no_success_condition_is_refused_with_the_reason(self) -> None:
        body = _prepare_body()
        body.pop("success_conditions")
        report = self._plan(body)
        self.assertFalse(report["planned"])
        kinds = {issue["kind"] for issue in report["decomposition"]["issues"]}
        self.assertIn("unmeasurable", kinds)
        self.assertIn("success condition", report["refusal"])

    def test_the_stated_fields_are_kept_and_the_unstated_ones_are_empty(self) -> None:
        report = self._plan(
            _prepare_body(
                constraints=["no network"],
                exclusions=[],
                initial_state={"disk.free": 10},
                provenance="workflow-a",
                correlation_id="corr-a",
            )
        )
        goal = self._view(report["goal_id"])["goal"]
        self.assertEqual(goal["constraints"], ["no network"])
        self.assertEqual(goal["exclusions"], [])
        self.assertEqual(goal["initial_state"], {"disk.free": 10})
        self.assertEqual(goal["provenance"], "workflow-a")
        self.assertEqual(goal["correlation_id"], "corr-a")
        self.assertEqual(goal["required_permissions"], [])
        self.assertEqual(goal["approval_conditions"], [])

    def test_a_blank_request_and_unknown_goal_are_refused_with_the_field(self) -> None:
        blank = self._client.post("/planning/goals", json={"tasks": []})
        self.assertEqual(blank.status_code, 422, blank.text)
        self.assertIn("request", blank.json()["detail"])
        missing = self._client.get("/planning/goals/goal_nope")
        self.assertEqual(missing.status_code, 404, missing.text)


class WorkflowBOrderingTests(_PlanningWorkflowCase):
    """B: dependency order is respected and a cycle is refused."""

    def test_a_valid_chain_is_ordered_by_dependency(self) -> None:
        report = self._plan(
            {
                "request": "ship the release",
                "success_conditions": [{"key": "release.shipped", "expected": True}],
                "tasks": [
                    {
                        "id": "publish",
                        "title": "Publish",
                        "action": "read_metric",
                        "parameters": {"metric": "system_status"},
                        "effects": ["release.shipped=True"],
                        "verification": "metric_read",
                        "depends_on": ["prepare"],
                    },
                    {
                        "id": "prepare",
                        "title": "Prepare",
                        "action": "read_metric",
                        "parameters": {"metric": "system_status"},
                        "effects": ["tree.clean=True"],
                        "verification": "metric_read",
                    },
                ],
            }
        )
        self.assertTrue(report["planned"], report["refusal"])
        order = [step["id"] for step in report["selected"]["plan"]["steps"]]
        self.assertLess(order.index("prepare"), order.index("publish"), order)

    def test_a_cycle_is_refused_with_the_cycle_named(self) -> None:
        report = self._plan(
            {
                "request": "do the impossible",
                "success_conditions": [{"key": "x", "expected": True}],
                "tasks": [
                    {"id": "a", "title": "A", "action": "do_a", "depends_on": ["b"]},
                    {"id": "b", "title": "B", "action": "do_b", "depends_on": ["a"]},
                ],
            }
        )
        self.assertFalse(report["planned"])
        kinds = {issue["kind"] for issue in report["decomposition"]["issues"]}
        self.assertIn("cycle", kinds)


class WorkflowCMissingPrerequisiteTests(_PlanningWorkflowCase):
    """C: a prerequisite nothing produces makes the plan infeasible, with names."""

    def test_the_missing_fact_is_named_in_the_refusal(self) -> None:
        report = self._plan(
            {
                "request": "finish the job",
                "success_conditions": [{"key": "job.done", "expected": True}],
                "tasks": [
                    {
                        "id": "finish",
                        "title": "Finish",
                        "action": "read_metric",
                        "parameters": {"metric": "system_status"},
                        "preconditions": ["tree.clean=True"],
                        "verification": "metric_read",
                    }
                ],
            }
        )
        kinds = {issue["kind"] for issue in report["decomposition"]["issues"]}
        self.assertIn("missing_prerequisite", kinds)
        self.assertFalse(report["planned"], "a plan the state cannot support is not planned")

    def test_a_grounded_prerequisite_makes_the_same_goal_plannable(self) -> None:
        self._observe(**{"tree.clean": True})
        report = self._plan(
            {
                "request": "finish the job",
                "success_conditions": [{"key": "workspace.prepared", "expected": True}],
                "tasks": [
                    {
                        "id": "finish",
                        "title": "Finish",
                        "action": "read_metric",
                        "parameters": {"metric": "system_status"},
                        "preconditions": ["tree.clean=True"],
                        "effects": ["report.read=True"],
                        "verification": "metric_read",
                    }
                ],
            }
        )
        self.assertTrue(report["planned"], report["refusal"])


class WorkflowDComparisonTests(_PlanningWorkflowCase):
    """D: candidates are compared and one is named, deterministically."""

    def test_two_planners_are_compared_and_the_winner_is_named(self) -> None:
        report = self._plan(_prepare_body())
        self.assertTrue(report["planned"], report["refusal"])
        planners = {item["planner"] for item in report["planners"]}
        self.assertGreaterEqual(len(planners), 1)
        self.assertTrue(all("candidates" in item for item in report["planners"]))
        self.assertEqual({candidate["planner"] for candidate in report["candidates"]}, planners)
        comparison = report["comparison"]
        self.assertEqual(comparison["selected"], report["selected"]["candidate_id"])
        self.assertTrue(comparison["rows"])
        self.assertTrue(all("score" in row for row in comparison["rows"]))
        # Every candidate is compared, exactly once, and the winner is first.
        self.assertEqual(
            sorted(row["candidate_id"] for row in comparison["rows"]),
            sorted(candidate["candidate_id"] for candidate in report["candidates"]),
        )
        self.assertTrue(comparison["rows"][0]["selected"], "the best plan leads the table")
        self.assertEqual(
            sum(1 for row in comparison["rows"] if row["selected"]), 1, "exactly one pick"
        )

    def test_the_same_goal_planned_twice_compares_the_same_way(self) -> None:
        first = self._plan(_prepare_body(request="identical request"))
        second = self._plan(_prepare_body(request="identical request"))
        self.assertEqual(first["selected"]["planner"], second["selected"]["planner"])
        self.assertEqual(
            [step["id"] for step in first["selected"]["plan"]["steps"]],
            [step["id"] for step in second["selected"]["plan"]["steps"]],
        )


class WorkflowEGroundingTests(_PlanningWorkflowCase):
    """E: a plan is grounded in observed state, and says which kind of belief."""

    def test_an_observed_fact_is_reported_as_observed_and_usable(self) -> None:
        self._observe(**{"tree.clean": True, "workspace.prepared": True})
        report = self._plan(
            _prepare_body(
                tasks=[
                    {
                        "id": "read",
                        "title": "Read the system status",
                        "action": "read_metric",
                        "parameters": {"metric": "system_status"},
                        "preconditions": ["tree.clean=True"],
                        "effects": ["report.read=True"],
                        "verification": "metric_read",
                    }
                ]
            )
        )
        facts = {fact["key"]: fact for fact in report["grounding"]["facts"]}
        self.assertTrue(facts["tree.clean"]["usable"])
        self.assertEqual(facts["tree.clean"]["basis"], "observed")
        self.assertTrue(facts["tree.clean"]["evidence"], "a fact keeps its evidence")
        self.assertFalse(facts["tree.clean"]["stale"])

    def test_what_the_caller_asserted_is_never_a_usable_observation(self) -> None:
        report = self._plan(_prepare_body(initial_state={"disk.free": 10}))
        facts = {fact["key"]: fact for fact in report["grounding"]["facts"]}
        self.assertFalse(facts["disk.free"]["usable"])
        self.assertEqual(facts["disk.free"]["source"], "caller_assertion")
        self.assertNotEqual(facts["disk.free"]["basis"], "observed")
        # The world model IS wired in this build, and the report says so rather
        # than pretending nothing could be read.
        self.assertTrue(report["grounding"]["world_available"])

    def test_the_grounding_says_when_no_world_model_could_be_read(self) -> None:
        report = self._nova.goals._grounding_service.ground(["disk.free"]).as_dict()
        self.assertTrue(report["world_available"])


class WorkflowFRevalidationTests(_PlanningWorkflowCase):
    """F: state that moved under the plan stops the step instead of running it."""

    def _prepare_body(self) -> dict[str, Any]:
        return {
            "request": "prepare the workspace",
            "success_conditions": [{"key": "workspace.prepared", "expected": True}],
            "tasks": [
                {
                    "id": "prepare",
                    "title": "Prepare the workspace",
                    "action": "read_metric",
                    "parameters": {"metric": "system_status"},
                    "preconditions": ["tree.clean=True"],
                    "effects": ["report.read=True"],
                    "verification": "metric_read",
                }
            ],
        }

    def test_state_that_moved_under_the_plan_stops_the_step(self) -> None:
        # The plan is built while the world says the tree is clean...
        self._observe(**{"tree.clean": True})
        report = self._plan(self._prepare_body())
        self.assertTrue(report["planned"], report["refusal"])
        # ...and a second source reports the opposite before the step would run.
        # Phase 22 keeps BOTH values rather than picking a side, so the honest
        # run-time answer is "this cannot be confirmed" — and the step is not run.
        self._observe(**{"tree.clean": False})
        run = self._run(report["goal_id"])
        self.assertEqual(run["status"], "needs_observation", run["reason"])
        self.assertFalse(run["executed"], "nothing may run on state that cannot be confirmed")
        self.assertEqual([item["verdict"] for item in run["revalidations"]], ["observe"])
        self.assertIn("cannot confirm", run["reason"])
        steps = (run["workflow"] or {}).get("state", {}).get("steps", {})
        self.assertEqual(steps["prepare"]["status"], "denied")
        self.assertNotIn(
            "completed",
            {outcome["status"] for outcome in steps.values()},
            "a refused step is not a completed one",
        )

    def test_state_nothing_ever_reported_asks_for_an_observation(self) -> None:
        report = self._plan(self._prepare_body())
        # Nothing observed the tree, so planning already refuses to call this
        # plannable: the state cannot support it.
        self.assertFalse(report["planned"])
        run = self._run(report["goal_id"])
        self.assertEqual(run["status"], "blocked", run["reason"])
        self.assertFalse(run["executed"])

    def test_a_fact_an_earlier_step_declared_helps_the_next_step_run(self) -> None:
        """The declared effects are the plan's own contract, so the gate honours them.

        A dependent step whose precondition an EARLIER step declares it leaves
        behind must not be blocked for "the state cannot confirm it" — that would
        make every multi-step plan unrunnable past its first step, since the fact
        exists because the plan said the first step would produce it.
        """
        self._observe(**{"workspace.prepared": True})
        report = self._plan(
            {
                "request": "prepare the workspace",
                "success_conditions": [{"key": "workspace.prepared", "expected": True}],
                "tasks": [
                    {
                        "id": "check",
                        "title": "Check the tree",
                        "action": "read_metric",
                        "parameters": {"metric": "system_status"},
                        "effects": ["tree.clean=True"],
                        "verification": "metric_read",
                    },
                    {
                        "id": "prepare",
                        "title": "Prepare the workspace",
                        "action": "read_metric",
                        "parameters": {"metric": "system_status"},
                        "depends_on": ["check"],
                        "preconditions": ["tree.clean=True"],
                        "effects": ["report.read=True"],
                        "verification": "metric_read",
                    },
                ],
            }
        )
        self.assertTrue(report["planned"], report["refusal"])
        run = self._run(report["goal_id"])
        self.assertEqual(run["status"], "completed", run["reason"])
        self.assertEqual([item["verdict"] for item in run["revalidations"]], ["proceed", "proceed"])
        steps = (run["workflow"] or {}).get("state", {}).get("steps", {})
        self.assertEqual(steps["check"]["status"], "completed")
        self.assertEqual(steps["prepare"]["status"], "completed")
        self.assertTrue(run["verified"])

    def test_the_gate_refuses_a_contradicted_precondition_before_any_attempt(self) -> None:
        """The refuse path, driven through the real gate and the real executor.

        A world mapping that CHANGES between planning and running is the one way
        to reach a true contradiction without Phase 22 keeping both values, so the
        grounding service gets a mutable mapping and the gate is the real gate.
        """
        world: dict[str, Any] = {"tree.clean": True, "workspace.prepared": True}
        manager = GoalManager(
            executor=self._nova.workflow_executor, grounding=StateGroundingService(world=world)
        )
        goal = manager.create(
            "prepare the workspace",
            success_conditions=(SuccessCondition(key="workspace.prepared", expected=True),),
        )
        report = manager.plan(
            goal.goal_id,
            tasks=[
                _task(
                    "prepare",
                    action="read_metric",
                    parameters={"metric": "system_status"},
                    preconditions=("tree.clean=True",),
                    effects=("report.read=True",),
                )
            ],
        )
        self.assertTrue(report.planned, report.refusal)
        world["tree.clean"] = False
        run = asyncio.run(manager.execute(goal.goal_id))
        self.assertEqual(run.status, "blocked", run.reason)
        self.assertFalse(run.executed)
        self.assertEqual([item.verdict for item in run.revalidations], ["refuse"])
        self.assertIn("contradicts", run.reason)


class WorkflowGCompletionTests(_PlanningWorkflowCase):
    """G: completion comes from a verified condition, never from the steps ending."""

    def test_a_goal_completes_only_when_the_world_observes_the_condition(self) -> None:
        self._observe(**{"workspace.prepared": True})
        report = self._plan(_prepare_body())
        self.assertTrue(report["planned"], report["refusal"])
        run = self._run(report["goal_id"])
        self.assertEqual(run["status"], "completed", run["reason"])
        self.assertTrue(run["executed"], "the step really ran")
        self.assertTrue(run["verified"], "the condition was really observed")
        self.assertEqual(run["unverified_steps"], [])
        self.assertEqual(run["completion"]["evidence"], ["workspace.prepared"])
        self.assertTrue(run["completion"]["conditions"][0]["answer"])
        # The run is readable afterwards, with its revalidations intact.
        stored = self._view(report["goal_id"])["run"]
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["revalidations"][0]["verdict"], "proceed")

    def test_a_condition_nothing_observed_never_reports_completion(self) -> None:
        report = self._plan(_prepare_body())
        run = self._run(report["goal_id"])
        self.assertEqual(run["status"], "needs_observation", run["reason"])
        self.assertFalse(run["verified"])
        self.assertTrue(run["completion"]["cannot_tell"])
        self.assertIn("no observation settled", run["completion"]["reason"])

    def test_a_condition_observed_false_is_a_failure_not_a_completion(self) -> None:
        self._observe(**{"workspace.prepared": False})
        report = self._plan(_prepare_body())
        run = self._run(report["goal_id"])
        self.assertEqual(run["status"], "failed", run["reason"])
        self.assertFalse(run["verified"])
        self.assertEqual(run["completion"]["status"], "failed")

    def test_work_already_done_is_skipped_and_never_claimed_as_executed(self) -> None:
        # The step's own effect is already observed, so the candidate has no steps.
        self._observe(**{"report.read": True})
        report = self._plan(_prepare_body())
        self.assertTrue(report["planned"], report["refusal"])
        self.assertEqual(report["selected"]["plan"]["steps"], [])
        run = self._run(report["goal_id"])
        self.assertNotEqual(run["status"], "denied", "no step was refused; there were none")
        self.assertFalse(run["executed"], "nothing ran, and the report says so")
        self.assertIn("needed no steps", run["reason"])


class WorkflowHApprovalTests(_PlanningWorkflowCase):
    """H: a consequential step waits for a person, and one approval is enough."""

    def _destructive_body(self) -> dict[str, Any]:
        return {
            "request": "clear the release scratch space",
            "success_conditions": [{"key": "scratch.cleared", "expected": True}],
            "tasks": [
                {
                    "id": "clear",
                    "title": "Clear the scratch space",
                    "action": "read_metric",
                    "effect": "destructive",
                    "parameters": {"metric": "system_status"},
                    "effects": ["report.read=True"],
                    "verification": "metric_read",
                }
            ],
        }

    def test_planning_reports_the_step_as_needing_approval(self) -> None:
        report = self._plan(self._destructive_body())
        self.assertTrue(report["planned"], report["refusal"])
        self.assertEqual(report["policy"]["needs_approval"], ["clear"])
        self.assertEqual(report["policy"]["denied"], [])

    def test_running_without_approval_is_denied_and_runs_nothing(self) -> None:
        report = self._plan(self._destructive_body())
        run = self._run(report["goal_id"])
        self.assertEqual(run["status"], "denied", run["reason"])
        self.assertFalse(run["executed"])
        self.assertIn("clear", run["reason"])

    def test_the_same_goal_runs_once_the_step_is_approved(self) -> None:
        self._observe(**{"scratch.cleared": True})
        report = self._plan(self._destructive_body())
        run = self._run(report["goal_id"], approved=["clear"])
        self.assertTrue(run["executed"], run["reason"])
        self.assertEqual(run["status"], "completed", run["reason"])
        self.assertEqual(self._nova.approved_plan_steps, set(), "the approval is not kept")


class WorkflowIReplanningTests(_PlanningWorkflowCase):
    """I: replanning happens, is counted, and stops at the goal's own ceiling."""

    def test_a_replan_is_counted_and_the_ceiling_refuses_the_next_one(self) -> None:
        report = self._plan(_prepare_body(max_replans=1))
        goal_id = report["goal_id"]
        run = self._run(goal_id)
        self.assertEqual(run["status"], "needs_observation", run["reason"])

        decision = self._client.post(f"/planning/goals/{goal_id}/reassess").json()
        self.assertFalse(decision["exhausted"])
        self.assertEqual(decision["remaining"], 1)

        # The first replan (planning the SAME goal again) is counted...
        replan = self._nova.goals.plan(goal_id)
        self.assertTrue(replan.planned, replan.refusal)
        self.assertEqual(self._nova.goals._monitor(goal_id).replans_used, 1)
        # ...and the second is refused by the ceiling itself, not attempted.
        ceiling = self._nova.goals.plan(goal_id)
        self.assertFalse(ceiling.planned)
        self.assertIn("ceiling is reached", ceiling.refusal)

    def test_the_ceiling_is_enforced_by_planning_the_same_goal_again(self) -> None:
        report = self._plan(_prepare_body(max_replans=1))
        goal_id = report["goal_id"]
        self._run(goal_id)
        # Replanning goes through the manager for the SAME goal id.
        first = self._nova.goals.plan(goal_id, tasks=[_task("read", action="read_metric")])
        self.assertTrue(first.planned or first.refusal == "", first.refusal)
        second = self._nova.goals.plan(goal_id, tasks=[_task("read", action="read_metric")])
        self.assertFalse(second.planned)
        self.assertIn("ceiling is reached", second.refusal)

    def test_reassess_on_an_unknown_goal_is_a_404(self) -> None:
        response = self._client.post("/planning/goals/goal_nope/reassess")
        self.assertEqual(response.status_code, 404, response.text)


class WorkflowJPersistenceTests(_PlanningWorkflowCase):
    """J: goals survive a restart; a corrupt record does not stop the boot."""

    def test_a_goal_survives_a_restart_and_restores_unplanned(self) -> None:
        report = self._plan(_prepare_body(provenance="workflow-j"))
        goal_id = report["goal_id"]
        self._run(goal_id)

        self._client.__exit__(None, None, None)
        self._client = TestClient(create_app())
        self._client.__enter__()

        status = self._client.get("/planning/status").json()
        self.assertGreaterEqual(status["restored"]["restored"], 1, status["restored"])
        listed = self._client.get("/planning/goals").json()
        self.assertIn(goal_id, [goal["goal_id"] for goal in listed["goals"]])
        self.assertIn(goal_id, [goal["goal_id"] for goal in listed["goals"]])
        # A restored goal has no SELECTED plan: a plan is a decision this process
        # never made, so restoring one would claim it did.
        view = self._view(goal_id)
        self.assertIsNone(view["selected"])
        self.assertIsNotNone(view["decomposition"], "the decomposition is durable")

    def test_a_corrupt_goal_record_is_skipped_without_failing_the_boot(self) -> None:
        report = self._plan(_prepare_body(provenance="workflow-j-corrupt"))
        corrupt_id = "goal_corrupt_fixture"
        self._nova.state_store.write(
            JsonGoalRepository(self._nova.state_store).key(corrupt_id),
            {"goal": "not a mapping", "decomposition": "nope"},
        )
        index = JsonGoalRepository(self._nova.state_store).index()
        self._nova.state_store.write("goal_index", {"goals": [*index, corrupt_id]})

        self._client.__exit__(None, None, None)
        self._client = TestClient(create_app())
        self._client.__enter__()

        status = self._client.get("/planning/status").json()
        self.assertGreaterEqual(status["restored"]["skipped"], 1, status["restored"])
        listed = self._client.get("/planning/goals").json()
        self.assertIn(report["goal_id"], [goal["goal_id"] for goal in listed["goals"]])


class WorkflowKEstimationHonestyTests(_PlanningWorkflowCase):
    """K: the estimate says what it is, and the status says what is wired."""

    def test_a_declared_effect_is_labelled_a_declaration_not_a_prediction(self) -> None:
        report = self._plan(_prepare_body())
        steps = {row["task_id"]: row for row in report["estimates"]["steps"]}
        self.assertEqual(steps["read"]["basis"], "rule_based")
        self.assertFalse(steps["read"]["predicted"])
        self.assertIsNone(steps["read"]["confidence"])
        self.assertIn("declaration, not a prediction", steps["read"]["note"])

    def test_the_status_reports_the_providers_and_the_capability_table(self) -> None:
        status = self._client.get("/planning/status").json()
        wired = status["wired"]["estimator"]
        self.assertIn("world_model", wired)
        self.assertTrue(wired["declared_effects"])
        rows = {row["name"]: row for row in status["capabilities"]}
        self.assertTrue(rows["planning.goal_representation"]["available"])
        self.assertEqual(rows["planning.generalization"]["state"], "future")
        self.assertFalse(
            rows["planning.generalization"]["available"],
            "the only planning capability still ahead of this build is Phase 26's",
        )
        self.assertNotIn(
            "planning.game_agent",
            rows,
            "Phase 25 ships the embodied agent; a deferred twin of it would contradict "
            "the embodied rows on the same registry",
        )
        self.assertIn("enabled", status)
        self.assertIn("defaults", status)
        # Nothing here may leak a plan body or an observation.
        self.assertNotIn("plan", status)
        self.assertNotIn("observed", status)

    def test_a_learned_rule_is_used_with_its_scope_and_status(self) -> None:
        # A Phase 23 rule for the read action, in this build's rule book.
        book = self._nova.learning.book
        book._rules["rule:workflow-k"] = _Rule(
            "rule:workflow-k",
            action="read_metric",
            outcome={"report.read": True},
            scope="read the system status",
            status="provisional",
        )
        report = self._plan(_prepare_body())
        steps = {row["task_id"]: row for row in report["estimates"]["steps"]}
        self.assertEqual(steps["read"]["basis"], "learned_rule")
        self.assertEqual(steps["read"]["source"], "rule:workflow-k")
        self.assertTrue(steps["read"]["predicted"])
        self.assertIn("learned rule", steps["read"]["note"])


class WorkflowLEventTests(_PlanningWorkflowCase):
    """L: the planning vocabulary really reaches the one event bus."""

    def test_the_whole_lifecycle_is_announced(self) -> None:
        self._observe(**{"workspace.prepared": True})
        report = self._plan(_prepare_body(correlation_id="corr-l"))
        self._run(report["goal_id"])
        self._client.post(f"/planning/goals/{report['goal_id']}/reassess")

        events = self._goal_events()
        for expected in (
            "goal.created",
            "goal.decomposed",
            "goal.plan_selected",
            "goal.executed",
            "goal.reassessed",
        ):
            self.assertIn(expected, events, events)

    def test_a_refusal_and_a_blocked_step_are_events_of_their_own(self) -> None:
        self._plan(
            {
                "request": "do the impossible",
                "success_conditions": [{"key": "x", "expected": True}],
                "tasks": [
                    {"id": "a", "title": "A", "action": "do_a", "depends_on": ["b"]},
                    {"id": "b", "title": "B", "action": "do_b", "depends_on": ["a"]},
                ],
            }
        )
        events = self._goal_events()
        self.assertIn("goal.plan_refused", events, events)

    def test_cancelling_a_goal_is_announced_and_keeps_the_record(self) -> None:
        report = self._plan(_prepare_body())
        response = self._client.post(f"/planning/goals/{report['goal_id']}/cancel")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertIn("goal.cancelled", self._goal_events())


class RunLimitTests(unittest.TestCase):
    """A stated limit is enforced, and an overrun is REPORTED — not hidden.

    The gate in front of the step runner asks the goal's clock before each step,
    and the run asks it once more after the last one. Both halves matter: without
    the second, a plan whose single step overran its stated budget reported
    ``completed`` and nothing said the limit had been broken.
    """

    CHECK = "run_limit_step_reported"

    def _manager(self, calls: list[str], *, sleep: float, world: dict[str, Any]) -> GoalManager:
        def check(spec: VerificationSpec, output: Any) -> tuple[bool, str]:
            return (True, "ok") if isinstance(output, dict) and output.get("ok") else (False, "no")

        def handler(step: PlanStep, context: StepContext) -> dict[str, Any]:
            calls.append(step.id)
            if sleep:
                time.sleep(sleep)
            for effect in (step.parameters or {}).get("effects") or ():
                key, _, value = str(effect).partition("=")
                world[key.strip()] = value.strip() == "True"
            return {"ok": True}

        return GoalManager(
            executor=WorkflowExecutor(
                step_handler=handler,
                verifier=VerificationEngine(callables={self.CHECK: check}),
            ),
            grounding=StateGroundingService(world=world),
            estimator=OutcomeEstimator(),
            ranker=PlanRanker(),
            policy=ActionPolicy(),
            repository=JsonGoalRepository(_MemoryStore()),
            budget=PlanningBudget(),
        )

    def _task(self, task_id: str, **overrides: Any) -> Task:
        return _task(
            task_id,
            verification=VerificationSpec(method=VerificationMethod.CALLABLE, target=self.CHECK),
            **overrides,
        )

    def test_a_run_that_finishes_past_its_seconds_budget_reports_the_breach(self) -> None:
        calls: list[str] = []
        world: dict[str, Any] = {}
        # A generous budget so the FIRST gate check is certainly inside it, and a
        # step that certainly outlasts it: the run therefore ends past its limit
        # having done its work, which is the case that used to report completion
        # with no mention of the budget it broke.
        manager = self._manager(calls, sleep=1.5, world=world)
        goal = manager.create(
            "spend the budget",
            success_conditions=(
                SuccessCondition(key="spend.done", expected=True, description="done"),
            ),
            max_seconds=1.0,
        )
        manager.plan(goal.goal_id, tasks=[self._task("a", effects=("spend.done=True",))])
        run = asyncio.run(manager.execute(goal.goal_id))

        self.assertEqual(calls, ["a"], "the step still ran: the budget did not refuse it")
        self.assertEqual(run.status, "budget_exhausted", run.reason)
        self.assertIn("budget", run.reason)
        # The verdict is NOT discarded: the conditions really were verified, and the
        # breach is recorded beside them rather than by demoting the goal.
        self.assertTrue(run.verified, run.reason)
        self.assertTrue(run.executed)
        self.assertEqual(manager.get(goal.goal_id).status, GoalStatus.COMPLETED)

    def test_a_step_that_would_start_past_the_deadline_is_never_started(self) -> None:
        calls: list[str] = []
        world: dict[str, Any] = {}
        manager = self._manager(calls, sleep=1.5, world=world)
        goal = manager.create(
            "beat the clock",
            success_conditions=(
                SuccessCondition(key="clock.beaten", expected=True, description="done"),
            ),
            deadline_seconds=1.0,
        )
        manager.plan(
            goal.goal_id,
            tasks=[
                self._task("first", effects=("spend.done=True",)),
                self._task("second", effects=("clock.beaten=True",), depends_on=("first",)),
            ],
        )
        run = asyncio.run(manager.execute(goal.goal_id))

        self.assertEqual(run.status, "deadline_exceeded", run.reason)
        self.assertIn("deadline", run.reason)
        self.assertNotIn("second", calls, "a step past the deadline was started anyway")
        self.assertFalse(run.verified, "a goal that did not finish cannot be verified")

    def test_the_stop_kind_tells_a_contradicted_state_from_an_unreadable_one(self) -> None:
        """``blocked`` and ``needs_observation`` are different findings.

        Both arrive from the same gate, so one shared ``state`` stop kind reported
        an unconfirmable precondition as a state refusal — and those two labels are
        exactly what decides whether an operator re-observes or replans.

        The state MOVES between planning and running (the plan is built while the
        fact holds, and the world changes underneath it), which is the only way the
        gate is reached at all: a precondition nothing could satisfy at plan time
        never becomes a candidate.
        """

        def planned_and_run(world: dict[str, Any], then: dict[str, Any]) -> Any:
            calls: list[str] = []
            manager = self._manager(calls, sleep=0.0, world=world)
            goal = manager.create(
                "prepare the workspace",
                success_conditions=(
                    SuccessCondition(key="prepared", expected=True, description="prepared"),
                ),
            )
            report = manager.plan(
                goal.goal_id,
                tasks=[
                    self._task(
                        "a",
                        preconditions=("tree.clean=True",),
                        effects=("prepared=True",),
                    )
                ],
            )
            self.assertTrue(report.planned, report.refusal)
            world.clear()
            world.update(then)
            return asyncio.run(manager.execute(goal.goal_id)), calls

        # The world now says the tree is DIRTY, and the step assumes it is clean.
        run, calls = planned_and_run({"tree.clean": True}, {"tree.clean": False})
        self.assertEqual(run.status, "blocked", run.reason)
        self.assertEqual(calls, [], "a contradicted step must not run")
        self.assertTrue(run.revalidations[0].refused, run.revalidations[0].reason)

        # The same step with the fact simply GONE from every source: the gate cannot
        # confirm it, which is an observation to go and make, not a refusal.
        run, calls = planned_and_run({"tree.clean": True}, {})
        self.assertEqual(run.status, "needs_observation", run.reason)
        self.assertEqual(calls, [], "an unconfirmable step must not run either")
        self.assertTrue(run.revalidations[0].needs_observation, run.revalidations[0].reason)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
