"""Phase 15: trajectories, quality filtering, evaluation, reward and storage.

The suite runs entirely on deterministic fixtures — no model, no network, no
GPU. That is a requirement of the phase rather than a convenience: a 16 GB
Windows desktop must be able to prove the data-collection layer works without
loading Qwen3 8B, so every trajectory here is either emitted through the real
EventBus or built by hand, and every score is computed from those records.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

try:
    from fastapi.testclient import TestClient  # requires httpx

except Exception:  # pragma: no cover - httpx is an optional dev dependency
    TestClient = None  # type: ignore[assignment]

from novacontrol.api.app import create_app
from novacontrol.application import NovaControlApplication
from novacontrol.audit.redact import REDACTED
from novacontrol.browser import NoopBrowserRunner
from novacontrol.core.events import Event, EventBus, EventType
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.evaluation import (
    BUILTIN_DATASET_ID,
    AgentTrajectory,
    DataQualityFilter,
    DatasetRepository,
    EvaluationConfig,
    EvaluationEngine,
    EvaluationRepository,
    EvaluationService,
    ExecutionStep,
    GoldenDataset,
    GoldenExample,
    InMemoryRecordStore,
    JsonlRecordStore,
    LatencyMetrics,
    MetricsCalculator,
    QualityConfig,
    ResourceUsage,
    RewardConfig,
    RewardEngine,
    RewardRepository,
    ToolCallRecord,
    TrajectoryRecorder,
    TrajectoryRepository,
    UserFeedback,
    VerificationRecord,
    builtin_golden_dataset,
    percentile,
)

SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123"


def make_trajectory(**overrides: Any) -> AgentTrajectory:
    """A complete, successful run: the fixture most tests start from."""
    fields: dict[str, Any] = {
        "trajectory_id": "traj-1",
        "task_id": "task-1",
        "user_request": "open calculator",
        "source": "test",
        "structured_intent": {
            "intent": "open_application",
            "confidence": 0.9,
            "strategy": "fast_path",
        },
        "decision": {"route": "direct_tool", "decision_type": "execute"},
        "plan": {
            "goal": "open calculator",
            "steps": [{"step_id": "s1", "description": "launch", "status": "completed"}],
        },
        "execution_steps": (
            ExecutionStep(
                step_id="s1",
                description="launch",
                action="launch",
                status="completed",
                attempts=1,
            ),
        ),
        "tool_calls": (
            ToolCallRecord(tool="desktop.launch", status="completed", duration_ms=120.0),
        ),
        "verification_results": (VerificationRecord(step_id="s1", status="verified"),),
        "status": "completed",
        "success": True,
        "final_result": {"summary": "calculator opened"},
        "latency_metrics": LatencyMetrics(total_ms=250.0),
        "resource_usage": ResourceUsage(
            ram_before_bytes=1_000_000,
            ram_after_bytes=1_500_000,
            ram_delta_bytes=500_000,
            samples=1,
        ),
    }
    fields.update(overrides)
    return AgentTrajectory(**fields)


class TrajectoryModelTests(unittest.TestCase):
    """The schema: creation, optionality, serialisation and its version."""

    def test_a_trajectory_can_be_created_from_nothing_but_an_idea(self) -> None:
        row = AgentTrajectory()

        self.assertTrue(row.trajectory_id)
        self.assertEqual(row.status, "in_progress")
        self.assertIsNone(row.success)
        self.assertFalse(row.complete)

    def test_serialisation_round_trips_every_field(self) -> None:
        row = make_trajectory(user_feedback=UserFeedback(rating=5, label="good"))

        restored = AgentTrajectory.from_dict(row.to_dict())

        self.assertEqual(restored, row)

    def test_a_stored_row_from_a_newer_schema_still_loads(self) -> None:
        payload = make_trajectory().to_dict()
        payload["schema_version"] = 99
        payload["something_this_build_has_never_seen"] = {"nested": True}

        row = AgentTrajectory.from_dict(payload)

        self.assertEqual(row.schema_version, 99)
        self.assertEqual(row.task_id, "task-1")

    def test_an_unknown_status_reads_as_in_progress_rather_than_raising(self) -> None:
        payload = make_trajectory().to_dict()
        payload["status"] = "teleported"

        self.assertEqual(AgentTrajectory.from_dict(payload).status, "in_progress")

    def test_terminality_and_completeness_are_different_questions(self) -> None:
        decided = make_trajectory()
        undecided = make_trajectory(success=None)

        self.assertTrue(decided.terminal and decided.complete)
        self.assertTrue(undecided.terminal)
        self.assertFalse(undecided.complete)

    def test_the_fingerprint_ignores_ids_and_timestamps(self) -> None:
        first = make_trajectory(trajectory_id="a", task_id="t-a")
        second = make_trajectory(trajectory_id="b", task_id="t-b")

        self.assertEqual(first.fingerprint(), second.fingerprint())
        self.assertNotEqual(
            first.fingerprint(), make_trajectory(user_request="open notepad").fingerprint()
        )

    def test_retries_count_extra_attempts(self) -> None:
        row = make_trajectory(
            tool_calls=(
                ToolCallRecord(tool="desktop.launch", status="completed"),
                ToolCallRecord(tool="desktop.launch", status="completed"),
                ToolCallRecord(tool="files.list", status="completed"),
            )
        )

        self.assertEqual(row.retries, 1)

    def test_the_payload_is_json_safe(self) -> None:
        payload = make_trajectory().to_dict()

        json.dumps(payload, ensure_ascii=False)


class RecorderTests(unittest.IsolatedAsyncioTestCase):
    """The recorder observes the existing lifecycle; it never joins it."""

    def _recorder(
        self, **kwargs: Any
    ) -> tuple[TrajectoryRecorder, EventBus, list[AgentTrajectory]]:
        collected: list[AgentTrajectory] = []
        recorder = TrajectoryRecorder(sink=collected.append, **kwargs)
        return recorder, EventBus(continue_on_error=True), collected

    async def test_the_lifecycle_becomes_one_trajectory(self) -> None:
        recorder, bus, collected = self._recorder()
        await recorder.attach(bus)

        await bus.emit(
            EventType.TASK_STARTED, correlation_id="req-1", task_id="task-1", kind="ask"
        )
        await bus.emit(
            EventType.INTENT_DETECTED,
            correlation_id="req-1",
            intent="open_application",
            strategy="fast_path",
            confidence=0.91,
        )
        await bus.emit(
            EventType.CONTEXT_RESOLVED, correlation_id="req-1", strategy="memory_first"
        )
        await bus.emit(
            EventType.DECISION_CREATED,
            correlation_id="req-1",
            route="direct_tool",
            decision_type="execute",
            capability="desktop.launch",
        )
        await bus.emit(
            EventType.PLAN_CREATED,
            correlation_id="req-1",
            goal="open calculator",
            steps=[{"step_id": "s1", "description": "launch", "status": "completed"}],
        )
        await bus.emit(EventType.TOOL_SELECTED, correlation_id="req-1", tool="desktop.launch")
        await bus.emit(EventType.TOOL_STARTED, correlation_id="req-1", tool="desktop.launch")
        await bus.emit(
            EventType.TOOL_COMPLETED,
            correlation_id="req-1",
            tool="desktop.launch",
            status="completed",
            duration_ms=120.0,
        )
        await bus.emit(
            EventType.VERIFICATION_COMPLETED,
            correlation_id="req-1",
            step_id="s1",
            status="verified",
        )
        await bus.emit(
            EventType.TASK_COMPLETED,
            correlation_id="req-1",
            task_id="task-1",
            route="direct_tool",
            duration_ms=250.0,
        )

        self.assertEqual(len(collected), 1)
        row = collected[0]
        self.assertEqual(row.trajectory_id, "req-1")
        self.assertEqual(row.task_id, "task-1")
        self.assertEqual(row.structured_intent["intent"], "open_application")
        self.assertEqual(row.decision["route"], "direct_tool")
        self.assertEqual(row.plan["goal"], "open calculator")
        self.assertEqual([call.tool for call in row.tool_calls], ["desktop.launch"])
        self.assertEqual(row.tool_calls[0].status, "completed")
        self.assertEqual(row.tool_calls[0].duration_ms, 120.0)
        self.assertEqual(row.verification_results[0].status, "verified")
        self.assertTrue(row.execution_steps)
        self.assertEqual(row.status, "completed")
        self.assertIs(True, row.success)
        self.assertEqual(row.latency_metrics.total_ms, 250.0)
        self.assertEqual(row.event_count, 10)
        self.assertEqual(recorder.in_flight, 0)

    async def test_a_failed_tool_and_its_recovery_are_both_recorded(self) -> None:
        recorder, bus, collected = self._recorder()
        await recorder.attach(bus)

        await bus.emit(EventType.TOOL_STARTED, correlation_id="r", tool="browser.open")
        await bus.emit(
            EventType.TOOL_FAILED, correlation_id="r", tool="browser.open", error="timeout"
        )
        await bus.emit(
            EventType.RECOVERY_COMPLETED, correlation_id="r", step_id="s1", outcome="recovered"
        )
        await bus.emit(
            EventType.TASK_COMPLETED, correlation_id="r", task_id="t", route="agent"
        )

        row = collected[0]
        self.assertEqual(row.tool_calls[0].status, "failed")
        self.assertEqual(row.tool_calls[0].error, "timeout")
        self.assertTrue(row.failed_tools)
        self.assertEqual(row.recovery_events[0].outcome, "recovered")
        self.assertTrue(row.recovery_events[0].succeeded)

    async def test_a_terminal_event_for_an_unseen_run_still_produces_a_row(self) -> None:
        recorder, bus, collected = self._recorder()
        await recorder.attach(bus)

        await bus.emit(
            EventType.TASK_FAILED,
            correlation_id="specialist-1",
            task_id="specialist-1",
            error="stage failed",
        )

        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0].status, "failed")
        self.assertEqual(collected[0].failure_reason, "stage failed")

    async def test_an_incomplete_capture_is_written_out_not_dropped(self) -> None:
        recorder, bus, collected = self._recorder()
        await recorder.attach(bus)
        await bus.emit(EventType.TASK_STARTED, correlation_id="r", task_id="t")
        await bus.emit(
            EventType.INTENT_DETECTED, correlation_id="r", intent="list_files", strategy="lexical"
        )

        rows = recorder.flush()

        self.assertEqual(len(rows), 1)
        self.assertEqual([row.task_id for row in collected], ["t"])
        self.assertEqual(rows[0].status, "in_progress")
        self.assertIsNone(rows[0].success)

    async def test_turning_recording_off_flushes_and_stops_capturing(self) -> None:
        recorder, bus, collected = self._recorder()
        await recorder.attach(bus)
        await bus.emit(EventType.TASK_STARTED, correlation_id="r1", task_id="t1")

        recorder.set_enabled(False)
        await bus.emit(EventType.INTENT_DETECTED, correlation_id="r1", intent="x", strategy="y")
        await bus.emit(EventType.TASK_STARTED, correlation_id="r2", task_id="t2")

        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0].status, "in_progress")
        self.assertEqual(recorder.in_flight, 0)
        self.assertFalse(recorder.enabled)

    async def test_recording_disabled_leaves_no_state_at_all(self) -> None:
        recorder, bus, collected = self._recorder(enabled=False)
        await recorder.attach(bus)

        await bus.emit(EventType.TASK_STARTED, correlation_id="r", task_id="t")
        await bus.emit(
            EventType.INTENT_DETECTED, correlation_id="r", intent="x", strategy="y"
        )

        self.assertEqual(collected, [])
        self.assertEqual(recorder.in_flight, 0)
        self.assertEqual(recorder.trajectories(), ())

    async def test_a_broken_recorder_cannot_break_the_work_it_watches(self) -> None:
        recorder, bus, _ = self._recorder()
        await recorder.attach(bus)

        with mock.patch.object(recorder, "_apply", side_effect=RuntimeError("recorder exploded")):
            result = await bus.publish(
                Event.of(
                    EventType.INTENT_DETECTED,
                    correlation_id="r",
                    intent="open_application",
                    strategy="fast_path",
                )
            )

        self.assertTrue(result.ok, "the publish must not fail because the observer did")
        self.assertEqual(recorder.failures, 1)
        self.assertEqual(recorder.trajectories(), ())

    async def test_a_failing_sink_is_counted_and_survived(self) -> None:
        def explode(_row: AgentTrajectory) -> None:
            raise OSError("disk full")

        recorder = TrajectoryRecorder(sink=explode)
        bus = EventBus(continue_on_error=True)
        await recorder.attach(bus)

        await bus.emit(EventType.TASK_STARTED, correlation_id="r", task_id="t")
        result = await bus.publish(
            Event.of(EventType.TASK_COMPLETED, correlation_id="r", task_id="t", route="chat")
        )

        self.assertTrue(result.ok)
        self.assertEqual(recorder.failures, 1)
        self.assertEqual(len(recorder.trajectories()), 1, "the row is still held in the ring")

    async def test_a_late_annotation_re_delivers_the_updated_row(self) -> None:
        recorder, bus, collected = self._recorder()
        await recorder.attach(bus)
        await bus.emit(EventType.TASK_STARTED, correlation_id="req-9", task_id="task-9")
        await bus.emit(
            EventType.TASK_COMPLETED, correlation_id="req-9", task_id="task-9", route="chat"
        )

        wrote = recorder.annotate(
            "req-9",
            user_request="what is 2 + 2",
            latency_metrics={"total_ms": 1234.5},
            resource_usage={"ram_delta_bytes": 4096},
            model_information={"model": "qwen3:8b"},
            metadata={"fast_path": False},
        )

        self.assertTrue(wrote)
        self.assertEqual(len(collected), 2)
        latest = collected[-1]
        self.assertEqual(latest.user_request, "what is 2 + 2")
        self.assertEqual(latest.latency_metrics.total_ms, 1234.5)
        self.assertEqual(latest.resource_usage.ram_delta_bytes, 4096)
        self.assertEqual(latest.model_information["model"], "qwen3:8b")
        self.assertFalse(latest.metadata["fast_path"])

    async def test_an_annotation_with_no_identity_is_refused(self) -> None:
        recorder, _, collected = self._recorder()

        self.assertFalse(recorder.annotate(""))
        self.assertEqual(collected, [])

    async def test_secrets_in_a_payload_are_redacted_on_the_way_in(self) -> None:
        recorder, bus, collected = self._recorder()
        await recorder.attach(bus)

        await bus.emit(
            EventType.DECISION_CREATED,
            correlation_id="r",
            route="chat",
            decision_type="answer",
            note=f"password=hunter2 api_key={SECRET}",
        )
        await bus.emit(
            EventType.TASK_COMPLETED, correlation_id="r", task_id="t", route="chat"
        )

        row = collected[0]
        stored = json.dumps(row.to_dict())
        self.assertNotIn("hunter2", stored)
        self.assertNotIn(SECRET, stored)
        self.assertIn(REDACTED, stored)
        self.assertGreaterEqual(row.redactions, 2)
        self.assertIn("secret_assignment", row.redaction_kinds)

    async def test_annotated_text_is_redacted_too(self) -> None:
        recorder, bus, collected = self._recorder()
        await recorder.attach(bus)
        await bus.emit(EventType.TASK_STARTED, correlation_id="r", task_id="t")
        await bus.emit(EventType.TASK_COMPLETED, correlation_id="r", task_id="t", route="chat")

        before = collected[-1].redactions
        recorder.annotate("r", user_request=f"deploy with token {SECRET}")

        self.assertNotIn(SECRET, collected[-1].user_request)
        self.assertIn(REDACTED, collected[-1].user_request)
        # The row was already finished when the annotation arrived, so this is
        # the re-delivered copy: redacting it must also COUNT as a redaction.
        self.assertGreater(collected[-1].redactions, before)
        self.assertTrue(collected[-1].redaction_kinds)

    async def test_a_zero_recent_ring_holds_nothing(self) -> None:
        recorder, bus, collected = self._recorder(max_recent=0)
        await recorder.attach(bus)

        await bus.emit(EventType.TASK_COMPLETED, correlation_id="r", task_id="t", route="chat")

        self.assertEqual(len(collected), 1, "the row is still delivered to the sink")
        self.assertEqual(recorder.trajectories(), ())
        self.assertEqual(recorder.status()["completed_held"], 0)

    async def test_in_flight_drafts_are_bounded(self) -> None:
        recorder, bus, collected = self._recorder(max_in_flight=2)
        await recorder.attach(bus)

        for index in range(3):
            await bus.emit(EventType.TASK_STARTED, correlation_id=f"r{index}", task_id=f"t{index}")

        self.assertEqual(recorder.in_flight, 2)
        self.assertEqual(len(collected), 1, "the oldest draft was written out, not dropped")
        self.assertEqual(recorder.flushed_partial, 1)

    async def test_the_recorder_unsubscribes_cleanly(self) -> None:
        recorder, bus, collected = self._recorder()
        await recorder.attach(bus)
        await recorder.detach(bus)

        await bus.emit(EventType.TASK_STARTED, correlation_id="r", task_id="t")

        self.assertEqual(collected, [])
        self.assertEqual(bus.subscriber_count(EventType.TASK_STARTED.value), 0)


class QualityFilterTests(unittest.TestCase):
    """The filter classifies; it never deletes, and it always says why."""

    def test_a_complete_run_is_accepted(self) -> None:
        verdict = DataQualityFilter().assess(make_trajectory())

        self.assertEqual(verdict.verdict, "accepted")
        self.assertTrue(verdict.accepted)
        self.assertEqual(verdict.issues, ())
        self.assertIn("presence", verdict.checks)

    def test_an_incomplete_run_is_held_for_review(self) -> None:
        verdict = DataQualityFilter().assess(
            make_trajectory(status="in_progress", success=None)
        )

        self.assertEqual(verdict.verdict, "needs_review")
        self.assertIn("incomplete_execution", verdict.codes())

    def test_an_empty_row_is_rejected(self) -> None:
        verdict = DataQualityFilter().assess(AgentTrajectory(trajectory_id="empty"))

        self.assertEqual(verdict.verdict, "rejected")
        self.assertIn("empty_trajectory", verdict.codes())

    def test_success_beside_a_failed_verification_needs_review(self) -> None:
        row = make_trajectory(
            verification_results=(VerificationRecord(step_id="s1", status="failed"),)
        )

        verdict = DataQualityFilter().assess(row)

        self.assertEqual(verdict.verdict, "needs_review")
        self.assertIn("failed_verification", verdict.codes())

    def test_a_run_with_a_failed_tool_and_no_recovery_needs_review(self) -> None:
        row = make_trajectory(
            tool_calls=(ToolCallRecord(tool="browser.open", status="failed", error="timeout"),)
        )

        verdict = DataQualityFilter().assess(row)

        self.assertEqual(verdict.verdict, "needs_review")
        self.assertIn("contradictory_outcome", verdict.codes())

    def test_a_refusal_is_held_as_safety_evidence(self) -> None:
        row = make_trajectory(
            tool_calls=(
                ToolCallRecord(
                    tool="files.delete",
                    status="denied",
                    error="permission denied",
                    requires_confirmation=True,
                    approved=False,
                ),
            ),
            success=False,
            failure_reason="refused",
        )

        verdict = DataQualityFilter().assess(row)

        self.assertEqual(verdict.verdict, "needs_review")
        self.assertIn("unsafe_action", verdict.codes())
        self.assertIn("unsafe_action", verdict.to_dict()["reasons"])

    def test_an_action_that_ran_after_its_confirmation_was_refused_is_rejected(self) -> None:
        row = make_trajectory(
            tool_calls=(
                ToolCallRecord(
                    tool="files.delete",
                    status="completed",
                    requires_confirmation=True,
                    approved=False,
                ),
            )
        )

        verdict = DataQualityFilter().assess(row)

        self.assertEqual(verdict.verdict, "rejected")
        self.assertIn("unsafe_action", verdict.codes())

    def test_duplicate_work_is_rejected_with_the_original_named(self) -> None:
        quality = DataQualityFilter()
        first = make_trajectory(trajectory_id="a", task_id="t-a")
        second = make_trajectory(trajectory_id="b", task_id="t-b")

        self.assertEqual(quality.assess(first).verdict, "accepted")
        verdict = quality.assess(second)

        self.assertEqual(verdict.verdict, "rejected")
        self.assertIn("duplicate", verdict.codes())
        self.assertIn("a", verdict.to_dict()["issues"][0]["detail"])

    def test_residual_sensitive_text_rejects_the_row(self) -> None:
        row = make_trajectory(final_result={"note": f"token={SECRET}"})

        verdict = DataQualityFilter().assess(row)

        self.assertEqual(verdict.verdict, "rejected")
        self.assertIn("sensitive_data", verdict.codes())

    def test_residual_sensitive_text_can_be_held_instead_of_rejected(self) -> None:
        row = make_trajectory(final_result={"note": f"token={SECRET}"})

        verdict = DataQualityFilter(QualityConfig(reject_on_sensitive=False)).assess(row)

        self.assertEqual(verdict.verdict, "needs_review")
        self.assertIn("sensitive_data", verdict.codes())

    def test_a_malformed_row_is_rejected(self) -> None:
        cyclic: dict[str, Any] = {}
        cyclic["self"] = cyclic
        row = make_trajectory(metadata=cyclic)

        verdict = DataQualityFilter().assess(row)

        self.assertEqual(verdict.verdict, "rejected")
        self.assertIn("malformed_data", verdict.codes())

    def test_a_tool_call_without_a_name_is_rejected(self) -> None:
        row = make_trajectory(tool_calls=(ToolCallRecord(tool="", status="completed"),))

        verdict = DataQualityFilter().assess(row)

        self.assertEqual(verdict.verdict, "rejected")
        self.assertIn("invalid_tool_call", verdict.codes())

    def test_a_newer_schema_is_held_for_review(self) -> None:
        verdict = DataQualityFilter().assess(make_trajectory(schema_version=99))

        self.assertEqual(verdict.verdict, "needs_review")
        self.assertIn("unknown_schema", verdict.codes())

    def test_a_noisy_row_is_held_for_review(self) -> None:
        row = make_trajectory(
            execution_steps=tuple(
                ExecutionStep(step_id=f"s{index}", status="completed") for index in range(70)
            )
        )

        verdict = DataQualityFilter().assess(row)

        self.assertEqual(verdict.verdict, "needs_review")
        self.assertIn("noisy_example", verdict.codes())

    def test_filtering_can_be_switched_off_without_losing_the_note(self) -> None:
        verdict = DataQualityFilter(QualityConfig(enabled=False)).assess(AgentTrajectory())

        self.assertEqual(verdict.verdict, "accepted")
        self.assertEqual(verdict.checks, ())
        self.assertIn("filter_disabled", verdict.codes())

    def test_stats_count_verdicts_and_reasons(self) -> None:
        quality = DataQualityFilter()
        quality.assess(make_trajectory())
        quality.assess(AgentTrajectory(trajectory_id="empty"))

        stats = quality.stats()

        self.assertEqual(stats["considered"], 2)
        self.assertEqual(stats["by_verdict"]["accepted"], 1)
        self.assertEqual(stats["by_verdict"]["rejected"], 1)
        self.assertEqual(stats["by_code"]["empty_trajectory"], 1)

        quality.reset()
        self.assertEqual(quality.stats()["considered"], 0)


class EvaluationEngineTests(unittest.TestCase):
    """Nine dimensions, scored separately, on evidence or not at all."""

    def setUp(self) -> None:
        self.engine = EvaluationEngine()

    def test_a_complete_run_scores_every_dimension_it_has_evidence_for(self) -> None:
        result = self.engine.evaluate(make_trajectory())

        self.assertEqual(result.overall_status, "pass")
        self.assertIs(True, result.task_success)
        self.assertEqual(
            set(result.scores()),
            {
                "nlu",
                "decision",
                "tool_selection",
                "planning",
                "execution",
                "verification",
                "recovery",
                "safety",
                "efficiency",
            },
        )
        self.assertIsNotNone(result.nlu_score)
        self.assertIsNotNone(result.execution_score)
        self.assertIsNotNone(result.efficiency_score)
        self.assertIsNone(result.recovery_score, "nothing failed, so recovery has no evidence")

    def test_a_dimension_without_evidence_says_unknown_not_zero(self) -> None:
        result = self.engine.evaluate(AgentTrajectory(trajectory_id="bare"))

        self.assertEqual(result.overall_status, "unknown")
        self.assertTrue(all(score.score is None for score in result.dimensions))

    def test_a_false_success_is_caught_by_the_verification_dimension(self) -> None:
        row = make_trajectory(
            verification_results=(VerificationRecord(step_id="s1", status="failed"),)
        )

        result = self.engine.evaluate(row)

        self.assertEqual(result.verification_score, 0.0)
        findings = " ".join(result.dimension("verification").findings)  # type: ignore[union-attr]
        self.assertIn("false success", findings)

    def test_an_escalated_one_step_request_is_flagged(self) -> None:
        row = make_trajectory(
            decision={"route": "agent"},
            tool_calls=(),
            execution_steps=(),
            plan={},
        )

        result = self.engine.evaluate(row)

        decision = result.dimension("decision")
        self.assertIsNotNone(decision)
        self.assertLess(decision.score or 0.0, 1.0)  # type: ignore[union-attr]
        self.assertIn("unnecessary", " ".join(decision.findings))  # type: ignore[union-attr]

    def test_a_denied_action_counts_as_a_safety_intervention(self) -> None:
        row = make_trajectory(
            tool_calls=(
                ToolCallRecord(
                    tool="files.delete",
                    status="denied",
                    error="permission denied",
                    requires_confirmation=True,
                    approved=False,
                ),
            ),
            success=False,
            failure_reason="refused",
        )

        result = self.engine.evaluate(row)

        self.assertEqual(result.safety_score, 1.0)
        self.assertEqual(result.overall_status, "fail", "the task itself did not succeed")

    def test_a_golden_expectation_is_checked_field_by_field(self) -> None:
        example = builtin_golden_dataset().example("core-report-machine-facts")
        assert example is not None
        row = make_trajectory(
            user_request=example.request,
            structured_intent={"intent": "system_info", "confidence": 0.88},
            decision={"route": "system_tools"},
            tool_calls=(ToolCallRecord(tool="machine_facts", status="completed"),),
        )

        result = self.engine.evaluate(row, expected=example)

        self.assertEqual(result.golden_example_id, "core-report-machine-facts")
        self.assertEqual(result.golden_matches["intent"], True)
        self.assertEqual(result.golden_matches["route"], True)
        self.assertEqual(result.golden_matches["tool"], True)

    def test_a_wrong_expectation_shows_up_in_the_score(self) -> None:
        example = GoldenExample(
            example_id="x",
            request="open calculator",
            expected_intent="open_application",
            expected_decision={"route": "direct_tool"},
            expected_tool="files.list",
            expected_outcome="success",
        )
        row = make_trajectory(
            structured_intent={"intent": "conversation", "confidence": 0.9},
            decision={"route": "chat"},
        )

        result = self.engine.evaluate(row, expected=example)

        self.assertEqual(result.golden_matches["intent"], False)
        self.assertEqual(result.golden_matches["route"], False)
        self.assertEqual(result.golden_matches["tool"], False)
        assert result.nlu_score is not None
        self.assertLess(result.nlu_score, 0.8, "a missed intent must cost the NLU score")

    def test_plan_properties_from_a_golden_example_are_enforced(self) -> None:
        example = GoldenExample(
            example_id="plan",
            request="explain an event bus",
            plan_properties={"min_steps": 0, "max_steps": 1},
        )
        row = make_trajectory(plan={}, execution_steps=())

        result = self.engine.evaluate(row, expected=example)

        self.assertEqual(result.dimension("planning").score, 1.0)  # type: ignore[union-attr]

    def test_the_result_serialises_and_reloads(self) -> None:
        result = self.engine.evaluate(make_trajectory())

        restored = type(result).from_dict(result.to_dict())

        self.assertEqual(restored.evaluation_id, result.evaluation_id)
        self.assertEqual(restored.scores(), result.scores())
        self.assertEqual(restored.overall_status, result.overall_status)

    def test_the_named_scores_are_the_dimensions(self) -> None:
        payload = self.engine.evaluate(make_trajectory()).to_dict()

        self.assertIn("nlu_score", payload)
        self.assertIn("efficiency_score", payload)
        self.assertEqual(payload["scores"]["nlu"], payload["nlu_score"])
        self.assertEqual(payload["evaluator_version"], "phase15.1")

    def test_costs_over_budget_reduce_the_efficiency_score(self) -> None:
        engine = EvaluationEngine(EvaluationConfig(latency_budget_ms=100.0))
        row = make_trajectory(latency_metrics=LatencyMetrics(total_ms=1000.0))

        result = engine.evaluate(row)

        self.assertLess(result.efficiency_score or 0.0, 1.0)
        self.assertIn("over budget", " ".join(result.dimension("efficiency").findings))  # type: ignore[union-attr]


class RewardEngineTests(unittest.TestCase):
    """A reward is a weighted reading of observable outcomes — with its reasons."""

    def _score(self, row: AgentTrajectory, engine: RewardEngine | None = None) -> Any:
        engine = engine or RewardEngine()
        return engine.score(row, EvaluationEngine().evaluate(row))

    def test_a_good_run_rewards_every_component_it_has_evidence_for(self) -> None:
        result = self._score(make_trajectory())

        self.assertGreater(result.total_reward, 0.0)
        self.assertEqual(result.component_rewards["task_success"], 1.0)
        self.assertEqual(result.penalties["failed_execution"], 0.0)
        self.assertIn("total", result.explanation_summary)

    def test_the_breakdown_names_every_factor(self) -> None:
        result = self._score(make_trajectory())

        names = {component.name for component in result.components}
        self.assertEqual(
            names,
            {
                "task_success",
                "verification_success",
                "tool_correctness",
                "plan_efficiency",
                "safety",
                "latency",
                "resource_efficiency",
            },
        )
        for component in result.components:
            self.assertTrue(component.reason, f"{component.name} must say why it scored")
        self.assertEqual(
            {penalty.name for penalty in result.penalty_breakdown},
            {
                "unnecessary_action",
                "failed_execution",
                "failed_verification",
                "unsafe_action",
                "excessive_retry",
            },
        )

    def test_weights_are_configurable_and_change_the_total(self) -> None:
        weights = {
            "task_success": 2.0,
            "verification_success": 0.0,
            "tool_correctness": 0.0,
            "plan_efficiency": 0.0,
            "safety": 0.0,
            "latency": 0.0,
            "resource_efficiency": 0.0,
        }
        penalties = dict.fromkeys(
            (
                "unnecessary_action",
                "failed_execution",
                "failed_verification",
                "unsafe_action",
                "excessive_retry",
            ),
            0.0,
        )
        engine = RewardEngine(RewardConfig(component_weights=weights, penalty_weights=penalties))

        result = self._score(make_trajectory(), engine)

        self.assertEqual(result.total_reward, 2.0)
        self.assertEqual(result.weights["component_weights"]["task_success"], 2.0)

    def test_config_from_mapping_ignores_an_unusable_weight(self) -> None:
        config = RewardConfig.from_mapping(
            {
                "component_weights": {"task_success": float("nan"), "latency": 0.5},
                "max_retries": -3,
            }
        )

        self.assertEqual(config.weight("task_success"), 1.0)
        self.assertEqual(config.weight("latency"), 0.5)
        self.assertEqual(config.max_retries, RewardConfig().max_retries)

    def test_an_action_that_bypassed_confirmation_is_penalised(self) -> None:
        row = make_trajectory(
            tool_calls=(
                ToolCallRecord(
                    tool="files.delete",
                    status="completed",
                    requires_confirmation=True,
                    approved=False,
                ),
            )
        )

        result = self._score(row)

        self.assertGreater(result.penalties["unsafe_action"], 0.0)
        self.assertIn("unsafe_action", result.explanation_summary)

    def test_a_refusal_is_not_penalised(self) -> None:
        row = make_trajectory(
            tool_calls=(
                ToolCallRecord(
                    tool="files.delete",
                    status="denied",
                    error="permission denied",
                    requires_confirmation=True,
                    approved=False,
                ),
            ),
            success=False,
            failure_reason="refused",
        )

        result = self._score(row)

        self.assertEqual(result.penalties["unsafe_action"], 0.0)
        self.assertEqual(result.component_rewards["safety"], 0.3)

    def test_a_failed_verification_is_penalised(self) -> None:
        row = make_trajectory(
            verification_results=(VerificationRecord(step_id="s1", status="failed"),)
        )

        result = self._score(row)

        self.assertGreater(result.penalties["failed_verification"], 0.0)

    def test_excessive_retries_are_penalised(self) -> None:
        row = make_trajectory(
            tool_calls=tuple(
                ToolCallRecord(tool="browser.open", status="completed") for _ in range(5)
            )
        )

        result = self._score(row)

        self.assertGreater(result.penalties["excessive_retry"], 0.0)
        self.assertGreater(result.penalties["unnecessary_action"], 0.0)

    def test_a_failed_task_loses_the_success_component_and_takes_the_penalty(self) -> None:
        row = make_trajectory(success=False, failure_reason="tool crashed")

        result = self._score(row)

        self.assertEqual(result.component_rewards["task_success"], 0.0)
        self.assertEqual(result.penalties["failed_execution"], 0.5)

    def test_the_total_is_clamped_and_normalised(self) -> None:
        row = make_trajectory(success=False, failure_reason="everything broke")

        result = self._score(row)

        self.assertGreaterEqual(result.total_reward, -3.0)
        self.assertLessEqual(result.total_reward, 3.0)
        self.assertIsNotNone(result.normalized_reward)
        assert result.normalized_reward is not None
        self.assertGreaterEqual(result.normalized_reward, -1.0)
        self.assertLessEqual(result.normalized_reward, 1.0)

    def test_the_reward_serialises_with_its_breakdown(self) -> None:
        result = self._score(make_trajectory())

        restored = type(result).from_dict(result.to_dict())

        self.assertEqual(restored.total_reward, result.total_reward)
        self.assertEqual(restored.reward_version, result.reward_version)
        self.assertEqual(
            [component.name for component in restored.components],
            [component.name for component in result.components],
        )
        self.assertTrue(restored.evidence)

    def test_unmeasured_figures_are_neutral_rather_than_assumed(self) -> None:
        row = make_trajectory(
            latency_metrics=LatencyMetrics(),
            resource_usage=ResourceUsage(),
        )

        result = self._score(row)

        self.assertEqual(result.component_rewards["latency"], 0.05)
        self.assertEqual(result.component_rewards["resource_efficiency"], 0.05)
        self.assertIn("not measured", result.component("latency").reason)  # type: ignore[union-attr]


class GoldenDatasetTests(unittest.TestCase):
    """The dataset is small, deterministic and versioned."""

    def test_the_builtin_dataset_loads(self) -> None:
        dataset = builtin_golden_dataset()

        self.assertEqual(dataset.dataset_id, BUILTIN_DATASET_ID)
        self.assertGreaterEqual(len(dataset), 5)
        self.assertTrue(dataset.description)
        self.assertIsNotNone(dataset.example("core-refuse-destructive"))

    def test_every_example_names_something_real(self) -> None:
        dataset = builtin_golden_dataset()

        for example in dataset:
            with self.subTest(example=example.example_id):
                self.assertTrue(example.request)
                self.assertTrue(example.expected_intent)
                self.assertTrue(example.tags)

    def test_it_is_deterministic(self) -> None:
        first = builtin_golden_dataset().to_dict()
        second = builtin_golden_dataset().to_dict()

        self.assertEqual(first, second)

    def test_it_round_trips_and_can_be_versioned(self) -> None:
        dataset = builtin_golden_dataset()
        restored = GoldenDataset.from_dict(dataset.to_dict())

        self.assertEqual(len(restored), len(dataset))

        next_version = dataset.with_version("1.1.0")
        self.assertEqual(next_version.version, "1.1.0")
        self.assertEqual(dataset.version, "1.0.0", "the original is unchanged")

    def test_examples_can_be_selected_by_tag(self) -> None:
        dataset = builtin_golden_dataset()

        self.assertTrue(dataset.by_tag("safety"))
        self.assertEqual(dataset.by_tag("no-such-tag"), ())

    def test_plan_properties_are_checked(self) -> None:
        example = GoldenExample(
            example_id="p", request="x", plan_properties={"min_steps": 2, "max_steps": 3}
        )

        self.assertEqual(example.plan_violations(2), ())
        self.assertTrue(example.plan_violations(1))
        self.assertTrue(example.plan_violations(9))


class StorageTests(unittest.TestCase):
    """Repositories over the project's existing JSONL pattern. Replaceable."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.trajectories = TrajectoryRepository(JsonlRecordStore(self.root / "t.jsonl"))
        self.evaluations = EvaluationRepository(InMemoryRecordStore())
        self.rewards = RewardRepository(InMemoryRecordStore())
        self.datasets = DatasetRepository(InMemoryRecordStore())

    def test_a_trajectory_survives_a_real_file(self) -> None:
        row = make_trajectory()

        self.trajectories.save(row)
        reloaded = TrajectoryRepository(
            JsonlRecordStore(self.root / "t.jsonl")
        ).get("traj-1")

        self.assertEqual(reloaded, row)
        self.assertTrue((self.root / "t.jsonl").exists())
        self.assertTrue(self.trajectories.store.local)

    def test_saving_the_same_id_twice_replaces_rather_than_duplicates(self) -> None:
        self.trajectories.save(make_trajectory())
        self.trajectories.save(make_trajectory(latency_metrics=LatencyMetrics(total_ms=42.0)))

        rows = self.trajectories.list()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].latency_metrics.total_ms, 42.0)

    def test_filtering_by_task_model_date_and_status(self) -> None:
        self.trajectories.save(
            make_trajectory(
                trajectory_id="a",
                task_id="task-a",
                timestamp="2026-09-01T10:00:00+00:00",
                model_information={"model": "qwen3:8b"},
            )
        )
        self.trajectories.save(
            make_trajectory(
                trajectory_id="b",
                task_id="task-b",
                timestamp="2026-09-20T10:00:00+00:00",
                status="failed",
                success=False,
                model_information={"model": "llama3:8b"},
            )
        )

        self.assertEqual(
            [row.trajectory_id for row in self.trajectories.list(task_id="task-a")], ["a"]
        )
        self.assertEqual(
            [row.trajectory_id for row in self.trajectories.list(status="failed")], ["b"]
        )
        self.assertEqual(
            [row.trajectory_id for row in self.trajectories.list(model="qwen3")], ["a"]
        )
        self.assertEqual(
            [row.trajectory_id for row in self.trajectories.list(since="2026-09-15")], ["b"]
        )
        self.assertEqual(
            [row.trajectory_id for row in self.trajectories.list(until="2026-09-15")], ["a"]
        )
        self.assertEqual(self.trajectories.list(limit=1)[0].trajectory_id, "b")

    def test_an_unreadable_line_does_not_poison_the_file(self) -> None:
        self.trajectories.save(make_trajectory())
        with (self.root / "t.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("{not json at all\n")

        self.assertEqual(len(self.trajectories.list()), 1)

    def test_a_repository_is_capped(self) -> None:
        repo = TrajectoryRepository(InMemoryRecordStore(), cap=2)
        for index in range(4):
            repo.save(make_trajectory(trajectory_id=f"t{index}", task_id=f"task{index}"))

        self.assertEqual([row.trajectory_id for row in repo.list()], ["t3", "t2"])

    def test_pruning_by_age_and_by_cap(self) -> None:
        now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
        for index, stamp in enumerate(("2026-01-01T00:00:00+00:00", now.isoformat())):
            self.trajectories.save(
                make_trajectory(trajectory_id=f"t{index}", timestamp=stamp)
            )

        removed = self.trajectories.prune(older_than_days=30, now=now)

        self.assertEqual(removed, 1)
        self.assertEqual(self.trajectories.count(), 1)

        cut = self.trajectories.prune(max_records=0)
        self.assertEqual(cut, 0, "a cap of zero means no cap, not delete everything")

    def test_evaluations_and_rewards_are_stored_by_trajectory(self) -> None:
        row = make_trajectory()
        evaluation = EvaluationEngine().evaluate(row)
        reward = RewardEngine().score(row, evaluation)

        self.evaluations.save(evaluation)
        self.rewards.save(reward)

        stored_evaluation = self.evaluations.for_trajectory("traj-1")
        stored_reward = self.rewards.for_trajectory("traj-1")
        assert stored_evaluation is not None and stored_reward is not None
        self.assertEqual(stored_evaluation.evaluation_id, evaluation.evaluation_id)
        self.assertEqual(stored_reward.reward_id, reward.reward_id)
        by_id = self.evaluations.get(evaluation.evaluation_id)
        assert by_id is not None
        self.assertEqual(by_id.evaluation_id, evaluation.evaluation_id)
        self.assertEqual(
            [item.reward_id for item in self.rewards.list(min_total=0.0)], [reward.reward_id]
        )
        self.assertEqual(self.rewards.list(min_total=99.0), ())
        self.assertEqual(
            self.evaluations.list(status="pass")[0].evaluation_id, evaluation.evaluation_id
        )

    def test_dataset_versions_accumulate(self) -> None:
        dataset = builtin_golden_dataset()
        self.datasets.save(dataset)
        self.datasets.save(dataset.with_version("1.1.0"))

        versions = self.datasets.versions(dataset.dataset_id)

        self.assertEqual([item.version for item in versions], ["1.0.0", "1.1.0"])
        self.assertEqual(self.datasets.latest(dataset.dataset_id).version, "1.1.0")  # type: ignore[union-attr]
        self.assertEqual(self.datasets.list()[0].dataset_id, dataset.dataset_id)

    def test_a_row_without_an_identity_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.trajectories.save_row({"task_id": "no id here"})

    def test_clearing_reports_what_went(self) -> None:
        self.trajectories.save(make_trajectory())

        self.assertEqual(self.trajectories.clear(), 1)
        self.assertEqual(self.trajectories.count(), 0)


class MetricsTests(unittest.TestCase):
    """Aggregates over stored rows, including the percentiles."""

    def test_percentile_is_none_for_no_sample(self) -> None:
        self.assertIsNone(percentile([], 0.5))
        self.assertEqual(percentile([5.0], 0.95), 5.0)

    def test_p50_and_p95_use_linear_interpolation(self) -> None:
        values = [float(index) for index in range(1, 101)]

        self.assertAlmostEqual(percentile(values, 0.5) or 0.0, 50.5, places=3)
        self.assertAlmostEqual(percentile(values, 0.95) or 0.0, 95.05, places=3)

    def test_the_figures_are_computed_from_stored_rows(self) -> None:
        good = make_trajectory(
            trajectory_id="a", task_id="ta", latency_metrics=LatencyMetrics(total_ms=100.0)
        )
        bad = make_trajectory(
            trajectory_id="b",
            task_id="tb",
            success=False,
            failure_reason="boom",
            status="failed",
            latency_metrics=LatencyMetrics(total_ms=300.0),
        )
        unmeasured = make_trajectory(
            trajectory_id="c", task_id="tc", latency_metrics=LatencyMetrics()
        )
        engine = EvaluationEngine()
        evaluations = tuple(engine.evaluate(row) for row in (good, bad, unmeasured))
        rewards = tuple(
            RewardEngine().score(row, engine.evaluate(row)) for row in (good, bad, unmeasured)
        )

        metrics = MetricsCalculator().compute((good, bad, unmeasured), evaluations, rewards)

        self.assertEqual(metrics["trajectories"], 3)
        self.assertAlmostEqual(metrics["task_success_rate"] or 0.0, 2 / 3, places=6)
        self.assertAlmostEqual(metrics["failure_rate"] or 0.0, 1 / 3, places=6)
        self.assertEqual(metrics["measured_latency_runs"], 2)
        self.assertAlmostEqual(metrics["average_latency_ms"] or 0.0, 200.0)
        self.assertAlmostEqual(metrics["p50_latency_ms"] or 0.0, 200.0)
        self.assertAlmostEqual(metrics["p95_latency_ms"] or 0.0, 290.0, places=3)
        self.assertEqual(metrics["verification_success_rate"], 1.0)
        self.assertIsNotNone(metrics["average_reward"])
        self.assertIsNotNone(metrics["tool_selection_accuracy"])

    def test_routing_rates_do_not_claim_to_sum_to_one_hundred(self) -> None:
        fast = make_trajectory(trajectory_id="a", decision={"route": "direct_tool"})
        model = make_trajectory(trajectory_id="b", decision={"route": "local_llm"})
        other = make_trajectory(trajectory_id="c", decision={"route": "chat"})

        metrics = MetricsCalculator().compute((fast, model, other))

        self.assertAlmostEqual(metrics["fast_path_percentage"] or 0.0, 1 / 3, places=6)
        self.assertAlmostEqual(metrics["llm_escalation_percentage"] or 0.0, 1 / 3, places=6)
        self.assertEqual(metrics["routes"], {"chat": 1, "direct_tool": 1, "local_llm": 1})

    def test_routing_rates_count_runs_not_distinct_routes(self) -> None:
        # Nine runs through the model and one through a direct tool: the
        # percentages describe the RUNS (10% / 90%), not the two route names
        # (which would read as 50% each).
        model = tuple(
            make_trajectory(
                trajectory_id=f"m{index}",
                task_id=f"tm{index}",
                decision={"route": "local_llm"},
            )
            for index in range(9)
        )
        fast = make_trajectory(trajectory_id="d", task_id="td", decision={"route": "direct_tool"})

        metrics = MetricsCalculator().compute(model + (fast,))

        self.assertAlmostEqual(metrics["fast_path_percentage"] or 0.0, 1 / 10, places=6)
        self.assertAlmostEqual(metrics["llm_escalation_percentage"] or 0.0, 9 / 10, places=6)
        self.assertEqual(metrics["routes"], {"direct_tool": 1, "local_llm": 9})

    def test_an_empty_store_has_no_rates_rather_than_zero_rates(self) -> None:
        metrics = MetricsCalculator().compute(())

        self.assertEqual(metrics["trajectories"], 0)
        self.assertIsNone(metrics["task_success_rate"])
        self.assertIsNone(metrics["average_reward"])
        self.assertIsNone(metrics["p95_latency_ms"])
        self.assertEqual(metrics["quality"]["considered"], 0)

    def test_safety_interventions_and_retries_are_counted(self) -> None:
        refused = make_trajectory(
            trajectory_id="a",
            tool_calls=(
                ToolCallRecord(
                    tool="files.delete",
                    status="denied",
                    error="denied",
                    requires_confirmation=True,
                    approved=False,
                ),
            ),
        )
        retried = make_trajectory(
            trajectory_id="b",
            tool_calls=(
                ToolCallRecord(tool="browser.open", status="completed"),
                ToolCallRecord(tool="browser.open", status="completed"),
            ),
        )

        metrics = MetricsCalculator().compute((refused, retried))

        self.assertEqual(metrics["safety_intervention_rate"], 0.5)
        self.assertEqual(metrics["retry_rate"], 0.5)
        self.assertEqual(metrics["tool_calls"], 3)


class ServiceTests(unittest.TestCase):
    """The service is the order the pieces run in, and the failure boundary."""

    def setUp(self) -> None:
        self.service = EvaluationService()

    def test_a_recorded_trajectory_produces_all_three_rows(self) -> None:
        outcome = self.service.record(make_trajectory())

        self.assertEqual(outcome["quality"]["verdict"], "accepted")
        self.assertTrue(outcome["evaluation_id"])
        self.assertTrue(outcome["reward_id"])
        found = self.service.trajectory("traj-1")
        assert found is not None
        self.assertEqual(found["trajectory"]["evaluation_id"], outcome["evaluation_id"])
        self.assertEqual(found["evaluation"]["overall_status"], "pass")
        self.assertEqual(found["reward"]["total_reward"], outcome["total_reward"])
        self.assertEqual(self.service.trajectories.count(), 1)
        self.assertEqual(self.service.evaluations.count(), 1)
        self.assertEqual(self.service.rewards.count(), 1)

    def test_re_recording_one_trajectory_updates_rather_than_duplicates(self) -> None:
        self.service.record(make_trajectory())
        self.service.record(make_trajectory(latency_metrics=LatencyMetrics(total_ms=99.0)))

        self.assertEqual(self.service.trajectories.count(), 1)
        self.assertEqual(self.service.evaluations.count(), 1)
        self.assertEqual(self.service.rewards.count(), 1)
        found = self.service.trajectory("traj-1")
        assert found is not None
        self.assertEqual(found["trajectory"]["latency_metrics"]["total_ms"], 99.0)

    def test_an_unknown_trajectory_is_absent_not_invented(self) -> None:
        self.assertIsNone(self.service.trajectory("never-happened"))

    def test_a_broken_step_is_counted_and_survived(self) -> None:
        broken = EvaluationEngine()
        with mock.patch.object(broken, "evaluate", side_effect=RuntimeError("engine down")):
            self.service.evaluator = broken
            self.service._on_trajectory(make_trajectory())

        self.assertEqual(self.service.failures, 1)
        self.assertEqual(self.service.trajectories.count(), 0)

    def test_feedback_is_stored_beside_the_run_without_changing_the_reward_rules(self) -> None:
        self.service.record(make_trajectory())
        before = self.service.trajectory("traj-1")
        assert before is not None

        updated = self.service.feedback("traj-1", rating=5, label="good", comment="worked")

        assert updated is not None
        self.assertEqual(updated.user_feedback.rating, 5)
        after = self.service.trajectory("traj-1")
        assert after is not None
        self.assertEqual(after["trajectory"]["user_feedback"]["label"], "good")
        self.assertNotEqual(after["reward"]["reward_id"], "")
        self.assertIsNone(self.service.feedback("never-happened", rating=1))

    def test_the_summary_reports_what_is_stored(self) -> None:
        self.service.record(make_trajectory())

        summary = self.service.summary()

        self.assertEqual(summary["trajectories"]["stored"], 1)
        self.assertEqual(summary["evaluations"]["stored"], 1)
        self.assertEqual(summary["rewards"]["stored"], 1)
        self.assertEqual(summary["quality"]["accepted"], 1)
        self.assertIn("task_success_rate", summary["trajectories"])
        self.assertFalse(summary["privacy"]["stores_chain_of_thought"])
        self.assertTrue(summary["privacy"]["recording_can_be_disabled"])

    def test_recording_can_be_switched_off_and_on(self) -> None:
        self.assertTrue(self.service.recording_enabled)

        self.assertFalse(self.service.set_recording(False))
        self.assertFalse(self.service.recording_enabled)
        self.assertTrue(self.service.set_recording(True))

    def test_retention_removes_old_rows_from_every_store(self) -> None:
        now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
        self.service.record(make_trajectory(timestamp=(now - timedelta(days=90)).isoformat()))
        self.service.record(
            make_trajectory(
                trajectory_id="fresh", task_id="task-2", timestamp=now.isoformat()
            )
        )

        removed = self.service.apply_retention()

        self.assertEqual(removed["trajectories"], 1)
        self.assertEqual(self.service.trajectories.count(), 1)

    def test_events_are_published_for_each_stored_row(self) -> None:
        seen: list[tuple[str, dict[str, Any]]] = []
        self.service.set_publisher(lambda type_, payload: seen.append((type_, dict(payload))))

        self.service.record(make_trajectory())

        self.assertEqual(
            [type_ for type_, _ in seen],
            ["trajectory.recorded", "evaluation.completed", "reward.computed"],
        )
        self.assertEqual(seen[1][1]["trajectory_id"], "traj-1")
        self.assertIn("nlu", seen[1][1]["scores"])
        self.assertEqual(seen[2][1]["reward_id"], "rw-traj-1")
        self.assertIn("total_reward", seen[2][1])

    def test_a_golden_example_named_by_a_run_is_scored_against_it(self) -> None:
        row = make_trajectory(
            metadata={"golden_example_id": "core-list-files"},
            user_request="list the files in my Downloads folder",
            structured_intent={"intent": "list_files", "confidence": 0.9},
            decision={"route": "direct_tool"},
        )

        self.service.record(row)

        found = self.service.trajectory("traj-1")
        assert found is not None
        self.assertEqual(found["evaluation"]["golden_example_id"], "core-list-files")

    def test_a_published_dataset_version_supersedes_the_builtin_copy(self) -> None:
        revised = builtin_golden_dataset().with_version("2.0.0")
        self.service.publish_dataset(revised)

        self.assertEqual(self.service.golden_dataset().version, "2.0.0")
        self.assertEqual(len(self.service.datasets_list()), 1)


class RecorderServiceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """The recorder and the service together, over a real EventBus."""

    async def test_a_lifecycle_recorded_through_the_bus_lands_in_storage(self) -> None:
        service = EvaluationService()
        bus = EventBus(continue_on_error=True)
        await service.start(bus)
        try:
            await bus.emit(EventType.TASK_STARTED, correlation_id="req", task_id="task")
            await bus.emit(
                EventType.INTENT_DETECTED,
                correlation_id="req",
                intent="open_application",
                strategy="fast_path",
                confidence=0.95,
            )
            await bus.emit(
                EventType.DECISION_CREATED,
                correlation_id="req",
                route="direct_tool",
                decision_type="execute",
            )
            await bus.emit(EventType.TOOL_STARTED, correlation_id="req", tool="desktop.launch")
            await bus.emit(
                EventType.TOOL_COMPLETED,
                correlation_id="req",
                tool="desktop.launch",
                status="completed",
            )
            await bus.emit(
                EventType.TASK_COMPLETED, correlation_id="req", task_id="task", route="direct_tool"
            )
        finally:
            await service.stop(bus)

        found = service.trajectory("req")
        assert found is not None
        self.assertEqual(found["trajectory"]["status"], "completed")
        self.assertEqual(found["evaluation"]["overall_status"], "pass")
        self.assertEqual(found["trajectory"]["quality"]["verdict"], "accepted")
        self.assertIsNotNone(found["reward"]["total_reward"])
        self.assertEqual(service.failures, 0)

    async def test_stopping_writes_out_a_partial_capture(self) -> None:
        service = EvaluationService()
        bus = EventBus(continue_on_error=True)
        await service.start(bus)
        await bus.emit(EventType.TASK_STARTED, correlation_id="req", task_id="task")

        await service.stop(bus)

        self.assertEqual(service.trajectories.count(), 1)
        found = service.trajectory("req")
        assert found is not None
        self.assertEqual(found["trajectory"]["quality"]["verdict"], "needs_review")


class ApplicationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """A real application: recording must work, and must never break a request.

    No model is loaded anywhere here — the requests below are answered by the
    deterministic layers and the scratch brain, which is exactly what the phase
    asks a 16 GB Windows desktop to be able to test.
    """

    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.app = NovaControlApplication(data_dir=self._tmp.name)
        await self.app.start()
        self.addCleanup(self._stop)

    async def _stop(self) -> None:
        await self.app.stop()

    async def test_a_live_request_is_recorded_end_to_end(self) -> None:
        response = await self.app.handle_request("hello there")

        self.assertTrue(response.summary or response.route)
        rows = self.app.evaluation.list_trajectories(limit=5)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.status, "completed")
        self.assertIs(True, row.success)
        self.assertTrue(row.structured_intent.get("intent"))
        self.assertTrue(row.decision.get("route"))
        self.assertIsNotNone(row.latency_metrics.total_ms)
        self.assertIn(row.quality["verdict"], {"accepted", "needs_review"})  # type: ignore[index]
        self.assertEqual(row.model_information.get("model", ""), "", "no model was loaded")
        found = self.app.evaluation_trajectory(row.trajectory_id)
        self.assertIsNotNone(found["evaluation"])
        self.assertIsNotNone(found["reward"])

    async def test_a_broken_sink_cannot_break_a_live_request(self) -> None:
        def explode(_row: AgentTrajectory) -> None:
            raise OSError("evidence store is on fire")

        self.app.evaluation.recorder.set_sink(explode)

        response = await self.app.handle_request("hello there")

        self.assertTrue(response.route)
        self.assertGreaterEqual(self.app.evaluation.recorder.failures, 1)

    async def test_recording_disabled_stores_nothing_and_still_serves(self) -> None:
        self.app.settings.update(evaluation_enabled=False)
        self.app.apply_evaluation_settings()

        response = await self.app.handle_request("hello there")

        self.assertTrue(response.route)
        self.assertFalse(self.app.evaluation_status()["enabled"])
        self.assertEqual(self.app.evaluation.list_trajectories(limit=5), ())
        self.assertEqual(self.app.evaluation.trajectories.count(), 0)

    async def test_the_rows_are_written_beside_the_audit_trail(self) -> None:
        await self.app.handle_request("hello there")

        path = Path(self._tmp.name) / "evaluation" / "trajectories.jsonl"
        self.assertTrue(path.exists())
        self.assertIn("trajectory_id", path.read_text(encoding="utf-8"))

    async def test_the_metrics_reflect_the_live_request(self) -> None:
        await self.app.handle_request("hello there")

        metrics = self.app.evaluation_metrics()

        self.assertEqual(metrics["trajectories"], 1)
        self.assertEqual(metrics["task_success_rate"], 1.0)
        self.assertIsNotNone(metrics["average_reward"])
        summary = self.app.evaluation_summary()
        self.assertEqual(summary["datasets"]["active"], BUILTIN_DATASET_ID)


@unittest.skipIf(TestClient is None, "httpx not installed")
class EvaluationApiTests(unittest.TestCase):
    """The read-only /evaluation/* surface, over real HTTP."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._nova: NovaControlApplication | None = None
        self._patchers = [
            mock.patch("novacontrol.api.app.NovaControlApplication", side_effect=self._isolated),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self._client = TestClient(create_app())
        self._client.__enter__()
        self.addCleanup(self._client.__exit__, None, None, None)

    def _isolated(self, **kwargs: Any) -> NovaControlApplication:
        kwargs["data_dir"] = self._tmp.name
        app = NovaControlApplication(**kwargs)
        self._nova = app
        return app

    def test_the_summary_is_served(self) -> None:
        response = self._client.get("/evaluation/summary")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("trajectories", body)
        self.assertIn("quality", body)
        self.assertIn("retention", body)
        self.assertFalse(body["privacy"]["stores_chain_of_thought"])

    def test_a_recorded_request_can_be_read_back_with_its_scores(self) -> None:
        self._client.post("/ask", json={"request": "hello there"})
        assert self._nova is not None
        rows = self._nova.evaluation.list_trajectories(limit=1)
        self.assertTrue(rows, "the live request must have been recorded")

        response = self._client.get(f"/evaluation/trajectory/{rows[0].trajectory_id}")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["trajectory"]["trajectory_id"], rows[0].trajectory_id)
        self.assertIn("overall_status", body["evaluation"])
        self.assertIn("total_reward", body["reward"])

    def test_an_unknown_trajectory_is_a_404(self) -> None:
        response = self._client.get("/evaluation/trajectory/nope")

        self.assertEqual(response.status_code, 404)

    def test_metrics_and_rewards_are_served(self) -> None:
        self._client.post("/ask", json={"request": "hello there"})

        metrics = self._client.get("/evaluation/metrics")
        rewards = self._client.get("/evaluation/rewards", params={"limit": 5})

        self.assertEqual(metrics.status_code, 200)
        self.assertIn("task_success_rate", metrics.json())
        self.assertIn("p95_latency_ms", metrics.json())
        self.assertEqual(rewards.status_code, 200)
        self.assertIn("rewards", rewards.json())

    def test_the_settings_round_trip_carries_the_recording_switch(self) -> None:
        saved = self._client.post("/settings", json={"evaluation_enabled": False})

        self.assertEqual(saved.status_code, 200)
        self.assertFalse(saved.json()["evaluation_enabled"])
        loaded = self._client.get("/settings")
        self.assertFalse(loaded.json()["evaluation_enabled"])
        assert self._nova is not None
        self.assertFalse(self._nova.evaluation.recording_enabled)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
