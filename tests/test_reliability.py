"""Phase 8: reliability, recovery and safety.

Four rules are worth testing more than the code around them:

  * **A call is not a completion.** A step that ran is only COMPLETED when a
    check PASSED; a check that could not be made is reported as unknown, and an
    action whose effect nobody can observe is SKIPPED rather than believed.
  * **Recovery is bounded and never repeats what it may not repeat.** Retries
    stop at the policy's ceiling, a destructive action is never retried, and a
    refusal is answered by a person or not at all.
  * **A task's states are checked.** An illegal transition is an error, not a
    silent overwrite, and pause, resume and cancel keep the state they found.
  * **One permission layer.** Risk is declared or derived in exactly one place,
    and a destructive action without approval does not run.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from novacontrol.application import NovaControlApplication
from novacontrol.core.security import PermissionScope, RiskLevel
from novacontrol.planning import (
    DeterministicVerifier,
    Plan,
    PlanStep,
    PlanStepStatus,
    RetryPolicy,
    StepEffect,
    VerificationMethod,
    VerificationResult,
    VerificationSpec,
    VerificationStatus,
    WorkflowExecutor,
    WorkflowResult,
)
from novacontrol.planning.models import FailureKind, StepError
from novacontrol.reliability import (
    InvalidTransitionError,
    PermissionDeclaration,
    PermissionManager,
    PermissionPolicy,
    RecoveryEngine,
    RecoveryKind,
    TaskCancelled,
    TaskCommand,
    TaskController,
    TaskState,
    TaskStateMachine,
    VerificationEngine,
    parse_task_command,
)
from novacontrol.tools import (
    FunctionTool,
    ToolExecutor,
    ToolRegistry,
    ToolRequest,
    ToolSchema,
)
from novacontrol.tools.metadata import ToolCategory, ToolMetadata


def _passing(step: PlanStep, output: dict[str, object]) -> VerificationResult:
    del step, output
    return VerificationResult(
        status=VerificationStatus.PASS,
        method=VerificationMethod.STATE_OBSERVED,
        expectation="the state changed",
        observed="changed=True",
    )


# --------------------------------------------------------------------------- #
# 8.1 Verification engine
# --------------------------------------------------------------------------- #


class VerificationEngineTests(unittest.TestCase):
    """The check that applies to a step, and what its result may claim."""

    def test_a_write_is_checked_against_the_file_it_claims_to_write(self) -> None:
        # "Never assume a successful call means a completed action": the step
        # attached no check, so the engine derives one from the action, and the
        # answer is about the FILE rather than about the tool's return value.
        engine = VerificationEngine()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.txt"
            step = PlanStep(
                "Write the report",
                "Write it to disk.",
                action="write_file",
                tool="write_file",
                parameters={"path": str(target)},
            )
            missing = engine.verify(step, {"success": True})

            self.assertEqual(missing.status, VerificationStatus.FAIL)
            self.assertFalse(missing.success)
            self.assertEqual(missing.method, VerificationMethod.FILE_EXISTS)
            self.assertIn("exists=False", missing.reason)
            self.assertGreater(missing.confidence, 0.5)
            self.assertIn(":derived", missing.verifier)

            target.write_text("done", encoding="utf-8")
            present = engine.verify(step, {"success": True})

            self.assertEqual(present.status, VerificationStatus.PASS)
            self.assertTrue(present.success)
            self.assertTrue(present.verified)
            self.assertEqual(present.actual_state, "exists=True")

    def test_an_action_with_no_observable_effect_is_skipped_not_passed(self) -> None:
        engine = VerificationEngine()
        step = PlanStep(
            "Read the memory usage",
            "Ask the system monitor for RAM.",
            action="get_ram",
            tool="system_monitor",
            parameters={"device": "ram"},
        )
        result = engine.verify(step, {"ram_gb": 32})

        self.assertEqual(result.status, VerificationStatus.SKIPPED)
        self.assertFalse(result.success)
        self.assertFalse(result.verified)

    def test_an_action_whose_argument_is_missing_derives_nothing(self) -> None:
        engine = VerificationEngine()
        step = PlanStep(
            "Open the editor", "Open it.", action="open_application", tool="open_application"
        )
        self.assertEqual(engine.default_spec_for(step).method, VerificationMethod.NONE)
        self.assertEqual(engine.verify(step, {}).status, VerificationStatus.SKIPPED)

    def test_opening_an_application_is_checked_against_the_process(self) -> None:
        engine = VerificationEngine(process_probe=lambda name: name == "code")
        step = PlanStep(
            "Open VS Code",
            "Launch the editor.",
            action="open_application",
            tool="open_application",
            parameters={"application": "code"},
        )

        self.assertEqual(engine.default_spec_for(step).method, VerificationMethod.PROCESS_RUNNING)
        self.assertEqual(engine.verify(step, {}).status, VerificationStatus.PASS)

        closed = VerificationEngine(process_probe=lambda name: False)
        failure = closed.verify(step, {})
        self.assertEqual(failure.status, VerificationStatus.FAIL)
        self.assertIn("code", failure.reason)

    def test_a_process_probe_that_cannot_tell_is_not_a_failure(self) -> None:
        # No psutil, no window probe: the check says so instead of reporting a
        # failure nobody measured.
        engine = VerificationEngine(process_probe=lambda name: None)
        step = PlanStep(
            "Open VS Code",
            "Launch the editor.",
            action="open_application",
            parameters={"application": "code"},
        )
        result = engine.verify(step, {})

        self.assertEqual(result.status, VerificationStatus.INCONCLUSIVE)
        self.assertFalse(result.success)
        self.assertEqual(result.confidence, 0.0)
        self.assertIn("Cannot tell", result.reason)

    def test_the_steps_own_check_outranks_the_tools_strategy(self) -> None:
        engine = VerificationEngine()
        engine.register_tool_strategy("write_file", lambda step, out: (True, "trust me"))
        step = PlanStep(
            "Write the report",
            "Write it.",
            tool="write_file",
            verification=VerificationSpec(
                method=VerificationMethod.FILE_EXISTS, target="does/not/exist.txt"
            ),
        )
        result = engine.verify(step, {})

        self.assertEqual(result.status, VerificationStatus.FAIL)
        self.assertNotIn(":write_file", result.verifier)

    def test_a_registered_tool_strategy_is_used_and_stamped(self) -> None:
        engine = VerificationEngine()
        engine.register_tool_strategy(
            "publish_report", lambda step, out: bool(out.get("published"))
        )
        step = PlanStep("Publish", "Publish it.", action="publish_report", tool="publish_report")

        self.assertEqual(engine.tool_strategies(), ("publish_report",))
        passed = engine.verify(step, {"published": True})
        self.assertEqual(passed.status, VerificationStatus.PASS)
        self.assertEqual(passed.verifier, "verification-engine:publish_report")

        failed = engine.verify(step, {"published": False})
        self.assertEqual(failed.status, VerificationStatus.FAIL)

    def test_a_strategy_that_raises_is_an_unknown_not_a_pass(self) -> None:
        def broken(step: PlanStep, output: dict[str, object]) -> bool:
            raise RuntimeError("the check itself is broken")

        engine = VerificationEngine()
        engine.register_tool_strategy("publish_report", broken)
        result = engine.run_strategy(
            "publish_report",
            broken,
            step=PlanStep("Publish", "Publish it.", tool="publish_report"),
            output={},
        )

        self.assertEqual(result.status, VerificationStatus.INCONCLUSIVE)
        self.assertFalse(result.success)
        self.assertIn("RuntimeError", result.error)

    def test_a_strategy_that_returns_prose_has_not_verified_anything(self) -> None:
        engine = VerificationEngine()
        engine.register_tool_strategy("publish_report", lambda step, out: "looks fine to me")
        result = engine.verify(
            PlanStep("Publish", "Publish it.", tool="publish_report"), {}
        )
        self.assertEqual(result.status, VerificationStatus.INCONCLUSIVE)
        self.assertIn("looks fine to me", result.reason)

    def test_a_tool_that_knows_how_to_check_itself_is_asked(self) -> None:
        engine = VerificationEngine()
        engine.register_tool_strategy(
            "send_message", lambda step, out: (True, "the endpoint acked it")
        )
        result = engine.verify_tool(
            "send_message", {"success": True}, action="send_message", parameters={"message": "hi"}
        )
        self.assertEqual(result.status, VerificationStatus.PASS)
        self.assertIn("acked", result.reason)

    def test_a_tool_nothing_can_check_says_so(self) -> None:
        engine = VerificationEngine()
        result = engine.verify_tool("mystery_tool", {"whatever": 1}, action="mystery_tool")
        self.assertEqual(result.status, VerificationStatus.SKIPPED)
        self.assertIn("No verification is available", result.reason)

    def test_a_window_is_checked_when_a_probe_is_wired(self) -> None:
        engine = VerificationEngine(window_probe=lambda name: "code" in name.lower())
        spec = VerificationSpec(method=VerificationMethod.WINDOW_EXISTS, target="VS Code")
        self.assertEqual(engine.verify_spec(spec, {}).status, VerificationStatus.PASS)

        unwired = VerificationEngine()
        result = unwired.verify_spec(spec, {})
        self.assertEqual(result.status, VerificationStatus.INCONCLUSIVE)
        self.assertIn("Cannot tell", result.reason)

    def test_reachability_is_checked_when_a_probe_is_wired(self) -> None:
        engine = VerificationEngine(network_probe=lambda target: target.startswith("https://"))
        spec = VerificationSpec(method=VerificationMethod.NETWORK_REACHABLE, target="https://example.com")
        self.assertEqual(engine.verify_spec(spec, {}).status, VerificationStatus.PASS)

        unreachable = VerificationEngine(network_probe=lambda target: False)
        self.assertEqual(
            unreachable.verify_spec(spec, {}).status, VerificationStatus.FAIL
        )

    def test_a_malformed_endpoint_is_unknown_rather_than_unreachable(self) -> None:
        engine = VerificationEngine()
        spec = VerificationSpec(method=VerificationMethod.NETWORK_REACHABLE, target="   ")
        self.assertEqual(engine.verify_spec(spec, {}).status, VerificationStatus.INCONCLUSIVE)
        self.assertEqual(engine.probe_network(""), None)

    def test_a_tools_own_report_is_read_as_a_report(self) -> None:
        engine = VerificationEngine()
        spec = VerificationSpec(method=VerificationMethod.RESULT_REPORTED)

        reported = engine.verify_spec(spec, {"success": True})
        self.assertEqual(reported.status, VerificationStatus.PASS)
        self.assertEqual(reported.confidence, 0.6)
        self.assertEqual(reported.metadata["evidence_kind"], "reported")
        self.assertIn("not an observed state", reported.reason)

        self.assertEqual(engine.verify_spec(spec, {}).status, VerificationStatus.INCONCLUSIVE)
        self.assertEqual(
            engine.verify_spec(spec, {"status": "failed"}).status, VerificationStatus.FAIL
        )

    def test_the_structured_result_carries_its_own_provenance(self) -> None:
        engine = VerificationEngine()
        result = engine.verify_spec(
            VerificationSpec(method=VerificationMethod.EXIT_CODE, target="pytest"), {"exit_code": 0}
        )
        payload = result.to_dict()

        for key in (
            "status",
            "method",
            "expectation",
            "observed",
            "reason",
            "evidence",
            "success",
            "verifier",
            "expected_state",
            "actual_state",
            "confidence",
            "error",
            "metadata",
        ):
            self.assertIn(key, payload)
        self.assertTrue(payload["success"])
        self.assertFalse(payload["error"])

    def test_the_engine_is_still_the_deterministic_verifier(self) -> None:
        # Backward compatibility: the registered CALLABLE checks the application
        # already relies on keep working through the engine.
        engine = VerificationEngine(callables={"file_written": lambda spec, output: True})
        result = engine.verify_spec(
            VerificationSpec(method=VerificationMethod.CALLABLE, target="file_written"), {}
        )
        self.assertEqual(result.status, VerificationStatus.PASS)
        self.assertEqual(engine.report()["callables"], ["file_written"])

    def test_disable_derivation_and_a_stepless_check_is_skipped(self) -> None:
        engine = VerificationEngine(derive_defaults=False)
        step = PlanStep(
            "Write the report",
            "Write it.",
            action="write_file",
            tool="write_file",
            parameters={"path": "report.txt"},
        )
        self.assertEqual(engine.verify(step, {}).status, VerificationStatus.SKIPPED)


class VerificationBackwardCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    """The executor and the verifier it already had are unchanged."""

    async def test_a_plain_verifier_still_reports_a_checkless_step_as_unverified(self) -> None:
        async def handler(step: PlanStep) -> dict[str, object]:
            return {"ok": True}

        plan = Plan(
            goal="write a file",
            steps=(
                PlanStep(
                    "Write it",
                    "Write the file.",
                    id="write",
                    action="write_file",
                    tool="write_file",
                    parameters={"path": "nowhere/at/all.txt"},
                ),
            ),
        )
        result = await WorkflowExecutor(
            step_handler=handler, verifier=DeterministicVerifier()
        ).execute(plan)

        step = result.step_results["write"]
        self.assertEqual(step.status, PlanStepStatus.UNVERIFIED)
        self.assertEqual(step.verification_result, "skipped")

    async def test_the_engine_turns_the_same_step_into_a_reported_failure(self) -> None:
        async def handler(step: PlanStep) -> dict[str, object]:
            return {"ok": True}

        plan = Plan(
            goal="write a file",
            steps=(
                PlanStep(
                    "Write it",
                    "Write the file.",
                    id="write",
                    action="write_file",
                    tool="write_file",
                    parameters={"path": "nowhere/at/all.txt"},
                ),
            ),
        )
        result = await WorkflowExecutor(
            step_handler=handler, verifier=VerificationEngine()
        ).execute(plan)

        step = result.step_results["write"]
        self.assertEqual(step.status, PlanStepStatus.FAILED)
        self.assertEqual(step.error.kind, FailureKind.VERIFICATION_FAILED)


# --------------------------------------------------------------------------- #
# 8.2 Recovery engine
# --------------------------------------------------------------------------- #


class RecoveryEngineTests(unittest.IsolatedAsyncioTestCase):
    """Bounded, safe, and honest about what it could not recover."""

    async def test_a_transient_failure_is_retried_and_then_succeeds(self) -> None:
        engine = RecoveryEngine(retry_policy=RetryPolicy(max_attempts=2))
        attempts = 0

        async def execute(step: PlanStep) -> dict[str, object]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporarily unavailable")
            return {"summary": "done"}

        step = PlanStep("Open Code", "Open it.", tool="open_application")
        run = await engine.run_with_recovery(step, execute=execute, verify=_passing)

        self.assertTrue(run.succeeded)
        self.assertTrue(run.verified)
        self.assertEqual(run.attempts, 2)
        self.assertEqual(run.plans[0].kind, RecoveryKind.RETRY)

    async def test_the_retry_budget_is_a_ceiling_not_a_suggestion(self) -> None:
        engine = RecoveryEngine(retry_policy=RetryPolicy(max_attempts=2))
        calls: list[int] = []

        async def execute(step: PlanStep) -> dict[str, object]:
            calls.append(1)
            raise RuntimeError("temporarily unavailable")

        run = await engine.run_with_recovery(
            PlanStep("Open Code", "Open it.", tool="open_application"),
            execute=execute,
            verify=_passing,
        )

        self.assertFalse(run.succeeded)
        self.assertEqual(len(calls), 2)
        self.assertEqual(run.attempts, 2)
        self.assertIn("exhausted", run.summary)

    async def test_the_hard_ceiling_cannot_be_configured_away(self) -> None:
        engine = RecoveryEngine(retry_policy=RetryPolicy(max_attempts=10))
        calls: list[int] = []

        async def execute(step: PlanStep) -> dict[str, object]:
            calls.append(1)
            raise RuntimeError("temporarily unavailable")

        await engine.run_with_recovery(
            PlanStep("Open Code", "Open it.", tool="open_application"),
            execute=execute,
            verify=_passing,
            max_attempts=1,
        )
        self.assertEqual(len(calls), 1)

    async def test_a_destructive_action_is_stopped_rather_than_retried(self) -> None:
        from novacontrol.planning import StepEffect

        engine = RecoveryEngine(retry_policy=RetryPolicy(max_attempts=5))
        calls: list[int] = []

        async def execute(step: PlanStep) -> dict[str, object]:
            calls.append(1)
            raise RuntimeError("access is denied halfway through")

        run = await engine.run_with_recovery(
            PlanStep(
                "Delete the folder",
                "Remove it.",
                tool="file_delete",
                effect=StepEffect.DESTRUCTIVE,
            ),
            execute=execute,
            verify=_passing,
        )

        self.assertEqual(len(calls), 1)
        self.assertFalse(run.succeeded)
        self.assertTrue(run.stopped_safely)
        self.assertIn("never repeated", run.plans[0].reason)

    async def test_a_denial_asks_a_person_and_never_retries_itself(self) -> None:
        engine = RecoveryEngine(
            retry_policy=RetryPolicy(max_attempts=3), confirmation_available=True
        )
        calls: list[int] = []

        async def execute(step: PlanStep) -> dict[str, object]:
            calls.append(1)
            raise PermissionError("the action was not approved")

        step = PlanStep("Send the report", "Send it.", tool="send_report")
        run = await engine.run_with_recovery(
            step, execute=execute, verify=_passing, confirm=lambda step: False
        )

        self.assertEqual(len(calls), 1)
        self.assertTrue(run.requires_confirmation)
        self.assertIn("was not approved", run.summary)

    async def test_an_approved_action_is_tried_again(self) -> None:
        engine = RecoveryEngine(
            retry_policy=RetryPolicy(max_attempts=2), confirmation_available=True
        )
        attempts = 0

        async def execute(step: PlanStep) -> dict[str, object]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError("the action was not approved")
            return {"summary": "sent"}

        run = await engine.run_with_recovery(
            PlanStep("Send the report", "Send it.", tool="send_report"),
            execute=execute,
            verify=_passing,
            confirm=lambda step: True,
        )

        self.assertTrue(run.succeeded)
        self.assertEqual(run.attempts, 2)
        self.assertTrue(run.plans[0].requires_confirmation)

    async def test_without_a_confirmation_channel_a_denial_stops_safely(self) -> None:
        engine = RecoveryEngine(retry_policy=RetryPolicy(max_attempts=3))

        async def execute(step: PlanStep) -> dict[str, object]:
            raise PermissionError("approval was not granted")

        run = await engine.run_with_recovery(
            PlanStep("Send the report", "Send it.", tool="send_report"),
            execute=execute,
            verify=_passing,
        )

        self.assertFalse(run.succeeded)
        self.assertTrue(run.plans[0].stopped_safely)
        self.assertIn("no one to ask", run.plans[0].reason)

    async def test_a_registered_alternative_is_what_gets_tried_next(self) -> None:
        engine = RecoveryEngine(retry_policy=RetryPolicy(max_attempts=3))
        seen: list[str] = []

        def alternative(step: PlanStep, error: StepError) -> PlanStep | None:
            if "not registered" not in error.message:
                return None
            return step.with_(tool="desktop_open_application")

        engine.register_alternative("open_application", alternative)

        async def execute(step: PlanStep) -> dict[str, object]:
            seen.append(step.tool)
            if step.tool != "desktop_open_application":
                raise RuntimeError("tool is not registered")
            return {"summary": "opened"}

        run = await engine.run_with_recovery(
            PlanStep("Open Code", "Open it.", tool="open_application"),
            execute=execute,
            verify=_passing,
        )

        self.assertTrue(run.succeeded)
        self.assertEqual(seen, ["open_application", "desktop_open_application"])
        self.assertEqual(run.plans[0].kind, RecoveryKind.ALTERNATIVE)
        self.assertEqual(engine.alternatives(), ("open_application",))

    async def test_an_alternative_that_loops_back_is_not_tried_twice(self) -> None:
        # Two built-ins can name each other. Bouncing between them until the
        # budget runs out is a retry loop wearing two names, so the second sight
        # of a tool this run already failed with stops it.
        engine = RecoveryEngine(
            retry_policy=RetryPolicy(max_attempts=4),
            known_tools=("open_application", "desktop_open_application"),
        )
        seen: list[str] = []

        async def execute(step: PlanStep) -> dict[str, object]:
            seen.append(step.tool)
            raise RuntimeError("tool is not registered")

        run = await engine.run_with_recovery(
            PlanStep("Open Code", "Open it.", tool="open_application"),
            execute=execute,
            verify=_passing,
        )

        self.assertEqual(seen, ["open_application", "desktop_open_application"])
        self.assertFalse(run.succeeded)
        self.assertIn("no different way", run.summary)

    def test_a_built_in_alternative_is_only_offered_when_its_tool_exists(self) -> None:
        error = StepError(FailureKind.MISSING_DEPENDENCY, "tool is not registered")
        step = PlanStep("Open Code", "Open it.", tool="open_application")

        known = RecoveryEngine(known_tools=("desktop_open_application",))
        self.assertEqual(
            known.alternative_for(step, error).tool, "desktop_open_application"
        )

        unknown = RecoveryEngine(known_tools=("something_else",))
        self.assertIsNone(unknown.alternative_for(step, error))

        late = RecoveryEngine(known_tools=lambda: ("desktop_open_application",))
        self.assertEqual(
            late.alternative_for(step, error).tool, "desktop_open_application"
        )

    def test_an_alternative_is_never_offered_for_a_destructive_action(self) -> None:
        from novacontrol.planning import StepEffect

        engine = RecoveryEngine(known_tools=("desktop_delete_file",))
        step = PlanStep(
            "Delete it", "Delete it.", tool="delete_file", effect=StepEffect.DESTRUCTIVE
        )
        error = StepError(FailureKind.MISSING_DEPENDENCY, "tool is not registered")
        self.assertIsNone(engine.alternative_for(step, error))

    async def test_missing_machinery_escalates_when_something_can_take_over(self) -> None:
        engine = RecoveryEngine(
            retry_policy=RetryPolicy(max_attempts=3), escalation_available=True
        )

        async def execute(step: PlanStep) -> dict[str, object]:
            raise RuntimeError("the dependency is not installed")

        run = await engine.run_with_recovery(
            PlanStep("Build it", "Build the project.", tool="project_build"),
            execute=execute,
            verify=_passing,
        )

        self.assertFalse(run.succeeded)
        self.assertEqual(run.plans[0].kind, RecoveryKind.ESCALATE)
        self.assertIn("needs reasoning", run.summary)

    async def test_missing_machinery_stops_honestly_with_nowhere_to_escalate(self) -> None:
        engine = RecoveryEngine(retry_policy=RetryPolicy(max_attempts=3))

        async def execute(step: PlanStep) -> dict[str, object]:
            raise RuntimeError("the dependency is not installed")

        run = await engine.run_with_recovery(
            PlanStep("Build it", "Build the project.", tool="project_build"),
            execute=execute,
            verify=_passing,
        )

        self.assertEqual(run.plans[0].kind, RecoveryKind.STOP)
        self.assertIn("no escalation target", run.plans[0].reason)

    async def test_a_result_nobody_can_check_is_unconfirmed_rather_than_retried(self) -> None:
        engine = RecoveryEngine(
            retry_policy=RetryPolicy(max_attempts=3), verifier=VerificationEngine()
        )
        calls: list[int] = []

        async def execute(step: PlanStep) -> dict[str, object]:
            calls.append(1)
            return {"summary": "did something"}

        run = await engine.run_with_recovery(
            PlanStep("Do the thing", "Do it.", tool="mystery_tool"),
            execute=execute,
            verify=None,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(run.attempts, 1)
        self.assertFalse(run.succeeded)
        self.assertFalse(run.verified)
        self.assertTrue(run.executed)
        self.assertIn("unconfirmed", run.summary)

    def test_a_recovery_plan_speaks_the_executors_vocabulary(self) -> None:
        # A repair needs BUDGET: the default policy is "run once, never retry",
        # so the same failure under it escalates instead of being repaired.
        engine = RecoveryEngine(retry_policy=RetryPolicy(max_attempts=2))
        plan = engine.plan_recovery(
            PlanStep(
                "Write it",
                "Write it.",
                tool="write_file",
                parameters={"path": "out.txt", "mode": "fast"},
            ),
            StepError(FailureKind.INVALID_PARAMETERS, "unknown argument: mode", retryable=True),
            attempts_made=1,
        )

        self.assertEqual(plan.kind, RecoveryKind.ALTERNATIVE)
        self.assertEqual(plan.as_decision().action.value, "modify_parameters")
        self.assertIn("mode", plan.adjustments)

    def test_analysis_separates_safe_to_repeat_from_allowed_to_repeat(self) -> None:
        engine = RecoveryEngine(retry_policy=RetryPolicy(max_attempts=2))
        analysis = engine.analyze_failure(
            PlanStep("Open Code", "Open it.", tool="open_application"),
            StepError(FailureKind.TRANSIENT, "connection reset", retryable=True),
            attempts_made=1,
        )
        self.assertTrue(analysis.retry_safe)
        self.assertTrue(analysis.attempts_left)

        spent = engine.analyze_failure(
            PlanStep("Open Code", "Open it.", tool="open_application"),
            StepError(FailureKind.TRANSIENT, "connection reset", retryable=True),
            attempts_made=2,
        )
        self.assertFalse(spent.attempts_left)


# --------------------------------------------------------------------------- #
# 8.3 Task state machine
# --------------------------------------------------------------------------- #


class TaskStateMachineTests(unittest.TestCase):
    """The legal moves, and the refusal to make an illegal one."""

    def test_a_task_walks_the_normal_path(self) -> None:
        machine = TaskStateMachine(task_id="t1")
        machine.start_planning()
        machine.start_running(current_step="write")
        machine.begin_verification(current_step="write")
        snapshot = machine.complete()

        self.assertEqual(snapshot.current_state, TaskState.COMPLETED)
        self.assertEqual(snapshot.previous_state, TaskState.VERIFYING)
        self.assertTrue(snapshot.terminal)
        self.assertEqual(
            machine.history,
            (
                TaskState.PENDING,
                TaskState.PLANNING,
                TaskState.RUNNING,
                TaskState.VERIFYING,
                TaskState.COMPLETED,
            ),
        )

    def test_every_field_the_specification_asks_for_travels_with_the_task(self) -> None:
        machine = TaskStateMachine(task_id="t1", parent_task_id="parent", metadata={"goal": "ship"})
        machine.start_planning()
        machine.start_running(current_step="step-1")
        payload = machine.snapshot.to_dict()

        for key in (
            "task_id",
            "parent_task_id",
            "current_state",
            "previous_state",
            "current_step",
            "retry_count",
            "error",
            "cancellation_requested",
            "pause_requested",
            "metadata",
            "created_at",
            "updated_at",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["parent_task_id"], "parent")
        self.assertEqual(payload["current_step"], "step-1")

    def test_an_illegal_transition_is_refused_with_the_reason(self) -> None:
        machine = TaskStateMachine(task_id="t1")
        with self.assertRaises(InvalidTransitionError) as caught:
            machine.complete()

        message = str(caught.exception)
        self.assertIn("pending", message)
        self.assertIn("completed", message)
        self.assertIn("planning", message)
        self.assertEqual(machine.state, TaskState.PENDING)

    def test_a_terminal_state_cannot_be_left(self) -> None:
        machine = TaskStateMachine(task_id="t1")
        machine.start_planning()
        machine.cancel()
        self.assertEqual(machine.state, TaskState.CANCELLED)
        self.assertFalse(machine.can_transition(TaskState.RUNNING))
        with self.assertRaises(InvalidTransitionError):
            machine.start_running()

    def test_failed_and_cancelled_are_reachable_from_any_working_state(self) -> None:
        for state in (
            TaskState.PLANNING,
            TaskState.RUNNING,
            TaskState.WAITING,
            TaskState.VERIFYING,
            TaskState.RECOVERING,
            TaskState.RETRYING,
        ):
            with self.subTest(state=state):
                machine = TaskStateMachine(task_id="t1", state=state)
                self.assertTrue(machine.can_transition(TaskState.FAILED))
                self.assertTrue(machine.can_transition(TaskState.CANCELLED))

    def test_a_retry_is_counted(self) -> None:
        machine = TaskStateMachine(task_id="t1", state=TaskState.RUNNING)
        machine.begin_retry(current_step="write")
        machine.start_running()
        machine.begin_retry(current_step="write")

        self.assertEqual(machine.snapshot.retry_count, 2)
        self.assertEqual(machine.state, TaskState.RETRYING)

    def test_a_resume_returns_to_the_state_that_was_interrupted(self) -> None:
        # The bug this pins: resuming always landed in RUNNING, so a task paused
        # while still PLANNING resumed as if it had a plan.
        planning = TaskStateMachine(task_id="t1")
        planning.start_planning()
        planning.pause()
        self.assertEqual(planning.state, TaskState.PAUSED)
        self.assertEqual(planning.resume().current_state, TaskState.PLANNING)

        running = TaskStateMachine(task_id="t2", state=TaskState.RUNNING)
        running.pause()
        self.assertEqual(running.resume().current_state, TaskState.RUNNING)

        # A phase that only a RUNNER can re-enter lands in the loop, because
        # claiming the verification resumed would be claiming the wrong thing.
        verifying = TaskStateMachine(task_id="t3", state=TaskState.VERIFYING)
        verifying.pause()
        self.assertEqual(verifying.resume().current_state, TaskState.RUNNING)

    def test_a_resume_can_be_directed_where_a_caller_knows_better(self) -> None:
        machine = TaskStateMachine(task_id="t1", state=TaskState.RUNNING)
        machine.pause()
        self.assertEqual(
            machine.resume(target=TaskState.WAITING).current_state, TaskState.WAITING
        )

    def test_every_change_can_be_observed(self) -> None:
        seen: list[tuple[str, str]] = []

        def observer(snapshot: object, previous: TaskState) -> None:
            seen.append((snapshot.current_state.value, previous.value))  # type: ignore[attr-defined]

        machine = TaskStateMachine(task_id="t1", observer=observer)
        machine.start_planning()
        machine.start_running()

        self.assertEqual(seen, [("planning", "pending"), ("running", "planning")])


# --------------------------------------------------------------------------- #
# 8.4 Pause / resume / cancel
# --------------------------------------------------------------------------- #


class TaskControlTests(unittest.IsolatedAsyncioTestCase):
    """Control commands that stop work without breaking it."""

    def test_the_phrases_a_person_uses_are_read_exactly(self) -> None:
        self.assertEqual(parse_task_command("pause"), TaskCommand.PAUSE)
        self.assertEqual(parse_task_command("Pause this task."), TaskCommand.PAUSE)
        self.assertEqual(parse_task_command("resume"), TaskCommand.RESUME)
        self.assertEqual(parse_task_command("carry on"), TaskCommand.RESUME)
        self.assertEqual(parse_task_command("cancel"), TaskCommand.CANCEL)
        self.assertEqual(parse_task_command("stop this task"), TaskCommand.CANCEL)
        self.assertEqual(parse_task_command("stop the task"), TaskCommand.CANCEL)

    def test_words_that_only_look_like_commands_are_not_commands(self) -> None:
        # "stop" alone would make every request a way to kill the running job.
        self.assertIsNone(parse_task_command("stop the music"))
        self.assertIsNone(parse_task_command("what is the status of my order"))
        self.assertIsNone(parse_task_command("open chrome"))
        self.assertIsNone(parse_task_command(""))

    async def test_cancellation_stops_at_a_checkpoint_and_raises(self) -> None:
        controller = TaskController()
        controller.open_task(task_id="t1", state=TaskState.RUNNING)

        result = controller.cancel("t1")
        self.assertTrue(result.accepted)
        self.assertEqual(result.state, TaskState.RUNNING)  # requested, not yet stopped

        with self.assertRaises(TaskCancelled):
            await controller.checkpoint("t1", step="write")
        self.assertEqual(controller.snapshot("t1").current_state, TaskState.CANCELLED)
        self.assertTrue(controller.snapshot("t1").cancellation_requested)

    async def test_a_cancellable_operation_is_told_to_stop(self) -> None:
        controller = TaskController()
        controller.open_task(task_id="t1", state=TaskState.RUNNING)
        told: list[str] = []
        controller.register_cancel_hook("t1", lambda task_id: told.append(task_id))

        controller.cancel("t1")
        with self.assertRaises(TaskCancelled):
            await controller.checkpoint("t1")

        self.assertEqual(told, ["t1"])

    async def test_a_pause_holds_the_work_and_a_resume_lets_it_continue(self) -> None:
        controller = TaskController()
        machine = controller.open_task(task_id="t1", state=TaskState.RUNNING)
        controller.save_checkpoint("t1", step="step-2", done=("step-1",))

        paused = controller.pause("t1")
        self.assertTrue(paused.accepted)
        self.assertEqual(paused.state, TaskState.PAUSED)
        self.assertTrue(machine.pause_requested)

        waiting = asyncio.create_task(controller.checkpoint("t1", step="step-2"))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())

        resumed = controller.resume("t1")
        self.assertTrue(resumed.accepted)
        self.assertIn("step-2", resumed.summary)
        await asyncio.wait_for(waiting, timeout=1)

        self.assertEqual(controller.snapshot("t1").current_state, TaskState.RUNNING)
        self.assertFalse(controller.snapshot("t1").pause_requested)
        self.assertEqual(controller.checkpoint_of("t1")["step"], "step-2")

    async def test_cancelling_a_paused_task_stops_it_where_it_stands(self) -> None:
        controller = TaskController()
        controller.open_task(task_id="t1", state=TaskState.RUNNING)
        controller.pause("t1")

        result = controller.cancel("t1")
        self.assertTrue(result.accepted)
        self.assertEqual(result.state, TaskState.CANCELLED)

    def test_a_pause_during_planning_resumes_into_planning(self) -> None:
        controller = TaskController()
        controller.open_task(task_id="t1", state=TaskState.PENDING)
        controller.machine("t1").start_planning()

        controller.pause("t1")
        resumed = controller.resume("t1")

        self.assertTrue(resumed.accepted)
        self.assertEqual(resumed.state, TaskState.PLANNING)
        self.assertIn("planning", resumed.summary)

    def test_resuming_something_that_never_paused_says_so(self) -> None:
        controller = TaskController()
        controller.open_task(task_id="t1", state=TaskState.RUNNING)

        result = controller.resume("t1")

        self.assertTrue(result.accepted)
        self.assertIn("was not paused", result.summary)

    def test_a_completed_task_cannot_be_paused_or_cancelled(self) -> None:
        controller = TaskController()
        controller.open_task(task_id="t1", state=TaskState.RUNNING)
        controller.machine("t1").complete()

        paused = controller.pause("t1")
        self.assertFalse(paused.accepted)
        self.assertIn("cannot go from completed", paused.summary)

        cancelled = controller.cancel("t1")
        self.assertFalse(cancelled.accepted)
        self.assertEqual(controller.snapshot("t1").current_state, TaskState.COMPLETED)

    def test_a_command_with_no_task_is_reported_rather_than_guessed(self) -> None:
        controller = TaskController()
        result = controller.handle_command("pause")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.accepted)
        self.assertIn("no task", result.summary)

    def test_a_command_reaches_the_newest_unfinished_task(self) -> None:
        controller = TaskController()
        controller.open_task(task_id="old", state=TaskState.RUNNING)
        controller.machine("old").complete()
        controller.open_task(task_id="new", state=TaskState.RUNNING)

        result = controller.handle_command("pause this task")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.task_id, "new")
        self.assertTrue(result.accepted)

    def test_the_status_command_reports_the_checkpoint(self) -> None:
        controller = TaskController()
        controller.open_task(task_id="t1", state=TaskState.RUNNING)
        controller.save_checkpoint("t1", step="step-3")

        result = controller.handle_command("status")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.command, TaskCommand.STATUS)
        self.assertEqual(result.checkpoint["step"], "step-3")

    def test_an_unknown_task_is_an_error_not_an_empty_answer(self) -> None:
        controller = TaskController()
        with self.assertRaises(KeyError):
            controller.pause("nope")


# --------------------------------------------------------------------------- #
# 8.5 Permission / risk system
# --------------------------------------------------------------------------- #


class _Catalog:
    """A tiny stand-in for the tool catalogue the manager reads metadata from."""

    def __init__(self, *entries: ToolMetadata) -> None:
        self._entries = {entry.name: entry for entry in entries}

    def get(self, name: str) -> ToolMetadata | None:
        return self._entries.get(name)


class PermissionManagerTests(unittest.TestCase):
    """One layer that knows how dangerous an action is."""

    def test_the_specifications_examples_are_the_derived_levels(self) -> None:
        manager = PermissionManager()
        self.assertEqual(manager.risk_of("", action="get_ram"), RiskLevel.LOW)
        self.assertEqual(manager.risk_of("", action="open_application"), RiskLevel.LOW)
        self.assertEqual(manager.risk_of("", action="create_file"), RiskLevel.LOW)
        self.assertEqual(manager.risk_of("", action="modify_source_code"), RiskLevel.MEDIUM)
        self.assertEqual(manager.risk_of("", action="delete_file"), RiskLevel.HIGH)
        self.assertEqual(manager.risk_of("", action="send_message"), RiskLevel.HIGH)
        self.assertEqual(manager.risk_of("", action="purchase_order"), RiskLevel.CRITICAL)

    def test_the_row_that_destroys_data_wins_over_the_row_that_touches_the_network(self) -> None:
        manager = PermissionManager()
        decision = manager.assess("", action="delete_order")
        self.assertEqual(decision.risk, RiskLevel.HIGH)
        self.assertTrue(decision.destructive)
        self.assertFalse(decision.reversible)

    def test_low_risk_needs_no_confirmation_and_medium_does(self) -> None:
        manager = PermissionManager()
        self.assertFalse(manager.requires_confirmation("", action="get_ram"))
        self.assertFalse(manager.requires_confirmation("", action="create_file"))
        self.assertTrue(manager.requires_confirmation("", action="modify_source_code"))
        self.assertTrue(manager.check("", action="create_file").allow)

    def test_a_destructive_action_without_approval_does_not_run(self) -> None:
        manager = PermissionManager()
        refused = manager.check("", action="delete_file")
        self.assertFalse(refused.allow)
        self.assertIn("cannot be undone", refused.reason)

        approved = manager.check("", action="delete_file", approved=True)
        self.assertTrue(approved.allow)
        self.assertTrue(approved.requires_confirmation)

    def test_a_critical_action_always_needs_a_person(self) -> None:
        manager = PermissionManager()
        refused = manager.check("", action="purchase_item")
        self.assertFalse(refused.allow)
        self.assertIn("critical", refused.reason)
        self.assertTrue(manager.check("", action="purchase_item", approved=True).allow)

    def test_a_destructive_action_is_never_retried_automatically(self) -> None:
        manager = PermissionManager()
        self.assertFalse(manager.may_retry("", action="delete_file"))
        self.assertFalse(manager.may_retry("", action="send_message"))
        self.assertTrue(manager.may_retry("", action="get_ram"))

    def test_a_declaration_outranks_a_derivation(self) -> None:
        manager = PermissionManager()
        manager.declare("get_ram", PermissionDeclaration(risk_level=RiskLevel.HIGH))
        decision = manager.assess("get_ram", action="read_system_info")

        self.assertEqual(decision.risk, RiskLevel.HIGH)
        self.assertEqual(decision.source, "declared")
        self.assertIn("declared", decision.reason)

    def test_a_declaration_that_understates_an_action_is_still_corrected(self) -> None:
        # An operator registering a tool as LOW does not make sending a message
        # undoable, and the layer says so rather than obeying the shallower
        # statement.
        manager = PermissionManager()
        manager.declare("bridge", risk_level=RiskLevel.LOW)
        decision = manager.assess("bridge", action="send_message")

        self.assertEqual(decision.risk, RiskLevel.HIGH)
        self.assertEqual(decision.source, "derived")
        self.assertTrue(decision.external_side_effect)

    def test_sources_combine_and_the_strictest_reading_wins(self) -> None:
        catalog = _Catalog(
            ToolMetadata(
                name="send_message",
                category=ToolCategory.PHONE,
                risk=RiskLevel.LOW,
                permissions=(PermissionScope.PHONE_CONTROL,),
                reversible=False,
            )
        )
        manager = PermissionManager(catalog=catalog)
        decision = manager.assess("send_message", action="send_message")

        # The tool's metadata states the permissions, and the verb states what
        # the action IS: sending leaves the machine and cannot be undone.
        self.assertEqual(decision.permissions, (PermissionScope.PHONE_CONTROL,))
        self.assertEqual(decision.risk, RiskLevel.HIGH)
        self.assertEqual(decision.source, "derived")
        self.assertFalse(decision.reversible)
        self.assertTrue(decision.external_side_effect)
        self.assertFalse(manager.check("send_message", action="send_message").allow)

    def test_a_neutral_tool_cannot_launder_a_destructive_action(self) -> None:
        # The hole this closes: file_manager's own metadata is a MEDIUM tool
        # that lists, reads, copies and writes, so taking it at its word made a
        # delete through it a MEDIUM, non-destructive, retryable call.
        catalog = _Catalog(
            ToolMetadata(
                name="file_manager",
                category=ToolCategory.FILES,
                risk=RiskLevel.MEDIUM,
                permissions=(PermissionScope.FILESYSTEM_READ, PermissionScope.FILESYSTEM_WRITE),
            )
        )
        manager = PermissionManager(catalog=catalog)
        decision = manager.assess("file_manager", action="delete_file")

        self.assertEqual(decision.risk, RiskLevel.HIGH)
        self.assertTrue(decision.destructive)
        self.assertFalse(decision.reversible)
        self.assertTrue(decision.requires_confirmation)
        self.assertFalse(manager.check("file_manager", action="delete_file").allow)
        self.assertFalse(manager.may_retry("file_manager", action="delete_file"))

    def test_a_tie_reports_the_source_a_person_can_go_and_look_at(self) -> None:
        manager = PermissionManager()
        manager.declare("delete_file", risk_level=RiskLevel.HIGH, destructive=True)
        decision = manager.assess("delete_file", action="delete_file")
        self.assertEqual(decision.source, "declared")
        self.assertEqual(decision.risk, RiskLevel.HIGH)

    def test_a_high_risk_tool_cannot_declare_confirmation_away(self) -> None:
        catalog = _Catalog(
            ToolMetadata(name="wipe_disk", risk=RiskLevel.CRITICAL, requires_confirmation=False)
        )
        manager = PermissionManager(catalog=catalog)
        self.assertTrue(manager.requires_confirmation("wipe_disk"))
        self.assertEqual(manager.risk_of("wipe_disk"), RiskLevel.CRITICAL)

    def test_the_policy_is_what_decides_where_confirmation_starts(self) -> None:
        strict = PermissionManager(
            policy=PermissionPolicy(
                confirm_at=frozenset({RiskLevel.HIGH, RiskLevel.CRITICAL})
            )
        )
        self.assertFalse(strict.requires_confirmation("", action="modify_source_code"))
        self.assertTrue(strict.requires_confirmation("", action="delete_file"))
        self.assertTrue(strict.check("", action="modify_source_code").allow)

    def test_the_status_readout_is_serializable(self) -> None:
        manager = PermissionManager()
        manager.declare("dangerous_thing", risk_level=RiskLevel.HIGH, destructive=True)
        payload = manager.to_dict()

        self.assertEqual(payload["declared"], ["dangerous_thing"])
        self.assertIn("high", payload["policy"]["confirm_at"])
        self.assertEqual(len(payload["levels"]), 4)


class ToolMetadataRiskTests(unittest.TestCase):
    """The declaration fields, and that the old ones still work."""

    def test_the_new_fields_are_additive_and_serialized(self) -> None:
        metadata = ToolMetadata(
            name="delete_file",
            risk=RiskLevel.HIGH,
            permissions=(PermissionScope.FILESYSTEM_WRITE,),
            required_permission=PermissionScope.FILESYSTEM_WRITE,
            requires_confirmation=True,
            reversible=False,
            destructive=True,
        )
        payload = metadata.to_dict()

        self.assertEqual(payload["risk"], "high")
        self.assertEqual(payload["risk_level"], "high")
        self.assertEqual(payload["required_permission"], "filesystem:write")
        self.assertFalse(payload["reversible"])
        self.assertTrue(payload["destructive"])
        self.assertFalse(payload["external_side_effect"])
        # Unchanged fields, unchanged names.
        self.assertTrue(payload["requires_approval"])
        self.assertEqual(payload["permissions"], ["filesystem:write"])

    def test_merging_takes_the_strictest_answer(self) -> None:
        safe = ToolMetadata(name="thing", risk=RiskLevel.LOW, reversible=True)
        risky = ToolMetadata(
            name="thing",
            risk=RiskLevel.HIGH,
            reversible=False,
            destructive=True,
            external_side_effect=True,
            required_permission=PermissionScope.SHELL_EXECUTE,
        )
        merged = safe.merged(risky)

        self.assertEqual(merged.risk, RiskLevel.HIGH)
        self.assertFalse(merged.reversible)
        self.assertTrue(merged.destructive)
        self.assertTrue(merged.external_side_effect)
        self.assertEqual(merged.required_permission, PermissionScope.SHELL_EXECUTE)


class ExecutorRiskIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """The executor's new collaboration, and its old behaviour."""

    class _RecordingGateway:
        def __init__(self, *, approve: bool) -> None:
            self.approve = approve
            self.requests: list[object] = []

        async def request_approval(self, request: object) -> object:
            from novacontrol.core.security import ApprovalDecision

            self.requests.append(request)
            return ApprovalDecision(
                request_id=request.id,  # type: ignore[attr-defined]
                approved=self.approve,
                decided_by="test",
                reason="test decision",
            )

    def _registry(
        self,
        name: str = "wipe_disk",
        *,
        permissions: tuple[PermissionScope, ...] = (),
    ) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            FunctionTool(
                name,
                ToolSchema(name, "Do the thing this test needs."),
                lambda arguments: {"ok": True},
                required_permissions=permissions,
            )
        )
        return registry

    async def test_without_the_layer_the_executor_behaves_exactly_as_before(self) -> None:
        registry = self._registry()
        executor = ToolExecutor(registry)
        result = await executor.execute(ToolRequest("wipe_disk", {}))
        self.assertEqual(result.status.value, "completed")

    async def test_a_high_risk_tool_with_no_scopes_is_now_put_to_a_person(self) -> None:
        catalog = _Catalog(
            ToolMetadata(name="wipe_disk", risk=RiskLevel.CRITICAL, destructive=True)
        )
        manager = PermissionManager(catalog=catalog)
        gateway = self._RecordingGateway(approve=False)
        executor = ToolExecutor(
            self._registry(), approval_gateway=gateway, catalog=catalog, risk=manager
        )

        result = await executor.execute(ToolRequest("wipe_disk", {}))

        self.assertEqual(result.status.value, "denied")
        self.assertEqual(len(gateway.requests), 1)
        request = gateway.requests[0]
        self.assertEqual(request.risk, RiskLevel.CRITICAL)  # type: ignore[attr-defined]
        self.assertEqual(request.metadata["risk_source"], "metadata")  # type: ignore[attr-defined]

    async def test_a_derived_risk_is_enough_to_ask_about_an_undeclared_tool(self) -> None:
        # Nothing in the catalog declares "wipe_disk": the verb is what makes it
        # high risk, and the fact that the level was DERIVED is reported.
        gateway = self._RecordingGateway(approve=False)
        executor = ToolExecutor(
            self._registry(), approval_gateway=gateway, risk=PermissionManager()
        )

        result = await executor.execute(ToolRequest("wipe_disk", {}))

        self.assertEqual(result.status.value, "denied")
        self.assertEqual(gateway.requests[0].risk, RiskLevel.HIGH)  # type: ignore[attr-defined]
        self.assertEqual(gateway.requests[0].metadata["risk_source"], "derived")  # type: ignore[attr-defined]

    async def test_an_approved_high_risk_tool_runs(self) -> None:
        catalog = _Catalog(ToolMetadata(name="wipe_disk", risk=RiskLevel.HIGH, destructive=True))
        executor = ToolExecutor(
            self._registry(),
            approval_gateway=self._RecordingGateway(approve=True),
            catalog=catalog,
            risk=PermissionManager(catalog=catalog),
        )
        result = await executor.execute(ToolRequest("wipe_disk", {}))
        self.assertEqual(result.status.value, "completed")

    async def test_a_low_risk_reading_is_not_interrupted(self) -> None:
        gateway = self._RecordingGateway(approve=False)
        executor = ToolExecutor(
            self._registry("get_ram"), approval_gateway=gateway, risk=PermissionManager()
        )
        result = await executor.execute(ToolRequest("get_ram", {}))

        self.assertEqual(result.status.value, "completed")
        self.assertEqual(gateway.requests, [])


# --------------------------------------------------------------------------- #
# The phase, wired into the application
# --------------------------------------------------------------------------- #


def _verdict(status: VerificationStatus, reason: str = "") -> VerificationResult:
    """A check's answer, built the way a registered strategy returns one."""
    return VerificationResult(
        status=status,
        method=VerificationMethod.CALLABLE,
        expectation="the step's result matches what was expected",
        observed="what the step reported",
        reason=reason
        or (
            "the result matched"
            if status is VerificationStatus.PASS
            else "the result did not match"
        ),
    )


def _launch_step(step_id: str) -> PlanStep:
    """The specification's own example: launch an application, then prove it is up."""
    return PlanStep(
        title="Open VS Code",
        description="Launch the editor.",
        id=step_id,
        action="open_application",
        tool="open_application",
        effect=StepEffect.LOCAL_WRITE,
        parameters={"application": "Code"},
    )


class ApplicationReliabilityWiringTests(unittest.IsolatedAsyncioTestCase):
    """Phase 8 through the application, not through its parts.

    The tests above exercise the engines directly. These drive the objects the
    APPLICATION runs — the verifier its plan executor calls, the controller a
    "pause" reaches, the risk layer every tool passes through — because a
    subsystem that exists but is not wired into the product is not a delivered
    feature.
    """

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = NovaControlApplication(data_dir=Path(self._tmp.name))
        await self.app.start()

    async def asyncTearDown(self) -> None:
        await self.app.stop()
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------------

    def _engine(self, probe: Callable[[str], bool | None]) -> VerificationEngine:
        """Bind a verification engine with a known process probe to the app."""
        engine = VerificationEngine(process_probe=probe)
        self.app.plan_verifier = engine
        self.app.workflow_executor.verifier = engine
        return engine

    async def _execute(
        self,
        *steps: PlanStep,
        handler: Callable[..., Any] | None = None,
    ) -> WorkflowResult:
        """Run one plan through the application's own executor."""
        self.app.approved_plan_steps = {step.id for step in steps}
        if handler is not None:
            self.app.workflow_executor.step_handler = handler
        return await self.app.workflow_executor.execute(Plan(goal="a goal", steps=tuple(steps)))

    def _runner(self, task_id: str, count: int = 4) -> tuple[asyncio.Task[None], list[str]]:
        """A task body that does real work, reports each checkpoint, and stops."""
        control = self.app.task_control
        ran: list[str] = []

        async def body() -> None:
            try:
                for index in range(count):
                    await asyncio.sleep(0.01)  # the work of one step
                    ran.append(f"step-{index}")
                    await control.checkpoint(task_id, step=f"step-{index}")
            except TaskCancelled:
                ran.append("<stopped>")

        return asyncio.create_task(body()), ran

    # -- 8.1 verification ------------------------------------------------------

    async def test_the_app_verifies_with_the_engine(self) -> None:
        self.assertIsInstance(self.app.plan_verifier, VerificationEngine)
        self.assertIsInstance(self.app.plan_verifier, DeterministicVerifier)
        report = self.app.reliability_status()["verification"]
        self.assertEqual(report["verifier"], "verification-engine")
        # The callables the application registered are still checked the same way.
        self.assertIn("metric_read", report["callables"])

    async def test_launching_an_application_is_confirmed_by_finding_its_process(self) -> None:
        self._engine(lambda name: name.lower() == "code")

        async def launch(_step: PlanStep, _context: Any) -> dict[str, Any]:
            return {"pid": 4242, "application": "Code"}

        step = _launch_step("open-1")
        result = await self._execute(step, handler=launch)
        outcome = result.step_results["open-1"]

        self.assertEqual(outcome.status, PlanStepStatus.COMPLETED)
        assert outcome.verification is not None
        self.assertTrue(outcome.verification.success)
        self.assertEqual(outcome.verification.method, VerificationMethod.PROCESS_RUNNING)
        self.assertGreater(outcome.verification.confidence, 0.0)
        self.assertEqual(result.unverified_steps, ())

    async def test_an_application_that_never_started_is_a_failure(self) -> None:
        self._engine(lambda name: False)

        async def launch(_step: PlanStep, _context: Any) -> dict[str, Any]:
            return {"pid": 4242}

        step = _launch_step("open-2")
        result = await self._execute(step, handler=launch)
        outcome = result.step_results["open-2"]

        # The launch call returned; the editor is not running. Only one of those
        # is success, and it is the one the process probe decides.
        self.assertEqual(outcome.status, PlanStepStatus.FAILED)
        assert outcome.error is not None
        self.assertEqual(outcome.error.kind, FailureKind.VERIFICATION_FAILED)

    async def test_a_launch_nobody_can_check_is_reported_unverified(self) -> None:
        self._engine(lambda name: None)  # no probe on this machine can tell

        async def launch(_step: PlanStep, _context: Any) -> dict[str, Any]:
            return {"pid": 4242}

        step = _launch_step("open-3")
        result = await self._execute(step, handler=launch)
        outcome = result.step_results["open-3"]

        self.assertEqual(outcome.status, PlanStepStatus.UNVERIFIED)
        self.assertIn("open-3", result.unverified_steps)
        self.assertFalse(result.verified)

    async def test_a_write_that_never_reached_the_disk_is_a_failure(self) -> None:
        absent = Path(self._tmp.name) / "never-written.txt"
        step = PlanStep(
            title="Write the report",
            description="Write it.",
            id="write-1",
            action="write_file",
            tool="write_file",
            effect="local_write",
            parameters={"path": str(absent)},
        )

        async def write(_step: PlanStep, _context: Any) -> dict[str, Any]:
            return {"written": True, "path": str(absent)}

        result = await self._execute(step, handler=write)
        outcome = result.step_results["write-1"]

        # The tool reported success. The file is not there. That is a failure,
        # and it is the whole reason a call is not a completion.
        self.assertEqual(outcome.status, PlanStepStatus.FAILED)
        assert outcome.error is not None
        self.assertEqual(outcome.error.kind, FailureKind.VERIFICATION_FAILED)
        # And the evidence of the check that failed travels with the failure
        # rather than being dropped: "not_run" here would be a false statement.
        assert outcome.verification is not None
        self.assertEqual(outcome.verification.method, VerificationMethod.FILE_EXISTS)
        self.assertEqual(outcome.verification.status, VerificationStatus.FAIL)
        self.assertEqual(outcome.verification_result, "fail")
        self.assertIn("write-1", result.errors)

    async def test_the_structured_result_carries_what_the_specification_asks(self) -> None:
        self._engine(lambda name: True)

        async def launch(_step: PlanStep, _context: Any) -> dict[str, Any]:
            return {"pid": 4242}

        step = _launch_step("open-4")
        result = await self._execute(step, handler=launch)
        outcome = result.step_results["open-4"]
        assert outcome.verification is not None
        reported = outcome.verification.to_dict()

        for field in (
            "success",
            "status",
            "verifier",
            "expected_state",
            "actual_state",
            "confidence",
            "error",
            "metadata",
        ):
            self.assertIn(field, reported)
        self.assertTrue(reported["success"])
        self.assertEqual(reported["verifier"], "verification-engine:derived")
        self.assertEqual(outcome.verification_result, "pass")

    async def test_a_steps_own_check_outranks_a_registered_tool_strategy(self) -> None:
        engine = self._engine(lambda name: True)
        engine.register_tool_strategy("write_file", lambda step, output: True)
        step = PlanStep(
            title="Build the project",
            description="Build it.",
            id="build-1",
            action="build_project",
            tool="write_file",
            verification=VerificationSpec(
                VerificationMethod.OUTPUT_CONTAINS, target="stdout", expect="BUILD OK"
            ),
        )

        result = engine.verify(step, {"stdout": "BUILD FAILED"})

        self.assertEqual(result.status, VerificationStatus.FAIL)
        self.assertEqual(result.method, VerificationMethod.OUTPUT_CONTAINS)

    async def test_the_engine_checks_a_tool_without_a_plan_step(self) -> None:
        engine = self._engine(lambda name: True)
        missing = Path(self._tmp.name) / "absent.txt"

        result = engine.verify_tool(
            "write_file", {"written": True}, parameters={"path": str(missing)}
        )

        self.assertEqual(result.status, VerificationStatus.FAIL)
        self.assertEqual(result.method, VerificationMethod.FILE_EXISTS)

    # -- 8.2 recovery ----------------------------------------------------------

    async def test_a_transient_failure_is_retried_until_it_works(self) -> None:
        attempts: list[int] = []

        async def flaky(_step: PlanStep) -> dict[str, Any]:
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                raise RuntimeError("the launcher timed out")
            return {"pid": 7}

        run = await self.app.recovery_engine.run_with_recovery(
            _launch_step("open-5"),
            execute=flaky,
            verify=lambda step, output: _verdict(VerificationStatus.PASS),
        )

        self.assertTrue(run.succeeded)
        self.assertTrue(run.verified)
        self.assertEqual(run.attempts, 2)
        self.assertIn("verified", run.summary)

    async def test_destructive_work_is_never_retried(self) -> None:
        executed: list[str] = []

        async def remove(_step: PlanStep) -> dict[str, Any]:
            executed.append(_step.tool)
            raise RuntimeError("the file was locked")

        step = PlanStep(
            title="Delete the archive",
            description="Delete it.",
            id="delete-1",
            action="delete_file",
            tool="file_manager",
            effect=StepEffect.DESTRUCTIVE,
            parameters={"path": "archive.zip"},
        )
        run = await self.app.recovery_engine.run_with_recovery(
            step,
            execute=remove,
            verify=lambda step, output: _verdict(VerificationStatus.PASS),
        )

        # A delete that half-succeeded, run again, deletes something else.
        self.assertEqual(len(executed), 1)
        self.assertFalse(run.succeeded)
        self.assertTrue(run.stopped_safely)

    async def test_recovery_stops_at_the_attempt_ceiling(self) -> None:
        executed: list[str] = []

        async def always_fails(_step: PlanStep) -> dict[str, Any]:
            executed.append(_step.tool)
            return {}

        run = await self.app.recovery_engine.run_with_recovery(
            _launch_step("open-6"),
            execute=always_fails,
            verify=lambda step, output: _verdict(VerificationStatus.FAIL),
            retry_policy=RetryPolicy(max_attempts=3),
            max_attempts=9999,  # asking for more than the policy allows
        )

        self.assertLessEqual(len(executed), 3)
        self.assertLessEqual(run.attempts, 3)
        self.assertFalse(run.succeeded)

    async def test_recovery_runs_a_nothing_checked_step_as_unconfirmed(self) -> None:
        async def ran(_step: PlanStep) -> dict[str, Any]:
            return {"ok": True}

        run = await self.app.recovery_engine.run_with_recovery(
            _launch_step("open-7"),
            execute=ran,
            verify=lambda step, output: _verdict(
                VerificationStatus.INCONCLUSIVE, "no probe is available on this machine"
            ),
        )

        # Executed and unconfirmed is reported as exactly that, never as done.
        self.assertTrue(run.executed)
        self.assertFalse(run.verified)
        self.assertFalse(run.succeeded)
        self.assertIn("unconfirmed", run.summary)

    async def test_a_builtin_alternative_is_filtered_against_the_live_registry(self) -> None:
        executed: list[str] = []

        async def not_registered(_step: PlanStep) -> dict[str, Any]:
            executed.append(_step.tool)
            raise RuntimeError(
                f"Tool {_step.tool!r} is not registered on this installation."
            )

        run = await self.app.recovery_engine.run_with_recovery(
            _launch_step("open-8"),
            execute=not_registered,
            verify=lambda step, output: _verdict(VerificationStatus.PASS),
        )

        # The built-in alternative for opening an application is
        # ``desktop_open_application``. This installation does not register that
        # tool, so it is not offered — the registry is asked, not assumed — and
        # the run stops instead of rerouting into another missing tool.
        self.assertEqual(executed, ["open_application"])
        self.assertFalse(run.succeeded)
        self.assertIn("no escalation target", run.summary)

    async def test_a_tool_strategy_is_used_to_verify_a_tools_result(self) -> None:
        engine = self.app.plan_verifier
        assert isinstance(engine, VerificationEngine)
        checked: list[str] = []

        def strategy(step: PlanStep, output: Mapping[str, Any]) -> bool:
            checked.append(step.tool)
            return bool(output.get("applied"))

        engine.register_tool_strategy("installed_applications", strategy)
        result = await self.app.recovery_engine.run_with_recovery(
            PlanStep(
                title="Install the tool",
                description="Install it.",
                id="install-1",
                action="install_tool",
                tool="installed_applications",
                effect=StepEffect.LOCAL_WRITE,
            ),
            execute=lambda step: {"applied": True},
        )

        self.assertEqual(checked, ["installed_applications"])
        self.assertTrue(result.succeeded)
        assert result.verification is not None
        self.assertEqual(result.verification.verifier, "verification-engine:installed_applications")

    async def test_a_denial_asks_a_person_and_stops_when_they_refuse(self) -> None:
        asked: list[str] = []

        async def refused(_step: PlanStep) -> dict[str, Any]:
            raise PermissionError("this action was not approved")

        run = await self.app.recovery_engine.run_with_recovery(
            _launch_step("open-9"),
            execute=refused,
            verify=lambda step, output: _verdict(VerificationStatus.PASS),
            confirm=lambda step: asked.append(step.id) or False,
        )

        self.assertEqual(asked, ["open-9"])
        self.assertFalse(run.succeeded)
        self.assertTrue(run.requires_confirmation)

    # -- 8.3 / 8.4 task state and control -------------------------------------

    async def test_a_task_walks_its_states_and_can_be_read_back(self) -> None:
        control = self.app.task_control
        task = control.open_task(task_id="app-task", state=TaskState.PENDING)
        task.start_planning()
        task.start_running(current_step="open VS Code")
        task.begin_verification(current_step="check the process")
        task.complete()

        snapshot = task.snapshot
        self.assertEqual(snapshot.current_state, TaskState.COMPLETED)
        self.assertEqual(snapshot.previous_state, TaskState.VERIFYING)
        self.assertEqual(snapshot.current_step, "check the process")
        self.assertIsNotNone(snapshot.created_at)
        self.assertEqual(
            [state.value for state in task.history],
            ["pending", "planning", "running", "verifying", "completed"],
        )
        # The application reports the task it is working on, rather than holding
        # the state where only the code that made it can see it.
        reported = self.app.reliability_status()["tasks"]
        self.assertEqual(reported[0]["task_id"], "app-task")
        self.assertEqual(reported[0]["current_state"], "completed")
        self.assertEqual(reported[0]["previous_state"], "verifying")

    def test_an_illegal_transition_is_refused(self) -> None:
        task = self.app.task_control.open_task(task_id="app-task-2")

        with self.assertRaises(InvalidTransitionError):
            task.complete()

        self.assertEqual(task.snapshot.current_state, TaskState.PENDING)

    async def test_pausing_holds_a_running_task_at_a_checkpoint(self) -> None:
        control = self.app.task_control
        control.open_task(task_id="parked", state=TaskState.RUNNING)
        control.save_checkpoint("parked", step="step-0", done=())
        held = control.pause("parked", reason="the person said pause")

        running, ran = self._runner("parked")
        await asyncio.sleep(0.05)

        self.assertTrue(held.accepted)
        self.assertFalse(running.done())  # held, not killed: its work is intact
        self.assertEqual(ran, ["step-0"])  # it got to its checkpoint and stopped
        self.assertEqual(control.snapshot("parked").current_state, TaskState.PAUSED)
        self.assertTrue(control.snapshot("parked").pause_requested)
        self.assertEqual(control.checkpoint_of("parked")["step"], "step-0")
        # And it does NOT walk on: a pause that let the runner through would be
        # a status field, not a control.
        await asyncio.sleep(0.05)
        self.assertEqual(ran, ["step-0"])

        control.cancel("parked", reason="finish the test")
        await asyncio.wait_for(running, timeout=1)

    async def test_resuming_continues_from_the_checkpoint_it_held(self) -> None:
        control = self.app.task_control
        control.open_task(task_id="resumed", state=TaskState.RUNNING)
        control.save_checkpoint("resumed", step="step-0", done=())
        control.pause("resumed")
        running, ran = self._runner("resumed")
        await asyncio.sleep(0.05)
        self.assertEqual(ran, ["step-0"])  # parked at its first checkpoint

        resumed = control.resume("resumed")
        await asyncio.wait_for(running, timeout=1)

        self.assertTrue(resumed.accepted)
        self.assertFalse(control.snapshot("resumed").pause_requested)
        # It picked the same run back up rather than starting it over: the steps
        # it had already done stay done.
        self.assertEqual(ran, ["step-0", "step-1", "step-2", "step-3"])
        self.assertIn("step-0", resumed.summary)
        self.assertEqual(control.snapshot("resumed").current_state, TaskState.RUNNING)

    async def test_cancelling_a_parked_task_stops_the_runner_cleanly(self) -> None:
        control = self.app.task_control
        machine = control.open_task(task_id="parked-2", state=TaskState.RUNNING)
        control.save_checkpoint("parked-2", step="step-0")
        control.pause("parked-2")
        running, ran = self._runner("parked-2")
        await asyncio.sleep(0.05)
        self.assertEqual(ran, ["step-0"])

        control.cancel("parked-2", reason="the person said stop")
        await asyncio.wait_for(running, timeout=1)  # no transition error

        self.assertEqual(control.snapshot("parked-2").current_state, TaskState.CANCELLED)
        self.assertEqual(ran, ["step-0", "<stopped>"])
        self.assertEqual(machine.history[-1], TaskState.CANCELLED)

    async def test_resuming_a_cancelled_task_does_not_revive_it(self) -> None:
        control = self.app.task_control
        task = control.open_task(task_id="cancelled", state=TaskState.RUNNING)
        control.cancel("cancelled", reason="the person said stop")

        resumed = control.resume("cancelled")

        # A resume must not promise a continuation that is not going to happen.
        self.assertFalse(resumed.accepted)
        self.assertIn("cancellation pending", resumed.summary)
        self.assertTrue(task.snapshot.cancellation_requested)
        # And the task still stops where stopping is safe.
        with self.assertRaises(TaskCancelled):
            await control.checkpoint("cancelled", step="the next step")
        self.assertEqual(task.snapshot.current_state, TaskState.CANCELLED)
        self.assertEqual(task.history[-1], TaskState.CANCELLED)

    def test_the_control_phrases_reach_the_apps_task(self) -> None:
        control = self.app.task_control
        control.open_task(task_id="app-job", state=TaskState.RUNNING)

        paused = control.handle_command("pause")

        assert paused is not None
        self.assertEqual(paused.task_id, "app-job")
        self.assertTrue(paused.accepted)
        self.assertEqual(paused.state, TaskState.PAUSED)

    # -- 8.5 permissions and risk ---------------------------------------------

    def test_the_risk_table_in_the_specification_holds(self) -> None:
        table = (
            ("get_ram", RiskLevel.LOW),
            ("open_application", RiskLevel.LOW),
            ("create_file", RiskLevel.LOW),
            ("modify_source_code", RiskLevel.MEDIUM),
            ("phone_send_text", RiskLevel.HIGH),
            ("delete_file", RiskLevel.HIGH),
            ("purchase_order", RiskLevel.CRITICAL),
        )
        for action, expected in table:
            with self.subTest(action=action):
                self.assertEqual(self.app.risk.risk_of(action=action), expected)

    def test_a_destructive_action_does_not_run_without_approval(self) -> None:
        refused = self.app.risk.check("", action="delete_file")

        self.assertFalse(refused.allow)
        self.assertTrue(refused.requires_confirmation)
        self.assertTrue(refused.destructive)
        self.assertFalse(refused.reversible)
        self.assertIn("approval", refused.reason)
        # A person's approval is the only thing that lets it through.
        self.assertTrue(self.app.risk.check("", action="delete_file", approved=True).allow)

    def test_a_critical_action_needs_a_person_before_it_runs(self) -> None:
        refused = self.app.risk.check("", action="purchase_order")

        self.assertFalse(refused.allow)
        self.assertEqual(refused.risk, RiskLevel.CRITICAL)
        self.assertTrue(refused.external_side_effect)
        self.assertFalse(refused.reversible)
        self.assertIn("critical", refused.reason)
        # The person's approval is what it was waiting for, and only that.
        self.assertTrue(self.app.risk.check("", action="purchase_order", approved=True).allow)

    def test_an_action_that_leaves_the_machine_is_never_auto_retried(self) -> None:
        self.assertFalse(self.app.risk.may_retry("", action="send_message"))
        self.assertFalse(self.app.risk.may_retry("", action="delete_file"))
        self.assertTrue(self.app.risk.may_retry("", action="get_ram"))

    def test_the_permission_layer_reads_the_real_tool_catalog(self) -> None:
        levels = {
            name: self.app.risk.risk_of(name) for name in self.app.tool_catalog.names()
        }

        # Every tool the catalog describes resolves to a level, and the dangerous
        # ones are not quietly LOW.
        self.assertEqual(len(levels), len(self.app.tool_catalog))
        self.assertEqual(levels["machine_facts"], RiskLevel.LOW)
        self.assertEqual(levels["automation_manager"], RiskLevel.HIGH)
        self.assertTrue(self.app.risk.requires_confirmation("automation_manager"))

    async def test_the_existing_approval_gate_still_refuses_an_unapproved_action(self) -> None:
        # Backward compatibility: the approval layer that was already here is
        # still the thing that decides, and this phase did not route around it.
        step = _launch_step("sys-1").with_(effect=StepEffect.SYSTEM)
        result = await self.app.workflow_executor.execute(Plan(goal="a goal", steps=(step,)))
        outcome = result.step_results["sys-1"]

        self.assertEqual(outcome.status, PlanStepStatus.DENIED)
        assert outcome.error is not None
        self.assertEqual(outcome.error.kind, FailureKind.PERMISSION_DENIED)


if __name__ == "__main__":
    unittest.main()
