"""Phase 27 — continual learning, from the evaluator up to the HTTP surface.

The unit half drives the REAL engine over an in-memory store, so every
classification, retrieval decision, proposal, promotion and rollback asserted
here is the same code the application runs. The HTTP half boots the REAL
``create_app()`` stack over a disposable data directory and drives the phase's
endpoints, because a passing engine call proves nothing about a route that 422s.

Two properties are asserted rather than assumed:

* an execution that only confirms itself is never SUCCESS — the ``write`` step
  below says it completed and still the run is PARTIAL, because nothing
  independent checked what it produced;
* a strategy exists only after executed, verified evidence. Nothing in this file
  hands the engine a strategy record, and the benchmark section checks the
  measurement against its own baseline rather than a hard-coded percentage.
"""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from novacontrol.agentcore.state import ActionRecord, AgentTaskState, TaskPhase
from novacontrol.browser import NoopBrowserRunner
from novacontrol.continual.benchmark import LearningBenchmark
from novacontrol.continual.engine import ContinualLearningEngine
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.continual.models import OutcomeClass, StepEvidence
from novacontrol.continual.store import (
    InMemoryContinualLearningStore,
    JsonContinualLearningStore,
    LearningState,
)
from novacontrol.persistence.json_store import JsonStateStore

try:  # pragma: no cover - import guard mirrors the rest of the suite
    from fastapi.testclient import TestClient

    from novacontrol.api.app import create_app
    from novacontrol.application import NovaControlApplication
except Exception:  # pragma: no cover - httpx/fastapi missing
    TestClient = None  # type: ignore[assignment]
    create_app = None  # type: ignore[assignment]
    NovaControlApplication = None  # type: ignore[assignment]

KIND = "filesystem.note_roundtrip"


def _step(
    kind: str = "write",
    target: str = "a.txt",
    tool: str = "write_text",
    *,
    status: str = "completed",
    verified: bool | None = None,
    attempts: int = 1,
    error: str = "",
) -> StepEvidence:
    return StepEvidence(
        step_id=f"step-{kind}-{tool}",
        kind=kind,
        target=target,
        tool=tool,
        status=status,
        verified=verified,
        attempts=attempts,
        error=error,
    )


def _success_steps(target: str = "a.txt") -> list[StepEvidence]:
    return [
        _step("write", target, "write_text", verified=True),
        _step("verify", target, "read_text", verified=True),
    ]


def _capture_success(
    engine: ContinualLearningEngine,
    goal: str,
    *,
    device: str = "local",
    kind: str = KIND,
    target: str = "a.txt",
    strategy_id: str = "",
) -> tuple[Any, dict[str, Any]]:
    evidence = engine.capture(
        goal,
        task_kind=kind,
        context={"device": device, "app": "filesystem"},
        steps=_success_steps(target),
        verification={"passed": True, "status": "passed", "source": "tool"},
        strategy_id=strategy_id,
    )
    return evidence, engine.record(evidence)


class OutcomeEvaluatorTests(unittest.TestCase):
    """The six outcome classes, decided from evidence rather than intent."""

    def setUp(self) -> None:
        self.engine = ContinualLearningEngine(persist=False, auto_learn=False, auto_promote=False)

    def _assess(self, **kwargs: Any) -> Any:
        evidence = self.engine.capture("do the thing", **kwargs)
        assessment = self.engine.evaluator.assess(evidence)
        return evidence, assessment

    def test_verified_success(self) -> None:
        _, assessment = self._assess(
            steps=_success_steps(),
            verification={"passed": True, "status": "passed", "source": "tool"},
        )
        self.assertEqual(assessment.outcome, OutcomeClass.SUCCESS.value)
        self.assertTrue(assessment.verified)
        self.assertEqual(assessment.verification_source, "tool")

    def test_a_step_confirming_itself_is_not_success(self) -> None:
        """A write that says it wrote proves only that it returned."""
        evidence, assessment = self._assess(
            steps=[_step("write", "a.txt", "write_text", verified=True)],
            verification={},
        )
        self.assertNotEqual(assessment.outcome, OutcomeClass.SUCCESS.value)
        self.assertEqual(assessment.outcome, OutcomeClass.PARTIAL.value)
        self.assertFalse(assessment.verified)
        self.assertFalse(evidence.verified_outcome)

    def test_an_independent_verification_step_can_confirm_a_run(self) -> None:
        _, assessment = self._assess(steps=_success_steps(), verification={})
        self.assertEqual(assessment.outcome, OutcomeClass.SUCCESS.value)
        self.assertTrue(assessment.verified)
        self.assertEqual(assessment.verification_source, "step")

    def test_failed_verification_is_a_failure(self) -> None:
        _, assessment = self._assess(
            steps=_success_steps(),
            verification={"passed": False, "status": "failed", "source": "tool"},
        )
        self.assertEqual(assessment.outcome, OutcomeClass.FAILURE.value)
        self.assertFalse(assessment.verified)

    def test_a_retried_run_that_ends_verified_is_recovered(self) -> None:
        steps = [
            _step("write", "a.txt", "legacy_write", status="failed", verified=False, attempts=2),
            _step("write", "a.txt", "write_text", verified=True, attempts=2),
            _step("verify", "a.txt", "read_text", verified=True),
        ]
        _, assessment = self._assess(
            steps=steps,
            verification={"passed": True, "status": "passed", "source": "tool"},
            retries=1,
        )
        self.assertEqual(assessment.outcome, OutcomeClass.RECOVERED.value)
        self.assertTrue(assessment.verified)

    def test_verified_but_with_a_failed_step_is_only_partial(self) -> None:
        steps = [
            _step("write", "a.txt", "legacy_write", status="failed", verified=False),
            _step("write", "a.txt", "write_text", verified=True),
            _step("verify", "a.txt", "read_text", verified=True),
        ]
        _, assessment = self._assess(
            steps=steps,
            verification={"passed": True, "status": "passed", "source": "tool"},
        )
        self.assertEqual(assessment.outcome, OutcomeClass.PARTIAL.value)

    def test_no_evidence_at_all_is_unverified(self) -> None:
        _, assessment = self._assess()
        self.assertEqual(assessment.outcome, OutcomeClass.UNVERIFIED.value)
        self.assertFalse(assessment.verified)

    def test_a_negative_verdict_corrects_the_run(self) -> None:
        evidence = self.engine.capture(
            "create note a.txt",
            task_kind=KIND,
            steps=_success_steps(),
            verification={"passed": True, "status": "passed", "source": "tool"},
        )
        self.engine.record(evidence)
        result = self.engine.feedback(evidence.experience_id, "negative", note="wrong file")
        self.assertEqual(result["outcome"], OutcomeClass.USER_CORRECTED.value)
        self.assertFalse(self.engine.experience(evidence.experience_id)["verified"])

    def test_a_positive_note_does_not_reclassify_verified_work(self) -> None:
        """Regression: a note used to make every commented success 'corrected'."""
        evidence, _ = _capture_success(self.engine, "create note a.txt")
        self.assertEqual(self.engine.experience(evidence.experience_id)["outcome"], "success")
        result = self.engine.feedback(evidence.experience_id, "positive", note="thanks!")
        self.assertEqual(result["outcome"], OutcomeClass.SUCCESS.value)

    def test_positive_feedback_upgrades_an_unverified_run(self) -> None:
        evidence = self.engine.capture("do something", task_kind=KIND)
        self.engine.record(evidence)
        self.assertEqual(self.engine.experience(evidence.experience_id)["outcome"], "unverified")
        result = self.engine.feedback(evidence.experience_id, "positive")
        self.assertEqual(result["outcome"], OutcomeClass.SUCCESS.value)
        self.assertTrue(result["outcome"] == "success")


class RecordingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = ContinualLearningEngine(persist=False, auto_learn=False, auto_promote=False)

    def test_the_same_experience_id_is_recorded_once(self) -> None:
        evidence, first = _capture_success(self.engine, "create note a.txt")
        self.assertTrue(first["recorded"])
        second = self.engine.record(evidence)
        self.assertFalse(second["recorded"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(self.engine.status()["experiences"], 1)

    def test_the_fingerprint_includes_the_device(self) -> None:
        local = self.engine.capture("open chrome", task_kind="desktop.browser", context={"device": "desktop"})
        phone = self.engine.capture("open chrome", task_kind="desktop.browser", context={"device": "phone"})
        self.assertNotEqual(local.fingerprint, phone.fingerprint)

    def test_the_tool_list_follows_the_steps(self) -> None:
        evidence = self.engine.capture("create note a.txt", steps=_success_steps())
        self.assertIn("write_text", evidence.tools)
        self.assertIn("read_text", evidence.tools)

    def test_deleting_an_experience_removes_its_signal_effect(self) -> None:
        first = self.engine.capture(
            "write note one",
            task_kind=KIND,
            steps=[_step("write", "one.txt", "legacy_write", status="failed", verified=False)],
        )
        second = self.engine.capture(
            "write note two",
            task_kind=KIND,
            steps=[_step("write", "two.txt", "legacy_write", status="failed", verified=False)],
        )
        self.engine.record(first)
        self.engine.record(second)
        avoid = [
            signal
            for signal in self.engine.signals(direction="avoid")["signals"]
            if signal["kind"] == "tool_reliability"
        ]
        self.assertTrue(avoid)
        self.assertIn("legacy_write", avoid[0]["subject"])
        self.engine.delete(first.experience_id)
        remaining = [
            signal
            for signal in self.engine.signals(direction="avoid")["signals"]
            if signal["kind"] == "tool_reliability"
        ]
        self.assertEqual(remaining, [])


class RetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = ContinualLearningEngine(persist=False, auto_learn=False, auto_promote=False)

    def test_a_repeat_of_the_same_goal_retrieves_the_earlier_execution(self) -> None:
        _capture_success(self.engine, "create note a.txt")
        recommendation = self.engine.recommend("create note a.txt", task_kind=KIND, context={"device": "local"})
        self.assertEqual(recommendation.source, "evidence")
        self.assertTrue(recommendation.compatible)
        self.assertTrue(recommendation.steps)
        self.assertEqual(len(recommendation.evidence_ids), 1)

    def test_another_device_is_excluded_rather_than_scored(self) -> None:
        _capture_success(self.engine, "create note a.txt", device="local")
        recommendation = self.engine.recommend("create note a.txt", task_kind=KIND, context={"device": "phone"})
        self.assertEqual(recommendation.source, "none")
        self.assertFalse(recommendation.compatible)

    def test_another_task_kind_is_excluded(self) -> None:
        _capture_success(self.engine, "create note a.txt")
        recommendation = self.engine.recommend("create note a.txt", task_kind="desktop.browser")
        self.assertEqual(recommendation.source, "none")

    def test_an_unrelated_goal_retrieves_nothing(self) -> None:
        _capture_success(self.engine, "create note a.txt")
        recommendation = self.engine.recommend("send an email to bob", task_kind=KIND)
        self.assertEqual(recommendation.source, "none")

    def test_an_unverified_execution_is_never_offered_as_a_strategy(self) -> None:
        partial = self.engine.capture(
            "create note a.txt",
            task_kind=KIND,
            context={"device": "local"},
            steps=[_step("write", "a.txt", "write_text", verified=True)],
        )
        self.engine.record(partial)
        recommendation = self.engine.recommend("create note a.txt", task_kind=KIND, context={"device": "local"})
        self.assertEqual(recommendation.source, "none")
        self.assertEqual(recommendation.steps, [])


class StrategyLifecycleTests(unittest.TestCase):
    def _engine(self, **kwargs: Any) -> ContinualLearningEngine:
        options = {"persist": False, "auto_learn": True, "auto_promote": True, "auto_rollback": True}
        options.update(kwargs)
        return ContinualLearningEngine(**options)

    def test_unverified_executions_never_produce_a_strategy(self) -> None:
        engine = self._engine()
        for index in range(3):
            evidence = engine.capture(
                f"write note {index}",
                task_kind=KIND,
                steps=[_step("write", f"n{index}.txt", "write_text", verified=True)],
            )
            engine.record(evidence)
        self.assertEqual(engine.strategies()["total"], 0)
        self.assertIsNone(engine.propose(KIND))

    def test_two_verified_successes_promote_one_version(self) -> None:
        engine = self._engine()
        _capture_success(engine, "create note a.txt")
        self.assertEqual(engine.strategies()["total"], 0, "one success is not enough")
        _capture_success(engine, "create note b.txt")
        strategies = engine.strategies()["strategies"]
        self.assertEqual(len(strategies), 1)
        self.assertEqual(strategies[0]["status"], "active")
        self.assertEqual(strategies[0]["version"], 1)
        self.assertEqual(strategies[0]["stats"]["success"], 2)
        recommendation = engine.recommend("create note c.txt", task_kind=KIND, context={"device": "local"})
        self.assertEqual(recommendation.source, "strategy")
        self.assertTrue(recommendation.adapt_targets)

    def test_only_a_validated_proposal_may_be_promoted(self) -> None:
        engine = self._engine(auto_promote=False)
        _capture_success(engine, "create note a.txt")
        _capture_success(engine, "create note b.txt")
        proposal = engine.proposals()["proposals"][0]
        with self.assertRaises(ValueError):
            engine.promote(proposal["proposal_id"])
        result = engine.validate(proposal["proposal_id"])
        self.assertTrue(result["validated"], result)
        promoted = engine.promote(proposal["proposal_id"])
        self.assertEqual(promoted["status"], "active")

    def test_a_contradicting_failure_blocks_validation(self) -> None:
        engine = self._engine(auto_promote=False)
        _capture_success(engine, "create note a.txt")
        _capture_success(engine, "create note b.txt")
        proposal = engine.proposals()["proposals"][0]
        evidence = engine.capture(
            "create note c.txt",
            task_kind=KIND,
            strategy_id=proposal["strategy_id"],
            steps=[
                _step("write", "c.txt", "write_text", status="failed", verified=False),
                _step("verify", "c.txt", "read_text", status="failed", verified=False),
            ],
            verification={"passed": False, "status": "failed", "source": "tool"},
        )
        engine.record(evidence)
        result = engine.validate(proposal["proposal_id"])
        self.assertFalse(result["validated"])
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("contradict" in reason for reason in result["validation"]["reasons"]))

    def test_rollback_restores_the_previous_version(self) -> None:
        engine = self._engine()
        _capture_success(engine, "create note a.txt")
        _capture_success(engine, "create note b.txt")
        self.assertEqual(engine.strategies()["strategies"][0]["version"], 1)
        for index in range(3):
            _capture_success(engine, f"create note r{index}.txt", target=f"r{index}.txt")
        proposed = engine.propose(KIND)
        self.assertIsNotNone(proposed)
        result = engine.validate(proposed["proposal_id"])
        self.assertTrue(result["validated"], result)
        second = engine.promote(proposed["proposal_id"])
        self.assertEqual(second["version"], 2)
        restored = engine.rollback(KIND)
        self.assertIsNotNone(restored)
        versions = {row["version"]: row["status"] for row in engine.strategies()["strategies"]}
        self.assertEqual(versions[1], "active")
        self.assertEqual(versions[2], "rolled_back")

    def test_statistics_are_recomputed_from_evidence(self) -> None:
        engine = self._engine()
        first, _ = _capture_success(engine, "create note a.txt")
        _capture_success(engine, "create note b.txt")
        self.assertEqual(engine.strategies()["strategies"][0]["stats"]["success"], 2)
        engine.delete(first.experience_id)
        self.assertEqual(engine.strategies()["strategies"][0]["stats"]["success"], 1)

    def test_an_active_template_is_not_proposed_again(self) -> None:
        engine = self._engine()
        _capture_success(engine, "create note a.txt")
        _capture_success(engine, "create note b.txt")
        self.assertIsNone(engine.propose(KIND))

    def test_boundaries_are_part_of_the_read_surface(self) -> None:
        engine = self._engine()
        status = engine.status()
        self.assertFalse(status["boundaries"]["edits_source_code"])
        self.assertFalse(status["boundaries"]["changes_permissions"])
        self.assertFalse(status["boundaries"]["promotes_unverified_evidence"])
        self.assertTrue(status["boundaries"]["advisory_only"])
        overview = engine.overview()
        refused = {row["name"]: row["state"] for row in overview["capabilities"]}
        self.assertEqual(refused["source_code_self_modification"], "refused")
        self.assertEqual(refused["model_based_learning"], "unavailable")


class PersistenceTests(unittest.TestCase):
    def test_evidence_survives_a_restore(self) -> None:
        store = InMemoryContinualLearningStore()
        first = ContinualLearningEngine(store=store, persist=True, auto_learn=False)
        evidence, _ = _capture_success(first, "create note a.txt")
        first.flush()

        second = ContinualLearningEngine(store=store, persist=True, auto_learn=False)
        report = second.restore()
        self.assertEqual(report["experiences"], 1)
        self.assertTrue(report["restored"])
        self.assertEqual(second.experience(evidence.experience_id)["outcome"], "success")

    def test_evidence_survives_on_disk(self) -> None:
        with TemporaryDirectory() as temporary:
            store = JsonContinualLearningStore(JsonStateStore(Path(temporary)))
            first = ContinualLearningEngine(store=store, persist=True, auto_learn=False)
            evidence, _ = _capture_success(first, "create note a.txt")
            first.flush()

            second = ContinualLearningEngine(store=store, persist=True, auto_learn=False)
            second.restore()
            self.assertEqual(second.status()["experiences"], 1)
            self.assertEqual(second.experience(evidence.experience_id)["goal"], "create note a.txt")

    def test_a_corrupt_row_costs_exactly_itself(self) -> None:
        store = InMemoryContinualLearningStore()
        first = ContinualLearningEngine(store=store, persist=True, auto_learn=False)
        _capture_success(first, "create note a.txt")
        first.flush()
        payload = store.load()
        payload["experiences"].append({"experience_id": "exp:broken"})
        store.save(payload)

        second = ContinualLearningEngine(store=store, persist=True, auto_learn=False)
        report = second.restore()
        self.assertEqual(report["experiences"], 1)
        self.assertEqual(report["skipped"].get("experiences"), 1)

    def test_a_foreign_schema_is_not_reinterpreted(self) -> None:
        store = InMemoryContinualLearningStore(
            {"schema_version": "phase27.0", "experiences": [{"experience_id": "exp:x", "goal": "y"}]}
        )
        engine = ContinualLearningEngine(store=store, persist=False)
        report = engine.restore()
        self.assertEqual(report["experiences"], 0)
        self.assertEqual(report["skipped"].get("schema"), 1)

    def test_retention_prunes_the_corpus_to_its_cap(self) -> None:
        engine = ContinualLearningEngine(persist=False, auto_learn=False, max_experiences=16)
        for index in range(20):
            evidence = engine.capture(f"unrelated task {index}", task_kind=KIND)
            engine.record(evidence)
        self.assertEqual(engine.status()["experiences"], 16)

    def test_concurrent_records_all_land(self) -> None:
        engine = ContinualLearningEngine(persist=False, auto_learn=False)
        goals = [f"concurrent task {index}" for index in range(24)]

        def record(goal: str) -> None:
            engine.record(engine.capture(goal, task_kind=KIND))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(record, goals))
        self.assertEqual(engine.status()["experiences"], 24)
        self.assertEqual(engine.status()["errors"], [])


class BenchmarkTests(unittest.TestCase):
    def test_the_benchmark_measures_a_learned_improvement(self) -> None:
        engine = ContinualLearningEngine(store=InMemoryContinualLearningStore(), persist=False)
        report = LearningBenchmark(engine, task_count=3).run()

        baseline = report["baseline"]
        learned = report["learned"]
        improvement = report["improvement"]
        # The baseline writes the note (with a retry) but never verifies it.
        self.assertEqual(baseline["completed"], 3)
        self.assertEqual(baseline["verified"], 0)
        self.assertGreaterEqual(baseline["retries"], 1)
        # The learned path runs the promoted template and verifies every file.
        self.assertEqual(learned["completed"], 3)
        self.assertEqual(learned["verified"], 3)
        self.assertEqual(learned["retries"], 0)
        self.assertEqual(improvement["verified_completion_before"], 0.0)
        self.assertEqual(improvement["verified_completion_after"], 1.0)
        self.assertLess(improvement["retries_after"], improvement["retries_before"])
        # An unrelated family must not regress while the measured one improves.
        self.assertEqual(
            improvement["unrelated_completion_before"], improvement["unrelated_completion_after"]
        )
        self.assertGreaterEqual(report["learning"]["strategies"]["total"], 1)
        self.assertFalse(report["method"]["model_used"])


@unittest.skipIf(TestClient is None, "fastapi.testclient.TestClient requires httpx")
class ContinualHttpTests(unittest.TestCase):
    """The phase's routes through the real application over a temp data dir."""

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

    def _seed_successes(self, count: int = 2) -> None:
        for index in range(count):
            evidence = self._nova.continual.capture(
                f"create note h{index}.txt",
                task_kind=KIND,
                context={"device": "local"},
                steps=[
                    _step("write", f"h{index}.txt", "write_text", verified=True),
                    _step("verify", f"h{index}.txt", "read_text", verified=True),
                ],
                verification={"passed": True, "status": "passed", "source": "tool"},
            )
            self._nova.continual.record(evidence)

    def test_status_and_overview(self) -> None:
        status = self._client.get("/continual/status")
        self.assertEqual(status.status_code, 200)
        body = status.json()
        self.assertEqual(body["experiences"], 0)
        self.assertFalse(body["boundaries"]["edits_source_code"])
        overview = self._client.get("/continual/overview").json()
        self.assertIn("limits", overview)
        self.assertIn("capabilities", overview)

    def test_empty_reads_and_a_missing_experience(self) -> None:
        experiences = self._client.get("/continual/experiences")
        self.assertEqual(experiences.status_code, 200)
        self.assertEqual(experiences.json()["total"], 0)
        missing = self._client.get("/continual/experiences/exp:missing")
        self.assertEqual(missing.status_code, 404)
        delete = self._client.delete("/continual/experiences/exp:missing")
        self.assertEqual(delete.status_code, 404)

    def test_recommend_requires_a_goal(self) -> None:
        response = self._client.post("/continual/recommend", json={"goal": "  "})
        self.assertEqual(response.status_code, 422)

    def test_feedback_rejects_an_unknown_verdict(self) -> None:
        response = self._client.post(
            "/continual/feedback", json={"experience_id": "exp:missing", "verdict": "maybe"}
        )
        self.assertEqual(response.status_code, 422)

    def test_feedback_on_a_missing_experience_is_404(self) -> None:
        response = self._client.post(
            "/continual/feedback", json={"experience_id": "exp:missing", "verdict": "positive"}
        )
        self.assertEqual(response.status_code, 404)

    def test_a_recorded_execution_is_visible_over_http(self) -> None:
        _capture_success(self._nova.continual, "create note a.txt")
        listing = self._client.get("/continual/experiences", params={"task_kind": KIND}).json()
        self.assertEqual(listing["total"], 1)
        record = listing["experiences"][0]
        self.assertEqual(record["outcome"], "success")
        one = self._client.get(f"/continual/experiences/{record['experience_id']}")
        self.assertEqual(one.status_code, 200)
        self.assertEqual(one.json()["goal"], "create note a.txt")

    def test_promote_before_validate_is_409(self) -> None:
        self._nova.continual.auto_promote = False
        self._seed_successes(2)
        proposals = self._nova.continual.proposals()["proposals"]
        self.assertEqual(len(proposals), 1)
        proposal_id = proposals[0]["proposal_id"]
        refused = self._client.post(f"/continual/proposals/{proposal_id}/promote", json={})
        self.assertEqual(refused.status_code, 409)
        validated = self._client.post(f"/continual/proposals/{proposal_id}/validate")
        self.assertEqual(validated.status_code, 200)
        self.assertTrue(validated.json()["validated"])
        promoted = self._client.post(f"/continual/proposals/{proposal_id}/promote", json={})
        self.assertEqual(promoted.status_code, 200)
        self.assertEqual(promoted.json()["status"], "active")

    def test_unknown_proposal_routes_are_404(self) -> None:
        self.assertEqual(self._client.post("/continual/proposals/proposal:x/validate").status_code, 404)
        self.assertEqual(self._client.post("/continual/proposals/proposal:x/promote", json={}).status_code, 404)

    def test_a_device_execution_is_recorded(self) -> None:
        plan = self._client.post("/command/plan", json={"command": "open calculator"}).json()
        token = plan["approval"]["token"]
        executed = self._client.post(
            "/command/execute", json={"command": "open calculator", "approval_token": token}
        )
        self.assertEqual(executed.status_code, 200)
        listing = self._client.get("/continual/experiences").json()
        self.assertEqual(listing["total"], 1)
        record = listing["experiences"][0]
        self.assertEqual(record["task_kind"], "desktop.command")
        self.assertEqual(record["source"], "execution")
        self.assertEqual(record["context"]["device"], "desktop")
        self.assertEqual(len(record["steps"]), 1)

    def test_an_agentic_task_is_recorded_from_its_verified_state(self) -> None:
        state = AgentTaskState(goal="open calculator", id="agentic-test-1")
        state.status = TaskPhase.COMPLETED
        state.interpretation = {"required_tools": ["desktop"]}
        state.plan = [
            {
                "id": "s1",
                "kind": "desktop",
                "description": "open the calculator",
                "target": "calculator",
                "expected": "Calculator is visible",
                "status": "done",
            }
        ]
        state.actions = [
            ActionRecord(
                step_id="s1",
                action="open_application",
                target="calculator",
                success=True,
            )
        ]
        state.verification = {
            "step:s1": {"status": "pass", "reason": "Calculator is visible"},
            "final": {"status": "pass", "reason": "Observed evidence supports goal completion."},
        }

        class _FakeAgentic:
            async def run_task(self, request: str) -> AgentTaskState:
                return state

        original = self._nova.agentic
        self._nova.agentic = _FakeAgentic()
        try:
            response = self._client.post("/agent/run", json={"request": "open calculator"})
        finally:
            self._nova.agentic = original
        self.assertEqual(response.status_code, 200, response.text)
        listing = self._nova.continual.experiences(task_kind="agentic.task")
        self.assertEqual(listing["total"], 1)
        record = listing["experiences"][0]
        self.assertEqual(record["outcome"], "success")
        self.assertEqual(record["source"], "execution")
        self.assertEqual(record["context"]["device"], "desktop")
        self.assertEqual(record["correlation_id"], "agentic-test-1")
        self.assertEqual(record["steps"][0]["status"], "completed")
        self.assertTrue(record["steps"][0]["verified"])

    def test_an_agentic_run_without_a_final_verdict_is_not_success(self) -> None:
        state = AgentTaskState(goal="open calculator", id="agentic-test-2")
        state.status = TaskPhase.COMPLETED
        state.interpretation = {"required_tools": ["desktop"]}
        state.plan = [
            {
                "id": "s1",
                "kind": "desktop",
                "description": "open the calculator",
                "target": "calculator",
                "expected": "Calculator is visible",
                "status": "done",
            }
        ]
        state.actions = [
            ActionRecord(
                step_id="s1",
                action="open_application",
                target="calculator",
                success=True,
            )
        ]
        state.verification = {"step:s1": {"status": "pass", "reason": "action returned"}}

        class _FakeAgentic:
            async def run_task(self, request: str) -> AgentTaskState:
                return state

        original = self._nova.agentic
        self._nova.agentic = _FakeAgentic()
        try:
            response = self._client.post("/agent/run", json={"request": "open calculator"})
        finally:
            self._nova.agentic = original
        self.assertEqual(response.status_code, 200, response.text)
        record = self._nova.continual.experiences(task_kind="agentic.task")["experiences"][0]
        self.assertEqual(record["outcome"], "partial")
        self.assertFalse(record["verified"])

    def test_a_plan_carries_a_learned_suggestion(self) -> None:
        for index in range(2):
            evidence = self._nova.continual.capture(
                "open calculator",
                task_kind="desktop.command",
                context={"device": "desktop", "app": ""},
                steps=[_step("open_application", "calculator", "open_application", verified=True)],
                verification={"passed": True, "status": "passed", "source": "tool"},
            )
            self._nova.continual.record(evidence)
        plan = self._client.post("/desktop/plan", json={"command": "open calculator"}).json()
        self.assertIn("continual", plan)
        self.assertEqual(plan["continual"]["source"], "strategy")
        self.assertTrue(plan["continual"]["steps"])
        self.assertTrue(plan["continual"]["adapt_targets"])

    def test_result_attempts_become_retries(self) -> None:
        payload = {
            "workflow": {
                "actions": [
                    {"id": "a1", "type": "open_application", "target": "calculator", "tool": "open_application"},
                    {"id": "a2", "type": "screenshot", "target": "screen", "tool": "screenshot"},
                ]
            },
            "execution_results": [
                {"action_id": "a1", "status": "completed", "verified": True, "attempts": 2},
                {"action_id": "a2", "status": "completed", "verified": True, "attempts": 1},
            ],
        }
        steps, verification, errors, retries = self._nova._continual_steps(payload)
        self.assertEqual(retries, 1)
        self.assertEqual(steps[0].attempts, 2)
        self.assertEqual(verification["status"], "passed")
        self.assertEqual(errors, [])

    def test_the_benchmark_does_not_pollute_the_live_corpus(self) -> None:
        before = self._client.get("/continual/status").json()["experiences"]
        response = self._client.post("/continual/benchmark", json={"tasks": 2})
        self.assertEqual(response.status_code, 200)
        report = response.json()
        self.assertFalse(report["method"]["model_used"])
        self.assertIn("baseline", report)
        self.assertIn("learned", report)
        self.assertEqual(report["improvement"]["verified_completion_before"], 0.0)
        self.assertGreater(report["improvement"]["verified_completion_after"], 0.0)
        after = self._client.get("/continual/status").json()["experiences"]
        self.assertEqual(before, after)


class LearningStateParsingTests(unittest.TestCase):
    def test_duplicate_rows_collapse_by_id(self) -> None:
        payload = {
            "schema_version": "phase27.1",
            "experiences": [
                {"experience_id": "exp:same", "goal": "one", "schema_version": "phase27.1"},
                {"experience_id": "exp:same", "goal": "two", "schema_version": "phase27.1"},
            ],
        }
        state = LearningState.from_payload(payload)
        self.assertEqual(len(state.experiences), 1)
        self.assertEqual(state.skipped, {})

    def test_a_non_list_section_is_reported_not_raised(self) -> None:
        state = LearningState.from_payload({"schema_version": "phase27.1", "experiences": {"no": "list"}})
        self.assertEqual(state.experiences, {})
        self.assertEqual(state.skipped.get("experiences"), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
