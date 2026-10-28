"""NF-20 — game planning and action selection.

Three layers, each failing for its own reason:

* **Model** — plan/policy validation and the rule registry: a widened bound, a
  step whose action is unsupported, or a plan without the evidence reference it
  claims fails here.
* **Planner** — the real ``GamePlanService`` over the REAL NF-16 fixture state
  and NF-17 interpretation, with a recording submit seam. Refusal codes,
  candidate scoring, approval, cancellation, invalidation, budget and replan
  bounds are all driven through the code the manager uses.
* **Integration** — the real manager and the real HTTP routes: a plan for a
  session with no state is an honest ``no_state`` refusal, a target that changed
  under a planned step is refused BEFORE dispatch, and every route answers 404
  for a session that does not exist.

No test sends real input: the only injector here records edges.
"""

from __future__ import annotations

import asyncio
import dataclasses
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

try:
    from fastapi.testclient import TestClient  # noqa: F401 - requires httpx
except Exception:  # pragma: no cover - httpx is an optional dev dependency
    TestClient = None  # type: ignore[assignment]

from test_novaforge import FakeDesktop, _mission
from test_novaforge_gameplay import RecordingInput, geometry
from test_novaforge_gameplay_api import _GameplayApiFixture
from test_novaforge_objectives import mission_state

from novacontrol.novaforge import NovaForgeManager


def _patch_manager(name: str, value: Any):
    """Patch a NovaForgeManager method for one assertion.

    The manager's instance attributes are read-only, so the class attribute is
    what gets replaced; the fake ignores its arguments and answers with `value`.
    """

    def fake(_self: Any, *_args: Any, **_kwargs: Any) -> Any:
        return value

    return mock.patch.object(NovaForgeManager, name, fake)
from novacontrol.novaforge.control import (
    ControlAction,
    ControlKind,
    ControlPolicy,
    ControlTarget,
)
from novacontrol.novaforge.knowledge import (
    EvidenceSource,
    KnowledgeCategory,
    KnowledgeEvidence,
    KnowledgeRecord,
    KnowledgeStatus,
)
from novacontrol.novaforge.objectives import interpret
from novacontrol.novaforge.state import HudStatus
from novacontrol.novaforge.planner import (
    REPLAN_TRIGGERS,
    ActionPlan,
    CandidateAction,
    GamePlanService,
    PlanPolicy,
    PlanRule,
    PlanRuleRegistry,
    PlanStatus,
    PlannedStep,
    PlanningContext,
    RiskLevel,
    default_plan_rules,
)

SESSION = "s1"
AT = "2026-10-25T10:00:00+00:00"


def control_target(**overrides: Any) -> ControlTarget:
    values: dict[str, Any] = {
        "session_id": SESSION,
        "title": "NC FORGE MISSION",
        "handle": 0x1234,
        "pid": 4242,
    }
    values.update(overrides)
    return ControlTarget(**values)


def make_context(
    *,
    state: Any | None = None,
    objective_state: Any | None = None,
    knowledge: tuple[KnowledgeRecord, ...] = (),
    gate: dict[str, Any] | None = None,
    budget: int | None = 5,
    executed: int = 0,
    age: float | None = 1.0,
    game: str = "",
) -> PlanningContext:
    current = state if state is not None else mission_state()
    interpretation = (
        objective_state if objective_state is not None else interpret(current)
    )
    return PlanningContext(
        session_id=SESSION,
        game=game or current.game,
        target=control_target(),
        state=current,
        objective_state=interpretation,
        knowledge=knowledge,
        gate=gate or {"allow": True, "state": "allowed", "reason": "test gate"},
        budget_remaining=budget,
        state_age_seconds=age,
        control_policy=ControlPolicy(),
        actions_executed=executed,
    )


def tied_rules() -> PlanRuleRegistry:
    """Two rules that differ in NOTHING but their id: their scores are equal."""

    def rule(rule_id: str) -> PlanRule:
        return PlanRule(
            rule_id=rule_id,
            kind=ControlKind.KEY_HOLD,
            key="w",
            duration_ms=100,
            description=f"hold w ({rule_id})",
            expected_effect="a hypothesis, never a verified effect",
            game_tokens=("nc forge mission",),
            objective_tokens=("collect",),
        )

    return PlanRuleRegistry((rule("a-first"), rule("b-second")))


class RecordingSubmit:
    """The submit seam: records actions and replays scripted envelopes."""

    def __init__(
        self,
        responses: list[Any] | None = None,
        *,
        gate: asyncio.Event | None = None,
        entered: asyncio.Event | None = None,
    ) -> None:
        self.actions: list[ControlAction] = []
        self.responses = list(responses or [])
        self.gate = gate
        self.entered = entered

    async def __call__(self, _session_id: str, action: ControlAction) -> dict[str, Any]:
        self.actions.append(action)
        if self.entered is not None:
            self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.responses:
            answer = self.responses.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return {
            "ok": True,
            "status": "completed",
            "code": "",
            "reason": "",
            "result": {
                "status": "completed",
                "code": "",
                "reason": "",
                "dispatched_at": AT,
            },
        }


def make_service(
    context: PlanningContext,
    submit: RecordingSubmit,
    *,
    policy: PlanPolicy | None = None,
    rules: PlanRuleRegistry | None = None,
    contexts: list[PlanningContext] | None = None,
    publish: Any = None,
) -> GamePlanService:
    queue = list(contexts or [context])

    async def reader(_session_id: str, _refresh: bool) -> PlanningContext:
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    return GamePlanService(
        policy=policy or PlanPolicy(),
        context_reader=reader,
        submit=submit,
        rules=rules if rules is not None else default_plan_rules(),
        publish=publish,
    )


class _ExplodingRules(PlanRuleRegistry):
    """A rule set whose matching raises: candidate generation must not escape."""

    def matching(self, game: str, objective: Any) -> list[Any]:
        raise RuntimeError("rule evaluation exploded")


class _SlowRules(PlanRuleRegistry):
    """A rule set that consumes the whole planning deadline before answering."""

    def matching(self, game: str, objective: Any) -> list[Any]:
        import time

        time.sleep(0.05)
        return super().matching(game, objective)


class PlanModelTests(unittest.TestCase):
    def test_policy_bounds_are_validated_not_clamped(self) -> None:
        self.assertEqual(PlanPolicy().errors(), [])
        for policy in (
            PlanPolicy(max_actions_per_plan=0),
            PlanPolicy(max_actions_per_plan=99),
            PlanPolicy(max_candidates=0),
            PlanPolicy(max_candidates=500),
            PlanPolicy(planning_deadline_ms=0),
            PlanPolicy(planning_deadline_ms=60_000),
            PlanPolicy(max_state_age_seconds=0),
            PlanPolicy(max_replans=-1),
            PlanPolicy(max_replans=99),
            PlanPolicy(weight_risk=-1.0),
        ):
            with self.subTest(policy=policy):
                self.assertTrue(policy.errors(), policy)

    def test_a_plan_without_its_evidence_or_objective_is_refused(self) -> None:
        plan = ActionPlan(
            plan_id="p1",
            session_id=SESSION,
            status=PlanStatus.PLANNED,
            created_at=AT,
            steps=[],
        )
        problems = plan.validate(ControlPolicy())
        self.assertTrue(any("at least one step" in item for item in problems), problems)
        self.assertTrue(any("objective reference" in item for item in problems), problems)
        self.assertTrue(any("observation it was based on" in item for item in problems), problems)

    def test_a_step_from_another_session_or_with_an_unsupported_action_is_refused(self) -> None:
        step = PlannedStep(
            index=1,
            action=ControlAction.from_payload(
                {"kind": "key_hold", "key": "f13-magic", "duration_ms": 100},
                session_id="other",
                target=control_target(session_id="other"),
            ),
            candidate_id="c",
            source="test",
            description="bad",
            expected_effect="none",
            risk=RiskLevel.LOW,
            reversible=True,
        )
        plan = ActionPlan(
            plan_id="p1",
            session_id=SESSION,
            status=PlanStatus.PLANNED,
            created_at=AT,
            objective_id="o1",
            state_id="state-1",
            observation_id="obs-1",
            steps=[step],
        )
        problems = plan.validate(ControlPolicy())
        self.assertTrue(any("different session" in item for item in problems), problems)
        self.assertTrue(any("not a key this build knows" in item for item in problems), problems)

    def test_rule_registry_reports_duplicates_instead_of_overwriting(self) -> None:
        rule = PlanRule(
            rule_id="dup",
            kind=ControlKind.KEY_PRESS,
            description="press",
            expected_effect="something",
            key="w",
        )
        registry = PlanRuleRegistry([rule, rule])
        self.assertEqual(len(registry.rows()), 1)
        self.assertTrue(any(row["reason"].startswith("duplicate") for row in registry.rejected))

    def test_no_rule_ships_for_a_commercial_game(self) -> None:
        rules = default_plan_rules()
        self.assertEqual(len(rules.rows()), 1)
        self.assertTrue(all(row["fixture_only"] for row in rules.rows()))


class PlannerSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_fixture_objective_yields_a_bounded_plan_that_is_not_executed(self) -> None:
        submit = RecordingSubmit()
        service = make_service(make_context(), submit)
        plan = await service.plan(SESSION)
        self.assertIs(plan.status, PlanStatus.AWAITING_APPROVAL)
        self.assertEqual(len(plan.steps), 2)  # counter 1/3, so two remaining
        self.assertEqual(plan.approval["status"], "pending")
        self.assertFalse(plan.to_dict()["effect_verified"])
        self.assertIn("no in-game effect", plan.to_dict()["effect_note"].lower())
        self.assertEqual(plan.to_dict()["evidence_refs"]["state_status"], "partial")
        self.assertIn("state_quality", plan.to_dict()["evidence_refs"])
        self.assertTrue(any("never a verified outcome" in line for line in plan.reports))
        self.assertEqual(submit.actions, [])
        self.assertIn("scored highest", plan.rationale)
        self.assertTrue(plan.evidence)
        feasible = [item for item in plan.candidates if item.feasible]
        self.assertEqual(len(feasible), 1)
        self.assertIsNotNone(feasible[0].score)
        self.assertIn("risk", dict(feasible[0].score_parts))

    async def test_the_action_count_never_exceeds_the_policy_or_remaining_budget(self) -> None:
        strict = make_service(
            make_context(budget=4), RecordingSubmit(), policy=PlanPolicy(max_actions_per_plan=1)
        )
        plan = await strict.plan(SESSION)
        self.assertEqual(len(plan.steps), 1)

        budget = make_service(make_context(budget=2), RecordingSubmit())
        plan = await budget.plan(SESSION)
        self.assertLessEqual(len(plan.steps), 2)

    async def test_no_state_is_insufficient_information_and_nothing_is_sent(self) -> None:
        submit = RecordingSubmit()
        context = PlanningContext(
            session_id=SESSION,
            game="NC FORGE MISSION",
            target=control_target(),
            state=None,
            objective_state=interpret(mission_state()),
            knowledge=(),
            gate={"allow": True, "state": "allowed", "reason": "test"},
            budget_remaining=5,
            state_age_seconds=None,
            control_policy=ControlPolicy(),
        )
        service = make_service(context, submit)
        plan = await service.plan(SESSION)
        self.assertIs(plan.status, PlanStatus.INSUFFICIENT_INFORMATION)
        self.assertEqual(plan.code, "no_state")
        self.assertEqual(plan.steps, [])
        self.assertEqual(submit.actions, [])

    async def test_a_stale_or_age_unreadable_state_is_refused(self) -> None:
        for age, code in ((99.0, "stale_state"), (None, "unknown_state_age")):
            with self.subTest(age=age):
                service = make_service(make_context(age=age), RecordingSubmit())
                plan = await service.plan(SESSION)
                self.assertIs(plan.status, PlanStatus.INSUFFICIENT_INFORMATION)
                self.assertEqual(plan.code, code)

    async def test_a_closed_session_gate_is_a_refusal_not_insufficient_information(self) -> None:
        gate = {"allow": False, "state": "closed", "reason": "the session is STOPPED"}
        service = make_service(make_context(gate=gate), RecordingSubmit())
        plan = await service.plan(SESSION)
        self.assertIs(plan.status, PlanStatus.REJECTED)
        self.assertEqual(plan.code, "gate_closed")
        self.assertIn("STOPPED", plan.reason)

    async def test_no_active_objective_or_a_stale_interpretation_is_refused(self) -> None:
        state = mission_state()
        empty = interpret(state)
        object.__setattr__(empty, "objectives", ())
        service = make_service(make_context(objective_state=empty), RecordingSubmit())
        plan = await service.plan(SESSION)
        self.assertEqual(plan.code, "no_active_objective")

        newer = mission_state(seq=2)
        stale = make_service(
            make_context(state=newer, objective_state=interpret(state)), RecordingSubmit()
        )
        plan = await stale.plan(SESSION)
        self.assertEqual(plan.code, "objective_stale")

    async def test_a_finished_counter_is_refused_without_claiming_verified_success(self) -> None:
        # NF-17 closes the objective when its counter reaches the target, so the
        # planner sees no OPEN objective. Either way, no step is generated and
        # nothing is reported as verified.
        service = make_service(
            make_context(state=mission_state(counter=3.0)), RecordingSubmit()
        )
        plan = await service.plan(SESSION)
        self.assertIn(
            (plan.status, plan.code),
            {
                (PlanStatus.NO_ACTION, "objective_progress_complete"),
                (PlanStatus.INSUFFICIENT_INFORMATION, "no_active_objective"),
            },
        )
        self.assertEqual(plan.steps, [])
        self.assertFalse(plan.to_dict()["effect_verified"])

    async def test_no_rule_and_no_verified_control_is_an_honest_no_plan(self) -> None:
        service = make_service(make_context(game="Some Unverified Game"), RecordingSubmit())
        plan = await service.plan(SESSION)
        self.assertIs(plan.status, PlanStatus.INSUFFICIENT_INFORMATION)
        self.assertEqual(plan.code, "no_plan_rule")
        self.assertIn("does not invent", plan.reason)

    async def test_unsupported_and_missing_field_rules_are_rejected_with_reasons(self) -> None:
        unsupported = PlanRuleRegistry(
            (
                PlanRule(
                    rule_id="bad-key",
                    kind=ControlKind.KEY_HOLD,
                    key="f13-magic",
                    duration_ms=100,
                    description="press an unknown key",
                    expected_effect="nothing",
                    game_tokens=("nc forge mission",),
                    objective_categories=(),
                ),
            )
        )
        service = make_service(make_context(), RecordingSubmit(), rules=unsupported)
        plan = await service.plan(SESSION)
        self.assertIs(plan.status, PlanStatus.NO_ACTION)
        self.assertEqual(plan.code, "no_feasible_action")
        self.assertTrue(any("not a key this build knows" in item.rejection for item in plan.candidates))
        self.assertEqual(plan.steps, [])

        missing = PlanRuleRegistry(
            (
                PlanRule(
                    rule_id="missing-field",
                    kind=ControlKind.KEY_PRESS,
                    key="w",
                    description="needs a field the state does not have",
                    expected_effect="nothing",
                    game_tokens=("nc forge mission",),
                    required_fields=("inventory",),
                ),
            )
        )
        plan = await make_service(make_context(), RecordingSubmit(), rules=missing).plan(SESSION)
        self.assertEqual(plan.code, "no_feasible_action")
        self.assertTrue(any("inventory" in item.rejection for item in plan.candidates))

    async def test_candidate_generation_failures_and_timeouts_are_safe_refusals(self) -> None:
        exploded = await make_service(
            make_context(), RecordingSubmit(), rules=_ExplodingRules(())
        ).plan(SESSION)
        self.assertIs(exploded.status, PlanStatus.FAILED)
        self.assertEqual(exploded.code, "candidate_generation_failed")
        self.assertIn("RuntimeError", exploded.reason)
        self.assertEqual(exploded.steps, [])

        slow = await make_service(
            make_context(),
            RecordingSubmit(),
            rules=_SlowRules(default_plan_rules().all()),
            policy=PlanPolicy(planning_deadline_ms=1),
        ).plan(SESSION)
        self.assertIs(slow.status, PlanStatus.INSUFFICIENT_INFORMATION)
        self.assertEqual(slow.code, "planning_timeout")
        self.assertEqual(slow.steps, [])

    async def test_an_exhausted_budget_is_refused_before_any_candidate_is_built(self) -> None:
        submit = RecordingSubmit()
        plan = await make_service(make_context(budget=0), submit).plan(SESSION)
        self.assertIs(plan.status, PlanStatus.REJECTED)
        self.assertEqual(plan.code, "budget_exhausted")
        self.assertEqual(submit.actions, [])

    async def test_verified_knowledge_can_plan_and_an_unbound_control_cannot(self) -> None:
        def record(**content: Any) -> KnowledgeRecord:
            return KnowledgeRecord(
                knowledge_id="k1",
                game="NC FORGE MISSION",
                category=KnowledgeCategory.CONTROL,
                key="collect",
                content={
                    "supports": ["collect"],
                    "kind": "key_hold",
                    "duration_ms": 80,
                    "risk": "low",
                    "reversible": True,
                    **content,
                },
                status=KnowledgeStatus.VERIFIED,
                last_verified_at=AT,
                evidence=(
                    KnowledgeEvidence(
                        source=EvidenceSource.SESSION,
                        summary="the counter advanced after the hold",
                        verified=True,
                        at=AT,
                        session_id=SESSION,
                        observation_id="s1:0001",
                    ),
                ),
            )

        bound = make_service(
            make_context(knowledge=(record(key="w"),)),
            RecordingSubmit(),
            rules=PlanRuleRegistry(()),
        )
        plan = await bound.plan(SESSION)
        self.assertIs(plan.status, PlanStatus.AWAITING_APPROVAL)
        self.assertEqual(len(plan.steps), 1)
        self.assertIn("knowledge:k1", plan.steps[0].source)

        unbound = make_service(
            make_context(knowledge=(record(),)),
            RecordingSubmit(),
            rules=PlanRuleRegistry(()),
        )
        plan = await unbound.plan(SESSION)
        self.assertIs(plan.status, PlanStatus.NO_ACTION)
        self.assertTrue(any("no input binding" in item.rejection for item in plan.candidates))

        undeclared = make_service(
            make_context(knowledge=(record(key="w", risk=""),)),
            RecordingSubmit(),
            rules=PlanRuleRegistry(()),
        )
        plan = await undeclared.plan(SESSION)
        self.assertIs(plan.status, PlanStatus.NO_ACTION)
        self.assertTrue(any("HIGH" in item.rejection for item in plan.candidates))

    async def test_high_risk_and_irreversible_candidates_need_explicit_policy(self) -> None:
        rule = PlanRule(
            rule_id="risky",
            kind=ControlKind.KEY_PRESS,
            key="w",
            description="a high-risk press",
            expected_effect="something irreversible",
            game_tokens=("nc forge mission",),
            risk=RiskLevel.HIGH,
            reversible=False,
        )
        refused = await make_service(
            make_context(), RecordingSubmit(), rules=PlanRuleRegistry((rule,))
        ).plan(SESSION)
        self.assertIs(refused.status, PlanStatus.NO_ACTION)
        self.assertTrue(any("HIGH" in item.rejection for item in refused.candidates))
        self.assertTrue(any("irreversible" in item.rejection for item in refused.candidates))

        allowed = await make_service(
            make_context(),
            RecordingSubmit(),
            rules=PlanRuleRegistry((rule,)),
            policy=PlanPolicy(allow_high_risk=True, allow_irreversible=True),
        ).plan(SESSION)
        self.assertIs(allowed.status, PlanStatus.AWAITING_APPROVAL)
        self.assertTrue(allowed.steps)

    def test_the_selection_key_never_contains_a_generated_name(self) -> None:
        """A tie is broken by the DECLARED action, never by a random uuid suffix.

        ``candidate_id`` ends in ``uuid4().hex[:6]``. Two candidates that declare
        the same thing must score-key equal regardless of their generated ids —
        a generated name is not a selection criterion and cannot be inspected.
        """
        first = CandidateAction(
            candidate_id="rule:x:aaaaaa",
            kind=ControlKind.KEY_HOLD,
            description="hold w",
            expected_effect="a hypothesis",
            source="declared rule",
            parameters={"kind": "key_hold", "key": "w", "duration_ms": 100},
            score=4.25,
        )
        second = dataclasses.replace(first, candidate_id="rule:x:zzzzzz")
        keys = {GamePlanService._selection_key(item) for item in (first, second)}
        self.assertEqual(len(keys), 1, "the random suffix must not be part of the key")
        key = next(iter(keys))
        self.assertEqual(key[0], -4.25)  # highest score first
        self.assertNotIn("aaaaaa", ";".join(str(part) for part in key))
        self.assertIn("key=w", ";".join(str(part) for part in key))

    async def test_the_same_evidence_selects_the_same_rule_every_time(self) -> None:
        """Two rules that differ in nothing but their id must not alternate."""
        service = make_service(make_context(), RecordingSubmit(), rules=tied_rules())
        plans = [await service.plan(SESSION) for _ in range(3)]
        for plan in plans:
            self.assertIs(plan.status, PlanStatus.AWAITING_APPROVAL)
        feasible = [item for item in plans[0].candidates if item.feasible]
        self.assertEqual(len(feasible), 2)
        self.assertEqual(feasible[0].score, feasible[1].score)
        self.assertEqual(
            GamePlanService._selection_key(feasible[0]),
            GamePlanService._selection_key(feasible[1]),
        )
        subjects = {
            GamePlanService._candidate_subject(plan.steps[0].candidate_id) for plan in plans
        }
        self.assertEqual(
            subjects, {("rule", "a-first")}, "the same evidence must select the same rule"
        )
        # The alternative it beat is named with its score: a feasible candidate
        # that lost is a comparison the record has to show, not hide.
        self.assertIn("Compared and not selected", plans[0].rationale)
        self.assertIn("b-second", plans[0].rationale)

    async def test_a_candidate_set_cut_by_its_bound_is_reported_as_partial(self) -> None:
        capped = await make_service(
            make_context(),
            RecordingSubmit(),
            rules=tied_rules(),
            policy=PlanPolicy(max_candidates=1),
        ).plan(SESSION)
        self.assertIs(capped.status, PlanStatus.AWAITING_APPROVAL)
        self.assertTrue(capped.budgets["candidates_truncated"])
        self.assertTrue(any("PARTIAL" in line for line in capped.reports))

        complete = await make_service(
            make_context(), RecordingSubmit(), rules=tied_rules()
        ).plan(SESSION)
        self.assertFalse(complete.budgets["candidates_truncated"])
        self.assertFalse(any("PARTIAL" in line for line in complete.reports))

    async def test_the_plan_records_the_sessions_prior_actions_beside_its_budget(self) -> None:
        plan = await make_service(
            make_context(budget=4, executed=3), RecordingSubmit()
        ).plan(SESSION)
        self.assertEqual(plan.budgets["remaining_actions"], 4)
        self.assertEqual(plan.budgets["actions_already_executed"], 3)


class PlannerExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_unapproved_plan_reports_and_sends_nothing(self) -> None:
        submit = RecordingSubmit()
        service = make_service(make_context(), submit)
        plan = await service.plan(SESSION)
        result = await service.execute(SESSION, plan.plan_id)
        self.assertIs(result.status, PlanStatus.AWAITING_APPROVAL)
        self.assertEqual(result.code, "approval_required")
        self.assertEqual(submit.actions, [])

    async def test_an_approved_plan_submits_each_step_once_and_claims_no_effect(self) -> None:
        submit = RecordingSubmit()
        service = make_service(make_context(), submit)
        plan = await service.plan(SESSION)
        result = await service.execute(SESSION, plan.plan_id, approved=True)
        self.assertIs(result.status, PlanStatus.EXECUTED)
        self.assertEqual(result.code, "all_steps_submitted")
        self.assertEqual(len(submit.actions), 2)
        self.assertEqual(len({item.action_id for item in submit.actions}), 2)
        self.assertEqual(len(result.execution["steps"]), 2)
        row = result.execution["steps"][0]
        for field in (
            "action_id", "kind", "status", "ok", "code", "reason", "error",
            "requested_at", "started_at", "dispatched_at", "finished_at",
            "duration_ms", "deadline_ms", "cleanup", "effect",
        ):
            self.assertIn(field, row)
        self.assertIsNone(row["effect"])
        self.assertFalse(result.execution["effect_verified"])
        self.assertIn("NF-21", result.execution["effect_note"])
        self.assertEqual(result.approval["approved_by"], "operator")

    async def test_a_refusal_on_a_step_stops_and_is_not_retried(self) -> None:
        submit = RecordingSubmit(
            [
                {
                    "ok": False,
                    "status": "rejected",
                    "code": "held",
                    "reason": "the key is already held",
                    "result": None,
                }
            ]
        )
        service = make_service(make_context(), submit)
        plan = await service.plan(SESSION)
        result = await service.execute(SESSION, plan.plan_id, approved=True)
        self.assertIs(result.status, PlanStatus.STOPPED)
        self.assertEqual(result.code, "held")
        self.assertEqual(len(submit.actions), 1)
        self.assertIn("held", result.reason)

    async def test_a_submit_bug_is_a_failed_step_not_an_exception(self) -> None:
        submit = RecordingSubmit([RuntimeError("injector exploded")])
        service = make_service(make_context(), submit)
        plan = await service.plan(SESSION)
        result = await service.execute(SESSION, plan.plan_id, approved=True)
        self.assertIs(result.status, PlanStatus.FAILED)
        self.assertEqual(result.code, "interface_error")
        self.assertIn("RuntimeError", result.reason)
        self.assertEqual(len(submit.actions), 1)

    async def test_cancelling_before_execution_stops_every_step(self) -> None:
        submit = RecordingSubmit()
        service = make_service(make_context(), submit)
        plan = await service.plan(SESSION)
        cancelled = await service.cancel(SESSION, plan.plan_id)
        self.assertIsNotNone(cancelled)
        self.assertIs(cancelled.status, PlanStatus.CANCELLED)
        await service.execute(SESSION, plan.plan_id, approved=True)
        self.assertEqual(submit.actions, [])

    async def test_a_second_plan_cannot_execute_concurrently(self) -> None:
        gate = asyncio.Event()
        entered = asyncio.Event()
        submit = RecordingSubmit(gate=gate, entered=entered)
        service = make_service(make_context(), submit, policy=PlanPolicy(max_actions_per_plan=1))
        first = await service.plan(SESSION)
        second = await service.plan(SESSION)
        task = asyncio.create_task(service.execute(SESSION, first.plan_id, approved=True))
        await asyncio.wait_for(entered.wait(), timeout=2)
        busy = await service.execute(SESSION, second.plan_id, approved=True)
        self.assertEqual(busy.code, "busy")
        self.assertIs(busy.status, PlanStatus.AWAITING_APPROVAL)
        gate.set()
        await asyncio.wait_for(task, timeout=2)
        self.assertIs(first.status, PlanStatus.EXECUTED)

    async def test_a_closed_or_stale_session_stops_between_steps(self) -> None:
        first = make_context()
        closed = make_context(gate={"allow": False, "state": "closed", "reason": "session stopped"})
        submit = RecordingSubmit()
        service = make_service(first, submit, contexts=[first, first, closed])
        plan = await service.plan(SESSION)
        result = await service.execute(SESSION, plan.plan_id, approved=True)
        self.assertIs(result.status, PlanStatus.REJECTED)
        self.assertEqual(result.code, "gate_closed")
        self.assertEqual(len(submit.actions), 1)

        stale = make_context(age=99.0)
        submit = RecordingSubmit()
        service = make_service(first, submit, contexts=[first, first, stale])
        plan = await service.plan(SESSION)
        result = await service.execute(SESSION, plan.plan_id, approved=True)
        self.assertIs(result.status, PlanStatus.INSUFFICIENT_INFORMATION)
        self.assertEqual(result.code, "stale_state")
        self.assertEqual(len(submit.actions), 1)

    async def test_an_objective_that_changed_after_the_first_step_invalidates_the_plan(self) -> None:
        first = make_context()
        changed_state = mission_state(seq=2, objective="Collect the audit tokens")
        changed = interpret(changed_state)
        object.__setattr__(
            changed,
            "objectives",
            tuple(
                dataclasses.replace(item, objective_id="other-objective")
                for item in changed.objectives
            ),
        )
        context = PlanningContext(
            session_id=SESSION,
            game=changed_state.game,
            target=control_target(),
            state=changed_state,
            objective_state=changed,
            knowledge=(),
            gate={"allow": True, "state": "allowed", "reason": "test"},
            budget_remaining=5,
            state_age_seconds=1.0,
            control_policy=ControlPolicy(),
        )
        submit = RecordingSubmit()
        service = make_service(first, submit, contexts=[first, first, context])
        plan = await service.plan(SESSION)
        result = await service.execute(SESSION, plan.plan_id, approved=True)
        self.assertIs(result.status, PlanStatus.INVALIDATED)
        self.assertEqual(result.code, "objective_changed")
        self.assertEqual(len(submit.actions), 1)

    async def test_a_budget_exhausted_after_the_first_step_stops_the_plan(self) -> None:
        first = make_context()
        spent = make_context(budget=0)
        submit = RecordingSubmit()
        service = make_service(first, submit, contexts=[first, first, spent])
        plan = await service.plan(SESSION)
        result = await service.execute(SESSION, plan.plan_id, approved=True)
        # A session-wide budget is not a transient state: another plan cannot
        # help, so this is a refusal rather than "replan required".
        self.assertIs(result.status, PlanStatus.REJECTED)
        self.assertEqual(result.code, "budget_exhausted")
        self.assertEqual(len(submit.actions), 1)

    async def test_a_step_after_a_state_change_rechecks_the_rules_declared_field(self) -> None:
        """The second step must not run against a state that lost the rule's field.

        The preconditions were checked when the plan was built; the fixture rule
        declares the ``progress`` field, so a reading where that field is no
        longer recognized is exactly the "critical precondition became false"
        case that must stop execution instead of pressing on.
        """
        first = make_context()
        gone = mission_state(counter_status=HudStatus.ABSENT)
        fresh = make_context(state=gone, objective_state=interpret(gone))
        submit = RecordingSubmit()
        service = make_service(first, submit, contexts=[first, first, fresh])
        plan = await service.plan(SESSION)
        result = await service.execute(SESSION, plan.plan_id, approved=True)
        self.assertIs(result.status, PlanStatus.STOPPED)
        self.assertEqual(result.code, "precondition_false")
        self.assertIn("progress", result.reason)
        self.assertEqual(len(submit.actions), 1)

    async def test_execution_rechecks_the_verified_knowledge_behind_a_step(self) -> None:
        """A knowledge plan is single-step, so the check is pinned directly."""
        record = KnowledgeRecord(
            knowledge_id="k2",
            game="NC FORGE MISSION",
            category=KnowledgeCategory.CONTROL,
            key="collect",
            content={
                "supports": ["collect"],
                "kind": "key_hold",
                "key": "w",
                "duration_ms": 80,
                "risk": "low",
                "reversible": True,
            },
            status=KnowledgeStatus.VERIFIED,
            last_verified_at=AT,
            evidence=(
                KnowledgeEvidence(
                    source=EvidenceSource.SESSION,
                    summary="the counter advanced after the hold",
                    verified=True,
                    at=AT,
                    session_id=SESSION,
                    observation_id="s1:0001",
                ),
            ),
        )
        service = make_service(
            make_context(knowledge=(record,)), RecordingSubmit(), rules=PlanRuleRegistry(())
        )
        plan = await service.plan(SESSION)
        self.assertEqual(len(plan.steps), 1)
        contradicted = dataclasses.replace(record, status=KnowledgeStatus.CONTRADICTED)
        problem = service._state_preconditions(plan, make_context(knowledge=(contradicted,)))
        self.assertIsNotNone(problem)
        assert problem is not None  # for the type checker
        status, code, reason = problem
        self.assertIs(status, PlanStatus.STOPPED)
        self.assertEqual(code, "precondition_false")
        self.assertIn("contradicted", reason)
        # A record the reader simply did not return is NOT a withdrawal: stopping
        # there would be a guess, not evidence.
        self.assertIsNone(service._state_preconditions(plan, make_context(knowledge=())))

    async def test_a_cancel_request_stops_a_running_plan_at_the_next_checkpoint(self) -> None:
        gate = asyncio.Event()
        entered = asyncio.Event()
        submit = RecordingSubmit(gate=gate, entered=entered)
        service = make_service(make_context(), submit)
        plan = await service.plan(SESSION)
        task = asyncio.create_task(service.execute(SESSION, plan.plan_id, approved=True))
        await asyncio.wait_for(entered.wait(), timeout=2)
        await service.cancel(SESSION, plan.plan_id)
        gate.set()
        result = await asyncio.wait_for(task, timeout=2)
        self.assertIs(result.status, PlanStatus.CANCELLED)
        self.assertEqual(result.code, "cancelled")


class ReplanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_unknown_trigger_or_unknown_plan_is_refused(self) -> None:
        service = make_service(make_context(), RecordingSubmit())
        plan = await service.plan(SESSION)
        refused = await service.replan(SESSION, plan.plan_id, reason="because")
        self.assertIs(refused.status, PlanStatus.REJECTED)
        self.assertEqual(refused.code, "unknown_trigger")
        self.assertIn("operator", refused.reason)
        self.assertIn("budget_exceeded", REPLAN_TRIGGERS)

        unknown = await service.replan(SESSION, "nope", reason="operator")
        self.assertEqual(unknown.code, "unknown_plan")

    async def test_the_replan_limit_is_enforced_with_a_terminal_status(self) -> None:
        service = make_service(
            make_context(), RecordingSubmit(), policy=PlanPolicy(max_replans=1)
        )
        plan = await service.plan(SESSION)
        await service.execute(SESSION, plan.plan_id, approved=True)
        plan.replan_count = 1
        refused = await service.replan(SESSION, plan.plan_id, reason="action_failed")
        self.assertIs(refused.status, PlanStatus.REPLAN_EXHAUSTED)
        self.assertEqual(refused.code, "replan_limit")

    async def test_identical_evidence_and_sequence_is_not_repeated(self) -> None:
        service = make_service(make_context(), RecordingSubmit())
        plan = await service.plan(SESSION)
        await service.execute(SESSION, plan.plan_id, approved=True)
        refused = await service.replan(SESSION, plan.plan_id, reason="action_failed")
        self.assertIs(refused.status, PlanStatus.REPLAN_EXHAUSTED)
        self.assertEqual(refused.code, "repeat_plan_refused")

    async def test_fresh_evidence_creates_a_child_plan_and_invalidates_the_old_one(self) -> None:
        first_context = make_context()
        second_state = mission_state(seq=2, counter=2.0)
        second_context = make_context(state=second_state)
        submit = RecordingSubmit()
        service = make_service(first_context, submit, contexts=[first_context, second_context])
        plan = await service.plan(SESSION)
        await service.execute(SESSION, plan.plan_id, approved=True)

        child = await service.replan(SESSION, plan.plan_id, reason="state_changed")
        self.assertIs(child.status, PlanStatus.AWAITING_APPROVAL)
        self.assertEqual(child.parent_plan_id, plan.plan_id)
        self.assertEqual(child.replan_count, 1)
        self.assertEqual(child.revision, 2)

    async def test_replanning_a_pending_plan_invalidates_it_before_the_child_exists(self) -> None:
        first_context = make_context()
        second_context = make_context(state=mission_state(seq=2, counter=2.0))
        service = make_service(
            first_context, RecordingSubmit(), contexts=[first_context, second_context]
        )
        plan = await service.plan(SESSION)
        child = await service.replan(SESSION, plan.plan_id, reason="state_changed")
        self.assertIs(plan.status, PlanStatus.INVALIDATED)
        self.assertEqual(plan.code, "replanned")
        self.assertTrue(child.steps)
        # The invalidated plan cannot be executed afterwards by its stale id.
        result = await service.execute(SESSION, plan.plan_id, approved=True)
        self.assertIs(result.status, PlanStatus.INVALIDATED)


class PlannerEventSeamTests(unittest.IsolatedAsyncioTestCase):
    """Every planning edge is announced through the injected publish seam."""

    async def test_a_plan_publishes_its_edges_in_order_and_claims_no_effect(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        service = make_service(
            make_context(),
            RecordingSubmit(),
            publish=lambda event_type, payload: events.append((event_type, payload)),
        )
        plan = await service.plan(SESSION)
        await service.execute(SESSION, plan.plan_id, approved=True)
        self.assertEqual(
            [event_type for event_type, _ in events],
            [
                "novaforge.plan.created",
                "novaforge.plan.started",
                "novaforge.plan.step",
                "novaforge.plan.step",
                "novaforge.plan.finished",
            ],
        )
        for event_type, payload in events:
            self.assertEqual(payload["session_id"], SESSION)
            self.assertEqual(payload["plan_id"], plan.plan_id)
            self.assertFalse(payload["effect_verified"])
        self.assertEqual(events[0][1]["status"], "awaiting_approval")
        self.assertEqual(events[-1][1]["status"], "executed")

    async def test_a_refusal_is_announced_and_a_broken_seam_never_fails_the_plan(self) -> None:
        refused: list[str] = []
        service = make_service(
            make_context(game="Some Unverified Game"),
            RecordingSubmit(),
            publish=lambda event_type, payload: refused.append(event_type),
        )
        plan = await service.plan(SESSION)
        self.assertIs(plan.status, PlanStatus.INSUFFICIENT_INFORMATION)
        self.assertEqual(refused, ["novaforge.plan.refused"])

        def broken(_event_type: str, _payload: dict[str, Any]) -> None:
            raise RuntimeError("the observer is down")

        service = make_service(make_context(), RecordingSubmit(), publish=broken)
        plan = await service.plan(SESSION)
        self.assertIs(plan.status, PlanStatus.AWAITING_APPROVAL)
        result = await service.execute(SESSION, plan.plan_id, approved=True)
        self.assertIs(result.status, PlanStatus.EXECUTED)

    async def test_every_plan_event_type_is_registered_on_the_timeline(self) -> None:
        """An unregistered type is DROPPED silently by ``_event`` — pin the table.

        The planner publishes to the bus AND the session timeline; the timeline
        refuses an unknown type, so a missing row here is a plan edge that never
        reaches the audit trail a person reads back.
        """
        from novacontrol.novaforge.timeline import SOURCES, EVENT_SPECS, TimelineLog

        for event_type in (
            "novaforge.plan.created", "novaforge.plan.started", "novaforge.plan.step",
            "novaforge.plan.finished", "novaforge.plan.refused",
            "novaforge.plan.invalidated", "novaforge.plan.cancelled",
            "novaforge.plan.replanned", "novaforge.plan.replan_refused",
        ):
            with self.subTest(event_type=event_type):
                self.assertIn(event_type, EVENT_SPECS)
        self.assertIn("planner", SOURCES)
        # Behavioural, not just table-shaped: the log must ACCEPT the event and
        # keep the source instead of filing it as "session".
        log = TimelineLog(session_id="a" * 32)
        event = log.append(
            "novaforge.plan.refused", source="planner", payload={"plan_id": "p1"}
        )
        self.assertEqual(event.type, "novaforge.plan.refused")
        self.assertEqual(event.category, "plan")
        self.assertEqual(event.source, "planner")
        self.assertEqual(event.payload["plan_id"], "p1")

    async def test_shutdown_stops_planning_and_cancels_running_work(self) -> None:
        submit = RecordingSubmit()
        service = make_service(make_context(), submit)
        plan = await service.plan(SESSION)
        await service.shutdown()
        self.assertTrue(service.status()["stopped"])
        refused = await service.plan(SESSION)
        self.assertIs(refused.status, PlanStatus.REJECTED)
        self.assertEqual(refused.code, "service_stopped")
        result = await service.execute(SESSION, plan.plan_id, approved=True)
        self.assertEqual(result.code, "service_stopped")
        self.assertEqual(submit.actions, [])

    async def test_shutdown_mid_plan_stops_at_the_next_checkpoint(self) -> None:
        gate = asyncio.Event()
        entered = asyncio.Event()
        submit = RecordingSubmit(gate=gate, entered=entered)
        service = make_service(make_context(), submit)
        plan = await service.plan(SESSION)
        task = asyncio.create_task(service.execute(SESSION, plan.plan_id, approved=True))
        await asyncio.wait_for(entered.wait(), timeout=2)
        await service.shutdown()
        gate.set()
        result = await asyncio.wait_for(task, timeout=2)
        self.assertIs(result.status, PlanStatus.CANCELLED)
        self.assertEqual(result.code, "cancelled")
        self.assertEqual(len(submit.actions), 1)

    async def test_a_cancelled_execute_restores_the_service_and_records_itself(self) -> None:
        """A caller that goes away must not leave the planner permanently "busy".

        ``execute`` awaited its first announcement OUTSIDE the try, so a
        cancellation arriving at that await (a disconnected request, an aborted
        task) skipped the finally: ``_running`` stayed set and every later plan
        was refused as busy for the life of the process.
        """
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocking(event_type: str, payload: dict[str, Any]) -> None:
            if event_type == "novaforge.plan.started":
                entered.set()
                await release.wait()

        submit = RecordingSubmit()
        service = make_service(make_context(), submit, publish=blocking)
        plan = await service.plan(SESSION)
        task = asyncio.create_task(service.execute(SESSION, plan.plan_id, approved=True))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(service.running)
        self.assertIs(plan.status, PlanStatus.CANCELLED)
        self.assertEqual(plan.code, "caller_cancelled")
        self.assertEqual(submit.actions, [])
        self.assertEqual(service.status()["counts"]["cancelled"], 1)
        # The next attempt is answered on its own merits, not as "busy".
        again = await service.execute(SESSION, plan.plan_id, approved=True)
        self.assertNotEqual(again.code, "busy")

    async def test_a_cancelled_step_submission_is_not_recorded_as_an_interface_error(
        self,
    ) -> None:
        """Nothing observed an interface error, so no plan may claim one."""
        gate = asyncio.Event()
        entered = asyncio.Event()
        submit = RecordingSubmit(gate=gate, entered=entered)
        service = make_service(make_context(), submit)
        plan = await service.plan(SESSION)
        task = asyncio.create_task(service.execute(SESSION, plan.plan_id, approved=True))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(service.running)
        self.assertIs(plan.status, PlanStatus.CANCELLED)
        self.assertEqual(plan.code, "caller_cancelled")
        self.assertNotEqual(plan.code, "interface_error")
        self.assertEqual(len(submit.actions), 1)


class PlanningManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.desktop = FakeDesktop()
        self.input = RecordingInput()

        async def probe(_handle: int) -> Any:
            return self.desktop.window

        self.manager = NovaForgeManager(
            data_dir=Path(self.temp.name),
            capture=self.desktop.capture,
            geometry=self.desktop.geometry,
            window_lines=self.desktop.window_lines,
            release_inputs=self.desktop.release,
            action_input=self.input,
            objective_probe=probe,
            capture_supported=True,
            frame_interval=0.05,
            pause_timeout=1.0,
            stop_timeout=2.0,
        )
        self.session_id = ""

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        self.temp.cleanup()

    async def start(self) -> None:
        described = await self.manager.start(_mission(frame_interval_seconds=0.05))
        self.session_id = described["session_id"]
        record = self.manager.get(self.session_id)
        for _ in range(60):
            if record is not None and record.target_validated:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(record is not None and record.target_validated)

    async def test_planning_status_lists_the_rules_and_the_live_gate(self) -> None:
        await self.start()
        status = self.manager.planning_status(self.session_id)
        self.assertTrue(status["available"])
        self.assertTrue(status["policy"]["require_approval"])
        self.assertEqual(len(status["rules"]), 1)
        self.assertFalse(status["effect_verified"])
        self.assertEqual(status["session"]["session_id"], self.session_id)
        self.assertIn("gate", status["session"])

    async def test_a_session_without_a_state_gets_no_state_and_sends_nothing(self) -> None:
        await self.start()
        receipt = await self.manager.plan_actions(self.session_id)
        self.assertIsInstance(receipt["plan"], dict)
        self.assertEqual(receipt["plan"]["status"], "insufficient_information")
        self.assertEqual(receipt["plan"]["code"], "no_state")
        self.assertEqual(self.input.presses, [])
        self.assertEqual(self.input.releases, [])

    async def test_a_planned_step_is_refused_when_the_target_changed_under_it(self) -> None:
        await self.start()
        record = self.manager.get(self.session_id)
        step = ControlAction.from_payload(
            {"kind": "key_hold", "key": "w", "duration_ms": 50},
            session_id=self.session_id,
            target=ControlTarget(
                session_id=self.session_id,
                title=record.target.title,
                handle=int(record.target.handle) + 1,
                pid=int(record.target.pid),
            ),
        )
        refused = await self.manager.submit_control_action(self.session_id, step)
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["code"], "target_changed")
        self.assertEqual(self.input.presses, [])

    async def test_a_planned_step_with_the_live_target_uses_the_nf19_path(self) -> None:
        await self.start()
        record = self.manager.get(self.session_id)
        step = ControlAction.from_payload(
            {"kind": "key_hold", "key": "w", "duration_ms": 50},
            session_id=self.session_id,
            target=ControlTarget(
                session_id=self.session_id,
                title=record.target.title,
                handle=int(record.target.handle),
                pid=int(record.target.pid),
            ),
        )
        result = await self.manager.submit_control_action(self.session_id, step)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.input.presses, ["key:w"])
        self.assertEqual(self.input.releases, ["key:w"])
        self.assertEqual(result["result"]["action_id"], step.action_id)
        self.assertFalse(result["effect_verified"])

    async def test_execute_without_a_plan_id_is_a_validation_error(self) -> None:
        from novacontrol.novaforge import MissionValidationError

        await self.start()
        with self.assertRaises(MissionValidationError):
            await self.manager.execute_plan(self.session_id, {})

    async def test_a_plan_decision_lands_on_the_session_timeline(self) -> None:
        """The audit trail is the point: a plan edge that never arrives is not one.

        The timeline refuses an unregistered event type and the manager swallows
        that refusal, so this asserts the event is really in the stored evidence
        rather than that the code called a recorder.
        """
        await self.start()
        await self.manager.plan_actions(self.session_id)
        events = self.manager.timeline_payload(self.session_id, limit=100)["events"]
        rows = [
            row
            for row in events
            if str(row.get("type", "")).startswith("novaforge.plan.")
        ]
        self.assertTrue(rows, "the plan decision must reach the session timeline")
        self.assertEqual(rows[-1]["type"], "novaforge.plan.refused")
        self.assertEqual(rows[-1]["source"], "planner")
        self.assertEqual(rows[-1]["category"], "plan")
        self.assertEqual(rows[-1]["payload"]["code"], "no_state")
        self.assertFalse(rows[-1]["payload"]["effect_verified"])

    async def test_cancelling_a_session_stops_its_plan_before_finalizing(self) -> None:
        await self.start()
        await self.manager.stop(self.session_id)
        receipt = await self.manager.cancel_plan(self.session_id)
        self.assertFalse(receipt["cancelled"])
        self.assertIsNone(receipt["plan"])


class PlanningApiTests(_GameplayApiFixture):
    def test_planning_routes_require_known_sessions(self) -> None:
        for method, path, payload in (
            ("get", "/novaforge/sessions/nope/plan", None),
            ("post", "/novaforge/sessions/nope/plan", None),
            ("post", "/novaforge/sessions/nope/plan/execute", {"plan_id": "p"}),
            ("post", "/novaforge/sessions/nope/plan/cancel", {}),
            ("post", "/novaforge/sessions/nope/plan/replan", {"plan_id": "p"}),
        ):
            with self.subTest(path=path):
                response = getattr(self._client, method)(
                    path, **(({"json": payload} if payload is not None else {}))
                )
                self.assertEqual(response.status_code, 404, response.text)

    def test_plan_status_build_and_refusals_are_explicit(self) -> None:
        session = self._validated_session()
        session_id = session["session_id"]

        status = self._client.get(f"/novaforge/sessions/{session_id}/plan").json()
        self.assertEqual(len(status["rules"]), 1)
        self.assertTrue(status["policy"]["require_approval"])

        built = self._client.post(f"/novaforge/sessions/{session_id}/plan").json()
        plan = built["plan"]
        self.assertEqual(plan["status"], "insufficient_information")
        self.assertEqual(plan["code"], "no_state")
        self.assertFalse(built["effect_verified"])
        self.assertEqual(self.input.presses, [])

        missing = self._client.post(
            f"/novaforge/sessions/{session_id}/plan/execute", json={}
        )
        self.assertEqual(missing.status_code, 422, missing.text)

        unknown = self._client.post(
            f"/novaforge/sessions/{session_id}/plan/execute", json={"plan_id": "nope"}
        )
        self.assertEqual(unknown.status_code, 200, unknown.text)
        self.assertEqual(unknown.json()["plan"]["code"], "unknown_plan")

        cancelled = self._client.post(f"/novaforge/sessions/{session_id}/plan/cancel")
        self.assertEqual(cancelled.status_code, 200)
        self.assertFalse(cancelled.json()["cancelled"])

    # ── NF-20 limitation 5: approval is a TOKEN, not a request flag ──────────
    # The gap recorded by NF-20 was that `/plan/execute` accepted `approved: true`
    # — a flag an authenticated caller can set for itself. A plan awaiting
    # approval now carries the same server-minted, single-use, time-limited
    # token the desktop/browser/phone command plans carry, and the route refuses
    # (403) without it. These tests mint through the REAL receipt path the routes
    # use and drive the REAL route, so the gate is exercised where a caller can
    # actually reach it; the plan service's in-process `approved=` contract (and
    # the fixture states that reach it) are untouched.

    AWAITING = {
        "status": "awaiting_approval",
        "approval": {"required": True, "status": "pending"},
    }

    def _minted_token(self, plan_id: str = "plan-under-test") -> str:
        receipt = self._nova.forge_plan_receipt(
            {
                "plan": {
                    "plan_id": plan_id,
                    "approval": {"required": True, "status": "pending"},
                }
            }
        )
        return str(receipt["plan"]["approval"]["token"])

    def test_only_a_plan_awaiting_approval_is_served_a_token(self) -> None:
        awaiting = self._nova.forge_plan_receipt(
            {"plan": {"plan_id": "p1", "approval": {"required": True, "status": "pending"}}}
        )
        self.assertTrue(awaiting["plan"]["approval"]["token"])

        not_required = self._nova.forge_plan_receipt(
            {"plan": {"plan_id": "p2", "approval": {"required": False, "status": "not_required"}}}
        )
        self.assertNotIn("token", not_required["plan"]["approval"])

        already = self._nova.forge_plan_receipt(
            {"plan": {"plan_id": "p3", "approval": {"required": True, "status": "approved"}}}
        )
        self.assertNotIn("token", already["plan"]["approval"])

        untouched = {"plan": None}
        self.assertEqual(self._nova.forge_plan_receipt(untouched), untouched)

        # The status view nests the plan under `current`; it is minted too, or a
        # reloaded panel would show a plan it cannot approve.
        status_shape = {
            "current": {"plan_id": "p4", "approval": {"required": True, "status": "pending"}}
        }
        served = self._nova.forge_plan_receipt(status_shape)
        self.assertTrue(served["current"]["approval"]["token"])

    def test_executing_an_awaiting_plan_without_its_token_is_refused(self) -> None:
        session_id = self._validated_session()["session_id"]
        with _patch_manager("plan_gate", self.AWAITING):
            refused = self._client.post(
                f"/novaforge/sessions/{session_id}/plan/execute",
                json={"plan_id": "plan-under-test", "approved": True},
            )
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertIn("approval token", refused.json()["detail"])
        self.assertEqual(self.input.presses, [], "a refused approval must dispatch nothing")

    def test_a_minted_token_executes_once_and_a_replay_is_refused(self) -> None:
        session_id = self._validated_session()["session_id"]
        token = self._minted_token()
        body = {"plan_id": "plan-under-test", "approved": True, "approval_token": token}
        with _patch_manager("plan_gate", self.AWAITING):
            first = self._client.post(
                f"/novaforge/sessions/{session_id}/plan/execute", json=body
            )
            self.assertEqual(first.status_code, 200, first.text)
            # The plan id in this fixture is not a stored plan, so the planner's
            # own answer is the refusal — the point is that the GATE let it
            # through exactly once, and that nothing reached the injector.
            self.assertEqual(first.json()["plan"]["code"], "unknown_plan")
            self.assertEqual(self.input.presses, [])
            replay = self._client.post(
                f"/novaforge/sessions/{session_id}/plan/execute", json=body
            )
        self.assertEqual(replay.status_code, 403, replay.text)

    def test_a_token_for_another_plan_does_not_authorize_this_one(self) -> None:
        session_id = self._validated_session()["session_id"]
        token = self._minted_token("plan-a")
        with _patch_manager("plan_gate", self.AWAITING):
            refused = self._client.post(
                f"/novaforge/sessions/{session_id}/plan/execute",
                json={"plan_id": "plan-b", "approved": True, "approval_token": token},
            )
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertIn("does not match", refused.json()["detail"])

    def test_an_expired_token_is_refused(self) -> None:
        session_id = self._validated_session()["session_id"]
        ttl = self._nova.approval_ttl_seconds
        self._nova.approval_ttl_seconds = -1.0  # mint an already-expired token
        try:
            token = self._minted_token()
        finally:
            self._nova.approval_ttl_seconds = ttl
        with _patch_manager("plan_gate", self.AWAITING):
            refused = self._client.post(
                f"/novaforge/sessions/{session_id}/plan/execute",
                json={"plan_id": "plan-under-test", "approved": True, "approval_token": token},
            )
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertIn("expired", refused.json()["detail"])

    def test_a_plan_that_needs_no_approval_passes_the_gate_untouched(self) -> None:
        session_id = self._validated_session()["session_id"]
        policy_free = {"status": "planned", "approval": {"required": False, "status": "not_required"}}
        with _patch_manager("plan_gate", policy_free):
            answer = self._client.post(
                f"/novaforge/sessions/{session_id}/plan/execute",
                json={"plan_id": "plan-under-test", "approved": True},
            )
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(answer.json()["plan"]["code"], "unknown_plan")

    def test_the_status_route_serves_a_usable_token_for_a_pending_plan(self) -> None:
        """A reloaded panel gets a token for the plan it is showing."""
        session_id = self._validated_session()["session_id"]
        body = {
            "current": {
                "plan_id": "plan-from-status",
                "approval": {"required": True, "status": "pending"},
            }
        }
        with _patch_manager("planning_status", body):
            served = self._client.get(f"/novaforge/sessions/{session_id}/plan")
        self.assertEqual(served.status_code, 200, served.text)
        token = served.json()["current"]["approval"]["token"]
        self.assertTrue(token)
        with _patch_manager("plan_gate", self.AWAITING):
            answer = self._client.post(
                f"/novaforge/sessions/{session_id}/plan/execute",
                json={"plan_id": "plan-from-status", "approved": True, "approval_token": token},
            )
        self.assertEqual(answer.status_code, 200, answer.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
