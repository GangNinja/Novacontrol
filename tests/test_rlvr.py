"""Phase 19: RLVR + critique-based learning — objective evidence as a reward.

Everything here runs on deterministic fixtures, recorded observations and a mock
environment: no model is loaded, no CUDA or NVIDIA GPU is required, no network
call is made by a verifier, nothing trains for real, and the optional training
dependencies may be absent. That mirrors the phase's own promise — a 16 GB
desktop with an Intel iGPU and an NPU must be able to prove the subsystem works —
so every check reads facts the system already recorded (a file, a process
report, a test summary, an HTTP response, rows, git state, an output, a JSON
object) instead of spawning a second, uncontrolled execution path.

The tests are grouped the way the phase specifies them:

  * VERIFIERS — all eight adapters, each on a pass and a failure,
  * REGISTRY — registration, discovery, selection, versioning, integrity,
  * RLVR — verification, reward, evidence, validation, partial and multi-step
    rewards, verifier failure, inconclusive verification,
  * CRITIQUE — generation, categories, severity, evidence, corrections,
  * INTEGRATION — critique → preference, critique → reward, verification →
    reward, trajectory → critique,
  * SECURITY — verifier and expectation tampering refused, configuration
    protected, permissions consulted,
  * DRY-RUN — the complete ten-stage pipeline on synthetic tasks,
  * REGRESSION — no reasoning trace is ever stored, the roster includes RLVR,
    and the normal application boots with the subsystem switched on but idle.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient

from novacontrol.api.app import create_app
from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.evaluation import (
    AgentTrajectory,
    DimensionScore,
    EvaluationDimension,
    EvaluationResult,
    ExecutionStep,
    LatencyMetrics,
    ToolCallRecord,
    VerificationRecord,
)
from novacontrol.rlhf.rollout import MockEnvironment, ScriptedPolicy
from novacontrol.rlhf.storage import build_rlhf_repositories
from novacontrol.rlvr import (
    RLVR_MODE,
    RLVR_PIPELINE_STAGES,
    CorrectedExample,
    CorrectedExampleBuilder,
    CorrectionProposal,
    CorrectionRepository,
    CritiqueCategory,
    CritiqueConfig,
    CritiqueDatasetBuilder,
    CritiqueDatasetRepository,
    CritiqueDatasetRules,
    CritiqueEngine,
    CritiqueResult,
    CritiqueSeverity,
    CritiqueSource,
    CritiqueTrainingMethod,
    CustomVerifier,
    DatabaseVerifier,
    FileVerifier,
    GitVerifier,
    HTTPVerifier,
    OutputVerifier,
    ProcessVerifier,
    RLVREvaluator,
    RLVRManager,
    RLVRModule,
    RLVRTrainer,
    RewardTamperGuard,
    SchemaVerifier,
    TestVerifier,
    VerifiableRewardConfig,
    VerifiableRewardProvider,
    VerifiableRewardValidator,
    VerificationRequest,
    VerificationResult,
    VerificationSecurityPolicy,
    VerificationStatus,
    VerificationSummary,
    VerifierPolicyConfig,
    VerifierRegistry,
    VerifierSelector,
    build_rlvr_repositories,
    critique_reward,
    default_verifiers,
)
from novacontrol.rlvr.config import RLVRTrainingConfig
from novacontrol.rlvr.datasets import critique_dataset_fingerprint
from novacontrol.rlvr.models import RLVR_VERSION, CorrectionStatus
from novacontrol.rlvr.pipeline import RLVRPipeline
from novacontrol.rlvr.rewards import (
    COMPONENT_CORRECTION,
    COMPONENT_PARTIAL,
    COMPONENT_VERIFICATION,
    PENALTY_EFFICIENCY,
    PENALTY_SAFETY,
)
from novacontrol.rlvr.security import SECURITY_EXPECTED_CHANGED
from novacontrol.evaluation.reward import RewardResult
from novacontrol.rlhf.config import RLTrainingConfig
from novacontrol.rlhf.rewards import RewardRequest
from novacontrol.training.storage import build_training_repositories


# ── fixtures ────────────────────────────────────────────────────────────────


def ask(
    verifier_id: str = "output",
    *,
    task_id: str = "task-1",
    trajectory_id: str = "traj-1",
    step_id: str = "",
    scope: str = "task",
    action: Mapping[str, Any] | None = None,
    expected: Mapping[str, Any] | None = None,
    observation: Mapping[str, Any] | None = None,
    workspace: str = "",
    frozen: bool = True,
) -> VerificationRequest:
    """One checkable question, frozen the way a run freezes it."""
    request = VerificationRequest(
        verifier_id=verifier_id,
        task_id=task_id,
        trajectory_id=trajectory_id,
        step_id=step_id,
        scope=scope,
        action=dict(action or {}),
        expected=dict(expected or {}),
        observation=dict(observation or {}),
        workspace=workspace,
    )
    return request.frozen() if frozen else request


def verdict(
    verifier_id: str = "output",
    status: str = VerificationStatus.PASS.value,
    *,
    task_id: str = "task-1",
    trajectory_id: str = "traj-1",
    step_id: str = "",
    scope: str = "task",
    evidence: Sequence[str] = ("output_match",),
    expected: Mapping[str, Any] | None = None,
    observed: Mapping[str, Any] | None = None,
    error_category: str = "",
    score: float | None = None,
    detail: str = "",
) -> VerificationResult:
    """A recorded verification, in the shape the reward layer consumes."""
    return VerificationResult(
        task_id=task_id,
        trajectory_id=trajectory_id,
        step_id=step_id,
        verifier_id=verifier_id,
        verifier_version=RLVR_VERSION,
        status=status,
        passed=status == VerificationStatus.PASS.value,
        score=score,
        scope=scope,
        evidence=tuple(evidence),
        expected=dict(expected or {"exact": "SUCCESS"}),
        observed=dict(observed or {"output": "SUCCESS"}),
        expected_digest="digest-1",
        error_category=error_category,
        detail=detail,
    )


def fresh_registry(**overrides: Any) -> VerifierRegistry:
    policy = VerifierPolicyConfig(**overrides) if overrides else None
    return VerifierRegistry(policy=policy)


def recording_publisher() -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    events: list[tuple[str, dict[str, Any]]] = []

    def publish(type_: str, payload: Mapping[str, Any]) -> None:
        events.append((type_, dict(payload)))

    return publish, events


def rlvr_config(
    dataset_version: str = "critiques@1.0.0", **overrides: Any
) -> RLVRTrainingConfig:
    """A dry-run RLVR configuration pointed at one critique dataset version."""
    fields: dict[str, Any] = {
        "base_model": "tiny-model",
        "output_directory": "out",
        "dry_run": True,
        "rollout_count": 2,
        "max_steps": 4,
        "checkpoint_frequency": 1,
        "max_checkpoints": 2,
    }
    fields.update(overrides)
    return RLVRTrainingConfig(
        rl=RLTrainingConfig(**fields),
        critique_dataset_version=dataset_version,
        verifiers=VerifierPolicyConfig(),
        reward=VerifiableRewardConfig(),
        critique=CritiqueConfig(),
    )


def trajectory(
    trajectory_id: str = "traj-1",
    *,
    task_id: str = "task-1",
    success: bool = False,
    verified: bool = False,
    tool: str = "shell.run",
    status: str = "failed",
    verification_status: str = "failed",
) -> AgentTrajectory:
    """One recorded run, with observable fields only — never a thought."""
    return AgentTrajectory(
        trajectory_id=trajectory_id,
        task_id=task_id,
        user_request="run the build",
        structured_intent={"intent": "build_project", "confidence": 0.9},
        status=status,
        success=success,
        tool_calls=(
            ToolCallRecord(
                tool=tool,
                step_id="s1",
                capability="shell",
                arguments={"command": "build"},
                status=status,
                duration_ms=120.0,
            ),
        ),
        execution_steps=(
            ExecutionStep(
                step_id="s1",
                description="run the build",
                action="build",
                status=status,
                tool=tool,
            ),
        ),
        verification_results=(
            VerificationRecord(
                step_id="s1",
                status=verification_status,
                verifier="test",
                detail="the build left a failing test",
            ),
        ),
        latency_metrics=LatencyMetrics(total_ms=250.0),
        metadata={"source": "test"},
    )


def evaluation_for(trajectory_id: str = "traj-1") -> EvaluationResult:
    return EvaluationResult(
        evaluation_id="eval-1",
        trajectory_id=trajectory_id,
        task_id="task-1",
        overall_status="needs_improvement",
        task_success=False,
        dimensions=(
            DimensionScore(
                dimension=EvaluationDimension.SAFETY.value,
                score=0.0,
                status="fail",
                findings=("an unapproved destructive command ran",),
            ),
            DimensionScore(
                dimension=EvaluationDimension.TOOL_SELECTION.value,
                score=0.6,
                status="warn",
                findings=("a narrower tool existed",),
            ),
        ),
    )


def make_manager(root: str | Path, **overrides: Any) -> RLVRManager:
    """An RLVR manager wired to JSONL repositories under a temp directory.

    The run, checkpoint, model and evaluation stores are Phase 16's and the
    feedback stores are Phase 18's, deliberately: an RLVR run IS a training run,
    and a critique is feedback's sibling, not a second universe.
    """
    repos = build_rlvr_repositories(root)
    rlhf = build_rlhf_repositories(root)
    supervised, runs, checkpoints, models, evaluations = build_training_repositories(
        root
    )
    kwargs: dict[str, Any] = {
        "critiques": repos.critiques,
        "corrections": repos.corrections,
        "critique_datasets": repos.datasets,
        "datasets": rlhf.datasets,
        "feedback": rlhf.feedback,
        "ratings": rlhf.ratings,
        "disagreements": rlhf.disagreements,
        "supervised_datasets": supervised,
        "runs": runs,
        "checkpoints": checkpoints,
        "models": models,
        "evaluations": evaluations,
        "output_root": Path(root) / "rlvr_output",
        "default_config": RLTrainingConfig(
            base_model="tiny-model",
            output_directory=str(Path(root) / "rlvr_output"),
            dry_run=True,
            rollout_count=2,
            max_steps=4,
            checkpoint_frequency=1,
            max_checkpoints=2,
        ),
    }
    kwargs.update(overrides)
    return RLVRManager(**kwargs)


TASKS: tuple[dict[str, Any], ...] = (
    {
        "task_id": "t-ok",
        "actions": [{"name": "produce", "output": "SUCCESS"}],
        "script": [{"output": "SUCCESS", "done": True, "success": True}],
        "expectations": {"step:0": {"exact": "SUCCESS"}, "task": {"exact": "SUCCESS"}},
    },
    {
        "task_id": "t-fail",
        "actions": [{"name": "produce", "output": "FAILED"}],
        "script": [{"output": "FAILED", "done": True, "success": False}],
        "expectations": {"step:0": {"exact": "SUCCESS"}, "task": {"exact": "SUCCESS"}},
    },
)

LABELS: dict[str, str] = {"t-ok": "pass", "t-fail": "fail"}


# ── verifiers ───────────────────────────────────────────────────────────────


class FileVerifierTests(unittest.TestCase):
    """FileVerifier: existence, content, hash, structure, and the escape rule."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # Bytes, not text: a text write translates ``\n`` to ``\r\n`` on
        # Windows, and the content/hash checks are about the bytes on disk.
        (self.root / "report.txt").write_bytes(b"build ok\n")
        (self.root / "model.json").write_bytes(
            json.dumps({"name": "nova", "version": 2}).encode("utf-8")
        )
        self.verifier = FileVerifier()

    def test_content_and_hash_matching_reports_pass_with_evidence(self) -> None:
        digest = hashlib.sha256(b"build ok\n").hexdigest()

        result = self.verifier.verify(
            ask(
                "file",
                workspace=str(self.root),
                expected={"path": "report.txt", "content": "build ok\n", "sha256": digest},
            )
        )

        self.assertEqual(result.status, VerificationStatus.PASS.value)
        self.assertEqual(result.score, 1.0)
        self.assertIn("file_exists", result.evidence)
        self.assertIn("file_content", result.evidence)
        self.assertTrue(result.factual)

    def test_a_missing_file_fails_and_names_the_expectation(self) -> None:
        result = self.verifier.verify(
            ask("file", workspace=str(self.root), expected={"path": "absent.txt", "exists": True})
        )

        self.assertEqual(result.status, VerificationStatus.FAIL.value)
        self.assertFalse(result.passed)
        self.assertIn("absent.txt", result.detail)

    def test_a_json_body_is_checked_structurally(self) -> None:
        result = self.verifier.verify(
            ask(
                "file",
                workspace=str(self.root),
                expected={"path": "model.json", "json": {"name": "nova", "version": 2}},
            )
        )

        self.assertEqual(result.status, VerificationStatus.PASS.value)

    def test_reading_outside_the_workspace_is_refused(self) -> None:
        result = self.verifier.verify(
            ask(
                "file",
                workspace=str(self.root),
                expected={"path": "../outside.txt", "exists": True},
            )
        )

        self.assertEqual(result.status, VerificationStatus.ERROR.value)
        self.assertIn("escapes the workspace", result.detail)

    def test_an_expectation_edited_after_freezing_is_refused(self) -> None:
        request = ask(
            "file",
            workspace=str(self.root),
            expected={"path": "report.txt", "content": "build ok\n"},
        )
        edited = VerificationRequest.from_dict(
            {**request.to_dict(), "expected": {"path": "report.txt", "content": "build failed\n"}}
        )

        result = self.verifier.verify(edited)

        self.assertEqual(result.status, VerificationStatus.ERROR.value)
        self.assertIn("changed after this request was frozen", result.detail)


class ProcessVerifierTests(unittest.TestCase):
    """ProcessVerifier reads what the runner recorded; it does not spawn."""

    def setUp(self) -> None:
        self.verifier = ProcessVerifier()

    def test_an_expected_exit_code_passes_from_recorded_facts(self) -> None:
        result = self.verifier.verify(
            ask(
                "process",
                expected={"state": "exited", "exit_code": 0, "stdout_contains": "ok"},
                observation={"process": {"running": False, "exit_code": 0, "stdout": "ok\n"}},
            )
        )

        self.assertEqual(result.status, VerificationStatus.PASS.value)
        self.assertIn("exit_code", result.evidence)

    def test_a_nonzero_exit_code_fails_as_an_execution_error(self) -> None:
        result = self.verifier.verify(
            ask(
                "process",
                expected={"exit_code": 0},
                observation={"process": {"running": False, "exit_code": 3, "stdout": ""}},
            )
        )

        self.assertEqual(result.status, VerificationStatus.FAIL.value)
        self.assertEqual(result.error_category, CritiqueCategory.EXECUTION_ERROR.value)

    def test_no_process_facts_is_inconclusive_rather_than_guessed(self) -> None:
        result = self.verifier.verify(
            ask("process", expected={"exit_code": 0}, observation={})
        )

        self.assertEqual(result.status, VerificationStatus.INCONCLUSIVE.value)
        self.assertIn("no process facts", result.detail)


class TestVerifierTests(unittest.TestCase):
    """TestVerifier answers counts from the recorded summary, never from hopes."""

    def setUp(self) -> None:
        self.verifier = TestVerifier()

    def test_min_passed_and_max_failed_are_read_from_the_summary(self) -> None:
        result = self.verifier.verify(
            ask(
                "test",
                expected={"result": "pass", "min_passed": 18, "max_failed": 0},
                observation={"tests": {"passed": 18, "failed": 0, "total": 18}},
            )
        )

        self.assertEqual(result.status, VerificationStatus.PASS.value)
        self.assertIn("test_result", result.evidence)

    def test_a_failing_test_run_fails_with_the_count_named(self) -> None:
        result = self.verifier.verify(
            ask(
                "test",
                expected={"result": "pass", "max_failed": 0},
                observation={"tests": {"passed": 17, "failed": 1, "total": 18}},
            )
        )

        self.assertEqual(result.status, VerificationStatus.FAIL.value)
        self.assertIn("expected at most 0", result.detail)

    def test_a_partial_count_passes_the_passing_checks_only(self) -> None:
        result = self.verifier.verify(
            ask(
                "test",
                expected={"min_passed": 18, "total": 18},
                observation={"tests": {"passed": 17, "failed": 1, "total": 18}},
            )
        )

        self.assertEqual(result.status, VerificationStatus.PARTIAL.value)
        self.assertAlmostEqual(result.score or 0.0, 0.5, places=6)

    def test_running_a_command_without_allow_execution_is_refused(self) -> None:
        result = self.verifier.verify(
            ask("test", expected={"result": "pass", "command": "pytest -q", "run": True})
        )

        self.assertEqual(result.status, VerificationStatus.ERROR.value)
        self.assertIn("allow_execution", result.detail)

    def test_an_absent_report_is_inconclusive_not_a_pass(self) -> None:
        result = self.verifier.verify(ask("test", expected={"result": "pass"}))

        self.assertEqual(result.status, VerificationStatus.INCONCLUSIVE.value)


class HTTPVerifierTests(unittest.TestCase):
    """HTTPVerifier checks a recorded response; it never re-issues the call."""

    def setUp(self) -> None:
        self.verifier = HTTPVerifier()

    def test_status_schema_and_fields_pass_from_the_recorded_response(self) -> None:
        result = self.verifier.verify(
            ask(
                "http",
                expected={"status": 200, "schema": {"id": "int"}, "fields": {"ok": True}},
                observation={"http": {"status": 200, "body": {"id": 1, "ok": True}}},
            )
        )

        self.assertEqual(result.status, VerificationStatus.PASS.value)
        self.assertTrue(result.factual)

    def test_a_wrong_status_fails(self) -> None:
        result = self.verifier.verify(
            ask(
                "http",
                expected={"status": 200},
                observation={"http": {"status": 503, "body": "unavailable"}},
            )
        )

        self.assertEqual(result.status, VerificationStatus.FAIL.value)

    def test_a_missing_required_field_loses_the_schema_check(self) -> None:
        """The response arrived (status right) but was wrong (field absent):
        partial credit is the honest reading, and it is not a pass."""
        result = self.verifier.verify(
            ask(
                "http",
                expected={"status": 200, "schema": {"id": "int"}},
                observation={"http": {"status": 200, "body": {"name": "nova"}}},
            )
        )

        self.assertEqual(result.status, VerificationStatus.PARTIAL.value)
        self.assertFalse(result.passed)
        self.assertLess(result.reading(), 1.0)


class DatabaseVerifierTests(unittest.TestCase):
    """DatabaseVerifier checks observed rows and state changes."""

    def setUp(self) -> None:
        self.verifier = DatabaseVerifier()

    def test_expected_rows_and_count_pass(self) -> None:
        rows = [{"id": 1, "name": "nova"}, {"id": 2, "name": "buff"}]

        result = self.verifier.verify(
            ask(
                "database",
                expected={"rows": [{"id": 2, "name": "buff"}], "count": 2},
                observation={"database": {"rows": rows}},
            )
        )

        self.assertEqual(result.status, VerificationStatus.PASS.value)

    def test_a_missing_row_fails(self) -> None:
        result = self.verifier.verify(
            ask(
                "database",
                expected={"rows": [{"id": 9}]},
                observation={"database": {"rows": [{"id": 1}]}},
            )
        )

        self.assertEqual(result.status, VerificationStatus.FAIL.value)

    def test_an_expected_field_value_is_checked(self) -> None:
        result = self.verifier.verify(
            ask(
                "database",
                expected={"values": {"name": "nova"}},
                observation={"database": {"rows": [{"name": "other"}]}},
            )
        )

        self.assertEqual(result.status, VerificationStatus.FAIL.value)


class GitVerifierTests(unittest.TestCase):
    """GitVerifier checks the repository state a run recorded."""

    def setUp(self) -> None:
        self.verifier = GitVerifier()

    def test_branch_head_and_cleanliness_pass(self) -> None:
        result = self.verifier.verify(
            ask(
                "git",
                expected={"branch": "main", "head_commit": "abc123", "clean": True},
                observation={
                    "git": {"branch": "main", "head_commit": "abc123def", "clean": True}
                },
            )
        )

        self.assertEqual(result.status, VerificationStatus.PASS.value)

    def test_a_dirty_tree_where_clean_was_expected_fails(self) -> None:
        result = self.verifier.verify(
            ask(
                "git",
                expected={"clean": True},
                observation={"git": {"branch": "main", "clean": False, "modified": ["a.py"]}},
            )
        )

        self.assertEqual(result.status, VerificationStatus.FAIL.value)

    def test_a_diff_expectation_is_checked_for_content(self) -> None:
        result = self.verifier.verify(
            ask(
                "git",
                expected={"diff_contains": "def verify"},
                observation={"git": {"diff": "--- a\n+++ b\n+def verify(self): pass\n"}},
            )
        )

        self.assertEqual(result.status, VerificationStatus.PASS.value)


class OutputVerifierTests(unittest.TestCase):
    """OutputVerifier: exact, normalised and structured readings."""

    def setUp(self) -> None:
        self.verifier = OutputVerifier()

    def test_exact_output_passes(self) -> None:
        result = self.verifier.verify(
            ask("output", expected={"exact": "SUCCESS"}, observation={"output": "SUCCESS"})
        )

        self.assertEqual(result.status, VerificationStatus.PASS.value)
        self.assertIn("output_match", result.evidence)

    def test_different_output_fails_as_a_format_error(self) -> None:
        result = self.verifier.verify(
            ask("output", expected={"exact": "SUCCESS"}, observation={"output": "FAILED"})
        )

        self.assertEqual(result.status, VerificationStatus.FAIL.value)
        self.assertEqual(result.error_category, CritiqueCategory.OUTPUT_FORMAT_ERROR.value)

    def test_normalized_output_ignores_whitespace_and_case_when_asked(self) -> None:
        result = self.verifier.verify(
            ask(
                "output",
                expected={"normalized": "hello world", "ignore_case": True},
                observation={"output": "Hello   WORLD\n"},
            )
        )

        self.assertEqual(result.status, VerificationStatus.PASS.value)

    def test_structured_fields_are_compared_one_by_one(self) -> None:
        result = self.verifier.verify(
            ask(
                "output",
                expected={"structured": {"ok": True, "count": 2}},
                observation={"output": {"ok": True, "count": 2}},
            )
        )

        partial = self.verifier.verify(
            ask(
                "output",
                expected={"structured": {"ok": True, "count": 3}},
                observation={"output": {"ok": True, "count": 2}},
            )
        )

        self.assertEqual(result.status, VerificationStatus.PASS.value)
        self.assertEqual(partial.status, VerificationStatus.PARTIAL.value)

    def test_an_expectation_with_no_output_property_is_refused(self) -> None:
        result = self.verifier.verify(
            ask("output", expected={"unknown": 1}, observation={"output": "x"})
        )

        self.assertEqual(result.status, VerificationStatus.ERROR.value)


class SchemaVerifierTests(unittest.TestCase):
    """SchemaVerifier: required fields, types and constraints."""

    def setUp(self) -> None:
        self.verifier = SchemaVerifier()

    def test_a_value_matching_the_schema_passes(self) -> None:
        result = self.verifier.verify(
            ask(
                "schema",
                expected={
                    "schema": {
                        "name": "str",
                        "count": {"type": "int", "min": 1},
                        "status": {"enum": ["ok", "bad"]},
                    }
                },
                observation={"value": {"name": "nova", "count": 2, "status": "ok"}},
            )
        )

        self.assertEqual(result.status, VerificationStatus.PASS.value)

    def test_a_wrong_type_does_not_pass(self) -> None:
        result = self.verifier.verify(
            ask(
                "schema",
                expected={"schema": {"count": "int"}},
                observation={"value": {"count": "two"}},
            )
        )

        self.assertNotEqual(result.status, VerificationStatus.PASS.value)
        self.assertFalse(result.passed)
        self.assertIn("count", result.detail)

    def test_a_constraint_violation_does_not_pass(self) -> None:
        result = self.verifier.verify(
            ask(
                "schema",
                expected={"schema": {"count": {"type": "int", "min": 5}}},
                observation={"value": {"count": 2}},
            )
        )

        self.assertNotEqual(result.status, VerificationStatus.PASS.value)
        self.assertFalse(result.passed)

    def test_an_empty_schema_is_refused(self) -> None:
        result = self.verifier.verify(
            ask("schema", expected={"schema": {}}, observation={"value": {"a": 1}})
        )

        self.assertEqual(result.status, VerificationStatus.ERROR.value)


class CustomVerifierTests(unittest.TestCase):
    """A plugin's domain check normalises into a standard result."""

    def test_a_boolean_check_becomes_a_pass_or_fail(self) -> None:
        verifier = CustomVerifier(
            "domain",
            lambda request: request.expected.get("wanted") == request.observation.get("got"),
            name="Domain check",
            task_types=("domain",),
        )

        passed = verifier.verify(
            ask("domain", expected={"wanted": "a"}, observation={"got": "a"})
        )
        failed = verifier.verify(
            ask("domain", expected={"wanted": "a"}, observation={"got": "b"})
        )

        self.assertEqual(passed.status, VerificationStatus.PASS.value)
        self.assertEqual(failed.status, VerificationStatus.FAIL.value)

    def test_a_mapping_check_is_normalised_into_a_result(self) -> None:
        verifier = CustomVerifier(
            "domain",
            lambda request: {"status": "partial", "score": 0.5, "evidence": ["half"], "detail": "one of two"},
        )

        result = verifier.verify(ask("domain", expected={"x": 1}, observation={}))

        self.assertEqual(result.status, VerificationStatus.PARTIAL.value)
        self.assertEqual(result.evidence, ("half",))
        self.assertEqual(result.verifier_id, "domain")

    def test_the_default_set_is_the_eight_required_verifiers(self) -> None:
        found = {item.verifier_id for item in default_verifiers()}

        self.assertEqual(
            found,
            {"file", "process", "test", "http", "database", "git", "output", "schema"},
        )
        self.assertTrue(all(item.deterministic for item in default_verifiers()))


# ── registry and selection ───────────────────────────────────────────────────


class VerifierRegistryTests(unittest.TestCase):
    """Registration, discovery, versioning and integrity of the verifier set."""

    def setUp(self) -> None:
        self.registry = fresh_registry()

    def test_a_fresh_registry_holds_the_eight_builtins(self) -> None:
        found = self.registry.list()

        self.assertEqual(len(found), 8)
        self.assertEqual(found[0].verifier_id, "database")
        self.assertTrue(all(item.deterministic for item in found))
        self.assertTrue(self.registry.get("file"))

    def test_the_same_version_with_different_code_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.registry.register(
                CustomVerifier("file", lambda request: True, version=FileVerifier.version)
            )

        self.assertIn("cannot be modified in place", str(caught.exception))

    def test_a_newer_version_is_kept_with_its_history(self) -> None:
        upgraded = CustomVerifier(
            "file", lambda request: True, version="phase19.2", name="File verifier v2"
        )

        metadata = self.registry.register(upgraded)

        self.assertEqual(metadata.version, "phase19.2")
        self.assertEqual(FileVerifier.version, RLVR_VERSION)
        self.assertEqual(self.registry.record("file").history, (RLVR_VERSION,))
        self.assertEqual(self.registry.metadata("file").version, "phase19.2")

    def test_a_disabled_verifier_is_unselectable_but_still_listed(self) -> None:
        self.registry.disable("file", reason="under repair")

        self.assertIsNone(self.registry.get("file"))
        self.assertTrue(self.registry.validate("file"))
        listed = self.registry.list(enabled_only=False)
        self.assertIn("file", {item.verifier_id for item in listed})
        self.assertNotIn("file", {item.verifier_id for item in self.registry.list()})

        self.registry.enable("file")
        self.assertTrue(self.registry.get("file"))

    def test_unregister_removes_the_record_entirely(self) -> None:
        self.assertTrue(self.registry.unregister("git"))

        self.assertIsNone(self.registry.record("git"))
        self.assertNotIn("git", self.registry.snapshot())

    def test_a_snapshot_detects_an_added_or_changed_verifier(self) -> None:
        snapshot = self.registry.snapshot()
        self.assertEqual(self.registry.assert_unmodified(snapshot), ())

        self.registry.register(CustomVerifier("extra", lambda request: True))
        problems = self.registry.assert_unmodified(snapshot)

        self.assertTrue(problems)
        self.assertIn("extra", problems[0])

    def test_discovery_filters_by_category_and_determinism(self) -> None:
        by_category = self.registry.discover(category="schema")
        custom = CustomVerifier("plugin-check", lambda request: True, deterministic=False)
        self.registry.register(custom)

        deterministic = self.registry.discover(deterministic=True)

        self.assertEqual([item.verifier_id for item in by_category], ["schema"])
        self.assertNotIn("plugin-check", {item.verifier_id for item in deterministic})

    def test_registering_the_same_verifier_twice_is_idempotent(self) -> None:
        verifier = CustomVerifier("extra", lambda request: True)

        first = self.registry.register(verifier)
        second = self.registry.register(verifier)

        self.assertEqual(first.verifier_id, second.verifier_id)
        self.assertEqual(len(self.registry.list()), 9)


class VerifierSelectionTests(unittest.TestCase):
    """Selection prefers the expectation's own category, deterministic first."""

    def setUp(self) -> None:
        self.registry = fresh_registry()
        self.selector = VerifierSelector(self.registry)

    def test_an_output_expectation_selects_the_output_verifier(self) -> None:
        decision = self.selector.select(expected={"exact": "SUCCESS"}, observation={"output": "SUCCESS"})

        self.assertTrue(decision.ok)
        self.assertEqual(decision.verifier_id, "output")
        self.assertTrue(decision.deterministic)

    def test_the_expectation_outranks_the_action_a_file_write_produced(self) -> None:
        """A build task writes files, but its expectation is about output."""
        decision = self.selector.select(
            action={"name": "write_file", "path": "out.txt"},
            expected={"exact": "SUCCESS"},
            observation={"output": "SUCCESS", "file": {"exists": True}},
        )

        self.assertEqual(decision.verifier_id, "output")
        self.assertEqual(decision.category, "output")

    def test_a_file_expectation_selects_the_file_verifier(self) -> None:
        decision = self.selector.select(
            expected={"path": "out.txt", "sha256": "abc"}
        )

        self.assertEqual(decision.verifier_id, "file")

    def test_a_named_verifier_is_honoured_when_it_is_usable(self) -> None:
        decision = self.selector.select(request=ask("test", expected={"result": "pass"}))

        self.assertTrue(decision.ok)
        self.assertEqual(decision.verifier_id, "test")

    def test_a_disabled_named_verifier_is_refused_with_its_reason(self) -> None:
        self.registry.disable("test", reason="under repair")

        decision = self.selector.select(request=ask("test", expected={"result": "pass"}))

        self.assertFalse(decision.ok)
        self.assertIn("under repair", decision.reason)

    def test_nothing_matching_is_refused_without_invoking_an_llm(self) -> None:
        decision = self.selector.select(expected={"nonsense": True})

        self.assertFalse(decision.ok)
        self.assertFalse(decision.fallback_used)
        self.assertIn("no deterministic verifier matches", decision.reason)

    def test_a_switched_off_policy_selects_nothing(self) -> None:
        registry = fresh_registry(enabled=False)

        decision = registry.select(expected={"exact": "x"})

        self.assertFalse(decision.ok)
        self.assertIn("switched off", decision.reason)

    def test_deterministic_only_skips_a_non_deterministic_verifier(self) -> None:
        registry = fresh_registry(deterministic_only=True, categories=("custom",))
        registry.register(
            CustomVerifier("opinion", lambda request: True, deterministic=False)
        )

        decision = registry.select(expected={"x": 1})

        self.assertFalse(decision.ok)

    def test_max_verifiers_caps_the_candidate_list(self) -> None:
        registry = fresh_registry(max_verifiers=1)

        decision = registry.select(expected={"exact": "x"})

        self.assertEqual(len(decision.candidates), 1)

    def test_custom_categories_can_be_switched_off(self) -> None:
        registry = fresh_registry(allow_custom=False)
        registry.register(CustomVerifier("plugin-check", lambda request: True))

        decision = registry.select(expected={"x": 1})

        self.assertFalse(decision.ok)


# ── verifiable rewards ──────────────────────────────────────────────────────


class VerifiableRewardTests(unittest.TestCase):
    """A reward is derived from verified evidence, through a versioned policy."""

    def setUp(self) -> None:
        self.registry = fresh_registry()
        self.config = VerifiableRewardConfig()
        self.provider = VerifiableRewardProvider(self.config, registry=self.registry)

    def reward(self, results: Sequence[VerificationResult], **kwargs: Any) -> Any:
        return self.provider.reward_for(results, subject_id="t1", **kwargs)

    def test_a_verified_pass_pays_the_configured_value_with_evidence(self) -> None:
        result = self.reward([verdict()])

        self.assertEqual(result.total_reward, 1.0)
        self.assertEqual(result.reward_source, "verifier")
        self.assertEqual(result.reward_version, self.config.version)
        self.assertIn("verifier:output@phase19.1", result.evidence)
        self.assertIn("output_match", result.evidence)
        self.assertIn(self.config.version, result.explanation_summary + str(result.weights))

    def test_a_failed_verification_is_negative(self) -> None:
        result = self.reward(
            [verdict(status=VerificationStatus.FAIL.value, evidence=("output_match",))]
        )

        self.assertEqual(result.total_reward, -1.0)
        self.assertIn(COMPONENT_VERIFICATION, result.component_rewards)

    def test_partial_credit_uses_the_configured_partial_value(self) -> None:
        result = self.reward(
            [
                verdict(
                    status=VerificationStatus.PARTIAL.value,
                    score=1.0,
                    step_id="step:0",
                    scope="step",
                    evidence=("tests_passed",),
                )
            ]
        )

        self.assertEqual(result.total_reward, self.config.rewards["partial"])
        self.assertIn(COMPONENT_PARTIAL, result.component_rewards)

    def test_a_partially_verified_task_earns_no_positive_total(self) -> None:
        """Only a VERIFIED task may go positive: half a task is not success."""
        result = self.reward(
            [
                verdict(
                    status=VerificationStatus.PARTIAL.value,
                    score=1.0,
                    evidence=("tests_passed",),
                )
            ]
        )

        self.assertEqual(result.total_reward, 0.0)

    def test_inconclusive_verification_is_neutral(self) -> None:
        result = self.reward([verdict(status=VerificationStatus.INCONCLUSIVE.value, evidence=())])

        self.assertEqual(result.total_reward, 0.0)

    def test_a_verifier_error_is_a_small_penalty(self) -> None:
        result = self.reward(
            [verdict(status=VerificationStatus.ERROR.value, evidence=("verifier_error",))]
        )

        self.assertEqual(result.total_reward, self.config.rewards["error"])

    def test_the_task_verdict_governs_and_steps_are_bounded_partial_credit(self) -> None:
        """Four steps passed, the final check failed: a failure with progress."""
        results = [
            verdict(step_id=f"step:{index}", scope="step", evidence=("output_match",))
            for index in range(4)
        ] + [
            verdict(
                status=VerificationStatus.FAIL.value,
                evidence=("output_match",),
                detail="the final state was wrong",
            )
        ]

        result = self.reward(results)

        self.assertEqual(result.total_reward, -0.25)
        self.assertEqual(result.component_rewards[COMPONENT_VERIFICATION], -1.0)
        self.assertEqual(
            result.component_rewards[COMPONENT_PARTIAL], self.config.max_partial_credit
        )
        self.assertIn("governs", result.explanation_summary)

    def test_partial_credit_cannot_outvote_a_failed_task(self) -> None:
        results = [
            verdict(step_id="step:0", scope="step"),
            verdict(step_id="step:1", scope="step"),
            verdict(status=VerificationStatus.FAIL.value),
        ]

        result = self.reward(results)

        self.assertLess(result.total_reward, 0.0)
        self.assertLessEqual(result.total_reward, self.config.max_partial_credit * -0.0 + 0.0)

    def test_a_failed_task_never_goes_positive_from_a_correction_bonus(self) -> None:
        result = self.reward(
            [verdict(status=VerificationStatus.FAIL.value)],
            correction_verified=True,
        )

        self.assertLessEqual(result.total_reward, 0.0)

    def test_a_verified_correction_is_a_positive_component(self) -> None:
        result = self.reward([verdict()], correction_verified=True)

        self.assertIn(COMPONENT_CORRECTION, result.component_rewards)
        self.assertEqual(result.total_reward, 1.0)  # clipped at the configured ceiling

    def test_a_safety_failure_keeps_its_own_penalty(self) -> None:
        result = self.reward(
            [
                verdict(
                    status=VerificationStatus.FAIL.value,
                    evidence=("unsafe_command",),
                    detail="an unapproved destructive command ran",
                )
            ]
        )

        self.assertIn(PENALTY_SAFETY, result.penalties)
        self.assertEqual(result.penalties[PENALTY_SAFETY], self.config.safety_penalty)
        self.assertGreaterEqual(result.total_reward, self.config.safety_floor)

    def test_steps_without_a_task_verdict_sum_and_are_clipped(self) -> None:
        results = [
            verdict(step_id="step:0", scope="step"),
            verdict(step_id="step:1", scope="step"),
        ]

        result = self.reward(results)

        self.assertEqual(result.total_reward, self.config.clip_high)

    def test_evidence_marks_a_non_deterministic_verifier(self) -> None:
        self.registry.register(
            CustomVerifier("opinion", lambda request: True, deterministic=False)
        )

        evidence = self.provider.evidence_for([verdict("opinion")])

        self.assertFalse(evidence[0].deterministic)

    def test_no_verification_results_is_refused_by_validate(self) -> None:
        request = RewardRequest(metadata={"verifications": [], "task_id": "t1"})

        problems = self.provider.validate(request)

        self.assertTrue(problems)
        self.assertIn("no verification results", problems[0])

    def test_evidence_less_verification_is_refused_when_evidence_is_required(self) -> None:
        request = RewardRequest(
            metadata={"verifications": [verdict(evidence=())], "task_id": "t1"}
        )

        problems = self.provider.validate(request)

        self.assertTrue(any("cites no evidence" in item for item in problems))

    def test_an_unregistered_verifier_is_refused(self) -> None:
        request = RewardRequest(
            metadata={"verifications": [verdict("ghost")], "task_id": "t1"}
        )

        problems = self.provider.validate(request)

        self.assertTrue(any("not registered" in item for item in problems))


class CritiqueRewardTests(unittest.TestCase):
    """A critique alone carries a reward signal, with success and safety apart."""

    def setUp(self) -> None:
        self.critique = CritiqueResult(
            trajectory_id="traj-1",
            task_id="task-1",
            category=CritiqueCategory.OUTPUT_FORMAT_ERROR.value,
            severity=CritiqueSeverity.HIGH.value,
            failed_component="output",
            evidence=("output_match",),
            observed_behavior={"output": "FAILED"},
            expected_behavior={"exact": "SUCCESS"},
            source=CritiqueSource.VERIFIER.value,
            verification_id="v1",
            detail="the output did not match",
        )

    def test_a_verified_failure_is_negative(self) -> None:
        reward = critique_reward(self.critique)

        self.assertLess(reward.total_reward, 0.0)

    def test_an_efficiency_issue_is_a_small_penalty(self) -> None:
        slow = CritiqueResult(
            **{
                **self.critique.to_dict(),
                "category": CritiqueCategory.EFFICIENCY_ISSUE.value,
                "severity": CritiqueSeverity.LOW.value,
            }
        )

        reward = critique_reward(slow)

        self.assertIn(PENALTY_EFFICIENCY, reward.penalties)
        self.assertGreater(reward.total_reward, -1.0)

    def test_a_safety_error_is_a_strong_negative_signal(self) -> None:
        unsafe = CritiqueResult(
            **{
                **self.critique.to_dict(),
                "category": CritiqueCategory.SAFETY_ERROR.value,
                "severity": CritiqueSeverity.CRITICAL.value,
            }
        )

        reward = critique_reward(unsafe)

        self.assertIn(PENALTY_SAFETY, reward.penalties)
        self.assertLessEqual(reward.total_reward, -1.0)

    def test_a_verified_correction_is_a_positive_signal(self) -> None:
        corrected = CritiqueResult(
            **{
                **self.critique.to_dict(),
                "correction": {"output": "SUCCESS"},
                "severity": CritiqueSeverity.LOW.value,
            }
        )

        reward = critique_reward(corrected, subject_id="traj-1")

        self.assertGreater(reward.total_reward, 0.0)
        self.assertIn(COMPONENT_CORRECTION, reward.component_rewards)


class RewardValidationTests(unittest.TestCase):
    """The validator is what stops an inconsistent reward from teaching."""

    def setUp(self) -> None:
        self.registry = fresh_registry()
        self.config = VerifiableRewardConfig()
        self.provider = VerifiableRewardProvider(self.config, registry=self.registry)
        self.validator = VerifiableRewardValidator(self.config, registry=self.registry)

    def check(self, results: Sequence[VerificationResult], **kwargs: Any) -> Any:
        reward = self.provider.reward_for(results, subject_id="traj-1")
        return self.validator.check(
            reward,
            verifications=results,
            config_version=self.config.version,
            **kwargs,
        )

    def test_a_clean_verified_reward_is_valid(self) -> None:
        check = self.check([verdict()])

        self.assertEqual(check.status, "valid")
        self.assertEqual(check.findings, ())
        self.assertEqual(check.total_reward, 1.0)

    def test_a_reward_with_no_verification_is_invalid(self) -> None:
        reward = self.provider.reward_for([], subject_id="traj-1")

        check = self.validator.check(reward, config_version=self.config.version)

        self.assertEqual(check.status, "invalid")
        self.assertIn("evidence_missing", [item.code for item in check.findings])

    def test_an_unknown_verifier_is_invalid(self) -> None:
        check = self.check([verdict("ghost")])

        self.assertEqual(check.status, "invalid")
        self.assertIn("verifier_missing", [item.code for item in check.findings])

    def test_a_disabled_verifier_cannot_support_a_reward(self) -> None:
        self.registry.disable("output", reason="under repair")

        check = self.check([verdict()])

        self.assertEqual(check.status, "invalid")
        self.assertIn("verifier_disabled", [item.code for item in check.findings])

    def test_an_unknown_verifier_version_is_a_warning(self) -> None:
        self.registry.register(
            CustomVerifier("output", lambda request: True, version="phase19.9")
        )

        check = self.check([verdict()])

        self.assertEqual(check.status, "needs_review")
        self.assertIn("verifier_version_unknown", [item.code for item in check.findings])

    def test_an_expectation_edited_after_freezing_is_an_error(self) -> None:
        results = [verdict()]

        check = self.check(results, expected_digests={results[0].verification_id: "other"})

        self.assertEqual(check.status, "invalid")
        self.assertIn("expected_tampered", [item.code for item in check.findings])

    def test_a_reward_built_by_another_configuration_version_is_invalid(self) -> None:
        results = [verdict()]
        reward = self.provider.reward_for(results, subject_id="traj-1")

        check = self.validator.check(
            reward, verifications=results, config_version="verifiable.other"
        )

        self.assertEqual(check.status, "invalid")
        self.assertIn("config_version_mismatch", [item.code for item in check.findings])

    def test_a_reward_that_fights_its_task_verdict_is_invalid(self) -> None:
        results = [verdict()]
        contradictory = RewardResult(
            reward_id="vrw-traj-1",
            trajectory_id="traj-1",
            reward_source="verifier",
            total_reward=-1.0,
            reward_version=self.config.version,
        )

        check = self.validator.check(
            contradictory, verifications=results, config_version=self.config.version
        )

        self.assertEqual(check.status, "invalid")
        self.assertIn("reward_inconsistent", [item.code for item in check.findings])

    def test_a_swapped_verifier_under_a_run_snapshot_is_invalid(self) -> None:
        snapshot = self.registry.snapshot()
        self.registry.register(CustomVerifier("extra", lambda request: True))

        check = self.check([verdict()], snapshot=snapshot)

        self.assertEqual(check.status, "invalid")
        self.assertIn("verifier_tampered", [item.code for item in check.findings])

    def test_the_same_verifier_failing_two_subjects_is_not_a_duplicate(self) -> None:
        results = [
            verdict(
                status=VerificationStatus.FAIL.value,
                step_id="step:0",
                scope="step",
                evidence=("output_match",),
            ),
            verdict(status=VerificationStatus.FAIL.value, evidence=("output_match",)),
        ]

        check = self.check(results)

        self.assertEqual(check.findings, ())

    def test_the_same_verifier_failing_one_subject_twice_is_flagged(self) -> None:
        twice = [
            verdict(status=VerificationStatus.FAIL.value),
            verdict(status=VerificationStatus.FAIL.value),
        ]

        check = self.check(twice)

        self.assertEqual(check.status, "suspicious")
        self.assertIn("duplicate_verification", [item.code for item in check.findings])


# ── critique engine ─────────────────────────────────────────────────────────


class CritiqueEngineTests(unittest.TestCase):
    """Structured, evidence-based critiques — and never a reasoning trace."""

    def setUp(self) -> None:
        self.engine = CritiqueEngine()

    def test_a_failed_verification_becomes_a_verifier_sourced_critique(self) -> None:
        result = verdict(
            status=VerificationStatus.FAIL.value,
            step_id="step:2",
            scope="step",
            error_category=CritiqueCategory.OUTPUT_FORMAT_ERROR.value,
            evidence=("output_match",),
            detail="the output did not match",
        )

        found = self.engine.critique([result])

        self.assertEqual(len(found), 1)
        critique = found[0]
        self.assertEqual(critique.category, CritiqueCategory.OUTPUT_FORMAT_ERROR.value)
        self.assertEqual(critique.severity, CritiqueSeverity.HIGH.value)
        self.assertEqual(critique.source, CritiqueSource.VERIFIER.value)
        self.assertEqual(critique.failed_component, "output")
        self.assertEqual(critique.verification_id, result.verification_id)
        self.assertEqual(critique.step_id, "step:2")
        self.assertIn("output_match", critique.evidence)
        self.assertEqual(critique.observed_behavior, result.observed)
        self.assertEqual(critique.expected_behavior, result.expected)

    def test_a_correction_suggestion_is_structured_and_reasoning_free(self) -> None:
        found = self.engine.critique(
            [
                verdict(
                    status=VerificationStatus.FAIL.value,
                    error_category=CritiqueCategory.OUTPUT_FORMAT_ERROR.value,
                    evidence=("output_match",),
                )
            ]
        )

        correction = found[0].correction
        self.assertTrue(correction)
        self.assertIsInstance(correction, Mapping)
        text = json.dumps(found[0].to_dict(), default=str).lower()
        for forbidden in ("chain_of_thought", "reasoning", "thoughts", "rationale"):
            self.assertNotIn(forbidden, text)

    def test_a_passing_verification_is_not_critiqued(self) -> None:
        self.assertEqual(self.engine.critique([verdict()]), ())

    def test_an_inconclusive_verification_is_not_critiqued(self) -> None:
        self.assertEqual(
            self.engine.critique(
                [verdict(status=VerificationStatus.INCONCLUSIVE.value, evidence=())]
            ),
            (),
        )

    def test_a_safety_failure_is_critical_regardless_of_the_status_word(self) -> None:
        found = self.engine.critique(
            [
                verdict(
                    status=VerificationStatus.FAIL.value,
                    error_category=CritiqueCategory.SAFETY_ERROR.value,
                    evidence=("unsafe_command",),
                    detail="an unsafe command ran without approval",
                )
            ]
        )

        self.assertEqual(found[0].severity, CritiqueSeverity.CRITICAL.value)
        self.assertEqual(found[0].category, CritiqueCategory.SAFETY_ERROR.value)

    def test_a_partial_verification_is_medium_severity(self) -> None:
        found = self.engine.critique(
            [
                verdict(
                    status=VerificationStatus.PARTIAL.value,
                    score=0.5,
                    evidence=("tests_passed",),
                )
            ]
        )

        self.assertEqual(found[0].severity, CritiqueSeverity.MEDIUM.value)

    def test_the_engine_can_be_switched_off_or_narrowed_by_configuration(self) -> None:
        off = CritiqueEngine(CritiqueConfig(enabled=False))
        narrow = CritiqueEngine(CritiqueConfig(categories=(CritiqueCategory.SAFETY_ERROR.value,)))

        failed = verdict(status=VerificationStatus.FAIL.value, evidence=("output_match",))
        self.assertEqual(off.critique([failed]), ())
        self.assertEqual(narrow.critique([failed]), ())

    def test_a_trajectory_yields_critiques_from_rules_and_evaluation(self) -> None:
        found = self.engine.critique_trajectory(
            trajectory("traj-1"), evaluation=evaluation_for()
        )

        categories = {item.category for item in found}
        self.assertIn(CritiqueCategory.SAFETY_ERROR.value, categories)
        self.assertIn(CritiqueCategory.TOOL_SELECTION_ERROR.value, categories)
        self.assertIn(CritiqueCategory.VERIFICATION_ERROR.value, categories)
        self.assertTrue(all(item.source for item in found))
        self.assertTrue(all(item.evidence for item in found))

    def test_already_critiqued_verifications_are_skipped_by_id(self) -> None:
        failed = verdict(
            status=VerificationStatus.FAIL.value, evidence=("output_match",)
        )

        with_id = self.engine.critique_trajectory(
            trajectory("traj-1"),
            verifications=[failed],
            ignore_verification_ids=[failed.verification_id],
        )

        self.assertNotIn(
            failed.verification_id, {item.verification_id for item in with_id}
        )

    def test_a_successful_trajectory_is_not_second_guessed(self) -> None:
        found = self.engine.critique_trajectory(
            trajectory("traj-1", success=True, status="completed")
        )

        self.assertEqual(found, ())

    def test_a_counterfactual_needs_a_verified_alternative(self) -> None:
        comparison = self.engine.counterfactual(
            {"name": "run", "args": {"cwd": "/tmp"}},
            {"name": "run", "args": {"cwd": "/work"}},
            verification=verdict(),
            task_id="task-1",
        )
        unverified = self.engine.counterfactual(
            {"name": "run"},
            {"name": "run", "args": {"cwd": "/work"}},
        )

        self.assertIsNotNone(comparison)
        self.assertEqual(comparison.difference["changed_keys"], ["args"])
        self.assertEqual(comparison.evidence, ("output_match",))
        self.assertIsNone(unverified)

    def test_a_counterfactual_can_be_switched_off(self) -> None:
        engine = CritiqueEngine(CritiqueConfig(allow_counterfactual=False))

        self.assertIsNone(
            engine.counterfactual({"a": 1}, {"a": 2}, verification=verdict())
        )

    def test_summarise_counts_categories_severities_and_corrections(self) -> None:
        found = self.engine.critique(
            [
                verdict(
                    status=VerificationStatus.FAIL.value,
                    error_category=CritiqueCategory.OUTPUT_FORMAT_ERROR.value,
                    evidence=("output_match",),
                ),
                verdict(
                    "process",
                    status=VerificationStatus.FAIL.value,
                    error_category=CritiqueCategory.SAFETY_ERROR.value,
                    evidence=("unsafe_command",),
                    detail="unsafe",
                ),
            ]
        )

        summary = CritiqueEngine.summarise(found)

        self.assertEqual(summary.total, 2)
        self.assertEqual(summary.by_severity[CritiqueSeverity.CRITICAL.value], 1)
        self.assertEqual(summary.by_source[CritiqueSource.VERIFIER.value], 2)
        self.assertEqual(summary.highest, CritiqueSeverity.CRITICAL.value)
        self.assertEqual(summary.corrections, 1)

# ── corrected examples and datasets ─────────────────────────────────────────


def a_critique(**overrides: Any) -> CritiqueResult:
    """One failed-output critique, the row corrections are built from."""
    fields: dict[str, Any] = {
        "trajectory_id": "traj-1",
        "task_id": "task-1",
        "category": CritiqueCategory.OUTPUT_FORMAT_ERROR.value,
        "severity": CritiqueSeverity.HIGH.value,
        "failed_component": "output",
        "evidence": ("output_match",),
        "observed_behavior": {"output": "FAILED"},
        "expected_behavior": {"exact": "SUCCESS"},
        "correction": {"produce": {"exact": "SUCCESS"}},
        "source": CritiqueSource.VERIFIER.value,
        "verification_id": "v1",
        "detail": "the output did not match",
    }
    fields.update(overrides)
    return CritiqueResult(**fields)


def a_correction(
    critique: CritiqueResult | None = None,
    *,
    verification: VerificationResult | None = None,
    corrected: Mapping[str, Any] | None = None,
) -> CorrectedExample:
    """One correction of ``a_critique``, verified or not as the caller says."""
    return CorrectedExampleBuilder().build(
        original_input={"task": "produce SUCCESS"},
        original_output={"output": "FAILED"},
        critique=critique if critique is not None else a_critique(),
        corrected_output=dict(corrected if corrected is not None else {"output": "SUCCESS"}),
        verification=verification,
        source_trajectory_id="traj-1",
    )


class CorrectedExampleTests(unittest.TestCase):
    """Only corrections with sufficient evidence may be accepted."""

    def setUp(self) -> None:
        self.critique = a_critique()

    def test_a_verified_correction_is_accepted(self) -> None:
        row = a_correction(self.critique, verification=verdict())

        self.assertEqual(row.quality_status, CorrectionStatus.ACCEPTED.value)
        self.assertTrue(row.accepted)
        self.assertIsNotNone(row.verification_result)
        self.assertIn("output_match", row.evidence)
        self.assertEqual(row.source_trajectory_id, "traj-1")
        self.assertEqual(row.critique.critique_id, self.critique.critique_id)

    def test_a_correction_whose_verification_failed_is_rejected(self) -> None:
        row = a_correction(
            self.critique, verification=verdict(status=VerificationStatus.FAIL.value)
        )

        self.assertEqual(row.quality_status, CorrectionStatus.REJECTED.value)
        self.assertIn("correction_verification_failed", row.reasons)

    def test_an_unverifiable_correction_is_held_for_review(self) -> None:
        row = a_correction(self.critique)

        self.assertEqual(row.quality_status, CorrectionStatus.NEEDS_REVIEW.value)
        self.assertTrue(row.needs_review)
        self.assertIn("correction_not_verified", row.reasons)

    def test_hidden_reasoning_in_a_correction_is_rejected(self) -> None:
        row = a_correction(
            self.critique, corrected={"chain_of_thought": "I thought about it"}
        )

        self.assertEqual(row.quality_status, CorrectionStatus.REJECTED.value)
        self.assertIn("hidden_reasoning_present", row.reasons)

    def test_an_empty_correction_is_rejected(self) -> None:
        row = a_correction(self.critique, corrected={})

        self.assertEqual(row.quality_status, CorrectionStatus.REJECTED.value)
        self.assertIn("no_corrected_output", row.reasons)


class CritiqueDatasetTests(unittest.TestCase):
    """The dataset builder screens, splits and projects critique rows."""

    def setUp(self) -> None:
        self.critique = a_critique()
        self.correction = a_correction(self.critique, verification=verdict())
        self.builder = CritiqueDatasetBuilder()

    def build(self, **overrides: Any) -> Any:
        critiques = overrides.pop("critiques", [self.critique])
        corrections = overrides.pop("corrections", [self.correction])
        return self.builder.build(
            "critique-demo", critiques=critiques, corrections=corrections, **overrides
        )

    def test_a_dataset_with_a_verified_correction_is_valid(self) -> None:
        dataset = self.build()

        self.assertEqual(dataset.name, "critique-demo")
        self.assertEqual(dataset.dataset_version_id, "critique-demo@1.0.0")
        self.assertEqual(len(dataset.examples), 1)
        self.assertEqual(dataset.statistics.accepted, 1)
        self.assertEqual(self.builder.validate(dataset), ())
        self.assertTrue(dataset.splits)

    def test_a_critique_without_a_verified_correction_is_held(self) -> None:
        dataset = self.build(corrections=[a_correction(self.critique)])

        self.assertEqual(dataset.statistics.accepted, 0)
        self.assertEqual(dataset.statistics.needs_review, 1)
        self.assertTrue(self.builder.validate(dataset))
        self.assertEqual(dataset.accepted_examples(), ())

    def test_rejected_examples_are_screened_out_by_the_rules(self) -> None:
        rejected = a_correction(
            self.critique, verification=verdict(status=VerificationStatus.FAIL.value)
        )

        dataset = self.build(corrections=[rejected])

        self.assertEqual(len(dataset.examples), 0)
        self.assertEqual(dataset.statistics.skipped.get("critique_rejected"), 1)

    def test_a_severity_floor_screens_low_severity_rows(self) -> None:
        low = a_critique(severity=CritiqueSeverity.LOW.value)

        dataset = self.build(
            critiques=[low],
            rules=CritiqueDatasetRules(min_severity=CritiqueSeverity.CRITICAL.value),
        )

        # Two rows named the low-severity critique: the critique itself and the
        # correction attached to it, and both are screened for the same reason.
        self.assertEqual(len(dataset.examples), 0)
        self.assertEqual(
            dataset.statistics.skipped.get("critique_severity_below_floor"), 2
        )

    def test_the_same_inputs_produce_the_same_fingerprint(self) -> None:
        first = self.build()
        second = self.build()

        self.assertEqual(first.fingerprint(), second.fingerprint())
        self.assertEqual(
            critique_dataset_fingerprint(first), critique_dataset_fingerprint(second)
        )

    def test_an_existing_version_is_never_reused(self) -> None:
        dataset = self.build(existing_versions=["1.0.0"])

        self.assertEqual(dataset.dataset_version_id, "critique-demo@1.0.1")

    def test_the_dataset_projects_into_phase_16_sft_examples(self) -> None:
        dataset = self.build()

        examples = self.builder.as_sft_examples(dataset, accepted_only=True)

        self.assertEqual(len(examples), 1)
        self.assertEqual(examples[0].target, {"output": "SUCCESS"})
        self.assertEqual(examples[0].quality_status, CorrectionStatus.ACCEPTED.value)
        self.assertEqual(examples[0].source_trajectory_id, "traj-1")

    def test_the_dataset_projects_into_phase_17_preference_pairs(self) -> None:
        dataset = self.build()

        pairs = self.builder.to_preference_pairs(dataset)

        self.assertEqual(len(pairs), 1)
        pair = pairs[0]
        self.assertEqual(pair.chosen, {"output": "SUCCESS"})
        self.assertEqual(pair.rejected, {"output": "FAILED"})
        self.assertEqual(pair.preference_source, "verified_outcome")
        self.assertEqual(pair.chosen_outcome, {"verified": True})
        self.assertTrue(pair.strength.evidence)
        self.assertEqual(pair.strength.evidence[0].kind, "verified_success")

    def test_an_unverified_correction_never_becomes_a_preference_pair(self) -> None:
        dataset = self.build(corrections=[a_correction(self.critique)])

        self.assertEqual(self.builder.to_preference_pairs(dataset), ())

# ── storage ─────────────────────────────────────────────────────────────────


class RLVRStorageTests(unittest.TestCase):
    """Critiques, corrections and dataset versions persist as JSONL."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repos = build_rlvr_repositories(self._tmp.name)

    def test_a_critique_round_trips_with_its_evidence(self) -> None:
        critique = a_critique()

        saved = self.repos.critiques.save(critique)
        loaded = self.repos.critiques.get(critique.critique_id)

        self.assertEqual(saved.critique_id, critique.critique_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.category, critique.category)
        self.assertEqual(loaded.evidence, critique.evidence)
        self.assertEqual(
            self.repos.critiques.counts().get(critique.category), 1
        )

    def test_critiques_can_be_filtered_by_category_and_severity(self) -> None:
        self.repos.critiques.save(a_critique())
        self.repos.critiques.save(
            a_critique(category=CritiqueCategory.SAFETY_ERROR.value, severity=CritiqueSeverity.CRITICAL.value)
        )

        found = self.repos.critiques.list(category=CritiqueCategory.SAFETY_ERROR.value)

        self.assertEqual(len(list(found)), 1)

    def test_pending_corrections_are_the_ones_awaiting_review(self) -> None:
        held = a_correction(a_critique())
        accepted = a_correction(a_critique(), verification=verdict())
        self.repos.corrections.save(held)
        self.repos.corrections.save(accepted)

        pending = self.repos.corrections.pending()

        self.assertEqual([row.example_id for row in pending], [held.example_id])
        counts = self.repos.corrections.counts()
        self.assertEqual(counts.get(CorrectionStatus.NEEDS_REVIEW.value), 1)
        self.assertEqual(counts.get(CorrectionStatus.ACCEPTED.value), 1)

    def test_a_dataset_version_round_trips_and_is_immutable(self) -> None:
        builder = CritiqueDatasetBuilder()
        critique = a_critique()
        dataset = builder.build(
            "critique-demo",
            critiques=[critique],
            corrections=[a_correction(critique, verification=verdict())],
        )

        saved = self.repos.datasets.save(dataset)
        loaded = self.repos.datasets.get(dataset.dataset_version_id)

        self.assertEqual(saved.dataset_version_id, "critique-demo@1.0.0")
        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded.examples), 1)
        self.assertEqual(self.repos.datasets.latest("critique-demo").dataset_version_id, "critique-demo@1.0.0")

        changed = builder.build(
            "critique-demo",
            critiques=[a_critique(severity=CritiqueSeverity.CRITICAL.value)],
            corrections=[],
            version=dataset.version,
        )
        with self.assertRaises(ValueError) as caught:
            self.repos.datasets.save(changed)

        self.assertIn("already exists", str(caught.exception))


# ── security ────────────────────────────────────────────────────────────────


class FakePermissions:
    """A Phase 8-shaped permission layer a test can steer."""

    def __init__(self, *, allow: bool = True, requires_confirmation: bool = False) -> None:
        self.allow = allow
        self.requires_confirmation = requires_confirmation
        self.seen: list[str] = []

    def check(self, tool: str, *, action: str = "") -> Any:
        self.seen.append(tool)

        class _Decision:
            allow = self.allow
            requires_confirmation = self.requires_confirmation if self.allow else False
            risk = "low" if self.allow else "high"
            reason = "test decision"

        return _Decision()


class SecurityTests(unittest.TestCase):
    """A policy may not edit the thing that grades it, nor move the goalposts."""

    def setUp(self) -> None:
        self.guard = RewardTamperGuard()
        self.policy = VerificationSecurityPolicy()
        self.registry = fresh_registry()
        self.config = VerifiableRewardConfig()

    def test_writing_to_a_verifier_module_is_refused(self) -> None:
        verdict_ = self.guard.assess(
            {"action": "write", "path": "src/novacontrol/rlvr/verifiers.py"}
        )

        self.assertFalse(verdict_.allowed)
        self.assertEqual(verdict_.code, "protected_target")
        self.assertEqual(verdict_.risk_level, "critical")

    def test_a_verification_control_tool_is_refused_by_name(self) -> None:
        verdict_ = self.guard.assess({}, tool="disable_verifier")

        self.assertFalse(verdict_.allowed)
        self.assertEqual(verdict_.code, "verification_control")

    def test_editing_expected_results_is_refused(self) -> None:
        verdict_ = self.guard.assess(
            {"action": "modify", "target": "benchmark expected outputs"}
        )

        self.assertFalse(verdict_.allowed)
        self.assertEqual(verdict_.code, "protected_target")

    def test_an_ordinary_action_is_allowed(self) -> None:
        verdict_ = self.guard.assess({"action": "read", "path": "README.md"})

        self.assertTrue(verdict_.allowed)
        self.assertFalse(verdict_.code)

    def test_the_permission_layer_is_consulted_for_everything_else(self) -> None:
        permissions = FakePermissions(requires_confirmation=True)
        policy = VerificationSecurityPolicy(permissions=permissions)

        verdict_ = policy.authorize({"action": "run", "target": "build"}, tool="shell.run")

        self.assertFalse(verdict_.allowed)
        self.assertTrue(verdict_.requires_confirmation)
        self.assertIn("shell.run", permissions.seen)

    def test_a_refusal_by_the_guard_is_final(self) -> None:
        permissions = FakePermissions(allow=True)
        policy = VerificationSecurityPolicy(permissions=permissions)

        verdict_ = policy.authorize({}, tool="set_reward_weights")

        self.assertFalse(verdict_.allowed)
        self.assertEqual(permissions.seen, [])

    def test_a_changed_expectation_is_detected_by_its_snapshot(self) -> None:
        snapshot = self.policy.snapshot_expected({"exact": "SUCCESS"})

        intact = self.policy.expected_intact(snapshot, {"exact": "SUCCESS"})
        changed = self.policy.expected_intact(snapshot, {"exact": "FAILED"})

        self.assertEqual(intact, ())
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0].code, "expected_result_changed")

    def test_a_changed_reward_configuration_is_detected(self) -> None:
        pinned = self.policy.pin_reward_config(self.config)

        intact = self.policy.config_intact(pinned, self.config)
        changed = self.policy.config_intact(
            pinned, VerifiableRewardConfig(rewards={"pass": 5.0})
        )

        self.assertEqual(intact, ())
        self.assertEqual(changed[0].code, "reward_config_changed")

    def test_a_run_protection_detects_a_swapped_verifier_set(self) -> None:
        protection = self.policy.protect_run(
            "run-1", registry=self.registry, config=self.config
        )
        clean = self.policy.check_run(
            protection, registry=self.registry, config=self.config
        )
        self.registry.register(CustomVerifier("extra", lambda request: True))
        tampered = self.policy.check_run(
            protection, registry=self.registry, config=self.config
        )

        self.assertEqual(clean, ())
        self.assertTrue(tampered)
        self.assertEqual(tampered[0].code, "verifiers_changed")


# ── evaluation ──────────────────────────────────────────────────────────────


class RLVREvaluationTests(unittest.TestCase):
    """Evaluation reads recorded labels; it never guesses a ground truth."""

    def setUp(self) -> None:
        self.evaluator = RLVREvaluator()

    def test_verification_accuracy_counts_false_positives_and_negatives(self) -> None:
        results = [
            verdict("output", task_id="t1"),  # labelled pass, correct
            verdict("output", task_id="t2"),  # labelled fail, false positive
            verdict("output", status=VerificationStatus.FAIL.value, task_id="t3"),  # labelled pass, false negative
            verdict("output", status=VerificationStatus.FAIL.value, task_id="t4"),  # labelled fail, correct
        ]

        found = self.evaluator.evaluate(
            results=results,
            ground_truth={"t1": "pass", "t2": "fail", "t3": "pass", "t4": "fail"},
            run_id="run-1",
            dataset_version="critique-demo@1.0.0",
        )

        self.assertEqual(found.verification_accuracy, 0.5)
        self.assertEqual(found.false_positive_verification, 1)
        self.assertEqual(found.false_negative_verification, 1)
        self.assertEqual(found.run_id, "run-1")
        self.assertEqual(found.dataset_version, "critique-demo@1.0.0")
        self.assertTrue(found.evidence)

    def test_task_success_and_partial_success_come_from_summaries(self) -> None:
        summaries = [
            VerificationSummary.of(
                [
                    verdict("output", task_id="t1", step_id="step:0", scope="step"),
                    verdict("output", task_id="t1"),
                ]
            ),
            VerificationSummary.of(
                [
                    verdict("output", task_id="t2", step_id="step:0", scope="step"),
                    verdict(
                        "output",
                        status=VerificationStatus.FAIL.value,
                        task_id="t2",
                    ),
                ]
            ),
        ]

        found = self.evaluator.evaluate(summaries=summaries)

        self.assertEqual(found.task_success, 0.5)
        self.assertLess(found.partial_task_success or 0.0, 1.0)
        self.assertGreater(found.partial_task_success or 0.0, 0.0)

    def test_verifier_agreement_is_measured_from_pairs(self) -> None:
        same = (verdict("output"), verdict("output"))
        different = (verdict("output"), verdict("output", status=VerificationStatus.FAIL.value))

        found = self.evaluator.evaluate(agreement_pairs=[same, different])

        self.assertEqual(found.verifier_agreement, 0.5)
        self.assertEqual(found.verifier_pairs, 2)

    def test_critique_agreement_and_accuracy_come_from_recorded_pairs(self) -> None:
        critique = a_critique(category=CritiqueCategory.EXECUTION_ERROR.value)
        other = a_critique(category=CritiqueCategory.PLANNING_ERROR.value)
        matching = a_critique(category=CritiqueCategory.EXECUTION_ERROR.value)

        found = self.evaluator.evaluate(
            critiques=[critique, other],
            expected_categories={
                critique.critique_id: CritiqueCategory.EXECUTION_ERROR.value,
                # The recorded truth for this one is execution too, so the
                # critique that said planning_error is the disagreement.
                other.critique_id: CritiqueCategory.EXECUTION_ERROR.value,
            },
            critique_pairs=[(critique, matching), (critique, other)],
        )

        self.assertEqual(found.critique_accuracy, 0.5)
        self.assertEqual(found.critique_agreement, 0.5)

    def test_reward_integrity_and_correction_success_are_reported(self) -> None:
        validator = VerifiableRewardValidator()
        results = [verdict()]
        reward = VerifiableRewardProvider().reward_for(results, subject_id="traj-1")
        check = validator.check(reward, verifications=results)
        refused = VerifiableRewardValidator().check(
            VerifiableRewardProvider().reward_for([], subject_id="traj-2")
        )

        found = self.evaluator.evaluate(
            results=results,
            integrity=[check, refused],
            corrections=[
                a_correction(a_critique(), verification=verdict()),
                a_correction(a_critique()),
            ],
        )

        self.assertEqual(found.reward_integrity, {"valid": 1, "invalid": 1})
        self.assertEqual(found.correction_success, 0.5)

# ── trainer and critique method ─────────────────────────────────────────────


class RLVRTrainerTests(unittest.TestCase):
    """The RLVR trainer verifies, rewards and audits — and trains only on ask."""

    def setUp(self) -> None:
        self.config = rlvr_config()
        self.trainer = RLVRTrainer(self.config)

    def test_the_run_is_named_for_its_mode_and_algorithm(self) -> None:
        self.assertEqual(self.trainer.mode, RLVR_MODE)
        self.assertEqual(self.trainer.backend, "rlvr")
        self.assertEqual(self.trainer.name, f"{RLVR_MODE}:{self.config.algorithm}")
        self.assertTrue(self.trainer.rlvr_config.dry_run)

    def test_configuration_needs_a_critique_or_correction_dataset(self) -> None:
        without = RLVRTrainer(rlvr_config(dataset_version=""))
        with_dataset = self.trainer

        problems = without.validate_config()
        accepted = with_dataset.validate_config()

        self.assertTrue(problems.errors)
        self.assertTrue(
            any("critique_dataset_version" in item for item in problems.errors)
        )
        self.assertEqual(accepted.errors, ())

    def test_verify_row_freezes_the_expectation_and_stamps_the_verifier(self) -> None:
        result = self.trainer.verify_row(
            {
                "verifier_id": "output",
                "task_id": "task-1",
                "trajectory_id": "traj-1",
                "step_id": "step:0",
                "scope": "step",
                "expected": {"exact": "SUCCESS"},
                "observation": {"output": "SUCCESS"},
            }
        )

        self.assertEqual(result.status, VerificationStatus.PASS.value)
        self.assertEqual(result.verifier_id, "output")
        self.assertEqual(result.verifier_version, RLVR_VERSION)
        self.assertTrue(result.expected_digest)

    def test_an_unselectable_expectation_is_inconclusive_and_says_why(self) -> None:
        result = self.trainer.verify_row(
            {
                "task_id": "task-1",
                "expected": {"nonsense": True},
                "observation": {},
            }
        )

        self.assertEqual(result.status, VerificationStatus.INCONCLUSIVE.value)
        self.assertEqual(result.verifier_id, "")
        self.assertIn("no verifier was selected", result.detail)

    def test_a_named_verifier_that_is_not_registered_is_an_error(self) -> None:
        result = self.trainer.verify_row(
            {
                "verifier_id": "ghost",
                "expected": {"exact": "SUCCESS"},
                "observation": {"output": "SUCCESS"},
            }
        )

        self.assertEqual(result.status, VerificationStatus.ERROR.value)
        self.assertIn("ghost", result.detail)

    def test_selection_prefers_the_expectation_over_the_action(self) -> None:
        decision = self.trainer.select_verifier(
            action={"name": "write_file", "path": "out.txt"},
            expected={"exact": "SUCCESS"},
            observation={"output": "SUCCESS"},
        )

        self.assertEqual(decision.verifier_id, "output")

    def test_a_reward_travels_with_its_integrity_verdict(self) -> None:
        found = self.trainer.reward_for(
            [verdict(task_id="task-1", trajectory_id="traj-1")],
            task_id="task-1",
            trajectory_id="traj-1",
        )

        self.assertTrue(found.trusted)
        self.assertEqual(found.total, 1.0)
        self.assertEqual(found.integrity.status, "valid")
        self.assertEqual(found.reward.reward_version, self.trainer.reward_config_version)

    def test_objective_setup_is_described_without_training_anything(self) -> None:
        info = self.trainer.initialize_reward()

        self.assertEqual(info["reward_config_version"], self.config.reward.version)
        self.assertIn("file", info["verifiers"]["registered"])
        self.assertEqual(info["verifiers"]["deterministic"], 8)
        self.assertIn("critique_policy", info)

    def test_model_metadata_records_the_verifier_snapshot(self) -> None:
        metadata = self.trainer.model_metadata()

        self.assertTrue(metadata["verifiable"])
        block = metadata["rlvr"]
        self.assertEqual(block["reward_config_version"], self.config.reward.version)
        self.assertTrue(block["verifiers"])
        self.assertTrue(block["verification_required"])
        self.assertTrue(block["simulated"])

    def test_a_verification_batch_is_reported_by_status(self) -> None:
        report = self.trainer.verification_report(
            [
                verdict(),
                verdict(status=VerificationStatus.FAIL.value),
                verdict(status=VerificationStatus.INCONCLUSIVE.value, evidence=()),
            ]
        )

        self.assertEqual(report["total"], 3)
        self.assertEqual(report["by_status"]["pass"], 1)
        self.assertEqual(report["by_status"]["fail"], 1)
        self.assertEqual(report["by_status"]["inconclusive"], 1)
        self.assertEqual(report["verifiers"], ["output"])

    def test_critiques_come_from_failures_only(self) -> None:
        found = self.trainer.critique(
            [verdict(), verdict(status=VerificationStatus.FAIL.value)]
        )

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].source, CritiqueSource.VERIFIER.value)

    def test_corrections_are_held_until_something_verifies_them(self) -> None:
        critique = self.trainer.critique(
            [
                verdict(
                    status=VerificationStatus.FAIL.value,
                    error_category=CritiqueCategory.OUTPUT_FORMAT_ERROR.value,
                )
            ]
        )[0]

        rows = self.trainer.correct([critique])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].quality_status, CorrectionStatus.NEEDS_REVIEW.value)
        self.assertEqual(rows[0].original_output, {"output": "SUCCESS"})


class CritiqueTrainingMethodTests(unittest.TestCase):
    """Corrections, projections and planning — the method never trains."""

    def setUp(self) -> None:
        self.critique = a_critique()
        self.method = CritiqueTrainingMethod(method="sft")
        self.dataset = CritiqueDatasetBuilder().build(
            "critique-demo",
            critiques=[self.critique],
            corrections=[a_correction(self.critique, verification=verdict())],
        )

    def test_a_proposal_from_a_caller_is_verified_or_held(self) -> None:
        provided = CritiqueTrainingMethod(
            method="sft",
            corrector=lambda critique, context: {"output": "SUCCESS"},
        )

        held = provided.corrections([self.critique])
        verified = provided.corrections(
            [self.critique], verifications={self.critique.critique_id: verdict()}
        )

        self.assertEqual(held[0].quality_status, CorrectionStatus.NEEDS_REVIEW.value)
        self.assertEqual(
            verified[0].quality_status, CorrectionStatus.ACCEPTED.value
        )

    def test_a_correction_proposal_can_carry_its_own_verification(self) -> None:
        def propose(critique: CritiqueResult, context: Mapping[str, Any]) -> Any:
            return CorrectionProposal(corrected_output={"output": "SUCCESS"}, verification=verdict())

        method = CritiqueTrainingMethod(method="sft", corrector=propose)

        rows = method.corrections([self.critique])

        self.assertEqual(rows[0].quality_status, CorrectionStatus.ACCEPTED.value)

    def test_an_unverified_dataset_is_refused_by_validate(self) -> None:
        unverified = CritiqueDatasetBuilder().build(
            "critique-demo",
            critiques=[self.critique],
            corrections=[a_correction(self.critique)],
        )

        self.assertEqual(self.method.validate(self.dataset), ())
        self.assertTrue(self.method.validate(unverified))
        self.assertIn("no accepted critique example", " ".join(self.method.validate(unverified)))

    def test_the_dataset_projects_into_all_three_learning_methods(self) -> None:
        sft = self.method.to_sft(self.dataset)
        preference = self.method.to_preference(self.dataset)
        reward_rows = self.method.to_reward_examples(self.dataset)

        self.assertEqual(len(sft), 1)
        self.assertEqual(len(preference), 1)
        self.assertEqual(len(reward_rows), 1)
        self.assertEqual(reward_rows[0].kind, "critique")
        self.assertEqual(reward_rows[0].mode, "rlvr")
        self.assertIn("critique_derived", reward_rows[0].reasons)
        self.assertTrue(reward_rows[0].metadata["verified"])

    def test_the_plan_counts_projections_without_training(self) -> None:
        plan = self.method.plan(self.dataset)

        self.assertEqual(plan["projections"], {"sft": 1, "preference": 1, "reward": 1})
        self.assertFalse(plan["trained"])
        self.assertEqual(plan["method"], "sft")

    def test_the_method_dry_run_projects_and_optimizes_nothing(self) -> None:
        result = self.method.dry_run(self.dataset)

        self.assertFalse(result["trained"])
        self.assertEqual(result["reward_row_trust"]["total"], 1)
        self.assertEqual(result["accepted"], 1)

    def test_an_unknown_method_name_falls_back_to_sft(self) -> None:
        self.assertEqual(CritiqueTrainingMethod(method="telepathy").method, "sft")


# ── the pipeline and dry run ────────────────────────────────────────────────


class RLVRPipelineTests(unittest.TestCase):
    """Ten stages, deterministic inputs, no model, no optimizer."""

    def setUp(self) -> None:
        self.critique = a_critique()
        self.dataset = CritiqueDatasetBuilder().build(
            "critique-demo",
            critiques=[self.critique],
            corrections=[a_correction(self.critique, verification=verdict())],
        )
        self.pipeline = RLVRPipeline()
        self.config = rlvr_config(self.dataset.dataset_version_id)

    def test_the_plan_has_every_stage_and_blocks_without_a_dataset(self) -> None:
        ready = self.pipeline.plan(self.config, dataset=self.dataset, tasks=TASKS)
        blocked = self.pipeline.plan(
            rlvr_config(dataset_version=""), dataset=None, tasks=TASKS
        )

        self.assertEqual(
            [stage.name for stage in ready.stages], list(RLVR_PIPELINE_STAGES)
        )
        self.assertTrue(ready.ok)
        self.assertEqual(ready.blocked(), ())
        self.assertEqual(blocked.blocked(), ("training_configuration",))

    def test_the_plan_blocks_a_configuration_with_no_base_model(self) -> None:
        plan = self.pipeline.plan(
            rlvr_config(self.dataset.dataset_version_id, base_model=""),
            dataset=self.dataset,
            tasks=TASKS,
        )

        self.assertEqual(plan.blocked(), ("training_configuration",))
        self.assertIn("base_model", plan.stage("training_configuration").detail)

    def test_the_dry_run_walks_all_ten_stages(self) -> None:
        result = self.pipeline.dry_run(
            self.config, tasks=TASKS, dataset=self.dataset, labels=LABELS
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["blocked"], [])
        stages = {stage["stage"]: stage for stage in result["stages"]}
        for name in RLVR_PIPELINE_STAGES:
            self.assertEqual(stages[name]["status"], "done", name)
        self.assertTrue(result["note"].startswith("every stage ran"))

    def test_the_dry_run_verifies_and_rewards_both_tasks_honestly(self) -> None:
        result = self.pipeline.dry_run(
            self.config, tasks=TASKS, dataset=self.dataset, labels=LABELS
        )
        stages = {stage["stage"]: stage for stage in result["stages"]}
        by_task = {task["task_id"]: task for task in result["tasks"]}

        self.assertEqual(stages["verification"]["artifacts"]["by_status"]["pass"], 2)
        self.assertEqual(stages["verification"]["artifacts"]["by_status"]["fail"], 2)
        self.assertEqual(by_task["t-ok"]["reward"]["total_reward"], 1.0)
        self.assertEqual(by_task["t-fail"]["reward"]["total_reward"], -1.0)
        self.assertEqual(stages["reward_validation"]["artifacts"]["findings"], [])
        self.assertEqual(result["reward_total"], 0.0)

    def test_the_dry_run_evaluates_against_the_labels_it_was_given(self) -> None:
        result = self.pipeline.dry_run(
            self.config, tasks=TASKS, dataset=self.dataset, labels=LABELS
        )
        stages = {stage["stage"]: stage for stage in result["stages"]}
        evaluation = stages["evaluation"]["artifacts"]

        self.assertEqual(evaluation["verification_accuracy"], 1.0)
        self.assertEqual(evaluation["false_positive_verification"], 0)
        self.assertEqual(evaluation["false_negative_verification"], 0)
        self.assertEqual(evaluation["reward_integrity"], {"valid": 2})

    def test_a_dry_run_without_labels_reports_that_nothing_was_scored(self) -> None:
        result = self.pipeline.dry_run(self.config, tasks=TASKS, dataset=self.dataset)
        stages = {stage["stage"]: stage for stage in result["stages"]}

        self.assertEqual(stages["evaluation"]["status"], "skipped")
        self.assertIn("labels", stages["evaluation"]["detail"])

    def test_a_dry_run_is_reproducible(self) -> None:
        first = self.pipeline.dry_run(
            self.config, tasks=TASKS, dataset=self.dataset, labels=LABELS
        )
        second = self.pipeline.dry_run(
            self.config, tasks=TASKS, dataset=self.dataset, labels=LABELS
        )

        self.assertEqual(first["config_fingerprint"], second["config_fingerprint"])
        self.assertEqual(first["reward_total"], second["reward_total"])
        self.assertEqual(
            [task["reward"]["total_reward"] for task in first["tasks"]],
            [task["reward"]["total_reward"] for task in second["tasks"]],
        )

    def test_the_dry_run_produces_critiques_for_the_failing_task(self) -> None:
        result = self.pipeline.dry_run(
            self.config, tasks=TASKS, dataset=self.dataset, labels=LABELS
        )

        self.assertTrue(result["critiques"])
        self.assertTrue(all(item["source"] for item in result["critiques"]))
        self.assertTrue(all(item["evidence"] for item in result["critiques"]))

# ── the manager ─────────────────────────────────────────────────────────────


class RLVRManagerTests(unittest.TestCase):
    """The subsystem wired to real stores: rails hold, nothing starts alone."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.publish, self.events = recording_publisher()
        self.manager = make_manager(self._tmp.name, publish=self.publish)

    def event_types(self) -> list[str]:
        return [type_ for type_, _ in self.events]

    def test_the_verifier_registry_is_not_the_model_registry(self) -> None:
        """The inherited ``registry`` is the MODEL registry; a verifier is a
        check, and shadowing the parent's attribute would make every shared
        lifecycle read answer from the wrong store."""
        from novacontrol.training.registry import SFTModelRegistry

        self.assertIsInstance(self.manager.verifiers_registry, VerifierRegistry)
        self.assertIsInstance(self.manager.registry, SFTModelRegistry)
        self.assertIsNot(self.manager.registry, self.manager.verifiers_registry)
        self.assertEqual(len(self.manager.verifiers_registry.list()), 8)

    def test_the_headline_status_is_honest_about_being_optional(self) -> None:
        status = self.manager.rlvr_status()

        self.assertEqual(status["mode"], RLVR_MODE)
        self.assertEqual(status["verifiers"], 8)
        self.assertEqual(status["deterministic"], 8)
        self.assertTrue(status["dry_run"])
        self.assertEqual(status["reward_config_version"], VerifiableRewardConfig().version)
        self.assertIn("nothing starts automatically", status["note"])
        self.assertEqual(self.manager.status()["rlvr"]["verifiers"], 8)

    def test_a_question_is_verified_rewarded_and_audited(self) -> None:
        verified = self.manager.verify(
            [
                {
                    "verifier_id": "output",
                    "task_id": "task-1",
                    "trajectory_id": "traj-1",
                    "scope": "task",
                    "expected": {"exact": "SUCCESS"},
                    "observation": {"output": "SUCCESS"},
                }
            ]
        )
        reward = self.manager.reward_for(
            verified["results"], task_id="task-1", trajectory_id="traj-1"
        )

        self.assertTrue(verified["ok"])
        self.assertEqual(verified["report"]["by_status"]["pass"], 1)
        self.assertTrue(reward["ok"])
        self.assertEqual(reward["verified_reward"]["reward"]["total_reward"], 1.0)
        self.assertEqual(reward["verified_reward"]["integrity"]["status"], "valid")

    def test_registering_a_verifier_is_announced_and_usable(self) -> None:
        verifier = CustomVerifier("plugin-check", lambda request: True, task_types=("plugin",))

        registered = self.manager.register_verifier(verifier)

        self.assertTrue(registered["ok"])
        self.assertIn("plugin-check", [item.verifier_id for item in self.manager.verifiers_registry.list()])
        self.assertIn("rlvr.verifier_registered", self.event_types())

    def test_disabling_a_verifier_stops_it_from_answering(self) -> None:
        disabled = self.manager.disable_verifier("output", reason="under repair")
        verified = self.manager.verify(
            [{"expected": {"exact": "SUCCESS"}, "observation": {"output": "SUCCESS"}}]
        )

        self.assertTrue(disabled["ok"])
        self.assertFalse(self.manager.verifiers_registry.get("output"))
        self.assertEqual(
            verified["results"][0]["status"], VerificationStatus.INCONCLUSIVE.value
        )

    def test_critiques_are_recorded_listed_and_summarised(self) -> None:
        failed = verdict(
            status=VerificationStatus.FAIL.value,
            evidence=("output_match",),
        )

        recorded = self.manager.record_critiques([failed], trajectory=trajectory("traj-1"))

        self.assertTrue(recorded["ok"])
        stored = self.manager.critiques_list()
        self.assertEqual(len(stored), len(recorded["critiques"]))
        self.assertTrue(self.manager.critique_stats()["total"])
        self.assertIn("rlvr.critique_recorded", self.event_types())

    def test_a_correction_is_proposed_and_held_without_evidence(self) -> None:
        failed = verdict(status=VerificationStatus.FAIL.value, evidence=("output_match",))
        critique_id = self.manager.record_critiques([failed])["critiques"][0][
            "critique_id"
        ]

        proposed = self.manager.propose_corrections(critique_ids=[critique_id])

        self.assertTrue(proposed["ok"])
        self.assertEqual(len(proposed["corrections"]), 1)
        self.assertEqual(
            proposed["corrections"][0]["quality_status"],
            CorrectionStatus.NEEDS_REVIEW.value,
        )
        self.assertEqual(len(self.manager.corrections_pending()), 1)

    def test_proposing_without_a_named_or_stored_critique_is_refused(self) -> None:
        refused = self.manager.propose_corrections()

        self.assertFalse(refused["ok"])
        self.assertIn("no critique", refused["reason"])

    def test_a_dataset_from_a_held_correction_is_not_trainable(self) -> None:
        critique_id = self.manager.record_critiques(
            [verdict(status=VerificationStatus.FAIL.value)]
        )["critiques"][0]["critique_id"]

        built = self.manager.build_critique_dataset("demo", critique_ids=[critique_id])
        invalid = self.manager.validate_critique_dataset(built["dataset"]["dataset_version_id"])

        self.assertTrue(built["ok"])
        self.assertFalse(invalid["ok"])
        self.assertIn("no accepted example", " ".join(invalid["issues"]))

    def test_an_unverified_dataset_cannot_start_a_run(self) -> None:
        critique_id = self.manager.record_critiques(
            [verdict(status=VerificationStatus.FAIL.value)]
        )["critiques"][0]["critique_id"]
        dataset_version = self.manager.build_critique_dataset(
            "demo", critique_ids=[critique_id]
        )["dataset"]["dataset_version_id"]

        with self.assertRaises(ValueError) as caught:
            self.manager.create_run("tiny-model", dataset_version)

        self.assertIn("cannot train an RLVR run", str(caught.exception))

    def test_an_evidence_backed_dataset_creates_a_dry_run_run(self) -> None:
        # A deployment that accepts an unverifiable correction moves the whole
        # pipeline one rung: the row is accepted with its reason recorded, the
        # dataset becomes trainable, and the run still only plans a dry run.
        relaxed = CritiqueConfig(require_verification_for_correction=False)
        self.manager.rlvr_defaults = replace(
            self.manager.rlvr_defaults, critique=relaxed
        )
        self.manager.method = CritiqueTrainingMethod(
            method="sft", config=relaxed, reward_config=VerifiableRewardConfig()
        )
        failed = verdict(status=VerificationStatus.FAIL.value, evidence=("output_match",))
        critique_id = self.manager.record_critiques([failed])["critiques"][0][
            "critique_id"
        ]
        proposed = self.manager.propose_corrections(critique_ids=[critique_id])
        correction_id = proposed["corrections"][0]["example_id"]
        dataset_version = self.manager.build_critique_dataset(
            "demo", correction_ids=[correction_id]
        )["dataset"]["dataset_version_id"]

        run = self.manager.create_run("tiny-model", dataset_version)
        listed = self.manager.rlvr_runs()

        self.assertEqual(run.algorithm, RLVR_MODE)
        self.assertEqual(run.status, "created")
        self.assertEqual([item.run_id for item in listed], [run.run_id])
        self.assertEqual(
            self.manager.critique_dataset(dataset_version).dataset_version_id,
            dataset_version,
        )

    def test_the_dry_run_walks_the_stages_and_emits_its_event(self) -> None:
        result = self.manager.dry_run(
            config={"base_model": "tiny-model", "critique_dataset_version": "demo@1.0.0"},
            tasks=TASKS,
            labels=LABELS,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            [stage["status"] for stage in result["result"]["stages"]],
            ["done"] * len(RLVR_PIPELINE_STAGES),
        )
        self.assertIn("rlvr.dry_run_completed", self.event_types())

    def test_a_plan_is_offered_before_anything_starts(self) -> None:
        planned = self.manager.pipeline_plan(
            {"base_model": "tiny-model", "critique_dataset_version": "demo@1.0.0"},
            tasks=TASKS,
        )

        self.assertTrue(planned["ok"])
        self.assertEqual(planned["plan"]["blocked"], [])
        self.assertEqual(
            [stage["stage"] for stage in planned["plan"]["stages"]],
            list(RLVR_PIPELINE_STAGES),
        )

    def test_the_module_answers_with_seven_capabilities_and_an_idle_manager(self) -> None:
        module = RLVRModule(self.manager)
        idle = RLVRModule()

        self.assertEqual(module.name, "rlvr")
        self.assertEqual(len(module.capabilities), 7)
        self.assertEqual(
            {item.name for item in module.capabilities},
            {
                "rlvr.status",
                "rlvr.verifiers",
                "rlvr.critiques",
                "rlvr.corrections",
                "rlvr.datasets",
                "rlvr.pipeline",
                "rlvr.dry_run",
            },
        )
        self.assertIsNotNone(idle.manager)

# ── the live application ────────────────────────────────────────────────────


class ApplicationRLVRTests(unittest.IsolatedAsyncioTestCase):
    """The subsystem is wired, optional, dry by default and idle at boot."""

    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.app = NovaControlApplication(data_dir=self._tmp.name)
        await self.app.start()
        self.addCleanup(self._stop)

    async def _stop(self) -> None:
        await self.app.stop()

    def test_the_subsystem_is_wired_optional_and_idle(self) -> None:
        status = self.app.rlvr_status()

        self.assertTrue(status["enabled"])
        self.assertTrue(status["dry_run"])
        self.assertEqual(status["verifiers"], 8)
        self.assertEqual(status["runs"], 0)
        self.assertEqual(self.app.rlvr_runs()["runs"], [])
        self.assertIsInstance(self.app.rlvr.verifiers_registry, VerifierRegistry)

    def test_the_verifier_registry_does_not_shadow_the_model_registry(self) -> None:
        """The bug the CLI smoke found: the model registry must stay the one the
        shared lifecycle reads, and the verifier registry must be its own."""
        from novacontrol.training.registry import SFTModelRegistry

        self.assertIsInstance(self.app.rlvr.registry, SFTModelRegistry)
        self.assertIsNot(self.app.rlvr.registry, self.app.rlvr.verifiers_registry)
        self.assertEqual(self.app.rlvr_verifiers()["count"], 8)
        self.assertEqual(self.app.rlvr_verifiers()["enabled"], 8)

    def test_settings_reapplication_keeps_the_locked_defaults(self) -> None:
        applied = self.app.apply_rlvr_settings()

        self.assertTrue(applied["enabled"])
        self.assertTrue(applied["dry_run"])
        self.assertTrue(applied["deterministic_only"])
        self.assertTrue(applied["require_evidence"])

    def test_a_verification_becomes_a_reward_and_a_critique(self) -> None:
        verified = self.app.verify_rlvr(
            {
                "questions": [
                    {
                        "verifier_id": "output",
                        "task_id": "task-1",
                        "trajectory_id": "traj-1",
                        "scope": "task",
                        "expected": {"exact": "SUCCESS"},
                        "observation": {"output": "FAILED"},
                    }
                ]
            }
        )
        reward = self.app.rlvr_reward(
            {
                "verifications": verified["results"],
                "task_id": "task-1",
                "trajectory_id": "traj-1",
            }
        )
        recorded = self.app.record_rlvr_critiques(
            {"verifications": verified["results"]}
        )
        listed = self.app.rlvr_critiques(limit=5)

        self.assertTrue(verified["ok"])
        self.assertEqual(verified["results"][0]["status"], "fail")
        self.assertEqual(reward["verified_reward"]["integrity"]["status"], "valid")
        self.assertLess(reward["verified_reward"]["reward"]["total_reward"], 0.0)
        self.assertEqual(len(recorded["critiques"]), 1)
        self.assertGreaterEqual(len(listed["critiques"]), 1)

    def test_an_unverified_correction_cannot_authorize_a_run(self) -> None:
        verified = self.app.verify_rlvr(
            {
                "questions": [
                    {
                        "verifier_id": "output",
                        "task_id": "task-1",
                        "expected": {"exact": "SUCCESS"},
                        "observation": {"output": "FAILED"},
                    }
                ]
            }
        )
        recorded = self.app.record_rlvr_critiques({"verifications": verified["results"]})
        proposed = self.app.propose_rlvr_corrections(
            {"critique_ids": [item["critique_id"] for item in recorded["critiques"]]}
        )
        built = self.app.create_rlvr_critique_dataset(
            {
                "name": "demo",
                "correction_ids": [item["example_id"] for item in proposed["corrections"]],
            }
        )
        refused = self.app.create_rlvr_run(
            "tiny-model", built["dataset"]["dataset_version_id"]
        )

        self.assertTrue(proposed["held"] >= 1)
        self.assertTrue(built["ok"])
        self.assertFalse(self.app.validate_rlvr_critique_dataset(built["dataset"]["dataset_version_id"])["ok"])
        self.assertFalse(refused["ok"])
        self.assertIn("cannot train", str(refused["reason"]))
        self.assertEqual(self.app.rlvr_runs()["runs"], [])

    def test_a_dry_run_walks_the_stages_without_training(self) -> None:
        result = self.app.dry_run_rlvr(
            {"base_model": "nova-mock"},
            tasks=TASKS,
            dataset_version="critique-demo@1.0.0",
            labels=LABELS,
        )
        stages = {stage["stage"]: stage for stage in result["result"]["stages"]}

        self.assertTrue(result["ok"])
        self.assertEqual(stages["training_configuration"]["status"], "done")
        self.assertEqual(stages["evaluation"]["artifacts"]["verification_accuracy"], 1.0)
        self.assertEqual(result["result"]["reward_total"], 0.0)

    def test_a_plan_is_available_before_anything_runs(self) -> None:
        planned = self.app.rlvr_pipeline(
            {"base_model": "nova-mock"},
            tasks=TASKS,
            dataset_version="critique-demo@1.0.0",
        )

        self.assertTrue(planned["ok"])
        self.assertEqual(planned["plan"]["blocked"], [])


# ── the HTTP surface ────────────────────────────────────────────────────────


class RLVRApiTests(unittest.TestCase):
    """Real routes over the same rails the CLI and the library use."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._nova: NovaControlApplication | None = None
        self._patchers = [
            mock.patch(
                "novacontrol.api.app.NovaControlApplication", side_effect=self._isolated
            ),
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

    def test_status_summary_and_verifiers_are_served(self) -> None:
        status = self._client.get("/rlvr/status")
        summary = self._client.get("/rlvr/summary")
        verifiers = self._client.get("/rlvr/verifiers")

        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["verifiers"], 8)
        self.assertTrue(status.json()["dry_run"])
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertEqual(summary.json()["verifiers"], 8)
        self.assertEqual(verifiers.json()["count"], 8)

    def test_verify_reward_and_critique_routes_round_trip(self) -> None:
        question = {
            "verifier_id": "output",
            "task_id": "task-1",
            "trajectory_id": "traj-1",
            "scope": "task",
            "expected": {"exact": "SUCCESS"},
            "observation": {"output": "FAILED"},
        }

        verified = self._client.post("/rlvr/verify", json={"questions": [question]})
        self.assertEqual(verified.status_code, 200, verified.text)
        results = verified.json()["results"]

        reward = self._client.post(
            "/rlvr/reward",
            json={"verifications": results, "task_id": "task-1", "trajectory_id": "traj-1"},
        )
        recorded = self._client.post("/rlvr/critiques", json={"verifications": results})
        listed = self._client.get("/rlvr/critiques")

        self.assertEqual(verified.json()["results"][0]["status"], "fail")
        self.assertEqual(
            reward.json()["verified_reward"]["integrity"]["status"], "valid"
        )
        self.assertTrue(recorded.json()["ok"])
        self.assertTrue(listed.json()["critiques"])

    def test_a_dry_run_is_served_over_http(self) -> None:
        response = self._client.post(
            "/rlvr/dry-run",
            json={
                "config": {"base_model": "nova-mock"},
                "tasks": list(TASKS),
                "labels": LABELS,
                "dataset_version": "critique-demo@1.0.0",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["result"]["stages"]), len(RLVR_PIPELINE_STAGES))

    def test_runs_and_missing_datasets_are_answered_not_invented(self) -> None:
        empty = self._client.get("/rlvr/runs")
        missing = self._client.get("/rlvr/datasets/nope@1.0.0")
        refused = self._client.post(
            "/rlvr/runs", json={"model": "tiny-model", "dataset_version": "nope@1.0.0"}
        )

        self.assertEqual(empty.json()["runs"], [])
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(refused.status_code, 422)

    def test_disabling_a_verifier_over_http_is_reflected(self) -> None:
        disabled = self._client.post(
            "/rlvr/verifiers/disable", json={"verifier_id": "git", "reason": "test"}
        )
        verifiers = self._client.get("/rlvr/verifiers")
        enabled = self._client.post("/rlvr/verifiers/enable", json={"verifier_id": "git"})

        self.assertTrue(disabled.json()["ok"])
        self.assertEqual(verifiers.json()["enabled"], 7)
        self.assertTrue(enabled.json()["ok"])


# ── the CLI ─────────────────────────────────────────────────────────────────


class RLVRCliTests(unittest.IsolatedAsyncioTestCase):
    """`novacontrol rlvr …` reaches the application method it names."""

    def test_the_parser_offers_every_action(self) -> None:
        from novacontrol.cli.parser import build_parser

        parser = build_parser()
        actions = (
            "status", "summary", "verifiers", "verify", "reward", "critiques",
            "critique", "corrections", "propose", "datasets", "dataset", "build",
            "validate", "held", "pairs", "estimate", "pipeline", "dry-run",
            "create", "runs", "run", "checkpoints", "start", "pause", "resume",
            "cancel", "evaluate",
        )

        for action in actions:
            parsed = parser.parse_args(["rlvr", action])
            self.assertEqual(parsed.action, action, action)

        with self.assertRaises(SystemExit):
            parser.parse_args(["rlvr", "gamble"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["rlvr", "status", "--not-a-flag"])

    async def test_every_action_reaches_the_application_method_it_names(self) -> None:
        from novacontrol.cli.commands import _rlvr_action

        calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

        class Fake:
            def __getattr__(self, name: str):
                def record(*args: Any, **kwargs: Any) -> dict[str, Any]:
                    calls.append((name, args, kwargs))
                    return {"ok": True, "called": name}

                if name in {"start_rlvr_run", "resume_rlvr_run"}:
                    async def awaited(*args: Any, **kwargs: Any) -> dict[str, Any]:
                        return record(*args, **kwargs)

                    return awaited
                return record

        expected = {
            "status": "rlvr_status",
            "summary": "rlvr_summary",
            "verifiers": "rlvr_verifiers",
            "verify": "verify_rlvr",
            "reward": "rlvr_reward",
            "critiques": "rlvr_critiques",
            "critique": "record_rlvr_critiques",
            "corrections": "rlvr_corrections",
            "propose": "propose_rlvr_corrections",
            "datasets": "rlvr_critique_datasets",
            "dataset": "rlvr_critique_dataset",
            "build": "create_rlvr_critique_dataset",
            "validate": "validate_rlvr_critique_dataset",
            "held": "rlvr_held",
            "pairs": "rlvr_preference_pairs",
            "estimate": "estimate_rlvr",
            "pipeline": "rlvr_pipeline",
            "dry-run": "dry_run_rlvr",
            "create": "create_rlvr_run",
            "runs": "rlvr_runs",
            "run": "rlvr_run",
            "checkpoints": "rlvr_checkpoints",
            "start": "start_rlvr_run",
            "pause": "pause_rlvr_run",
            "resume": "resume_rlvr_run",
            "cancel": "cancel_rlvr_run",
            "evaluate": "rlvr_evaluation",
        }
        with TemporaryDirectory() as tmp:
            payload_file = Path(tmp) / "rlvr.json"
            tasks_file = Path(tmp) / "tasks.json"
            tasks_file.write_text(json.dumps([{"task_id": "t1"}]), encoding="utf-8")
            labels_file = Path(tmp) / "labels.json"
            labels_file.write_text(json.dumps({"t1": "pass"}), encoding="utf-8")
            payload_file.write_text(
                json.dumps(
                    {
                        "questions": [{"expected": {"exact": "x"}, "observation": {"output": "x"}}],
                        "verifications": [{"verifier_id": "output", "status": "pass"}],
                        "critiques": [{"category": "other"}],
                        "critique_ids": ["c1"],
                        "correction_ids": ["x1"],
                        "tasks": [{"task_id": "t1"}],
                        "task_id": "task-1",
                        "trajectory_id": "traj-1",
                    }
                ),
                encoding="utf-8",
            )
            app = Fake()
            args: dict[str, Any] = {
                "identifier": "critique-demo@1.0.0",
                "name": "probe",
                "model": "tiny-model",
                "dataset_version": "critique-demo@1.0.0",
                "category": "other",
                "severity": "high",
                "status": "created",
                "limit": 5,
                "file": str(payload_file),
                "labels": str(labels_file),
                "tasks": str(tasks_file),
                "overrides": ["rollout_count=2"],
                "confirm": False,
                "override": False,
                "reason": "because",
                "note": "probe",
                "pending_only": False,
            }
            for action, method in expected.items():
                calls.clear()
                result = await _rlvr_action(app, action, **args)
                self.assertEqual(len(calls), 1, action)
                self.assertEqual(calls[0][0], method, action)
                self.assertEqual(result["called"], method, action)
            calls.clear()
            with self.assertRaises(ValueError):
                await _rlvr_action(app, "teleport", **args)

    async def test_a_real_application_answers_the_status_action(self) -> None:
        from novacontrol.cli.commands import _rlvr_action

        with TemporaryDirectory() as tmp:
            app = NovaControlApplication(data_dir=tmp)
            await app.start()
            try:
                args: dict[str, Any] = {
                    "identifier": "",
                    "name": "",
                    "model": "",
                    "dataset_version": "",
                    "category": "",
                    "severity": "",
                    "status": "",
                    "limit": 5,
                    "file": "",
                    "labels": "",
                    "tasks": "",
                    "overrides": (),
                    "confirm": False,
                    "override": False,
                    "reason": "",
                    "note": "",
                    "pending_only": False,
                }
                status = await _rlvr_action(app, "status", **args)
            finally:
                await app.stop()

        self.assertEqual(status["verifiers"], 8)
        self.assertTrue(status["dry_run"])


# ── regression: no reasoning, nothing automatic ─────────────────────────────


FORBIDDEN_KEYS = {
    "reasoning",
    "chain_of_thought",
    "chain_of_thoughts",
    "thoughts",
    "private_reasoning",
    "scratchpad",
    "rationale",
    "internal_monologue",
}


def walk_keys(value: Any) -> set[str]:
    """Every mapping key in a stored document, at any depth."""
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            found.add(str(key))
            found |= walk_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found |= walk_keys(item)
    return found


class NoReasoningRegressionTests(unittest.TestCase):
    """Nothing this phase stores is, or contains, a reasoning trace."""

    def test_the_front_door_exports_the_phase_surface(self) -> None:
        import novacontrol.rlvr as package

        for name in (
            "RLVRTrainer",
            "RLVRManager",
            "VerifierRegistry",
            "VerifiableRewardProvider",
            "VerifiableRewardValidator",
            "CritiqueEngine",
            "CritiqueDatasetBuilder",
            "RewardTamperGuard",
            "RLVRPipeline",
        ):
            self.assertTrue(hasattr(package, name), name)

    def test_the_package_imports_no_optional_heavy_dependency(self) -> None:
        """``import novacontrol.rlvr`` pulls in none of the heavy optional deps.

        The check runs in a FRESH interpreter on purpose. This module (like
        every other test module) imports ``novacontrol.api.app``, whose
        module-level ``app = create_app()`` builds the whole application and
        reaches the telemetry layer's optional psutil probe — so whenever
        psutil happens to be installed, the process this test runs in already
        has it in ``sys.modules`` no matter what ``novacontrol.rlvr`` imports.
        Asserting on that process state would fail on a psutil host while the
        property under test still holds, and would pass vacuously on a host
        without psutil. A clean child process answers the real question:
        what does importing this package drag in?
        """
        import os
        import subprocess
        import sys

        source_root = Path(__file__).resolve().parents[1] / "src"
        probe = (
            "import sys\n"
            "import novacontrol.rlvr\n"
            "heavy = [name for name in ('torch', 'transformers', 'psutil')\n"
            "         if name in sys.modules]\n"
            "print(','.join(heavy))\n"
        )
        environment = {**os.environ, "PYTHONPATH": str(source_root)}
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            env=environment,
            check=True,
        )

        # A non-empty report names the dependency that leaked; empty is clean.
        self.assertEqual(completed.stdout.strip(), "", completed.stderr)

    def test_stored_rows_carry_no_reasoning_key(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = make_manager(tmp)
            failed = verdict(status=VerificationStatus.FAIL.value, evidence=("output_match",))
            manager.record_critiques([failed])
            critique_id = manager.critiques_list()[0].critique_id
            manager.propose_corrections(critique_ids=[critique_id])
            manager.build_critique_dataset(
                "demo", correction_ids=[item.example_id for item in manager.corrections_list()]
            )
            stored = sorted(Path(tmp).rglob("*.jsonl"))

            self.assertTrue(stored)
            for path in stored:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    keys = walk_keys(json.loads(line))
                    self.assertEqual(
                        keys & FORBIDDEN_KEYS, set(), f"{path.name}: {keys}"
                    )

    def test_a_fresh_installation_starts_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = make_manager(tmp)

            self.assertEqual(manager.rlvr_runs(), ())
            self.assertEqual(manager.rlvr_status()["runs"], 0)
            self.assertTrue(manager.rlvr_defaults.dry_run)
            self.assertEqual(manager.rlvr_defaults.algorithm, "mock_policy")


if __name__ == "__main__":  # pragma: no cover - unittest discovery is the entry point
    unittest.main()
