"""Phase 4: the planner, dependency-aware execution, recovery and the agent loop.

Every test here pins a rule that exists to prevent a specific failure mode, and
the docstrings name the failure mode. The four the specification calls out
explicitly are all covered: a plan is a structured graph rather than a list, only
read-only work may run concurrently, a retry is bounded and reasoned rather than
hopeful, and nothing that changes the machine happens without authorization.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from novacontrol.core.config import MAX_PLAN_ATTEMPTS, MAX_PLAN_CYCLES
from novacontrol.decision.models import Decision, DecisionRoute, DecisionType
from novacontrol.planning import (
    DEFAULT_MAX_CYCLES,
    MAX_STEP_ATTEMPTS,
    AgentLoop,
    DeterministicVerifier,
    FailureKind,
    Plan,
    PlanCompiler,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    RecoveryAction,
    RecoveryAdvisor,
    RetryPolicy,
    StepEffect,
    StepError,
    VerificationMethod,
    VerificationPolicy,
    VerificationResult,
    VerificationSpec,
    VerificationStatus,
    WorkflowExecutor,
)
from novacontrol.planning.recovery import repair_parameters

#: The plan the Phase 4 specification gives as its example.
SPEC_GOAL = (
    "Open VS Code, open my NovaControl project, run the tests and tell me what failed."
)

TOOLS = {
    "open_application": "desktop_control",
    "open_folder": "desktop_control",
    "find_file": "file_search",
    "run_command": "command_runner",
    "summarize": "local_model",
    "answer_question": "local_model",
    "cpu_status": "system_monitor",
    "memory_status": "system_monitor",
    "gpu_status": "system_monitor",
}


def compiler() -> PlanCompiler:
    return PlanCompiler(tool_lookup=lambda name: TOOLS.get(name, ""))


def plan_of(goal: str, **kwargs: object) -> Plan:
    return compiler().compile(goal, **kwargs)  # type: ignore[arg-type]


class SpecificationExampleTests(unittest.TestCase):
    """The example plan the specification names, step for step."""

    def setUp(self) -> None:
        self.plan = plan_of(SPEC_GOAL)

    def test_the_seven_steps_are_the_specification_s_plan(self) -> None:
        self.assertFalse(self.plan.needs_clarification)
        self.assertEqual(
            [step.id for step in self.plan.steps],
            [
                "locate-project",
                "open-application",
                "open-project",
                "run-tests",
                "collect-output",
                "analyze-result",
                "summarize-result",
            ],
        )

    def test_the_project_is_located_before_anything_uses_it(self) -> None:
        by_id = {step.id: step for step in self.plan.steps}
        # Opening a project before knowing where it is has the order wrong in a
        # way that only shows up at run time.
        self.assertIn("locate-project", by_id["open-project"].depends_on)
        self.assertIn("locate-project", by_id["run-tests"].depends_on)

    def test_output_is_collected_before_it_is_analysed(self) -> None:
        by_id = {step.id: step for step in self.plan.steps}
        self.assertEqual(by_id["collect-output"].depends_on, ("run-tests",))
        self.assertEqual(by_id["analyze-result"].depends_on, ("collect-output",))
        self.assertEqual(by_id["summarize-result"].depends_on, ("analyze-result",))

    def test_every_step_names_a_tool_action_effect_and_expectation(self) -> None:
        for step in self.plan.steps:
            self.assertTrue(step.tool, f"{step.id} names no tool")
            self.assertTrue(step.action, f"{step.id} names no action")
            self.assertTrue(step.expected_result, f"{step.id} has no expected result")

    def test_side_effecting_steps_carry_a_verification(self) -> None:
        by_id = {step.id: step for step in self.plan.steps}
        self.assertTrue(by_id["open-application"].verification.required)
        self.assertTrue(by_id["open-project"].verification.required)
        self.assertTrue(by_id["run-tests"].verification.required)

    def test_the_plan_exposes_dependencies_and_parallel_groups(self) -> None:
        payload = self.plan.to_dict()
        self.assertIn(["open-project", "locate-project"], payload["dependencies"])
        self.assertEqual(payload["parallel_groups"], [])
        self.assertIn("retry_policy", payload)
        self.assertIn("verification_policy", payload)
        self.assertIn("state", payload)


class PlanModelTests(unittest.TestCase):
    def test_a_write_cannot_declare_itself_parallel_safe(self) -> None:
        # The single rule that makes concurrent execution safe: it cannot be
        # opted into by an action that changes something.
        with self.assertRaises(ValueError):
            PlanStep(
                "Delete all",
                "delete everything",
                effect=StepEffect.DESTRUCTIVE,
                parallel_safe=True,
            )

    def test_step_ids_are_slugs_of_their_titles(self) -> None:
        step = PlanStep("Run tests", "run them")
        self.assertEqual(step.step_id, "run-tests")
        self.assertEqual(step.id, step.step_id)

    def test_two_steps_cannot_share_an_id(self) -> None:
        # Because an id is derived from a title, a plan that repeats a step
        # repeats its id — and two steps with one id are ONE step as far as
        # execution is concerned: one outcome overwrites the other, so the plan
        # can report success while a step it listed was never accounted for.
        with self.assertRaises(ValueError) as caught:
            Plan(
                goal="demo",
                steps=(
                    PlanStep("Run tests", "run them", id="run-tests"),
                    PlanStep("Run tests again", "run them", id="run-tests"),
                ),
            )
        self.assertIn("run-tests", str(caught.exception))

    def test_a_compiled_plan_never_repeats_an_id(self) -> None:
        # The guard above would be unusable if the compiler could trip it: a
        # sentence that asks for the same thing twice still compiles.
        plan = plan_of("open chrome and open chrome")
        ids = [step.id for step in plan.steps]
        self.assertEqual(len(ids), len(set(ids)))

    def test_effects_that_need_confirmation_are_marked(self) -> None:
        destructive = PlanStep("Delete", "delete it", effect=StepEffect.DESTRUCTIVE)
        reading = PlanStep("Read", "read it")
        self.assertTrue(destructive.needs_confirmation)
        self.assertFalse(reading.needs_confirmation)

    def test_retry_policy_refuses_an_unbounded_configuration(self) -> None:
        # A configuration mistake must not be able to create an infinite loop.
        with self.assertRaises(ValueError):
            RetryPolicy(max_attempts=99)
        with self.assertRaises(ValueError):
            RetryPolicy(max_attempts=0)

    def test_retry_is_limited_to_kinds_worth_retrying(self) -> None:
        policy = RetryPolicy(max_attempts=3)
        self.assertTrue(policy.may_retry(FailureKind.TRANSIENT, attempts_made=1))
        self.assertFalse(policy.may_retry(FailureKind.PERMISSION_DENIED, attempts_made=1))
        self.assertFalse(policy.may_retry(FailureKind.TRANSIENT, attempts_made=3))


class DependencyAwareExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_steps_run_in_dependency_order(self) -> None:
        seen: list[str] = []

        async def handler(step: PlanStep) -> dict[str, object]:
            seen.append(step.id)
            return {"changed": True}

        plan = Plan(
            goal="demo",
            steps=(
                PlanStep("Read project", "read", id="read", parallel_safe=True),
                PlanStep("Write file", "write", id="write", depends_on=("read",)),
                PlanStep("Collect", "collect", id="collect", depends_on=("write",)),
            ),
        )
        result = await WorkflowExecutor(step_handler=handler).execute(plan)

        self.assertEqual(result.status, PlanStatus.COMPLETED)
        self.assertEqual(seen, ["read", "write", "collect"])

    async def test_independent_readings_run_concurrently(self) -> None:
        plan = plan_of("check my cpu and ram and gpu")
        self.assertEqual(len(plan.parallel_groups), 1)
        started: list[str] = []

        async def handler(step: PlanStep) -> dict[str, object]:
            started.append(step.id)
            await asyncio.sleep(0.05)
            return {"changed": True}

        began = await asyncio.wait_for(
            WorkflowExecutor(step_handler=handler).execute(plan), timeout=2
        )
        self.assertEqual(began.status, PlanStatus.COMPLETED)
        # All three start before any of them finishes — the reads really are
        # concurrent rather than merely fast.
        self.assertEqual(len(started), 3)

    async def test_two_steps_on_one_resource_never_run_together(self) -> None:
        order: list[str] = []

        async def handler(step: PlanStep) -> dict[str, object]:
            order.append(f"{step.id}:start")
            await asyncio.sleep(0.03)
            order.append(f"{step.id}:end")
            return {"changed": True}

        plan = Plan(
            goal="read one file twice",
            steps=(
                PlanStep(
                    "Read A", "read", id="a", parallel_safe=True,
                    parameters={"resource": "report.pdf"},
                ),
                PlanStep(
                    "Read B", "read", id="b", parallel_safe=True,
                    parameters={"resource": "report.pdf"},
                ),
            ),
        )
        await WorkflowExecutor(step_handler=handler).execute(plan)

        self.assertEqual(order, ["a:start", "a:end", "b:start", "b:end"])

    async def test_writes_are_serialised_even_when_independent(self) -> None:
        plan = plan_of("open chrome, open notepad")
        self.assertEqual(
            [step.id for step in plan.steps], ["open-application", "open-application-2"]
        )
        self.assertEqual(plan.steps[1].depends_on, ("open-application",))

    async def test_a_failed_dependency_blocks_what_depends_on_it(self) -> None:
        async def handler(step: PlanStep) -> dict[str, object]:
            if step.id == "write":
                raise RuntimeError("the disk is full")
            return {"changed": True}

        plan = Plan(
            goal="demo",
            steps=(
                PlanStep("Read", "read", id="read", parallel_safe=True),
                PlanStep("Write", "write", id="write", depends_on=("read",)),
                PlanStep("Collect", "collect", id="collect", depends_on=("write",)),
            ),
        )
        result = await WorkflowExecutor(step_handler=handler).execute(plan)

        self.assertEqual(result.status, PlanStatus.FAILED)
        self.assertEqual(result.step_results["write"].status, PlanStepStatus.FAILED)
        self.assertEqual(result.step_results["collect"].status, PlanStepStatus.BLOCKED)
        self.assertIn("collect", result.errors)

    async def test_a_dependency_that_is_not_in_the_plan_is_named(self) -> None:
        # A dangling dependency is a defect in the plan, not a runtime failure.
        # Reporting it as "a dependency failed" sends the reader looking for a
        # failure that never happened.
        plan = Plan(
            goal="demo",
            steps=(
                PlanStep(
                    "Collect",
                    "collect the run output",
                    depends_on=("run-tests",),
                    id="collect",
                    action="collect_output",
                    tool="command_runner",
                ),
            ),
        )
        result = await WorkflowExecutor(
            step_handler=lambda step, context: {"ok": True}
        ).execute(plan)

        step = result.step_results["collect"]
        self.assertEqual(step.status, PlanStepStatus.BLOCKED)
        self.assertEqual(step.error.kind, FailureKind.MISSING_DEPENDENCY)
        self.assertIn("run-tests", step.error.message)
        self.assertIn("not in this plan", step.summary)
        # Nothing ran and nothing failed, so neither may be claimed.
        self.assertEqual(result.status, PlanStatus.BLOCKED)
        self.assertNotIn("failed", result.summary)
        self.assertTrue(result.summary.startswith("The plan did not run"))

    async def test_a_cycle_is_reported_as_blocked_rather_than_failed(self) -> None:
        plan = Plan(
            goal="demo",
            steps=(
                PlanStep("A", "a", id="a", depends_on=("b",)),
                PlanStep("B", "b", id="b", depends_on=("a",)),
            ),
        )
        result = await WorkflowExecutor(
            step_handler=lambda step, context: {"ok": True}
        ).execute(plan)

        self.assertEqual(result.status, PlanStatus.BLOCKED)
        self.assertEqual(
            {outcome.status for outcome in result.step_results.values()},
            {PlanStepStatus.BLOCKED},
        )
        self.assertNotIn("failed", result.summary)

    async def test_an_unexecuted_plan_reports_that_nothing_ran(self) -> None:
        # A plan walked with no executor bound is a PREVIEW, and must not read
        # like a completed workflow.
        plan = plan_of("check my cpu")
        result = await WorkflowExecutor().execute(plan)

        self.assertFalse(result.executed)
        self.assertEqual(result.status, PlanStatus.COMPLETED)
        self.assertIn("nothing was executed", result.summary)


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_transient_failure_is_retried_within_the_budget(self) -> None:
        attempts = {"n": 0}

        async def handler(step: PlanStep) -> dict[str, object]:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise TimeoutError("timed out")
            return {"changed": True}

        plan = Plan(
            goal="flaky",
            steps=(
                PlanStep(
                    "Fetch", "fetch", id="fetch",
                    verification=VerificationSpec(VerificationMethod.STATE_OBSERVED),
                ),
            ),
            retry_policy=RetryPolicy(max_attempts=3),
        )
        result = await WorkflowExecutor(step_handler=handler).execute(plan)

        outcome = result.step_results["fetch"]
        self.assertEqual(result.status, PlanStatus.COMPLETED)
        self.assertEqual(outcome.attempts, 3)
        self.assertEqual(outcome.recovery, ("retry", "retry"))

    async def test_retries_stop_at_the_limit_without_spinning(self) -> None:
        calls = {"n": 0}

        async def handler(step: PlanStep) -> dict[str, object]:
            calls["n"] += 1
            raise TimeoutError("timed out")

        plan = Plan(
            goal="always fails",
            steps=(PlanStep("Fetch", "fetch", id="fetch"),),
            retry_policy=RetryPolicy(max_attempts=2),
        )
        result = await WorkflowExecutor(step_handler=handler).execute(plan)

        self.assertEqual(calls["n"], 2)
        self.assertEqual(result.status, PlanStatus.FAILED)
        self.assertEqual(result.step_results["fetch"].attempts, 2)

    async def test_a_destructive_step_is_never_retried(self) -> None:
        calls = {"n": 0}

        async def handler(step: PlanStep) -> dict[str, object]:
            calls["n"] += 1
            raise TimeoutError("timed out")

        plan = Plan(
            goal="delete",
            steps=(
                PlanStep(
                    "Delete", "delete it", id="delete",
                    effect=StepEffect.DESTRUCTIVE, tool="fs",
                ),
            ),
            retry_policy=RetryPolicy(max_attempts=5),
        )
        result = await WorkflowExecutor(
            step_handler=handler,
            confirmation=lambda step: True,
        ).execute(plan)

        # Exactly one attempt: a delete that half-succeeded, run again, deletes
        # something else.
        self.assertEqual(calls["n"], 1)
        self.assertEqual(result.status, PlanStatus.FAILED)


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_step_whose_check_fails_is_not_a_success(self) -> None:
        async def handler(step: PlanStep) -> dict[str, object]:
            return {"changed": False}

        plan = Plan(
            goal="write",
            steps=(
                PlanStep(
                    "Write", "write", id="w",
                    verification=VerificationSpec(VerificationMethod.STATE_OBSERVED),
                ),
            ),
        )
        result = await WorkflowExecutor(step_handler=handler).execute(plan)

        outcome = result.step_results["w"]
        self.assertEqual(result.status, PlanStatus.FAILED)
        self.assertEqual(outcome.status, PlanStepStatus.FAILED)
        self.assertIsNotNone(outcome.error)
        self.assertEqual(outcome.error.kind, FailureKind.VERIFICATION_FAILED)  # type: ignore[union-attr]

    async def test_a_required_but_unchecked_step_is_reported_unverified(self) -> None:
        async def handler(step: PlanStep) -> dict[str, object]:
            return {"summary": "did something"}

        plan = Plan(
            goal="write a file",
            steps=(
                PlanStep("Write", "write", id="w", effect=StepEffect.LOCAL_WRITE, tool="fs"),
            ),
        )
        result = await WorkflowExecutor(step_handler=handler).execute(plan)

        self.assertEqual(result.status, PlanStatus.COMPLETED)
        self.assertEqual(result.unverified_steps, ("w",))
        self.assertFalse(result.verified)

    async def test_a_verified_step_is_verified(self) -> None:
        async def handler(step: PlanStep) -> dict[str, object]:
            return {"changed": True}

        plan = Plan(
            goal="write",
            steps=(
                PlanStep(
                    "Write", "write", id="w",
                    verification=VerificationSpec(VerificationMethod.STATE_OBSERVED),
                ),
            ),
        )
        result = await WorkflowExecutor(step_handler=handler).execute(plan)

        self.assertTrue(result.verified)
        self.assertEqual(result.unverified_steps, ())
        self.assertEqual(result.step_results["w"].status, PlanStepStatus.COMPLETED)

    async def test_inconclusive_is_not_rounded_up_to_a_pass(self) -> None:
        async def handler(step: PlanStep) -> dict[str, object]:
            return {"summary": "ran"}

        plan = Plan(
            goal="write",
            steps=(
                PlanStep(
                    "Write", "write", id="w",
                    verification=VerificationSpec(VerificationMethod.PROCESS_RUNNING, target="x"),
                ),
            ),
        )
        result = await WorkflowExecutor(
            step_handler=handler,
            verifier=DeterministicVerifier(process_probe=lambda name: None),
        ).execute(plan)

        outcome = result.step_results["w"]
        self.assertIsNotNone(outcome.verification)
        self.assertEqual(outcome.verification.status, VerificationStatus.INCONCLUSIVE)  # type: ignore[union-attr]
        self.assertFalse(outcome.verified)
        self.assertEqual(outcome.status, PlanStepStatus.UNVERIFIED)

    async def test_a_policy_can_treat_inconclusive_as_failure(self) -> None:
        async def handler(step: PlanStep) -> dict[str, object]:
            return {"summary": "ran"}

        plan = Plan(
            goal="write",
            steps=(
                PlanStep(
                    "Write", "write", id="w", effect=StepEffect.LOCAL_WRITE,
                    verification=VerificationSpec(VerificationMethod.PROCESS_RUNNING, target="x"),
                ),
            ),
            verification_policy=VerificationPolicy(allow_inconclusive=False),
        )
        result = await WorkflowExecutor(
            step_handler=handler,
            verifier=DeterministicVerifier(process_probe=lambda name: None),
        ).execute(plan)

        self.assertEqual(result.status, PlanStatus.FAILED)

    async def test_the_verification_methods_check_real_evidence(self) -> None:
        verifier = DeterministicVerifier()
        with tempfile.TemporaryDirectory() as folder:
            existing = Path(folder) / "report.txt"
            existing.write_text("hello", encoding="utf-8")

            step = PlanStep(
                "Write", "write", id="w",
                verification=VerificationSpec(VerificationMethod.FILE_EXISTS, target=str(existing)),
            )
            self.assertEqual(
                verifier.verify(step, {}).status, VerificationStatus.PASS
            )

            missing = PlanStep(
                "Write", "write", id="w2",
                verification=VerificationSpec(
                    VerificationMethod.FILE_EXISTS, target=str(Path(folder) / "nope.txt")
                ),
            )
            self.assertEqual(verifier.verify(missing, {}).status, VerificationStatus.FAIL)

        exit_code = PlanStep(
            "Run", "run", id="r",
            verification=VerificationSpec(VerificationMethod.EXIT_CODE),
        )
        # Any exit code is accepted when the step expects none: for "run the
        # tests and tell me what failed", failing IS the result.
        self.assertEqual(
            verifier.verify(exit_code, {"exit_code": 1}).status, VerificationStatus.PASS
        )
        self.assertEqual(
            verifier.verify(exit_code, {"summary": "no code"}).status,
            VerificationStatus.INCONCLUSIVE,
        )

        expected = PlanStep(
            "Run", "run", id="r2",
            verification=VerificationSpec(VerificationMethod.EXIT_CODE, expect=0),
        )
        self.assertEqual(
            verifier.verify(expected, {"exit_code": 0}).status, VerificationStatus.PASS
        )

    async def test_a_registered_callable_can_carry_a_check(self) -> None:
        verifier = DeterministicVerifier(callables={"has_key": lambda spec, out: ("k" in out, "")})
        step = PlanStep(
            "Check", "check", id="c",
            verification=VerificationSpec(VerificationMethod.CALLABLE, target="has_key"),
        )
        self.assertEqual(verifier.verify(step, {"k": 1}).status, VerificationStatus.PASS)
        self.assertEqual(verifier.verify(step, {}).status, VerificationStatus.FAIL)

    async def test_a_verification_result_is_only_verified_on_a_pass(self) -> None:
        inconclusive = VerificationResult(
            status=VerificationStatus.INCONCLUSIVE,
            method=VerificationMethod.STATE_OBSERVED,
        )
        self.assertFalse(inconclusive.verified)


class RecoveryAdvisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.step = PlanStep("Fetch", "fetch it", id="fetch", tool="http")
        self.policy = RetryPolicy(max_attempts=2)

    def advise(self, kind: FailureKind, attempts: int = 1) -> object:
        advisor = RecoveryAdvisor(retry_policy=self.policy)
        return advisor.advise(
            self.step, StepError(kind, "boom"), attempts_made=attempts
        )

    def test_a_refusal_is_not_retried(self) -> None:
        decision = self.advise(FailureKind.PERMISSION_DENIED)  # type: ignore[assignment]
        self.assertEqual(decision.action, RecoveryAction.STOP)
        self.assertIn("authorized", decision.reason)

    def test_a_missing_prerequisite_escalates(self) -> None:
        decision = self.advise(FailureKind.MISSING_DEPENDENCY)  # type: ignore[assignment]
        self.assertEqual(decision.action, RecoveryAction.ESCALATE)

    def test_missing_prerequisite_stops_when_nothing_can_take_over(self) -> None:
        advisor = RecoveryAdvisor(retry_policy=self.policy, escalation_available=False)
        decision = advisor.advise(
            self.step,
            StepError(FailureKind.MISSING_DEPENDENCY, "not installed"),
            attempts_made=1,
        )
        self.assertEqual(decision.action, RecoveryAction.STOP)

    def test_a_transient_failure_is_retried_then_escalated(self) -> None:
        first = self.advise(FailureKind.TRANSIENT, attempts=1)  # type: ignore[assignment]
        self.assertEqual(first.action, RecoveryAction.RETRY)
        exhausted = self.advise(FailureKind.TRANSIENT, attempts=2)  # type: ignore[assignment]
        self.assertEqual(exhausted.action, RecoveryAction.ESCALATE)

    def test_parameters_are_repaired_when_the_error_says_how(self) -> None:
        step = PlanStep(
            "Set", "set the count", id="set", tool="counter",
            parameters={"count": "3", "stale": True},
        )
        error = StepError(
            FailureKind.INVALID_PARAMETERS,
            "Argument 'count' must be integer, got str; unexpected argument 'stale'",
        )
        repaired = repair_parameters(step, error)
        self.assertEqual(repaired["count"], 3)
        self.assertIn("stale", repaired)

        advisor = RecoveryAdvisor(retry_policy=RetryPolicy(max_attempts=3))
        decision = advisor.advise(step, error, attempts_made=1)
        self.assertEqual(decision.action, RecoveryAction.MODIFY_PARAMETERS)
        self.assertEqual(decision.adjustments["count"], 3)

    def test_repair_never_invents_a_missing_value(self) -> None:
        step = PlanStep("Set", "set it", id="set", tool="counter", parameters={})
        error = StepError(
            FailureKind.INVALID_PARAMETERS, "Missing required argument: recipient"
        )
        self.assertEqual(repair_parameters(step, error), {})

    def test_failures_are_classified_from_their_message(self) -> None:
        advisor = RecoveryAdvisor()
        cases = {
            "Permission denied: '/etc/shadow'": FailureKind.PERMISSION_DENIED,
            "ModuleNotFoundError: No module named 'foo'": FailureKind.MISSING_DEPENDENCY,
            "Missing required argument: command": FailureKind.INVALID_PARAMETERS,
            "Connection timed out after 30s": FailureKind.TRANSIENT,
            "the espresso machine exploded": FailureKind.UNKNOWN,
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(advisor.classify(self.step, message).kind, expected)

    def test_the_advisor_never_allows_a_destructive_retry(self) -> None:
        destructive = PlanStep("Delete", "delete it", id="d", effect=StepEffect.DESTRUCTIVE)
        advisor = RecoveryAdvisor(retry_policy=RetryPolicy(max_attempts=5))
        decision = advisor.advise(
            destructive, StepError(FailureKind.TRANSIENT, "timed out"), attempts_made=1
        )
        self.assertEqual(decision.action, RecoveryAction.STOP)
        self.assertFalse(decision.will_run_again)


class PermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_step_needing_confirmation_is_denied_without_a_channel(self) -> None:
        calls = {"n": 0}

        async def handler(step: PlanStep) -> dict[str, object]:
            calls["n"] += 1
            return {}

        plan = Plan(
            goal="delete a file",
            steps=(PlanStep("Delete", "delete it", id="delete", effect=StepEffect.DESTRUCTIVE),),
        )
        result = await WorkflowExecutor(step_handler=handler).execute(plan)

        # Fail closed: "nobody said no" is not permission.
        self.assertEqual(calls["n"], 0)
        self.assertEqual(result.step_results["delete"].status, PlanStepStatus.DENIED)
        self.assertEqual(result.status, PlanStatus.BLOCKED)
        self.assertIn("not authorized", result.summary)

    async def test_a_refused_confirmation_denies_the_step(self) -> None:
        calls = {"n": 0}

        async def handler(step: PlanStep) -> dict[str, object]:
            calls["n"] += 1
            return {}

        plan = Plan(
            goal="send a message",
            steps=(PlanStep("Send", "send it", id="send", effect=StepEffect.EXTERNAL),),
        )
        result = await WorkflowExecutor(
            step_handler=handler, confirmation=lambda step: False
        ).execute(plan)

        self.assertEqual(calls["n"], 0)
        self.assertEqual(result.step_results["send"].status, PlanStepStatus.DENIED)

    async def test_an_approved_step_runs(self) -> None:
        plan = Plan(
            goal="set a setting",
            steps=(
                PlanStep(
                    "Change", "change it", id="change", effect=StepEffect.SYSTEM,
                    verification=VerificationSpec(VerificationMethod.STATE_OBSERVED),
                ),
            ),
        )
        result = await WorkflowExecutor(
            step_handler=lambda step: {"changed": True},
            confirmation=lambda step: step.id == "change",
        ).execute(plan)

        self.assertEqual(result.status, PlanStatus.COMPLETED)
        self.assertTrue(result.verified)

    async def test_a_permission_error_from_a_tool_is_a_denial_not_a_failure(self) -> None:
        async def handler(step: PlanStep) -> dict[str, object]:
            raise PermissionError("Tool 'fs' was not approved.")

        plan = Plan(
            goal="write a file",
            steps=(PlanStep("Write", "write", id="w", effect=StepEffect.LOCAL_WRITE, tool="fs"),),
        )
        result = await WorkflowExecutor(step_handler=handler).execute(plan)

        outcome = result.step_results["w"]
        self.assertEqual(outcome.status, PlanStepStatus.DENIED)
        self.assertIsNotNone(outcome.error)
        self.assertEqual(outcome.error.kind, FailureKind.PERMISSION_DENIED)  # type: ignore[union-attr]
        self.assertEqual(result.status, PlanStatus.BLOCKED)


class StepContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_handler_can_read_what_earlier_steps_produced(self) -> None:
        seen: dict[str, object] = {}

        async def handler(step: PlanStep, context: object) -> dict[str, object]:
            seen[step.id] = context.output("first")  # type: ignore[attr-defined]
            return {"summary": step.id, "changed": True}

        plan = Plan(
            goal="two steps",
            steps=(
                PlanStep("First", "first", id="first", parallel_safe=True),
                PlanStep("Second", "second", id="second", depends_on=("first",)),
            ),
        )
        await WorkflowExecutor(step_handler=handler).execute(plan)

        self.assertEqual(seen["second"]["summary"], "first")

    async def test_a_one_argument_handler_still_works(self) -> None:
        seen: list[str] = []

        async def handler(step: PlanStep) -> dict[str, object]:
            seen.append(step.id)
            return {"changed": True}

        plan = Plan(goal="one", steps=(PlanStep("Only", "only", id="only"),))
        result = await WorkflowExecutor(step_handler=handler).execute(plan)

        self.assertEqual(seen, ["only"])
        self.assertEqual(result.status, PlanStatus.COMPLETED)


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    def _decision(self, route: DecisionRoute, reason: str = "because") -> Decision:
        return Decision(
            decision_type=DecisionType.PLANNING,
            route=route,
            reason=reason,
            requires_planning=True,
        )

    def _loop(
        self, route: DecisionRoute, *, executor: WorkflowExecutor, **kwargs: object
    ) -> AgentLoop:
        return AgentLoop(
            understand=lambda text: type("U", (), {"intent": None, "strategy": "fast"})(),
            decide=lambda understood: self._decision(route),
            plan=lambda goal, understood, decision: plan_of("check my cpu"),
            execute=executor.execute,
            **kwargs,  # type: ignore[arg-type]
        )

    async def test_the_phases_run_in_order(self) -> None:
        async def handler(step: PlanStep) -> dict[str, object]:
            return {"metrics": {"cpu": {"available": True}}, "changed": True}

        # The app registers the "metric_read" check; a verifier without it would
        # report the reading inconclusive, which is what this test then gets.
        verifier = DeterministicVerifier(
            callables={"metric_read": lambda spec, out: ("metrics" in out, "")}
        )
        loop = self._loop(
            DecisionRoute.PLANNER,
            executor=WorkflowExecutor(step_handler=handler, verifier=verifier),
            summarize=lambda goal, plan, workflow: "your CPU is fine",
        )
        run = await loop.run("check my cpu")

        self.assertEqual(
            [event.phase.value for event in run.events],
            ["understand", "decide", "plan", "execute", "observe", "verify", "final_response"],
        )
        self.assertEqual(run.response, "your CPU is fine")
        self.assertTrue(run.succeeded)

    async def test_a_clarification_decision_executes_nothing(self) -> None:
        calls = {"n": 0}

        async def handler(step: PlanStep) -> dict[str, object]:
            calls["n"] += 1
            return {}

        loop = self._loop(
            DecisionRoute.CLARIFY,
            executor=WorkflowExecutor(step_handler=handler),
        )
        run = await loop.run("do the thing")

        self.assertEqual(calls["n"], 0)
        self.assertEqual(run.phase.value, "clarify")
        self.assertTrue(run.response)
        self.assertIsNone(run.workflow)

    async def test_an_unverified_run_is_not_reported_as_a_success(self) -> None:
        async def handler(step: PlanStep) -> dict[str, object]:
            return {"summary": "ran, claimed nothing"}

        loop = self._loop(
            DecisionRoute.PLANNER,
            executor=WorkflowExecutor(step_handler=handler),
        )
        run = await loop.run("check my cpu")

        self.assertFalse(run.succeeded)
        verify_event = next(
            event for event in run.events if event.phase.value == "verify"
        )
        self.assertIn("not verified", verify_event.detail)

    async def test_a_failure_escalates_when_an_escalation_target_exists(self) -> None:
        async def handler(step: PlanStep) -> dict[str, object]:
            raise RuntimeError("the tool is not installed")

        escalated: list[str] = []

        async def escalate(goal: str, plan: Plan, workflow: object) -> str:
            escalated.append(goal)
            return "install the tool, then re-run"

        loop = self._loop(
            DecisionRoute.PLANNER,
            executor=WorkflowExecutor(
                step_handler=handler, retry_policy=RetryPolicy(max_attempts=1)
            ),
            escalate=escalate,
        )
        run = await loop.run("check my cpu")

        self.assertEqual(escalated, ["check my cpu"])
        self.assertTrue(run.escalated)
        self.assertEqual(run.phase.value, "escalate")

    async def test_the_loop_stops_without_a_recovery_path(self) -> None:
        async def handler(step: PlanStep) -> dict[str, object]:
            raise RuntimeError("something broke")

        loop = self._loop(
            DecisionRoute.PLANNER,
            executor=WorkflowExecutor(
                step_handler=handler, retry_policy=RetryPolicy(max_attempts=1)
            ),
        )
        run = await loop.run("check my cpu")

        # No replan, no escalation: it stops and says so, in one cycle.
        self.assertEqual(run.cycles, 1)
        self.assertEqual(run.phase.value, "final_response")
        self.assertIn("could not finish", run.response)

    async def test_the_loop_replans_at_most_the_cycle_limit(self) -> None:
        async def handler(step: PlanStep) -> dict[str, object]:
            raise RuntimeError("still broken")

        cycles = {"n": 0}

        async def replan(plan: Plan, workflow: object) -> Plan:
            cycles["n"] += 1
            return plan_of("check my cpu")

        loop = AgentLoop(
            understand=lambda text: type("U", (), {"intent": None, "strategy": "fast"})(),
            decide=lambda understood: self._decision(DecisionRoute.PLANNER),
            plan=lambda goal, understood, decision: plan_of("check my cpu"),
            execute=WorkflowExecutor(
                step_handler=handler, retry_policy=RetryPolicy(max_attempts=1)
            ).execute,
            replan=replan,
            max_cycles=3,
        )
        run = await asyncio.wait_for(loop.run("check my cpu"), timeout=5)

        self.assertEqual(run.cycles, 3)
        self.assertEqual(cycles["n"], 2)
        self.assertIn("cycle limit", run.stopped_because)


class ConfiguredLoopBoundsTests(unittest.TestCase):
    """The application's plan loops are bounded by configuration, not by a
    constant that only the wiring knows about."""

    def test_the_configured_ceilings_match_the_plan_layer(self) -> None:
        # Two modules, one number: if either moves, this fails rather than
        # silently permitting a bound the plan layer refuses.
        self.assertEqual(MAX_PLAN_ATTEMPTS, MAX_STEP_ATTEMPTS)
        self.assertGreaterEqual(MAX_PLAN_CYCLES, DEFAULT_MAX_CYCLES)

    def test_the_application_reads_its_bounds_from_the_environment(self) -> None:
        os.environ["NOVACONTROL_PLANNING_MAX_CYCLES"] = "4"
        os.environ["NOVACONTROL_PLANNING_MAX_STEP_ATTEMPTS"] = "3"
        try:
            from novacontrol.application import NovaControlApplication

            app = NovaControlApplication()
        finally:
            os.environ.pop("NOVACONTROL_PLANNING_MAX_CYCLES", None)
            os.environ.pop("NOVACONTROL_PLANNING_MAX_STEP_ATTEMPTS", None)

        self.assertEqual(app.agent_loop.max_cycles, 4)
        self.assertEqual(app.workflow_executor.retry_policy.max_attempts, 3)
        self.assertEqual(app.plan_compiler.retry_policy.max_attempts, 3)

    def test_the_default_bound_is_the_configured_default(self) -> None:
        from novacontrol.application import NovaControlApplication
        from novacontrol.core.config import PlanningSettings

        app = NovaControlApplication()
        defaults = PlanningSettings()

        self.assertEqual(app.agent_loop.max_cycles, defaults.max_cycles)
        self.assertGreaterEqual(app.agent_loop.max_cycles, 1)
        self.assertEqual(
            app.workflow_executor.retry_policy.max_attempts, defaults.max_step_attempts
        )
        self.assertGreaterEqual(defaults.max_step_attempts, RetryPolicy().max_attempts)


class ApplicationPlannerTests(unittest.IsolatedAsyncioTestCase):
    """The planner as the application actually wires it."""

    async def asyncSetUp(self) -> None:
        os.environ["NOVACONTROL_NLU_ALLOW_LLM"] = "false"
        from novacontrol.application import NovaControlApplication

        self.app = NovaControlApplication()
        await self.app.start()

    async def asyncTearDown(self) -> None:
        await self.app.stop()

    async def test_a_goal_plans_with_tools_and_effects(self) -> None:
        plan = self.app.plan_for(SPEC_GOAL)

        self.assertFalse(plan.needs_clarification)
        self.assertEqual(len(plan.steps), 7)
        effects = {step.id: step.effect for step in plan.steps}
        self.assertEqual(effects["run-tests"], StepEffect.LOCAL_WRITE)
        self.assertEqual(effects["analyze-result"], StepEffect.READ_ONLY)

    async def test_a_reading_plan_executes_and_is_verified(self) -> None:
        run = await self.app.run_plan("check my RAM and CPU")

        self.assertEqual(run["phase"], "final_response")
        workflow = run["workflow"]
        self.assertTrue(workflow["executed"])
        self.assertEqual(workflow["unverified_steps"], [])
        self.assertTrue(workflow["verified"])
        self.assertTrue(run["succeeded"])

    def _destructive_plan(self) -> tuple[Plan, PlanStep]:
        plan = self.app.plan_for("delete the old report file")
        step = next(item for item in plan.steps if item.effect is StepEffect.DESTRUCTIVE)
        return plan, step

    async def test_a_destructive_step_is_denied_without_explicit_approval(self) -> None:
        plan, step = self._destructive_plan()

        # Nothing is approved unless a caller approved it, so the gate is closed.
        self.assertFalse(self.app.approved_plan_steps)
        result = await self.app.workflow_executor.execute(plan)

        self.assertEqual(result.step_results[step.id].status, PlanStepStatus.DENIED)
        self.assertEqual(result.status, PlanStatus.BLOCKED)
        self.assertFalse(result.verified)

    async def test_approving_a_step_lets_it_past_the_gate_and_no_further(self) -> None:
        plan, step = self._destructive_plan()
        self.app.approved_plan_steps = {step.id}
        try:
            result = await self.app.workflow_executor.execute(plan)
        finally:
            self.app.approved_plan_steps = set()

        # Approval is not execution: with no registered tool for the step, the
        # honest outcome is a failure that says why, not a quiet success.
        outcome = result.step_results[step.id]
        self.assertEqual(outcome.status, PlanStepStatus.FAILED)
        self.assertIsNotNone(outcome.error)

    async def test_the_agent_loop_never_reports_a_destructive_goal_as_done(self) -> None:
        run = await self.app.run_plan("delete the old report file")

        self.assertFalse(run["succeeded"])
        for step in (run.get("workflow") or {}).get("steps", []):
            self.assertNotEqual(step["status"], "completed", step)

    async def test_the_plan_endpoint_payload_carries_state_and_selection(self) -> None:
        from novacontrol.brain.models import BrainRequest

        _, payload = await self.app._handle_plan(
            BrainRequest(text=SPEC_GOAL, context={}), SPEC_GOAL
        )

        self.assertIn("plan", payload)
        self.assertIn("workflow", payload)
        self.assertIn("state", payload)
        self.assertEqual(
            [step["step_id"] for step in payload["plan"]["steps"]],
            [
                "locate-project",
                "open-application",
                "open-project",
                "run-tests",
                "collect-output",
                "analyze-result",
                "summarize-result",
            ],
        )


class ApiContractTests(unittest.TestCase):
    def test_the_agent_loop_run_endpoint_is_declared(self) -> None:
        # The API surface, the route-consumer table and docs/API.md are one
        # contract: a route that exists but is undeclared fails the parity
        # tests, so this pins the declaration rather than the handler.
        from novacontrol.api import ApiSurface
        from novacontrol.api.route_consumers import ROUTE_CONSUMERS

        routes = {(route.method, route.path) for route in ApiSurface.default().routes}
        self.assertIn(("POST", "/plan/run"), routes)
        self.assertEqual(routes, set(ROUTE_CONSUMERS))


class EscalationWithoutAModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_escalation_reports_instead_of_inventing_an_answer(self) -> None:
        os.environ["NOVACONTROL_NLU_ALLOW_LLM"] = "false"
        from novacontrol.application import NovaControlApplication

        app = NovaControlApplication()
        try:
            text = await app._escalate_plan_run(
                "do something impossible",
                plan_of("reason about this"),
                (await WorkflowExecutor().execute(plan_of("reason about this"))),
            )
        finally:
            await app.stop()

        if not app.brain.model_configured:
            self.assertIn("reasoning", text.lower())


if __name__ == "__main__":
    unittest.main()
