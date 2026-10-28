"""NF-21 — outcome verification and bounded recovery.

Three layers, each failing for its own reason:

* **Model** — contracts, observables and the policy bounds: a requirement that
  could never decide anything, a missing value and a widened bound all fail here.
* **Decision functions and the real service** — ``evaluate_requirement``,
  ``merge_verdicts``, ``classify_outcome`` and ``GameVerificationService`` over
  scripted readings, a scripted context and RECORDED recovery seams. Absence of
  evidence must never become a failure, a duplicate reading must never score
  twice, and the service has no dispatch seam at all, so no recovery path here
  can repeat an action.
* **Integration** — the real manager (plan → execute → verify) and the real HTTP
  routes. The state reader's own read surfaces are swapped for fixture readings
  (a real ``GameState`` and a real ``AnalysisOutcome``); everything else is the
  production path: the NF-20 planner, the NF-19 dispatcher, the planner's
  ``record_effects``, the session timeline and the recovery seams.

No test sends real input: the only injector is a recording one.
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

from novacontrol.novaforge import MissionValidationError, NovaForgeManager, SessionState
from novacontrol.novaforge.control import ControlAction, ControlTarget
from novacontrol.novaforge.knowledge import (
    EvidenceSource,
    KnowledgeCategory,
    KnowledgeEvidence,
    KnowledgeRecord,
    KnowledgeStatus,
    VerificationMethod,
)
from novacontrol.novaforge.objectives import interpret
from novacontrol.novaforge.planner import ActionPlan, PlanStatus, PlannedStep, RiskLevel
from novacontrol.novaforge.state_service import AnalysisOutcome, GameStateService
from novacontrol.novaforge.verification import (
    MAX_OBSERVATION_ATTEMPTS,
    MAX_VERIFICATION_MS,
    ActionOutcome,
    EffectContract,
    GameVerificationService,
    ObservableRequirement,
    ObservationEvidence,
    OutcomeStatus,
    RecoveryAction,
    RequirementVerdict,
    VerificationContext,
    VerificationPolicy,
    VerificationRule,
    VerificationRuleRegistry,
    VerificationStatus,
    classify_outcome,
    default_verification_rules,
    evaluate_requirement,
    merge_verdicts,
    requirement_problems,
    transition_pending,
)

SESSION = "s1"
AT = "2026-10-25T10:00:00+00:00"
GAME = "NC FORGE MISSION"

#: Distinguishes an omitted argument from an explicit ``None``.
_MISSING = object()

#: The objective NF-17's fixture state actually declares, so a fixture plan and
#: the interpretation of the same state agree on which objective is active.
OBJECTIVE_ID = interpret(mission_state()).objectives[0].objective_id


def control_target(**overrides: Any) -> ControlTarget:
    values: dict[str, Any] = {
        "session_id": SESSION,
        "title": "NC FORGE MISSION",
        "handle": 0x1234,
        "pid": 4242,
    }
    values.update(overrides)
    return ControlTarget(**values)


def hud_row(
    element_id: str,
    status: str,
    *,
    value: float | None = None,
    text: str = "",
    reason: str = "",
    label: str = "",
) -> dict[str, Any]:
    return {
        "element_id": element_id,
        "kind": "number",
        "label": label or element_id,
        "status": status,
        "value": value,
        "text": text or ("" if value is None else f"{value:g}"),
        "reason": reason,
    }


def evidence(
    *,
    observation_id: str = "obs-2",
    state_id: str = "state-2",
    session: str = SESSION,
    sequence: int = 2,
    age: float | None = 0.4,
    accepted: bool = True,
    code: str = "",
    reason: str = "",
    status: str = "partial",
    readings: dict[str, dict[str, Any]] | None = None,
    changes: tuple[dict[str, Any], ...] = (),
    target: dict[str, Any] | None = None,
) -> ObservationEvidence:
    return ObservationEvidence(
        observation_id=observation_id,
        state_id=state_id,
        session_id=session,
        game=GAME,
        sequence=sequence,
        at=AT,
        age_seconds=age,
        status=status,
        accepted=accepted,
        code=code,
        reason=reason,
        readings=(
            readings
            if readings is not None
            else {"progress": hud_row("progress", "recognized", value=2.0)}
        ),
        changes=changes,
        target=target
        if target is not None
        else {"handle": "4660", "pid": "4242", "start_time": "", "title": "NC FORGE MISSION"},
    )


def moving_verdict(
    *,
    field: str = "progress",
    comparison: str = "increases",
    motion: float = 2.0,
    target: float = 5.0,
) -> RequirementVerdict:
    """A REFUTED reference requirement whose field was measured moving toward its target."""
    return RequirementVerdict(
        field=field,
        comparison=comparison,
        verdict="refuted",
        observed="10 → 12",
        reason="test",
        motion=motion,
        motion_target=target,
    )


def executed_plan(
    *,
    steps: int = 1,
    dispatched: bool = True,
    ok: bool = True,
    code: str = "",
    reason: str = "test",
    status: PlanStatus = PlanStatus.EXECUTED,
    objective_id: str = OBJECTIVE_ID,
) -> ActionPlan:
    """One plan whose execution records look like NF-20's own (no parameters)."""
    plan = ActionPlan(
        plan_id="p1",
        session_id=SESSION,
        status=status,
        created_at=AT,
        game=GAME,
        objective_id=objective_id,
        objective_revision=1,
        state_id="state-1",
        observation_id="obs-1",
        state_sequence=1,
        execution={"started_at": AT, "finished_at": AT, "steps": [], "effect_verified": False},
    )
    for index in range(1, steps + 1):
        action = ControlAction.from_payload(
            {"kind": "key_hold", "key": "w", "duration_ms": 100},
            session_id=SESSION,
            target=control_target(),
        )
        plan.steps.append(
            PlannedStep(
                index=index,
                action=action,
                candidate_id=f"rule:fixture-collect@1:{index:06d}",
                source="NF-17 fixture scenario rule (not a commercial game)",
                description="Hold W for the fixture's declared control duration",
                expected_effect="The fixture's progress counter may advance by one",
                risk=RiskLevel.LOW,
                reversible=True,
            )
        )
        plan.execution["steps"].append(
            {
                "index": index,
                "action_id": action.action_id,
                "kind": "key_hold",
                "status": "completed" if ok else "failed",
                "ok": ok,
                "code": code,
                "reason": reason,
                "dispatched_at": AT if dispatched else "",
                "finished_at": AT,
            }
        )
    return plan


def make_context(
    plan: Any = _MISSING,
    *,
    baseline: dict[str, Any] | None = None,
    gate: dict[str, Any] | None = None,
    objective_state: Any | None = None,
    knowledge: tuple[KnowledgeRecord, ...] = (),
) -> VerificationContext:
    current = executed_plan() if plan is _MISSING else plan
    return VerificationContext(
        session_id=SESSION,
        game=GAME,
        plan=current,
        target={"handle": 0x1234, "pid": 4242, "title": "NC FORGE MISSION"},
        gate=gate or {"allow": True, "state": "allowed", "reason": "test gate"},
        objective_state=(
            objective_state if objective_state is not None else interpret(mission_state())
        ),
        baseline_hud=(
            baseline
            if baseline is not None
            else {"progress": hud_row("progress", "recognized", value=1.0)}
        ),
        baseline_state_id=current.state_id if current is not None else "",
        baseline_observation_id=current.observation_id if current is not None else "",
        baseline_sequence=current.state_sequence if current is not None else 0,
        knowledge=knowledge,
    )


class ScriptedObserve:
    """An observation seam that replays readings and records every call."""

    def __init__(self, items: list[Any], *, hook: Any = None) -> None:
        self.items = list(items)
        self.calls: list[str] = []
        self.hook = hook

    async def __call__(self, session_id: str) -> ObservationEvidence:
        self.calls.append(session_id)
        answer = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if self.hook is not None:
            await self.hook(len(self.calls))
        if isinstance(answer, Exception):
            raise answer
        return answer


class RecordingSeams:
    """Every recovery seam NF-21 may call, recorded and never dispatching."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.error = error
        self.plans: list[dict[str, Any]] = []

    def _record(self, name: str, session_id: str, detail: str) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append((name, session_id, detail))

    async def invalidate(self, session_id: str, plan_id: str) -> dict[str, Any]:
        self._record("invalidate", session_id, plan_id)
        return {"plan_id": plan_id, "status": "cancelled", "code": "cancelled"}

    async def replan(self, session_id: str, plan_id: str, reason: str) -> dict[str, Any]:
        self._record("replan", session_id, reason)
        return {"plan_id": "p2", "status": "awaiting_approval", "code": ""}

    async def pause(self, session_id: str, reason: str) -> dict[str, Any]:
        self._record("pause", session_id, reason)
        return {"state": "paused", "reason": reason}

    async def stop(self, session_id: str, reason: str) -> dict[str, Any]:
        self._record("stop", session_id, reason)
        return {"state": "stopped", "reason": reason}

    async def notify(self, session_id: str, payload: Any) -> dict[str, Any]:
        self._record("notify", session_id, str(payload.get("code") or ""))
        return {"notified": True, "code": payload.get("code")}

    async def record(self, session_id: str, plan_id: str, outcomes: Any) -> dict[str, Any]:
        rows = list(outcomes)
        self._record("record", session_id, plan_id)
        self.plans.append({"session_id": session_id, "plan_id": plan_id, "rows": rows})
        return {"recorded": len(rows)}


async def _no_sleep(_seconds: float) -> None:
    return None


class RecordingSleep:
    """Records how long the service asked to wait, instead of waiting."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


class SteppingClock:
    """A monotonic clock that advances on every read, so time visibly passes."""

    def __init__(self, step: float = 0.25) -> None:
        self.step = step
        self.now = 0.0

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class FailingRuleRegistry(VerificationRuleRegistry):
    """A registry whose rule lookup explodes — a verifier bug, not a verdict."""

    def matching(self, game: str, step: Any) -> list[VerificationRule]:
        raise RuntimeError("the rule registry exploded")


def make_service(
    context: VerificationContext,
    observe: Any,
    seams: RecordingSeams | None = None,
    *,
    policy: VerificationPolicy | None = None,
    rules: VerificationRuleRegistry | None = None,
    publish: Any = None,
    context_reader: Any = _MISSING,
    record: Any = _MISSING,
    invalidate: Any = _MISSING,
    replan: Any = _MISSING,
    pause: Any = _MISSING,
    stop: Any = _MISSING,
    notify: Any = _MISSING,
    monotonic: Any = None,
    sleep: Any = None,
) -> GameVerificationService:
    """The real service with every collaborator injected.

    A ``GameVerificationService`` is a slots dataclass, so every seam is passed
    here rather than assigned afterwards — which is also why the recovery seams
    are the only thing it could ever call.
    """
    seams = seams or RecordingSeams()

    async def reader(_session_id: str, _plan_id: str) -> VerificationContext:
        return context

    return GameVerificationService(
        policy=policy or VerificationPolicy(),
        context_reader=reader if context_reader is _MISSING else context_reader,
        observe=observe,
        rules=rules if rules is not None else default_verification_rules(),
        record=seams.record if record is _MISSING else record,
        invalidate=seams.invalidate if invalidate is _MISSING else invalidate,
        replan=seams.replan if replan is _MISSING else replan,
        pause=seams.pause if pause is _MISSING else pause,
        stop=seams.stop if stop is _MISSING else stop,
        notify=seams.notify if notify is _MISSING else notify,
        publish=publish,
        monotonic=monotonic or (lambda: 0.0),
        sleep=sleep or _no_sleep,
    )


def rule(
    rule_id: str,
    *requirements: ObservableRequirement,
    description: str = "test rule",
) -> VerificationRule:
    return VerificationRule(
        rule_id=rule_id,
        requirements=requirements,
        description=description,
        game_tokens=("nc forge mission",),
        kinds=("key_hold",),
        keys=("w",),
    )


class ContractModelTests(unittest.TestCase):
    def test_policy_bounds_are_validated_not_clamped(self) -> None:
        self.assertEqual(VerificationPolicy().errors(), [])
        for policy in (
            VerificationPolicy(max_steps_verified=0),
            VerificationPolicy(max_steps_verified=99),
            VerificationPolicy(max_observation_attempts=0),
            VerificationPolicy(max_observation_attempts=MAX_OBSERVATION_ATTEMPTS + 1),
            VerificationPolicy(max_verification_ms=10),
            VerificationPolicy(max_verification_ms=MAX_VERIFICATION_MS + 1),
            VerificationPolicy(max_action_ms=1),
            VerificationPolicy(max_action_ms=MAX_VERIFICATION_MS + 1),
            VerificationPolicy(max_state_age_seconds=0),
            VerificationPolicy(max_action_age_seconds=0),
            VerificationPolicy(min_change=-1),
            VerificationPolicy(confidence_threshold=2.0),
            VerificationPolicy(max_recovery_attempts=-1),
            VerificationPolicy(max_session_recoveries=99),
            VerificationPolicy(max_replans_per_plan=9),
            VerificationPolicy(max_outcomes_kept=0),
        ):
            with self.subTest(policy=policy):
                self.assertTrue(policy.errors(), policy)

    def test_a_policy_that_exceeds_its_own_action_deadline_is_refused(self) -> None:
        policy = VerificationPolicy(max_action_ms=MAX_VERIFICATION_MS, max_verification_ms=100)
        self.assertTrue(any("must not exceed" in item for item in policy.errors()), policy.errors())

    def test_a_service_with_an_invalid_policy_refuses_to_construct(self) -> None:
        with self.assertRaises(ValueError):
            make_service(
                make_context(),
                ScriptedObserve([evidence()]),
                policy=VerificationPolicy(max_verification_ms=10),
            )

    def test_an_unknown_comparison_or_a_missing_value_raises(self) -> None:
        with self.assertRaises(ValueError):
            ObservableRequirement(field="progress", comparison="increases-a-bit")
        with self.assertRaises(ValueError):
            ObservableRequirement(field="progress", comparison="equals")
        with self.assertRaises(ValueError):
            ObservableRequirement(field="", comparison="present")
        with self.assertRaises(ValueError):
            ObservableRequirement(field="progress", comparison="changes", tolerance=-1)

    def test_a_requirement_that_could_never_decide_anything_is_a_problem(self) -> None:
        vague = ObservableRequirement(field="progress", comparison="increases")
        self.assertTrue(requirement_problems(vague))
        self.assertEqual(
            requirement_problems(
                ObservableRequirement(field="progress", comparison="increases", min_change=1.0)
            ),
            [],
        )

    def test_a_rule_must_declare_a_decidable_observable(self) -> None:
        with self.assertRaises(ValueError):
            VerificationRule(rule_id="r1", requirements=(), description="nothing")
        with self.assertRaises(ValueError):
            VerificationRule(
                rule_id="r2",
                requirements=(ObservableRequirement(field="progress", comparison="increases"),),
                description="vague",
            )

    def test_rule_registry_reports_duplicates_instead_of_overwriting(self) -> None:
        duplicate = rule("dup", ObservableRequirement(field="progress", comparison="present"))
        registry = VerificationRuleRegistry([duplicate, duplicate])
        self.assertEqual(len(registry.rows()), 1)
        self.assertTrue(any(row["reason"].startswith("duplicate") for row in registry.rejected))

    def test_no_rule_ships_for_a_commercial_game(self) -> None:
        rows = default_verification_rules().rows()
        self.assertEqual(len(rows), 1)
        self.assertTrue(all(row["fixture_only"] for row in rows))
        self.assertEqual(rows[0]["observable"], "progress increases by at least 1")

    def test_a_contract_without_an_observable_cannot_be_verified(self) -> None:
        contract = EffectContract(
            contract_id="c1",
            session_id=SESSION,
            plan_id="p1",
            action_id="a1",
            step_index=1,
            kind="key_hold",
            expected_effect="the counter may advance",
            problems=("no verification rule declares an observable",),
        )
        self.assertFalse(contract.verifiable)
        status, code, reason = classify_outcome(contract, [])
        self.assertIs(status, OutcomeStatus.NOT_VERIFIABLE)
        self.assertEqual(code, "not_verifiable")
        self.assertIn("cannot be verified", reason)

    def test_a_contract_reports_its_own_problems_rather_than_raising(self) -> None:
        contract = EffectContract(
            contract_id="c1",
            session_id="",
            plan_id="",
            action_id="",
            step_index=0,
            kind="",
            expected_effect="",
            problems=("the step record names no action id",),
        )
        payload = contract.to_dict()
        self.assertFalse(payload["verifiable"])
        self.assertIn("no action id", payload["problems"][0])


class RequirementEvaluationTests(unittest.TestCase):
    def evaluate(
        self,
        requirement: ObservableRequirement,
        reading: ObservationEvidence,
        baseline: dict[str, Any] | None = None,
        *,
        pre_observation_id: str = "",
    ) -> RequirementVerdict:
        return evaluate_requirement(
            requirement,
            reading,
            baseline,
            pre_observation_id=pre_observation_id,
            min_change=1.0,
        )

    def test_an_absolute_requirement_is_decided_from_the_reading_alone(self) -> None:
        at_least = ObservableRequirement(field="progress", comparison="at_least", value=2.0)
        hit = self.evaluate(
            at_least, evidence(readings={"progress": hud_row("progress", "recognized", value=2.0)})
        )
        self.assertTrue(hit.satisfied)
        self.assertTrue(hit.direct)
        miss = self.evaluate(
            at_least, evidence(readings={"progress": hud_row("progress", "recognized", value=1.0)})
        )
        self.assertTrue(miss.refuted)
        self.assertIn("2", miss.reason)

    def test_a_field_that_was_not_read_is_indeterminate_never_refuted(self) -> None:
        for status, reason in (
            ("ambiguous", "the text was too short"),
            ("unreadable", "ocr could not run"),
            ("unsupported", "no measurement in this build"),
        ):
            with self.subTest(status=status):
                verdict = self.evaluate(
                    ObservableRequirement(field="progress", comparison="at_least", value=2.0),
                    evidence(readings={"progress": hud_row("progress", status, reason=reason)}),
                )
                self.assertEqual(verdict.verdict, "indeterminate")
                self.assertIn(status, verdict.reason)

    def test_an_undeclared_field_is_not_the_same_as_an_absent_one(self) -> None:
        present = ObservableRequirement(field="progress", comparison="present")
        absent = ObservableRequirement(field="progress", comparison="absent")
        undeclared = evidence(readings={})
        self.assertEqual(self.evaluate(present, undeclared).verdict, "indeterminate")
        self.assertEqual(self.evaluate(absent, undeclared).verdict, "indeterminate")
        read_absent = evidence(readings={"progress": hud_row("progress", "absent")})
        self.assertEqual(self.evaluate(present, read_absent).verdict, "refuted")
        self.assertEqual(self.evaluate(absent, read_absent).verdict, "satisfied")

    def test_a_relative_requirement_is_decided_by_the_pre_action_reading(self) -> None:
        increases = ObservableRequirement(field="progress", comparison="increases", min_change=1.0)
        baseline = hud_row("progress", "recognized", value=1.0)
        advanced = evidence(readings={"progress": hud_row("progress", "recognized", value=2.0)})
        verdict = self.evaluate(increases, advanced, baseline)
        self.assertTrue(verdict.satisfied)
        self.assertEqual(verdict.observed, "1 → 2")
        self.assertIn("+1", verdict.reason)
        stuck = evidence(readings={"progress": hud_row("progress", "recognized", value=1.0)})
        self.assertTrue(self.evaluate(increases, stuck, baseline).refuted)

    def test_a_measured_motion_is_reported_as_a_number_not_only_as_text(self) -> None:
        """Recovery has to see "moving, but not there yet" as a number, not a string.

        The verdict already computes the delta; keeping it structured is what lets
        a recovery decision tell a transition still in progress from an effect
        that never happened, without parsing rendered text. ``None`` means nothing
        numeric was measured (an absolute comparison has no baseline to move from).
        """
        increases = ObservableRequirement(field="progress", comparison="increases", min_change=5.0)
        baseline = hud_row("progress", "recognized", value=10.0)
        moving = self.evaluate(
            increases,
            evidence(readings={"progress": hud_row("progress", "recognized", value=12.0)}),
            baseline,
        )
        self.assertEqual(moving.verdict, "refuted")
        self.assertEqual(moving.motion, 2.0)
        self.assertEqual(moving.motion_target, 5.0)
        arrived = self.evaluate(
            increases,
            evidence(readings={"progress": hud_row("progress", "recognized", value=20.0)}),
            baseline,
        )
        self.assertEqual(arrived.verdict, "satisfied")
        self.assertEqual(arrived.motion, 10.0)
        absolute = self.evaluate(
            ObservableRequirement(field="progress", comparison="at_least", value=2.0),
            evidence(readings={"progress": hud_row("progress", "recognized", value=1.0)}),
        )
        self.assertIsNone(absolute.motion)
        self.assertIsNone(absolute.motion_target)

    def test_a_relative_requirement_without_a_baseline_is_never_refuted(self) -> None:
        verdict = self.evaluate(
            ObservableRequirement(field="progress", comparison="increases", min_change=1.0),
            evidence(readings={"progress": hud_row("progress", "recognized", value=2.0)}),
            None,
        )
        self.assertEqual(verdict.verdict, "indeterminate")
        self.assertIn("pre-action reading is not available", verdict.reason)

    def test_a_change_row_from_the_same_reading_corroborates_without_a_baseline(self) -> None:
        """NF-16's own diff of two real readings is evidence; it can never refute."""
        change = {
            "kind": "hud_value_changed",
            "field_name": "progress",
            "before": "1",
            "after": "2",
            "detail": "Progress counter changed from 1 to 2.",
            "observation_id": "obs-2",
        }
        verdict = self.evaluate(
            ObservableRequirement(
                field="progress",
                comparison="changes",
                tolerance=0.0,
                change_kinds=("hud_value_changed",),
            ),
            evidence(
                readings={"progress": hud_row("progress", "recognized", value=2.0)},
                changes=(change,),
            ),
            None,
        )
        self.assertTrue(verdict.satisfied)
        self.assertTrue(verdict.corroborated)

    def test_a_change_row_from_another_reading_is_not_used(self) -> None:
        change = {
            "kind": "hud_value_changed",
            "field_name": "progress",
            "detail": "Old reading.",
            "observation_id": "obs-OLD",
        }
        verdict = self.evaluate(
            ObservableRequirement(
                field="progress", comparison="changes", change_kinds=("hud_value_changed",)
            ),
            evidence(
                readings={"progress": hud_row("progress", "recognized", value=2.0)},
                changes=(change,),
            ),
            None,
        )
        self.assertEqual(verdict.verdict, "indeterminate")

    def test_the_pre_action_observation_cannot_show_a_change(self) -> None:
        verdict = self.evaluate(
            ObservableRequirement(field="progress", comparison="changes"),
            evidence(observation_id="obs-1"),
            hud_row("progress", "recognized", value=1.0),
            pre_observation_id="obs-1",
        )
        self.assertEqual(verdict.verdict, "indeterminate")
        self.assertIn("same observation", verdict.reason)

    def test_text_only_fields_are_compared_as_text(self) -> None:
        requirement = ObservableRequirement(field="objective", comparison="changes")
        baseline = hud_row("objective", "recognized", text="Collect the audit tokens")
        changed = evidence(
            readings={"objective": hud_row("objective", "recognized", text="Escape the facility")}
        )
        self.assertTrue(self.evaluate(requirement, changed, baseline).satisfied)

    def test_merge_keeps_the_newest_verdict_and_reports_contradictions(self) -> None:
        satisfied = RequirementVerdict(
            field="progress", comparison="increases", verdict="satisfied", observation_id="obs-2"
        )
        refuted = RequirementVerdict(
            field="progress", comparison="increases", verdict="refuted", observation_id="obs-3"
        )
        undecided = RequirementVerdict(
            field="progress", comparison="increases", verdict="indeterminate"
        )
        newest, note = merge_verdicts([[undecided], [satisfied]])
        self.assertEqual(note, "")
        self.assertEqual(len(newest), 1)
        self.assertTrue(newest[0].satisfied)  # the newest reading wins

        conflict, note = merge_verdicts([[satisfied], [refuted]])
        self.assertEqual(len(conflict), 1)
        self.assertIn("contradictory readings disagreed", note)
        self.assertTrue(conflict[0].refuted)

    def test_merging_an_empty_history_decides_nothing(self) -> None:
        verdicts, note = merge_verdicts([])
        self.assertEqual(verdicts, [])
        self.assertEqual(note, "")


class OutcomeClassificationTests(unittest.TestCase):
    def contract(self, *requirements: ObservableRequirement) -> EffectContract:
        return EffectContract(
            contract_id="c1",
            session_id=SESSION,
            plan_id="p1",
            action_id="a1",
            step_index=1,
            kind="key_hold",
            expected_effect="the counter advances",
            requirements=requirements
            or (ObservableRequirement(field="progress", comparison="present"),),
        )

    def verdict(self, value: str, field_name: str = "progress") -> RequirementVerdict:
        return RequirementVerdict(field=field_name, comparison="present", verdict=value)

    def test_only_a_full_set_of_satisfied_observables_is_a_success(self) -> None:
        contract = self.contract(
            ObservableRequirement(field="progress", comparison="present"),
            ObservableRequirement(field="objective", comparison="present"),
        )
        status, code, _ = classify_outcome(
            contract, [self.verdict("satisfied"), self.verdict("satisfied", "objective")]
        )
        self.assertIs(status, OutcomeStatus.VERIFIED_SUCCESS)
        self.assertEqual(code, "effect_observed")

    def test_a_refuted_observable_is_a_verified_failure(self) -> None:
        status, code, reason = classify_outcome(self.contract(), [self.verdict("refuted")])
        self.assertIs(status, OutcomeStatus.VERIFIED_FAILURE)
        self.assertEqual(code, "effect_not_observed")
        self.assertIn("did not meet", reason)

    def test_a_partly_supported_effect_is_partial_not_success(self) -> None:
        contract = self.contract(
            ObservableRequirement(field="progress", comparison="present"),
            ObservableRequirement(field="objective", comparison="present"),
        )
        status, code, reason = classify_outcome(
            contract, [self.verdict("satisfied"), self.verdict("indeterminate", "objective")]
        )
        self.assertIs(status, OutcomeStatus.PARTIAL_SUCCESS)
        self.assertEqual(code, "partial_effect_observed")
        self.assertIn("objective", reason)

    def test_nothing_decided_is_unknown_and_not_a_failure(self) -> None:
        status, code, reason = classify_outcome(
            self.contract(), [self.verdict("indeterminate")], attempts=3
        )
        self.assertIs(status, OutcomeStatus.OUTCOME_UNKNOWN)
        self.assertEqual(code, "evidence_insufficient")
        self.assertIn("missing evidence is not negative evidence", reason)

    def test_a_deadline_is_its_own_status(self) -> None:
        status, code, _ = classify_outcome(
            self.contract(), [self.verdict("indeterminate")], attempts=3, timed_out=True
        )
        self.assertIs(status, OutcomeStatus.VERIFICATION_TIMED_OUT)
        self.assertEqual(code, "verification_deadline")

    def test_an_aborted_run_outranks_every_other_reading(self) -> None:
        status, code, reason = classify_outcome(
            self.contract(), [self.verdict("satisfied")], aborted="the caller cancelled"
        )
        self.assertIs(status, OutcomeStatus.VERIFICATION_ABORTED)
        self.assertEqual(code, "aborted")
        self.assertEqual(reason, "the caller cancelled")


class ServiceVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_fixture_step_whose_counter_advanced_is_verified_success(self) -> None:
        plan = executed_plan()
        context = make_context(plan)
        observe = ScriptedObserve([evidence()])
        service = make_service(context, observe)
        result = await service.verify(SESSION, plan.plan_id)
        self.assertIs(result.status, VerificationStatus.VERIFIED)
        self.assertTrue(result.effect_verified)
        self.assertEqual(result.steps_verified, 1)
        outcome = result.outcomes[0]
        self.assertIs(outcome.status, OutcomeStatus.VERIFIED_SUCCESS)
        self.assertEqual(outcome.action_id, plan.steps[0].action.action_id)
        self.assertEqual(outcome.expected_effect, plan.steps[0].expected_effect)
        self.assertEqual(outcome.confidence, 1.0)
        self.assertIn("heuristic", outcome.confidence_basis)
        self.assertIn("not a calibrated probability", outcome.confidence_basis)
        self.assertIn(
            outcome.objective_id,
            {item.objective_id for item in context.objective_state.objectives},
        )
        self.assertIs(RecoveryAction(outcome.recovery["action"]), RecoveryAction.NONE)
        self.assertFalse(outcome.recovery["repeats_action"])
        self.assertEqual(observe.calls, [SESSION])

    async def test_the_outcomes_reach_the_planner_through_its_own_record(self) -> None:
        plan = executed_plan()
        seams = RecordingSeams()
        service = make_service(make_context(plan), ScriptedObserve([evidence()]), seams)
        result = await service.verify(SESSION, plan.plan_id)
        self.assertEqual([call for call in seams.calls if call[0] == "record"], [("record", SESSION, plan.plan_id)])
        rows = seams.plans[0]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action_id"], plan.steps[0].action.action_id)
        self.assertTrue(rows[0]["effect_verified"])
        self.assertEqual(rows[0]["status"], "verified_success")
        self.assertTrue(rows[0]["evidence_refs"])
        self.assertEqual(result.outcomes[0].outcome_id, rows[0]["outcome_id"])

    async def test_a_failing_record_seam_never_becomes_the_verdict(self) -> None:
        plan = executed_plan()

        async def broken(_session: str, _plan: str, _rows: Any) -> dict[str, Any]:
            raise RuntimeError("the plan store exploded")

        service = make_service(make_context(plan), ScriptedObserve([evidence()]), record=broken)
        result = await service.verify(SESSION, plan.plan_id)
        self.assertIs(result.status, VerificationStatus.VERIFIED)
        self.assertTrue(
            any("could not be given these outcomes" in item for item in result.limitations),
            result.limitations,
        )

    async def test_a_step_that_never_dispatched_is_skipped_never_failed(self) -> None:
        plan = executed_plan(dispatched=False)
        observe = ScriptedObserve([evidence()])
        service = make_service(make_context(plan), observe)
        result = await service.verify(SESSION, plan.plan_id)
        self.assertEqual(result.outcomes, [])
        self.assertEqual(result.steps_skipped, 1)
        self.assertEqual(result.steps[0]["code"], "not_dispatched")
        self.assertEqual(result.steps[0]["status"], "skipped")
        self.assertEqual(result.status, VerificationStatus.REFUSED)
        self.assertEqual(result.code, "no_dispatched_steps")
        self.assertFalse(result.effect_verified)
        self.assertEqual(observe.calls, [], "a step that never ran has no outcome to observe")

    async def test_a_dispatched_and_a_skipped_step_are_both_reported_honestly(self) -> None:
        plan = executed_plan(steps=2)
        plan.execution["steps"][1]["dispatched_at"] = ""
        service = make_service(make_context(plan), ScriptedObserve([evidence()]))
        result = await service.verify(SESSION, plan.plan_id)
        self.assertEqual(result.steps_verified, 1)
        self.assertEqual(result.steps_skipped, 1)
        self.assertEqual(len(result.outcomes), 1)
        self.assertEqual(result.steps[1]["status"], "skipped")
        self.assertTrue(any("skipped" in item for item in result.reasons))

    async def test_more_dispatched_steps_than_the_bound_reports_the_cap(self) -> None:
        plan = executed_plan(steps=3)
        service = make_service(
            make_context(plan),
            ScriptedObserve([evidence()]),
            policy=VerificationPolicy(max_steps_verified=1),
        )
        result = await service.verify(SESSION, plan.plan_id)
        self.assertEqual(result.steps_capped, 2)
        self.assertEqual(result.steps_verified, 1)
        self.assertTrue(any("capped" in item for item in result.reasons), result.reasons)

    async def test_a_contract_is_built_from_the_plan_step_it_came_from(self) -> None:
        """An execution row alone carries no kind, parameters or source to match on."""
        plan = executed_plan()
        context = make_context(plan)
        service = make_service(context, ScriptedObserve([evidence()]))
        view = service._step_view(plan, plan.execution["steps"][0])
        self.assertEqual(view["kind"], "key_hold")
        self.assertEqual(view["parameters"]["key"], "w")
        self.assertIn("fixture-collect@1", view["source"])
        contract = service.contract_for(context, plan.execution["steps"][0], plan=plan)
        self.assertTrue(contract.verifiable, contract.problems)
        self.assertTrue(contract.requirements[0].relative)
        self.assertEqual(contract.pre_observation_id, "obs-1")
        self.assertEqual(contract.expected_effect, plan.steps[0].expected_effect)

    async def test_a_declared_counter_that_did_not_advance_is_a_verified_failure(self) -> None:
        plan = executed_plan()
        same = evidence(readings={"progress": hud_row("progress", "recognized", value=1.0)})
        service = make_service(make_context(plan), ScriptedObserve([same]))
        result = await service.verify(SESSION, plan.plan_id)
        self.assertIs(result.status, VerificationStatus.FAILED)
        self.assertFalse(result.effect_verified)
        outcome = result.outcomes[0]
        self.assertIs(outcome.status, OutcomeStatus.VERIFIED_FAILURE)
        self.assertEqual(outcome.code, "effect_not_observed")
        self.assertEqual(outcome.confidence, 0.0)
        self.assertIs(RecoveryAction(outcome.recovery["action"]), RecoveryAction.REPLAN)
        self.assertTrue(outcome.recovery["requires_approval"])
        self.assertTrue(outcome.recovery["allowed"])

    async def test_an_unreadable_counter_is_unknown_and_never_a_failure(self) -> None:
        plan = executed_plan()
        unreadable = evidence(
            readings={"progress": hud_row("progress", "unreadable", reason="no ocr engine")}
        )
        service = make_service(make_context(plan), ScriptedObserve([unreadable]))
        result = await service.verify(SESSION, plan.plan_id)
        outcome = result.outcomes[0]
        self.assertIs(outcome.status, OutcomeStatus.OUTCOME_UNKNOWN)
        self.assertEqual(outcome.code, "evidence_insufficient")
        self.assertEqual(outcome.confidence, 0.0)
        self.assertEqual(outcome.observations, VerificationPolicy().max_observation_attempts)
        self.assertEqual(result.status, VerificationStatus.UNKNOWN)
        self.assertFalse(result.effect_verified)
        # Every bounded reading is spent, so the honest next step is a person —
        # not another read, and certainly not the action again.
        self.assertIs(
            RecoveryAction(outcome.recovery["action"]), RecoveryAction.REQUEST_OPERATOR
        )
        self.assertEqual(outcome.recovery["code"], "evidence_exhausted")
        self.assertFalse(outcome.recovery["repeats_action"])

    async def test_a_repeated_reading_is_a_duplicate_not_a_second_score(self) -> None:
        """A relative observable with no baseline never decides, so a second
        reading is taken — and the SAME reading must not score twice."""
        plan = executed_plan()
        rules = VerificationRuleRegistry(
            (rule("needs-a-baseline", ObservableRequirement(field="progress", comparison="increases", min_change=1.0)),)
        )
        same = evidence(readings={"progress": hud_row("progress", "recognized", value=2.0)})
        service = make_service(
            make_context(plan, baseline={}),
            ScriptedObserve([same]),
            rules=rules,
            policy=VerificationPolicy(max_observation_attempts=2),
        )
        result = await service.verify(SESSION, plan.plan_id)
        outcome = result.outcomes[0]
        self.assertEqual(len(outcome.evidence), 1)
        self.assertEqual(outcome.rejected[0]["code"], "duplicate_reading")
        self.assertIs(outcome.status, OutcomeStatus.OUTCOME_UNKNOWN)
        self.assertEqual(outcome.observations, 2)

    async def test_conflicting_readings_are_unknown_not_a_verdict(self) -> None:
        """One requirement is decided by both attempts and disagrees with itself."""
        plan = executed_plan()
        rules = VerificationRuleRegistry(
            (
                rule(
                    "strict-counter",
                    ObservableRequirement(field="progress", comparison="at_least", value=2.0),
                    ObservableRequirement(field="objective", comparison="increases", min_change=1.0),
                ),
            )
        )
        first = evidence(
            observation_id="obs-2",
            readings={"progress": hud_row("progress", "recognized", value=2.5)},
        )
        second = evidence(
            observation_id="obs-3",
            sequence=3,
            readings={"progress": hud_row("progress", "recognized", value=0.0)},
        )
        service = make_service(make_context(plan), ScriptedObserve([first, second]), rules=rules)
        result = await service.verify(SESSION, plan.plan_id)
        outcome = result.outcomes[0]
        self.assertIs(outcome.status, OutcomeStatus.OUTCOME_UNKNOWN)
        self.assertEqual(outcome.code, "conflicting_evidence")
        self.assertIn("contradictory readings disagreed", outcome.ambiguity)
        self.assertEqual([row["observation_id"] for row in outcome.evidence], ["obs-2", "obs-3"])

    async def test_a_stale_reading_is_rejected_and_drives_a_refresh(self) -> None:
        plan = executed_plan()
        stale = evidence(age=99.0, accepted=False, code="stale_reading", reason="9s old")
        unreadable = evidence(readings={"progress": hud_row("progress", "ambiguous", reason="blur")})
        service = make_service(
            make_context(plan),
            ScriptedObserve([stale, unreadable]),
            policy=VerificationPolicy(max_observation_attempts=2),
        )
        result = await service.verify(SESSION, plan.plan_id)
        outcome = result.outcomes[0]
        self.assertEqual(outcome.rejected[0]["code"], "stale_reading")
        self.assertIs(outcome.status, OutcomeStatus.OUTCOME_UNKNOWN)
        self.assertIs(
            RecoveryAction(outcome.recovery["action"]), RecoveryAction.REFRESH_OBSERVATION
        )
        self.assertEqual(outcome.recovery["code"], "stale_evidence")

    async def test_a_screen_that_is_only_ever_stale_pauses_the_session(self) -> None:
        """No frame was ever usable, so another read of the same screen cannot help.

        The pause goes through the session's OWN interface, dispatches no input, and
        tells the operator why — the spec's safe pause rather than a doomed refresh.
        """
        plan = executed_plan()
        stale = evidence(age=99.0, accepted=False, code="stale_reading", reason="9s old")
        policy = VerificationPolicy(max_observation_attempts=1)
        service = make_service(make_context(plan), ScriptedObserve([stale]), policy=policy)
        result = await service.verify(SESSION, plan.plan_id)
        outcome = result.outcomes[0]
        self.assertIs(outcome.status, OutcomeStatus.OUTCOME_UNKNOWN)
        self.assertEqual(outcome.evidence, ())
        self.assertFalse(outcome.verified_success)
        self.assertIs(RecoveryAction(outcome.recovery["action"]), RecoveryAction.PAUSE_SESSION)
        self.assertEqual(outcome.recovery["code"], "evidence_stale")
        self.assertTrue(outcome.recovery["requires_operator"])
        seams = RecordingSeams()
        service = make_service(
            make_context(plan), ScriptedObserve([stale]), seams, policy=policy
        )
        again = await service.verify(SESSION, plan.plan_id)
        applied = await service.recover(SESSION, again.outcomes[0].outcome_id, approve=True)
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["code"], "session_paused")
        self.assertIn(("pause", SESSION, "outcome_recovery"), seams.calls)

    async def test_a_reading_from_another_session_aborts_the_verification(self) -> None:
        plan = executed_plan()
        service = make_service(make_context(plan), ScriptedObserve([evidence(session="other")]))
        result = await service.verify(SESSION, plan.plan_id)
        outcome = result.outcomes[0]
        self.assertIs(outcome.status, OutcomeStatus.VERIFICATION_ABORTED)
        self.assertEqual(outcome.rejected[0]["code"], "wrong_session")
        self.assertEqual(result.status, VerificationStatus.ABORTED)
        self.assertFalse(result.effect_verified)

    async def test_a_reading_from_another_target_stops_the_operation(self) -> None:
        plan = executed_plan()
        moved = evidence(target={"handle": "999", "pid": "4242", "start_time": ""})
        service = make_service(make_context(plan), ScriptedObserve([moved]))
        result = await service.verify(SESSION, plan.plan_id)
        outcome = result.outcomes[0]
        self.assertEqual(outcome.rejected[0]["code"], "wrong_target")
        self.assertIs(RecoveryAction(outcome.recovery["action"]), RecoveryAction.STOP_OPERATION)
        self.assertTrue(outcome.recovery["allowed"])

    async def test_a_reading_with_no_readable_timestamp_is_rejected(self) -> None:
        plan = executed_plan()
        service = make_service(
            make_context(plan),
            ScriptedObserve([evidence(age=None)]),
            policy=VerificationPolicy(max_observation_attempts=1),
        )
        result = await service.verify(SESSION, plan.plan_id)
        self.assertEqual(result.outcomes[0].rejected[0]["code"], "age_unknown")

    async def test_an_older_reading_never_overrides_the_pre_action_state(self) -> None:
        plan = executed_plan()
        plan.state_sequence = 2
        service = make_service(
            make_context(plan),
            ScriptedObserve([evidence(observation_id="obs-0", state_id="state-0", sequence=1)]),
            policy=VerificationPolicy(max_observation_attempts=1),
        )
        result = await service.verify(SESSION, plan.plan_id)
        self.assertEqual(result.outcomes[0].rejected[0]["code"], "superseded_reading")

    async def test_a_timeout_is_its_own_status_and_is_not_retried(self) -> None:
        plan = executed_plan()
        clock = {"now": 0.0}

        def monotonic() -> float:
            return clock["now"]

        async def sleep(seconds: float) -> None:
            clock["now"] += seconds

        unresolved = evidence(readings={"progress": hud_row("progress", "ambiguous", reason="blur")})
        # The CONTRACT's own deadline is what expires here, and it is short enough
        # that the wait before a second reading no longer fits inside it.
        tight = rule(
            "tight-deadline",
            ObservableRequirement(field="progress", comparison="increases", min_change=1.0),
        )
        tight = dataclasses.replace(tight, settle_ms=0, deadline_ms=100)
        service = make_service(
            make_context(plan),
            ScriptedObserve([unresolved]),
            rules=VerificationRuleRegistry((tight,)),
            policy=VerificationPolicy(
                max_verification_ms=1000,
                max_action_ms=100,
                observation_interval_ms=60,
                settle_ms=0,
                max_observation_attempts=3,
            ),
            monotonic=monotonic,
            sleep=sleep,
        )
        result = await service.verify(SESSION, plan.plan_id)
        outcome = result.outcomes[0]
        self.assertIs(outcome.status, OutcomeStatus.VERIFICATION_TIMED_OUT)
        self.assertEqual(outcome.code, "verification_deadline")
        self.assertIs(RecoveryAction(outcome.recovery["action"]), RecoveryAction.REQUEST_OPERATOR)
        self.assertTrue(outcome.recovery["requires_operator"])
        self.assertEqual(result.status, VerificationStatus.TIMED_OUT)

    async def test_the_settle_window_is_awaited_not_blocking_the_loop(self) -> None:
        plan = executed_plan()
        waits: list[float] = []

        async def sleep(seconds: float) -> None:
            waits.append(seconds)

        service = make_service(
            make_context(plan),
            ScriptedObserve([evidence()]),
            policy=VerificationPolicy(settle_ms=250),
            sleep=sleep,
        )
        await service.verify(SESSION, plan.plan_id)
        self.assertEqual(waits, [0.25])

    async def test_cancelling_a_verification_stops_it_at_its_checkpoint(self) -> None:
        plan = executed_plan(steps=2)
        holder: dict[str, Any] = {}

        async def cancel_on_first(call: int) -> None:
            if call == 1:
                await holder["service"].cancel(SESSION)

        observe = ScriptedObserve([evidence()], hook=cancel_on_first)
        service = make_service(make_context(plan), observe)
        holder["service"] = service
        result = await service.verify(SESSION, plan.plan_id)
        self.assertEqual(len(result.outcomes), 1)
        self.assertEqual(observe.calls, [SESSION], "the loop must stop before the next read")
        self.assertTrue(any("cancelled" in item for item in result.reasons), result.reasons)

    async def test_cancelling_an_idle_service_is_an_honest_no_op(self) -> None:
        service = make_service(make_context(), ScriptedObserve([evidence()]))
        answer = await service.cancel(SESSION)
        self.assertFalse(answer["cancel_requested"])
        self.assertFalse(answer["running"])

    async def test_a_refused_gate_aborts_before_any_reading(self) -> None:
        plan = executed_plan()
        observe = ScriptedObserve([evidence()])
        gate = {"allow": False, "state": "paused", "reason": "the session is paused"}
        service = make_service(make_context(plan, gate=gate), observe)
        result = await service.verify(SESSION, plan.plan_id)
        self.assertEqual(result.status, VerificationStatus.ABORTED)
        self.assertEqual(result.code, "paused")
        self.assertEqual(observe.calls, [])
        self.assertEqual(result.counts["total"], 0)

    async def test_an_unknown_plan_or_a_missing_execution_is_refused(self) -> None:
        observe = ScriptedObserve([evidence()])
        service = make_service(make_context(None), observe)
        unknown = await service.verify(SESSION, "nope")
        self.assertEqual(unknown.status, VerificationStatus.REFUSED)
        self.assertEqual(unknown.code, "unknown_plan")

        empty = executed_plan()
        empty.execution["steps"] = []
        service = make_service(make_context(empty), observe)
        missing = await service.verify(SESSION, empty.plan_id)
        self.assertEqual(missing.code, "no_execution_record")
        self.assertEqual(observe.calls, [])

    async def test_a_plan_from_another_session_is_refused(self) -> None:
        plan = executed_plan()
        plan.session_id = "other"
        service = make_service(make_context(plan), ScriptedObserve([evidence()]))
        result = await service.verify(SESSION, plan.plan_id)
        self.assertEqual(result.code, "unknown_plan")

    async def test_one_verification_at_a_time_and_a_stopped_service(self) -> None:
        plan = executed_plan()
        gate = asyncio.Event()
        entered = asyncio.Event()

        class Blocking:
            def __init__(self) -> None:
                self.calls = 0

            async def __call__(self, _session: str) -> ObservationEvidence:
                self.calls += 1
                entered.set()
                await gate.wait()
                return evidence()

        blocking = Blocking()
        service = make_service(make_context(plan), blocking)
        task = asyncio.create_task(service.verify(SESSION, plan.plan_id))
        await asyncio.wait_for(entered.wait(), timeout=2)
        busy = await service.verify(SESSION, plan.plan_id)
        self.assertEqual(busy.code, "busy")
        self.assertEqual(busy.status, VerificationStatus.REFUSED)
        self.assertTrue(service.running)
        gate.set()
        await asyncio.wait_for(task, timeout=2)
        self.assertFalse(service.running)

        await service.shutdown()
        stopped = await service.verify(SESSION, plan.plan_id)
        self.assertEqual(stopped.code, "service_stopped")
        self.assertTrue(service.status()["stopped"])

    async def test_an_unreadable_context_is_a_refusal_not_a_crash(self) -> None:
        async def broken(_session: str, _plan: str) -> VerificationContext:
            raise RuntimeError("no evidence store")

        service = make_service(
            make_context(), ScriptedObserve([evidence()]), context_reader=broken
        )
        result = await service.verify(SESSION, "p1")
        self.assertEqual(result.status, VerificationStatus.REFUSED)
        self.assertEqual(result.code, "context_unavailable")
        self.assertIn("RuntimeError", result.reason)

    async def test_a_verifier_bug_is_never_reported_as_a_verdict(self) -> None:
        plan = executed_plan()
        service = make_service(
            make_context(plan), ScriptedObserve([evidence()]), rules=FailingRuleRegistry(())
        )
        result = await service.verify(SESSION, plan.plan_id)
        self.assertEqual(result.status, VerificationStatus.REFUSED)
        self.assertEqual(result.code, "verification_error")
        self.assertEqual(result.outcomes, [])
        self.assertFalse(result.effect_verified)

    async def test_an_observation_seam_that_raises_is_rejected_evidence(self) -> None:
        plan = executed_plan()
        service = make_service(
            make_context(plan),
            ScriptedObserve([RuntimeError("the state reader exploded")]),
            policy=VerificationPolicy(max_observation_attempts=1),
        )
        result = await service.verify(SESSION, plan.plan_id)
        outcome = result.outcomes[0]
        self.assertEqual(outcome.rejected[0]["code"], "observation_failed")
        self.assertIs(outcome.status, OutcomeStatus.OUTCOME_UNKNOWN)

    async def test_a_knowledge_record_that_declares_its_observable_is_a_contract(self) -> None:
        record = _control_record(
            {
                "kind": "key_hold",
                "key": "w",
                "effect": "the counter advances",
                "observable": {"field": "progress", "comparison": "increases", "min_change": 1.0},
            }
        )
        plan = _plan_from_knowledge()
        service = make_service(
            make_context(plan, knowledge=(record,)),
            ScriptedObserve([evidence()]),
            rules=VerificationRuleRegistry(()),
        )
        result = await service.verify(SESSION, plan.plan_id)
        self.assertEqual(result.steps_verified, 1)
        outcome = result.outcomes[0]
        self.assertIs(outcome.status, OutcomeStatus.VERIFIED_SUCCESS)
        self.assertEqual(outcome.knowledge_refs, (f"knowledge:{record.knowledge_id}",))

    async def test_a_verified_control_without_an_observable_is_not_verifiable(self) -> None:
        record = _control_record({"kind": "key_hold", "key": "w", "effect": "advance"})
        plan = _plan_from_knowledge()
        service = make_service(
            make_context(plan, knowledge=(record,)),
            ScriptedObserve([evidence()]),
            rules=VerificationRuleRegistry(()),
        )
        result = await service.verify(SESSION, plan.plan_id)
        outcome = result.outcomes[0]
        self.assertIs(outcome.status, OutcomeStatus.NOT_VERIFIABLE)
        self.assertIn("declares no `observable`", outcome.reason)
        self.assertIs(RecoveryAction(outcome.recovery["action"]), RecoveryAction.REQUEST_OPERATOR)
        self.assertTrue(outcome.recovery["requires_operator"])

    async def test_an_unverified_knowledge_record_is_not_a_contract(self) -> None:
        record = _control_record(
            {
                "kind": "key_hold",
                "key": "w",
                "effect": "advance",
                "observable": {"field": "progress", "comparison": "present"},
            },
            status=KnowledgeStatus.PROVISIONAL,
        )
        plan = _plan_from_knowledge()
        service = make_service(
            make_context(plan, knowledge=(record,)),
            ScriptedObserve([evidence()]),
            rules=VerificationRuleRegistry(()),
        )
        result = await service.verify(SESSION, plan.plan_id)
        outcome = result.outcomes[0]
        self.assertIs(outcome.status, OutcomeStatus.NOT_VERIFIABLE)
        self.assertIn("no verification rule", outcome.reason)

    async def test_two_rules_that_both_claim_a_step_are_refused_not_guessed(self) -> None:
        rules = VerificationRuleRegistry(
            tuple(
                rule(f"r{index}", ObservableRequirement(field="progress", comparison="present"))
                for index in (1, 2)
            )
        )
        plan = executed_plan()
        service = make_service(make_context(plan), ScriptedObserve([evidence()]), rules=rules)
        result = await service.verify(SESSION, plan.plan_id)
        outcome = result.outcomes[0]
        self.assertIs(outcome.status, OutcomeStatus.NOT_VERIFIABLE)
        self.assertIn("refusing to guess", outcome.reason)

    async def test_a_step_no_rule_covers_is_not_verifiable(self) -> None:
        plan = executed_plan()
        service = make_service(
            make_context(plan),
            ScriptedObserve([evidence()]),
            rules=VerificationRuleRegistry(()),
        )
        result = await service.verify(SESSION, plan.plan_id)
        outcome = result.outcomes[0]
        self.assertIs(outcome.status, OutcomeStatus.NOT_VERIFIABLE)
        self.assertIn("no verification rule", outcome.reason)
        self.assertIsNone(outcome.confidence)

    async def test_status_history_and_outcomes_are_readable(self) -> None:
        plan = executed_plan()
        service = make_service(make_context(plan), ScriptedObserve([evidence()]))
        result = await service.verify(SESSION, plan.plan_id)
        status = service.status(SESSION)
        self.assertTrue(status["available"])
        self.assertEqual(status["policy"]["max_recovery_attempts"], 2)
        self.assertEqual(len(status["rules"]), 1)
        self.assertEqual(status["counts"]["runs"], 1)
        self.assertEqual(status["counts"][OutcomeStatus.VERIFIED_SUCCESS.value], 1)
        self.assertEqual(len(service.outcomes(SESSION, 5)), 1)
        self.assertEqual(len(service.history(SESSION, 5)), 1)
        self.assertEqual(service.history(SESSION)[0]["verification_id"], result.verification_id)
        self.assertEqual(
            service.outcome(result.outcomes[0].outcome_id).status, OutcomeStatus.VERIFIED_SUCCESS
        )
        self.assertTrue(status["limitations"])
        self.assertTrue(status["policy"]["repetition_note"])

    async def test_a_verification_edge_reaches_the_publish_seam(self) -> None:
        plan = executed_plan()
        seen: list[tuple[str, dict[str, Any]]] = []

        async def publish(event_type: str, payload: dict[str, Any]) -> None:
            seen.append((event_type, payload))

        service = make_service(make_context(plan), ScriptedObserve([evidence()]), publish=publish)
        await service.verify(SESSION, plan.plan_id)
        self.assertEqual(
            [row[0] for row in seen],
            ["novaforge.outcome.started", "novaforge.outcome.verified"],
        )
        payload = seen[-1][1]
        self.assertEqual(payload["session_id"], SESSION)
        self.assertEqual(payload["status"], "verified")
        self.assertTrue(payload["effect_verified"])

    async def test_a_broken_publish_seam_never_fails_a_verification(self) -> None:
        plan = executed_plan()

        async def publish(_event_type: str, _payload: dict[str, Any]) -> None:
            raise RuntimeError("the bus is gone")

        service = make_service(make_context(plan), ScriptedObserve([evidence()]), publish=publish)
        result = await service.verify(SESSION, plan.plan_id)
        self.assertEqual(result.status, VerificationStatus.VERIFIED)


class RecoveryDecisionTests(unittest.IsolatedAsyncioTestCase):
    def make_outcome(
        self,
        status: OutcomeStatus,
        *,
        plan: ActionPlan | None = None,
        code: str = "",
        attempts: int = 1,
        observations: int | None = None,
        objective_id: str = "",
        rejected: tuple[dict[str, Any], ...] = (),
        verdicts: tuple[RequirementVerdict, ...] = (),
    ) -> ActionOutcome:
        current = plan if plan is not None else executed_plan()
        return ActionOutcome(
            outcome_id="o1",
            session_id=SESSION,
            plan_id=current.plan_id,
            action_id=current.steps[0].action.action_id,
            step_index=1,
            status=status,
            code=code,
            reason="test",
            rejected=rejected,
            verdicts=verdicts,
            attempts=attempts,
            observations=attempts if observations is None else observations,
            objective_id=objective_id,
        )

    def test_success_needs_no_recovery(self) -> None:
        plan = executed_plan()
        context = make_context(plan)
        service = make_service(context, ScriptedObserve([evidence()]))
        decision = service.decide_recovery(self.make_outcome(OutcomeStatus.VERIFIED_SUCCESS), context)
        self.assertIs(decision.action, RecoveryAction.NONE)
        self.assertFalse(decision.requires_approval)
        self.assertFalse(decision.requires_operator)
        self.assertFalse(decision.repeats_action)

    def test_a_not_verifiable_action_escalates_to_a_person(self) -> None:
        plan = executed_plan()
        context = make_context(plan)
        service = make_service(context, ScriptedObserve([evidence()]))
        decision = service.decide_recovery(
            self.make_outcome(OutcomeStatus.NOT_VERIFIABLE), context, recovery_eligible=False
        )
        self.assertIs(decision.action, RecoveryAction.REQUEST_OPERATOR)
        self.assertTrue(decision.requires_operator)

    def test_a_failed_action_may_be_replanned_but_never_repeated(self) -> None:
        plan = executed_plan(ok=False, code="failed")
        context = make_context(plan)
        service = make_service(context, ScriptedObserve([evidence()]))
        decision = service.decide_recovery(
            self.make_outcome(OutcomeStatus.VERIFIED_FAILURE, plan=plan, code="failed"), context
        )
        self.assertIs(decision.action, RecoveryAction.REPLAN)
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.requires_approval)
        self.assertFalse(decision.repeats_action)

    def test_every_declared_recovery_response_is_reachable_from_a_rule(self) -> None:
        """A response no rule can select is dead weight; this pins the whole vocabulary.

        Scope E declares these responses, so each must be reachable from evidence. Two
        of them were not (`wait_for_transition`, `pause_session`), which is why this
        structural test exists rather than a per-response assertion.
        """
        failed = executed_plan(ok=False, code="failed")
        refused = executed_plan(ok=False, code="target_changed")
        plain = make_context(failed)
        unsafe = make_context(
            failed, gate={"allow": False, "state": "target_lost", "reason": "gone"}
        )
        stale = ({"code": "stale_reading", "reason": "9s old"},)
        scenarios: list[tuple[ActionOutcome, VerificationContext, dict[str, Any]]] = [
            (self.make_outcome(OutcomeStatus.VERIFIED_SUCCESS, plan=failed), plain, {}),
            (
                self.make_outcome(OutcomeStatus.OUTCOME_UNKNOWN, plan=failed, observations=1),
                plain,
                {},
            ),
            (
                self.make_outcome(
                    OutcomeStatus.VERIFIED_FAILURE,
                    plan=failed,
                    code="failed",
                    verdicts=(moving_verdict(),),
                ),
                plain,
                {},
            ),
            (
                self.make_outcome(
                    OutcomeStatus.PARTIAL_SUCCESS,
                    plan=failed,
                    verdicts=(moving_verdict(motion=0.0),),
                ),
                plain,
                {},
            ),
            (
                self.make_outcome(
                    OutcomeStatus.VERIFIED_FAILURE, plan=refused, code="target_changed"
                ),
                make_context(refused),
                {},
            ),
            (
                self.make_outcome(OutcomeStatus.VERIFIED_FAILURE, plan=failed, code="failed"),
                plain,
                {},
            ),
            (
                self.make_outcome(
                    OutcomeStatus.OUTCOME_UNKNOWN,
                    plan=failed,
                    observations=VerificationPolicy().max_observation_attempts,
                    rejected=stale,
                ),
                plain,
                {},
            ),
            (
                self.make_outcome(OutcomeStatus.NOT_VERIFIABLE, plan=failed),
                plain,
                {"recovery_eligible": False},
            ),
            (self.make_outcome(OutcomeStatus.OUTCOME_UNKNOWN, plan=failed), unsafe, {}),
        ]
        reached = {
            make_service(context, ScriptedObserve([evidence()]))
            .decide_recovery(outcome, context, **extra)
            .action
            for outcome, context, extra in scenarios
        }
        self.assertEqual(reached, set(RecoveryAction))

    def test_a_transition_still_arriving_is_waited_for_and_never_replanned(self) -> None:
        """An effect that is visibly arriving is not a failed action.

        The contract measured the declared field moving toward its target, short of
        the declared amount: the bounded response is to wait out the action's own
        settle window and read again — never to replan around an action that is
        working, and never to repeat it.
        """
        plan = executed_plan(ok=False, code="failed")
        context = make_context(plan)
        service = make_service(context, ScriptedObserve([evidence()]))
        outcome = self.make_outcome(
            OutcomeStatus.VERIFIED_FAILURE,
            plan=plan,
            code="failed",
            verdicts=(moving_verdict(),),
        )
        decision = service.decide_recovery(outcome, context)
        self.assertIs(decision.action, RecoveryAction.WAIT_FOR_TRANSITION)
        self.assertEqual(decision.code, "delayed_transition")
        self.assertFalse(decision.requires_approval)
        self.assertFalse(decision.repeats_action)
        # A wait that did not resolve the outcome is never granted twice.
        service._transition_waits[outcome.action_id] = 1
        again = service.decide_recovery(outcome, context)
        self.assertIs(again.action, RecoveryAction.REPLAN)

    def test_a_transition_wait_is_not_granted_when_the_field_did_not_move(self) -> None:
        plan = executed_plan(ok=False, code="failed")
        context = make_context(plan)
        service = make_service(context, ScriptedObserve([evidence()]))
        for verdict in (
            moving_verdict(motion=0.0),
            moving_verdict(comparison="decreases", motion=2.0),
            RequirementVerdict(
                field="progress",
                comparison="increases",
                verdict="satisfied",
                motion=9.0,
                motion_target=5.0,
            ),
            RequirementVerdict(field="progress", comparison="increases", verdict="refuted"),
        ):
            with self.subTest(verdict=verdict.verdict, motion=verdict.motion):
                self.assertFalse(transition_pending(self.make_outcome(
                    OutcomeStatus.VERIFIED_FAILURE, plan=plan, verdicts=(verdict,)
                )))
        self.assertIs(
            service.decide_recovery(
                self.make_outcome(
                    OutcomeStatus.VERIFIED_FAILURE,
                    plan=plan,
                    code="failed",
                    verdicts=(moving_verdict(),),
                ),
                context,
                recovery_eligible=False,
            ).action,
            RecoveryAction.REQUEST_OPERATOR,
        )

    def test_a_partial_effect_still_moving_is_waited_for_and_a_static_one_is_re_evaluated(
        self,
    ) -> None:
        plan = executed_plan()
        context = make_context(plan)
        service = make_service(context, ScriptedObserve([evidence()]))
        moving = self.make_outcome(
            OutcomeStatus.PARTIAL_SUCCESS, plan=plan, verdicts=(moving_verdict(),)
        )
        self.assertIs(
            service.decide_recovery(moving, context).action, RecoveryAction.WAIT_FOR_TRANSITION
        )
        static = self.make_outcome(
            OutcomeStatus.PARTIAL_SUCCESS, plan=plan, verdicts=(moving_verdict(motion=0.0),)
        )
        unchanged = service.decide_recovery(static, context)
        self.assertIs(unchanged.action, RecoveryAction.REEVALUATE_STATE)
        self.assertEqual(unchanged.code, "partial_effect")

    def test_evidence_that_was_only_ever_stale_pauses_the_session_safely(self) -> None:
        """A stale frame must not lead to dispatching on a screen nobody can see."""
        plan = executed_plan()
        context = make_context(plan)
        service = make_service(context, ScriptedObserve([evidence()]))
        stale = ({"code": "stale_reading", "reason": "9s old"},)
        outcome = self.make_outcome(
            OutcomeStatus.OUTCOME_UNKNOWN,
            plan=plan,
            observations=VerificationPolicy().max_observation_attempts,
            rejected=stale,
        )
        decision = service.decide_recovery(outcome, context)
        self.assertIs(decision.action, RecoveryAction.PAUSE_SESSION)
        self.assertEqual(decision.code, "evidence_stale")
        self.assertTrue(decision.requires_operator)
        self.assertFalse(decision.repeats_action)
        # With pausing disabled the same finding escalates rather than silently
        # becoming an ordinary operator request with no explanation.
        disabled = make_service(
            context,
            ScriptedObserve([evidence()]),
            policy=VerificationPolicy(allow_pause=False),
        )
        escalated = disabled.decide_recovery(outcome, context)
        self.assertIs(escalated.action, RecoveryAction.REQUEST_OPERATOR)
        self.assertIn("pausing the session is disabled", escalated.blocked_reason)

    def test_the_session_recovery_time_budget_bounds_a_recovery(self) -> None:
        plan = executed_plan(ok=False, code="failed")
        context = make_context(plan)
        service = make_service(context, ScriptedObserve([evidence()]))
        outcome = self.make_outcome(OutcomeStatus.VERIFIED_FAILURE, plan=plan, code="failed")
        self.assertIs(service.decide_recovery(outcome, context).action, RecoveryAction.REPLAN)
        service._session_recovery_ms[SESSION] = float(service.policy.max_recovery_ms)
        spent = service.decide_recovery(outcome, context)
        self.assertIs(spent.action, RecoveryAction.REQUEST_OPERATOR)
        self.assertEqual(spent.code, "recovery_exhausted")
        self.assertIn("recovery time budget", spent.blocked_reason)

    def test_a_refused_step_invalidates_the_plan_instead_of_replanning(self) -> None:
        plan = executed_plan(ok=False, code="target_changed")
        context = make_context(plan)
        service = make_service(context, ScriptedObserve([evidence()]))
        decision = service.decide_recovery(
            self.make_outcome(
                OutcomeStatus.VERIFIED_FAILURE, plan=plan, code="target_changed"
            ),
            context,
        )
        self.assertIs(decision.action, RecoveryAction.INVALIDATE_PLAN)
        self.assertEqual(decision.code, "action_refused")
        self.assertFalse(decision.side_effecting)

    def test_replanning_is_bounded_per_plan(self) -> None:
        plan = executed_plan(ok=False, code="failed")
        context = make_context(plan)
        service = make_service(context, ScriptedObserve([evidence()]))
        outcome = self.make_outcome(OutcomeStatus.VERIFIED_FAILURE, plan=plan, code="failed")
        self.assertIs(service.decide_recovery(outcome, context).action, RecoveryAction.REPLAN)
        service._plan_replans[plan.plan_id] = 1
        second = service.decide_recovery(outcome, context)
        self.assertIs(second.action, RecoveryAction.REQUEST_OPERATOR)
        self.assertEqual(second.code, "recovery_exhausted")
        self.assertIn("already replanned", second.blocked_reason)

    def test_recovery_attempts_and_session_totals_are_bounded(self) -> None:
        plan = executed_plan(ok=False, code="failed")
        context = make_context(plan)
        service = make_service(
            context,
            ScriptedObserve([evidence()]),
            policy=VerificationPolicy(max_recovery_attempts=0, max_session_recoveries=0),
        )
        decision = service.decide_recovery(
            self.make_outcome(OutcomeStatus.VERIFIED_FAILURE, plan=plan, code="failed"), context
        )
        self.assertIs(decision.action, RecoveryAction.REQUEST_OPERATOR)
        self.assertIn("recovery limit", decision.blocked_reason)

    def test_replanning_with_no_planning_interface_is_escalated(self) -> None:
        plan = executed_plan(ok=False, code="failed")
        context = make_context(plan)
        service = make_service(context, ScriptedObserve([evidence()]), replan=None)
        decision = service.decide_recovery(
            self.make_outcome(OutcomeStatus.VERIFIED_FAILURE, plan=plan, code="failed"), context
        )
        self.assertIs(decision.action, RecoveryAction.REQUEST_OPERATOR)
        self.assertIn("no planning interface", decision.blocked_reason)

    def test_an_unsafe_session_stops_the_operation(self) -> None:
        gate = {"allow": False, "state": "target_lost", "reason": "the window is gone"}
        context = make_context(gate=gate)
        service = make_service(context, ScriptedObserve([evidence()]))
        decision = service.decide_recovery(self.make_outcome(OutcomeStatus.OUTCOME_UNKNOWN), context)
        self.assertIs(decision.action, RecoveryAction.STOP_OPERATION)
        self.assertIn("target_lost", decision.code)
        self.assertTrue(decision.allowed)

    def test_a_changed_objective_invalidates_the_obsolete_plan(self) -> None:
        plan = executed_plan(objective_id="an-objective-that-moved-on")
        context = make_context(plan)
        service = make_service(context, ScriptedObserve([evidence()]))
        decision = service.decide_recovery(
            self.make_outcome(OutcomeStatus.OUTCOME_UNKNOWN, plan=plan, objective_id="new-objective"),
            context,
        )
        self.assertIs(decision.action, RecoveryAction.INVALIDATE_PLAN)
        self.assertEqual(decision.code, "objective_changed")

    def test_an_unknown_outcome_asks_for_a_reading_then_a_person(self) -> None:
        plan = executed_plan()
        context = make_context(plan)
        service = make_service(context, ScriptedObserve([evidence()]))
        early = self.make_outcome(OutcomeStatus.OUTCOME_UNKNOWN, plan=plan, observations=1)
        self.assertIs(
            service.decide_recovery(early, context).action, RecoveryAction.REFRESH_OBSERVATION
        )
        late = self.make_outcome(
            OutcomeStatus.OUTCOME_UNKNOWN,
            plan=plan,
            observations=VerificationPolicy().max_observation_attempts,
        )
        decision = service.decide_recovery(late, context)
        self.assertIs(decision.action, RecoveryAction.REQUEST_OPERATOR)
        self.assertEqual(decision.code, "evidence_exhausted")
        self.assertFalse(decision.repeats_action)

    def test_an_aborted_verification_stops_rather_than_retries(self) -> None:
        context = make_context()
        service = make_service(context, ScriptedObserve([evidence()]))
        decision = service.decide_recovery(
            self.make_outcome(OutcomeStatus.VERIFICATION_ABORTED), context
        )
        self.assertIs(decision.action, RecoveryAction.STOP_OPERATION)

    def test_a_timeout_is_escalated_and_never_retried(self) -> None:
        context = make_context()
        service = make_service(context, ScriptedObserve([evidence()]))
        decision = service.decide_recovery(
            self.make_outcome(OutcomeStatus.VERIFICATION_TIMED_OUT), context
        )
        self.assertIs(decision.action, RecoveryAction.REQUEST_OPERATOR)
        self.assertEqual(decision.code, "verification_timeout")

    def test_recovery_repeats_no_action_and_cannot_dispatch_input(self) -> None:
        """The phase's central guarantee, asserted rather than asserted-in-prose."""
        context = make_context()
        service = make_service(context, ScriptedObserve([evidence()]))
        for action in RecoveryAction:
            self.assertNotIn("key", action.value)
            self.assertNotIn("mouse", action.value)
            self.assertNotIn("click", action.value)
        self.assertFalse(hasattr(service, "submit"))
        self.assertFalse(hasattr(service, "execute_action"))
        self.assertFalse(hasattr(service, "dispatch"))


class ServiceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_unapproved_side_effecting_recovery_is_refused(self) -> None:
        plan = executed_plan(ok=False, code="failed")
        same = evidence(readings={"progress": hud_row("progress", "recognized", value=1.0)})
        seams = RecordingSeams()
        service = make_service(make_context(plan), ScriptedObserve([same]), seams)
        result = await service.verify(SESSION, plan.plan_id)
        outcome = result.outcomes[0]
        refused = await service.recover(SESSION, outcome.outcome_id)
        self.assertFalse(refused["applied"])
        self.assertTrue(refused["requires_approval"])
        self.assertEqual(refused["code"], "approval_required")
        self.assertEqual([call[0] for call in seams.calls], ["record"])
        approved = await service.recover(SESSION, outcome.outcome_id, approve=True)
        self.assertTrue(approved["applied"])
        self.assertEqual(approved["code"], "replanned")
        self.assertIn(("replan", SESSION, "action_failed"), seams.calls)
        self.assertEqual(service.status(SESSION)["recoveries"]["session_total"], 1)

    async def test_a_fresh_observation_recovery_re_reads_without_repeating_anything(self) -> None:
        """A stale frame plus an unreadable one asks for a fresh reading, not a pause.

        One frame was too old and the next was read but unreadable: the capture path
        IS producing usable frames, so the bounded answer is a fresh observation
        (the all-stale case, where no frame was ever usable, pauses instead —
        ``test_evidence_that_was_only_ever_stale_pauses_the_session_safely``).
        """
        plan = executed_plan()
        stale = evidence(age=99.0, accepted=False, code="stale_reading", reason="9s old")
        unreadable = evidence(readings={"progress": hud_row("progress", "ambiguous", reason="blur")})
        observe = ScriptedObserve([stale, unreadable, evidence()])
        service = make_service(
            make_context(plan),
            observe,
            policy=VerificationPolicy(max_observation_attempts=2),
        )
        first = await service.verify(SESSION, plan.plan_id)
        self.assertIs(first.outcomes[0].status, OutcomeStatus.OUTCOME_UNKNOWN)
        self.assertIs(
            RecoveryAction(first.outcomes[0].recovery["action"]),
            RecoveryAction.REFRESH_OBSERVATION,
        )
        applied = await service.recover(SESSION, first.outcomes[0].outcome_id)
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["code"], "reobserved")
        self.assertEqual(applied["result"]["status"], "verified_success")
        self.assertEqual(observe.calls, [SESSION, SESSION, SESSION])
        self.assertEqual(len(service.outcomes(SESSION, 5)), 2)

    async def test_a_plan_invalidation_recovery_calls_the_plan_interface(self) -> None:
        # NF-19 REFUSED this step (target_changed) and the declared counter did
        # not move: a failed precondition, so the plan it belongs to is obsolete.
        plan = executed_plan(ok=False, code="target_changed")
        same = evidence(readings={"progress": hud_row("progress", "recognized", value=1.0)})
        seams = RecordingSeams()
        service = make_service(make_context(plan), ScriptedObserve([same]), seams)
        result = await service.verify(SESSION, plan.plan_id)
        outcome = result.outcomes[0]
        self.assertEqual(outcome.dispatch_code, "target_changed")
        self.assertIs(
            RecoveryAction(outcome.recovery["action"]), RecoveryAction.INVALIDATE_PLAN
        )
        applied = await service.recover(SESSION, outcome.outcome_id, approve=True)
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["code"], "plan_invalidated")
        self.assertIn(("invalidate", SESSION, plan.plan_id), seams.calls)

    async def test_a_recovery_is_refused_once_its_bound_is_spent(self) -> None:
        plan = executed_plan(ok=False, code="target_changed")
        same = evidence(readings={"progress": hud_row("progress", "recognized", value=1.0)})
        seams = RecordingSeams()
        service = make_service(
            make_context(plan),
            ScriptedObserve([same]),
            seams,
            policy=VerificationPolicy(max_recovery_attempts=1),
        )
        result = await service.verify(SESSION, plan.plan_id)
        outcome_id = result.outcomes[0].outcome_id
        first = await service.recover(SESSION, outcome_id, approve=True)
        self.assertTrue(first["applied"])
        second = await service.recover(SESSION, outcome_id, approve=True)
        self.assertFalse(second["applied"])
        self.assertEqual(second["code"], "recovery_not_allowed")
        self.assertIn("recovery limit", second["decision"]["blocked_reason"])
        self.assertEqual(len([call for call in seams.calls if call[0] == "invalidate"]), 1)

    async def test_stopping_without_a_session_interface_is_not_claimed(self) -> None:
        plan = executed_plan()
        moved = evidence(target={"handle": "999", "pid": "4242", "start_time": ""})
        service = make_service(
            make_context(plan), ScriptedObserve([moved]), stop=None, pause=None
        )
        result = await service.verify(SESSION, plan.plan_id)
        applied = await service.recover(SESSION, result.outcomes[0].outcome_id, approve=True)
        self.assertFalse(applied["applied"])
        self.assertEqual(applied["code"], "no_session_interface")

    async def test_recovery_of_an_unknown_outcome_is_a_refusal(self) -> None:
        service = make_service(make_context(), ScriptedObserve([evidence()]))
        refusal = await service.recover(SESSION, "not-an-outcome")
        self.assertFalse(refusal["applied"])
        self.assertEqual(refusal["code"], "unknown_outcome")
        self.assertEqual(service.status(SESSION)["counts"]["recovery_refused"], 1)

    async def test_a_recovery_seam_that_raises_is_reported_not_swallowed(self) -> None:
        # The step was REFUSED, so its effect cannot have happened: the counter
        # stays where it was and the outcome is a verified failure whose recovery
        # invalidates the plan through a seam that explodes.
        plan = executed_plan(ok=False, code="target_changed")
        same = evidence(readings={"progress": hud_row("progress", "recognized", value=1.0)})
        seams = RecordingSeams(error=RuntimeError("the planner is gone"))
        service = make_service(make_context(plan), ScriptedObserve([same]), seams)
        result = await service.verify(SESSION, plan.plan_id)
        outcome = result.outcomes[0]
        self.assertIs(outcome.status, OutcomeStatus.VERIFIED_FAILURE)
        self.assertIs(RecoveryAction(outcome.recovery["action"]), RecoveryAction.INVALIDATE_PLAN)
        applied = await service.recover(SESSION, outcome.outcome_id, approve=True)
        self.assertFalse(applied["applied"])
        self.assertEqual(applied["code"], "recovery_failed")
        self.assertIn("RuntimeError", applied["reason"])

    async def test_a_transition_wait_waits_reads_again_and_is_allowed_only_once(self) -> None:
        """The wait is real: it is awaited, then a NEW reading decides the outcome.

        The settle reading shows the fixture counter moving toward its declared
        target but not reaching it, so the recovery waits the action's own settle
        window and reads again — no input is dispatched, and a second wait for the
        same action is refused because the first one did not resolve it.
        """
        plan = executed_plan()
        sleeper = RecordingSleep()
        observe = ScriptedObserve(
            [
                evidence(readings={"progress": hud_row("progress", "recognized", value=1.5)}),
                evidence(readings={"progress": hud_row("progress", "recognized", value=6.0)}),
            ]
        )
        service = make_service(make_context(plan), observe, sleep=sleeper)
        result = await service.verify(SESSION, plan.plan_id)
        outcome = result.outcomes[0]
        self.assertIs(outcome.status, OutcomeStatus.VERIFIED_FAILURE)
        self.assertIs(
            RecoveryAction(outcome.recovery["action"]), RecoveryAction.WAIT_FOR_TRANSITION
        )
        applied = await service.recover(SESSION, outcome.outcome_id)
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["code"], "reobserved")
        self.assertEqual(applied["result"]["status"], "verified_success")
        self.assertIn(service.policy.wait_for_transition_ms / 1000.0, sleeper.waits)
        self.assertEqual(observe.calls, [SESSION, SESSION])
        second = await service.recover(SESSION, outcome.outcome_id, approve=True)
        self.assertFalse(second["applied"])
        self.assertEqual(second["code"], "recovery_not_allowed")
        self.assertIn("transition wait is already spent", second["decision"]["blocked_reason"])
        recoveries = service.status(SESSION)["recoveries"]
        self.assertEqual(recoveries["transition_waits"], {outcome.action_id: 1})
        self.assertEqual(recoveries["transition_wait_limit"], 1)

    async def test_time_spent_recovering_is_counted_against_the_session_budget(self) -> None:
        plan = executed_plan(ok=False, code="failed")
        observed = evidence(readings={"progress": hud_row("progress", "recognized", value=1.0)})
        seams = RecordingSeams()
        service = make_service(
            make_context(plan),
            ScriptedObserve([observed]),
            seams,
            policy=VerificationPolicy(max_recovery_ms=1),
            monotonic=SteppingClock(step=0.25),
        )
        result = await service.verify(SESSION, plan.plan_id)
        outcome_id = result.outcomes[0].outcome_id
        first = await service.recover(SESSION, outcome_id, approve=True)
        self.assertTrue(first["applied"])
        self.assertEqual(first["code"], "replanned")
        recoveries = service.status(SESSION)["recoveries"]
        self.assertGreaterEqual(recoveries["session_ms"], 1.0)
        self.assertEqual(recoveries["session_ms_limit"], 1)
        second = await service.recover(SESSION, outcome_id, approve=True)
        self.assertFalse(second["applied"])
        self.assertEqual(second["code"], "recovery_not_allowed")
        self.assertIn("recovery time budget", second["decision"]["blocked_reason"])
        self.assertEqual(len([call for call in seams.calls if call[0] == "replan"]), 1)

    async def test_an_exhausted_recovery_escalates_to_the_operator(self) -> None:
        plan = executed_plan(ok=False, code="failed")
        observed = evidence(readings={"progress": hud_row("progress", "recognized", value=1.0)})
        seams = RecordingSeams()
        service = make_service(
            make_context(plan),
            ScriptedObserve([observed]),
            seams,
            policy=VerificationPolicy(max_recovery_attempts=1),
        )
        result = await service.verify(SESSION, plan.plan_id)
        outcome_id = result.outcomes[0].outcome_id
        first = await service.recover(SESSION, outcome_id, approve=True)
        self.assertEqual(first["code"], "replanned")
        refused = await service.recover(SESSION, outcome_id, approve=True)
        self.assertFalse(refused["applied"])
        self.assertEqual(refused["code"], "recovery_not_allowed")
        self.assertIn("recovery limit", refused["decision"]["blocked_reason"])

    async def test_an_operator_escalation_notifies_with_the_evidence(self) -> None:
        plan = executed_plan()
        seams = RecordingSeams()
        service = make_service(
            make_context(plan),
            ScriptedObserve([evidence()]),
            seams,
            rules=VerificationRuleRegistry(()),
        )
        result = await service.verify(SESSION, plan.plan_id)
        applied = await service.recover(SESSION, result.outcomes[0].outcome_id, approve=True)
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["code"], "operator_notified")
        self.assertTrue(any(call[0] == "notify" for call in seams.calls))

    async def test_an_expired_contract_is_reported_rather_than_faked(self) -> None:
        plan = executed_plan()
        unresolved = evidence(readings={"progress": hud_row("progress", "ambiguous", reason="blur")})
        service = make_service(
            make_context(plan),
            ScriptedObserve([unresolved]),
            policy=VerificationPolicy(max_observation_attempts=1, max_outcomes_kept=1),
        )
        first = await service.verify(SESSION, plan.plan_id)
        outcome_id = first.outcomes[0].outcome_id
        for _ in range(3):
            await service.verify(SESSION, plan.plan_id)
        applied = await service.recover(SESSION, outcome_id, approve=True)
        self.assertFalse(applied["applied"])
        self.assertEqual(applied["code"], "unknown_outcome")


class VerificationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """The REAL manager: plan → execute → verify, with the evidence swapped."""

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
        self.pre_state: Any = None
        self.post_state: Any = None
        self.refusal = ""
        self.observed = self.pre_state
        self._install_evidence()

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        self.temp.cleanup()

    def _install_evidence(self) -> None:
        """Swap the state reader's read surfaces for fixture readings.

        The STATE MODEL stays real (``mission_state()`` is a real ``GameState``,
        and the answer is a real ``AnalysisOutcome``); what is replaced is where
        the reading comes from, because this host has no game to look at.
        """
        def current() -> Any:
            return self.observed

        async def analyze(*_args: Any, **_kwargs: Any) -> AnalysisOutcome:
            if self.refusal:
                return AnalysisOutcome(
                    analyzed=False, reason_code=self.refusal, reason="the frame was refused"
                )
            return AnalysisOutcome(analyzed=True, state=self.observed)

        def status(_service: Any) -> dict[str, Any]:
            history = [self.pre_state.to_dict()] if self.pre_state is not None else []
            return {
                "state": self.observed.to_dict() if self.observed else None,
                "history": history,
            }

        self.enterContext(
            mock.patch.object(GameStateService, "current", mock.PropertyMock(side_effect=current))
        )
        self.enterContext(mock.patch.object(GameStateService, "analyze", analyze))
        self.enterContext(mock.patch.object(GameStateService, "status", status))

    async def start(self) -> None:
        import datetime

        described = await self.manager.start(_mission(frame_interval_seconds=0.05))
        self.session_id = described["session_id"]
        record = self.manager.get(self.session_id)
        for _ in range(60):
            if record is not None and record.target_validated:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(record is not None and record.target_validated)
        self.pre_state = mission_state(
            session=self.session_id,
            seq=1,
            at=datetime.datetime.now(datetime.UTC).isoformat(),
            counter=1.0,
        )
        self.observed = self.pre_state
        await self.manager.objective_state.observe(self.pre_state)

    def advance(self, *, counter: float, seq: int = 2) -> None:
        import datetime

        self.post_state = mission_state(
            session=self.session_id,
            seq=seq,
            at=datetime.datetime.now(datetime.UTC).isoformat(),
            counter=counter,
        )
        self.observed = self.post_state

    async def execute(self) -> dict[str, Any]:
        plan = (await self.manager.plan_actions(self.session_id))["plan"]
        self.assertEqual(plan["status"], "awaiting_approval", plan)
        executed = await self.manager.execute_plan(
            self.session_id, {"plan_id": plan["plan_id"], "approved": True}
        )
        self.assertEqual(executed["plan"]["status"], "executed", executed)
        self.assertFalse(executed["effect_verified"])
        return plan

    async def test_a_plan_is_executed_and_then_verified_through_the_manager(self) -> None:
        await self.start()
        plan = await self.execute()
        self.assertIn("key:w", self.input.presses, "the plan must send its step through NF-19")

        self.advance(counter=2.0)
        verified = await self.manager.verify_outcomes(
            self.session_id, {"plan_id": plan["plan_id"]}
        )
        self.assertTrue(verified["effect_verified"], verified["verification"]["reason"])
        outcome = verified["verification"]["outcomes"][0]
        self.assertEqual(outcome["status"], "verified_success")
        self.assertEqual(outcome["action_id"], plan["steps"][0]["action_id"])

        current = self.manager.planner.get(plan["plan_id"])
        self.assertTrue(current.execution["effect_verified"])
        step = current.execution["steps"][0]
        self.assertEqual(step["effect"]["status"], "verified_success")
        self.assertEqual(step["effect"]["outcome_id"], outcome["outcome_id"])
        self.assertEqual(current.to_dict()["effect_verified"], True)
        status = self.manager.verification_status(self.session_id)
        self.assertTrue(status["effect_verified"])
        self.assertEqual(status["outcomes"][0]["status"], "verified_success")

    async def test_the_verdict_lands_on_the_session_timeline_as_the_verifier(self) -> None:
        await self.start()
        plan = await self.execute()
        self.advance(counter=2.0)
        await self.manager.verify_outcomes(self.session_id, {"plan_id": plan["plan_id"]})
        events = self.manager.timeline_payload(self.session_id, limit=200)["events"]
        rows = [row for row in events if str(row.get("type", "")).startswith("novaforge.outcome.")]
        self.assertTrue(rows, "the verdict must reach the session timeline")
        self.assertEqual(rows[-1]["type"], "novaforge.outcome.verified")
        self.assertEqual(rows[-1]["source"], "verifier")
        self.assertEqual(rows[-1]["category"], "outcome")
        self.assertEqual(rows[-1]["payload"]["status"], "verified")
        self.assertTrue(rows[-1]["payload"]["effect_verified"])

    async def test_an_unchanged_counter_is_a_failure_and_the_plan_says_so(self) -> None:
        await self.start()
        plan = await self.execute()
        self.advance(counter=1.0)
        verified = await self.manager.verify_outcomes(
            self.session_id, {"plan_id": plan["plan_id"]}
        )
        outcome = verified["verification"]["outcomes"][0]
        self.assertEqual(outcome["status"], "verified_failure")
        self.assertFalse(verified["effect_verified"])
        current = self.manager.planner.get(plan["plan_id"])
        self.assertFalse(current.execution["effect_verified"])
        self.assertIn("did not establish", current.to_dict()["effect_note"])
        self.assertFalse(self.manager.verification_status(self.session_id)["effect_verified"])

    async def test_a_state_reader_that_is_only_ever_stale_pauses_the_session(self) -> None:
        """Every reading came back stale, so the session pauses through ITS OWN interface.

        NF-16's ``observation_stale`` refusal is the same code vocabulary as this
        phase's ``stale_reading``, so the recovery layer recognises it — and pausing
        the session must not send anything through NF-19 to do it.
        """
        await self.start()
        plan = await self.execute()
        sent = list(self.input.presses)
        self.refusal = "observation_stale"
        verified = await self.manager.verify_outcomes(
            self.session_id, {"plan_id": plan["plan_id"]}
        )
        outcome = verified["verification"]["outcomes"][0]
        self.assertEqual(outcome["status"], "outcome_unknown")
        self.assertEqual(outcome["recovery"]["action"], "pause_session")
        self.assertEqual(outcome["recovery"]["code"], "evidence_stale")
        self.assertTrue(outcome["recovery"]["requires_operator"])
        applied = await self.manager.recover_outcome(
            self.session_id, {"outcome_id": outcome["outcome_id"], "approve": True}
        )
        self.assertTrue(applied["applied"], applied)
        self.assertEqual(applied["code"], "session_paused")
        self.assertIs(self.manager.get(self.session_id).state, SessionState.PAUSED)
        self.assertEqual(self.input.presses, sent, "a safe pause dispatches nothing")

    async def test_a_counter_that_is_still_moving_is_waited_out_then_read_again(self) -> None:
        """The declared transition is in progress, so the wait is chosen and re-read.

        The fixture counter moved by half a step: the effect is arriving rather than
        absent. The recovery waits the action's own settle window and takes a NEW
        reading, which then establishes the effect — and no input is re-sent.
        """
        await self.start()
        plan = await self.execute()
        sent = list(self.input.presses)
        self.advance(counter=1.5)
        verified = await self.manager.verify_outcomes(
            self.session_id, {"plan_id": plan["plan_id"]}
        )
        outcome = verified["verification"]["outcomes"][0]
        self.assertEqual(outcome["status"], "verified_failure")
        self.assertEqual(outcome["recovery"]["action"], "wait_for_transition")
        self.assertFalse(outcome["recovery"]["requires_approval"])
        self.advance(counter=3.0, seq=3)
        applied = await self.manager.recover_outcome(
            self.session_id, {"outcome_id": outcome["outcome_id"]}
        )
        self.assertTrue(applied["applied"], applied)
        self.assertEqual(applied["code"], "reobserved")
        self.assertEqual(applied["result"]["status"], "verified_success")
        self.assertEqual(self.input.presses, sent, "a wait re-sends no input")

    async def test_verifying_without_a_plan_id_uses_the_newest_plan_or_refuses(self) -> None:
        await self.start()
        with self.assertRaises(MissionValidationError):
            await self.manager.verify_outcomes(self.session_id, {})
        plan = await self.execute()
        self.advance(counter=2.0)
        verified = await self.manager.verify_outcomes(self.session_id, {})
        self.assertEqual(verified["plan_id"], plan["plan_id"])

    async def test_recovering_an_outcome_through_the_manager(self) -> None:
        await self.start()
        with self.assertRaises(MissionValidationError):
            await self.manager.recover_outcome(self.session_id, {})
        refusal = await self.manager.recover_outcome(self.session_id, {"outcome_id": "nope"})
        self.assertFalse(refusal["applied"])
        self.assertEqual(refusal["code"], "unknown_outcome")

        plan = await self.execute()
        sent = list(self.input.presses)
        self.advance(counter=1.0)
        verified = await self.manager.verify_outcomes(
            self.session_id, {"plan_id": plan["plan_id"]}
        )
        outcome_id = verified["verification"]["outcomes"][0]["outcome_id"]
        gated = await self.manager.recover_outcome(self.session_id, {"outcome_id": outcome_id})
        self.assertTrue(gated["requires_approval"])
        self.assertFalse(gated["applied"])
        approved = await self.manager.recover_outcome(
            self.session_id, {"outcome_id": outcome_id, "approve": True}
        )
        self.assertTrue(approved["applied"], approved)
        self.assertEqual(approved["decision"]["action"], "replan")
        # Recovery asks the planner for a plan; the planner's own policy decides
        # what that plan is, and nothing it returns has been dispatched.
        self.assertEqual(approved["code"], "replanned")
        self.assertIn("status", approved["result"])
        self.assertIn("plan_id", approved["result"])
        self.assertEqual(self.input.presses, sent, "recovery must send no input")
        cancelled = await self.manager.cancel_verification(self.session_id)
        self.assertFalse(cancelled["cancel_requested"])

    async def test_a_session_with_no_state_reports_no_evidence_rather_than_a_result(self) -> None:
        await self.start()
        plan = await self.execute()
        self.refusal = "observation_stale"
        verified = await self.manager.verify_outcomes(
            self.session_id, {"plan_id": plan["plan_id"]}
        )
        outcome = verified["verification"]["outcomes"][0]
        self.assertEqual(outcome["status"], "outcome_unknown")
        self.assertEqual(outcome["rejected"][0]["code"], "stale_reading")
        self.assertFalse(verified["effect_verified"])
        self.assertFalse(self.manager.verification_status(self.session_id)["effect_verified"])


def _control_record(
    content: dict[str, Any], *, status: KnowledgeStatus = KnowledgeStatus.VERIFIED
) -> KnowledgeRecord:
    return KnowledgeRecord(
        knowledge_id="k-1",
        game=GAME,
        category=KnowledgeCategory.CONTROL,
        key="hold w advances the counter",
        content=content,
        status=status,
        evidence=(
            KnowledgeEvidence(
                source=EvidenceSource.EXPERIMENT,
                summary="a bounded trial observed the counter advancing",
                method=VerificationMethod.EXPERIMENT,
                verified=True,
                at=AT,
                game=GAME,
            ),
        ),
        last_verified_at=AT if status is KnowledgeStatus.VERIFIED else "",
        provenance="test fixture",
    )


def _plan_from_knowledge() -> ActionPlan:
    """A plan whose step is NOT covered by a rule, so knowledge decides it."""
    plan = executed_plan()
    step = plan.steps[0]
    object.__setattr__(step, "candidate_id", "knowledge:k-1:000001")
    object.__setattr__(step, "source", "knowledge:k-1")
    return plan


class VerificationApiTests(_GameplayApiFixture):
    def test_the_outcome_routes_require_known_sessions(self) -> None:
        for method, path, payload in (
            ("get", "/novaforge/sessions/nope/outcomes", None),
            ("post", "/novaforge/sessions/nope/outcomes/verify", {}),
            ("post", "/novaforge/sessions/nope/outcomes/recover", {"outcome_id": "o"}),
            ("post", "/novaforge/sessions/nope/outcomes/cancel", None),
        ):
            with self.subTest(path=path):
                response = getattr(self._client, method)(
                    path, **({"json": payload} if payload is not None else {})
                )
                self.assertEqual(response.status_code, 404, response.text)

    def test_the_outcome_state_reports_the_rules_bounds_and_no_result(self) -> None:
        session = self._validated_session()
        session_id = session["session_id"]
        body = self._client.get(f"/novaforge/sessions/{session_id}/outcomes").json()
        self.assertTrue(body["available"])
        self.assertEqual(len(body["rules"]), 1)
        self.assertTrue(body["rules"][0]["fixture_only"])
        self.assertEqual(body["policy"]["max_recovery_attempts"], 2)
        self.assertFalse(body["effect_verified"])
        self.assertEqual(body["outcomes"], [])
        self.assertEqual(body["session"]["session_id"], session_id)
        self.assertFalse(body["counts"]["runs"])
        self.assertIsNone(body["plan"])

    def test_verifying_without_a_plan_is_a_validation_error(self) -> None:
        session = self._validated_session()
        session_id = session["session_id"]
        response = self._client.post(f"/novaforge/sessions/{session_id}/outcomes/verify", json={})
        self.assertEqual(response.status_code, 422, response.text)

    def test_recovering_without_an_outcome_id_is_a_validation_error(self) -> None:
        session = self._validated_session()
        session_id = session["session_id"]
        response = self._client.post(f"/novaforge/sessions/{session_id}/outcomes/recover", json={})
        self.assertEqual(response.status_code, 422, response.text)

    def test_cancelling_with_no_verification_in_flight_is_an_honest_no_op(self) -> None:
        session = self._validated_session()
        session_id = session["session_id"]
        body = self._client.post(f"/novaforge/sessions/{session_id}/outcomes/cancel").json()
        self.assertFalse(body["cancel_requested"])
        self.assertFalse(body["running"])

    def test_the_capability_row_reports_what_is_declared(self) -> None:
        body = self._client.get("/novaforge/capabilities").json()
        row = body["capabilities"]["outcome_verification"]
        self.assertTrue(row["available"])
        self.assertEqual(row["rules"], 1)
        self.assertEqual(row["declared_observables"], 1)
        self.assertTrue(row["fixture_only"])
        self.assertFalse(row["repeats_actions"])
        self.assertIn("max_verification_ms", row["policy"])


if __name__ == "__main__":  # pragma: no cover - direct invocation
    unittest.main()
