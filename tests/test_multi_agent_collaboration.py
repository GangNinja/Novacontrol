"""Phase 28 — multi-agent collaboration tests.

The happy path uses the REAL ``DataAnalysisAgent`` and ``VerificationAgent`` over
a real CSV: one agent per column, one independent recomputation, and aggregation
that must refuse an unverified run. Scripted agents appear only where a test has
to control timing, failure or contradictory claims; they are test harnesses, not
shipped agents.
"""

from __future__ import annotations

import asyncio
import csv
import json
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from typing import Any
from unittest import mock

from novacontrol.agents import (
    AgentRegistry,
    AgentResponse,
    AgentRole,
    AgentTask,
    AgentTaskStatus,
    CollaborationEngine,
    CollaborationMode,
    CollaborationPlan,
    CollaborationPolicy,
    CollaborationStatus,
    CollaborationSubtask,
    CollaborationTask,
    DataAnalysisAgent,
    DeveloperAgent,
    HardwareSchedule,
    ProviderCapabilities,
    SubtaskResult,
    SubtaskStatus,
    TokenLedger,
    VerificationAgent,
)
from novacontrol.agents.collaboration_controls import (
    ProviderPreflightChecker,
    backoff_decision,
)
from novacontrol.core.config import CollaborationSettings
from novacontrol.agents.models import AgentMessage
from novacontrol.agents.pipeline import SpecialistPipeline
from novacontrol.agents.verification import VerificationAgent
from novacontrol.application_helpers import ApprovedApprovalGateway
from novacontrol.optimization.models import ResourceAssessment, ResourceLevel
from novacontrol.continual.engine import ContinualLearningEngine
from novacontrol.continual.store import InMemoryContinualLearningStore
from novacontrol.persistence.json_store import JsonStateStore
from novacontrol.reliability.permissions import PermissionManager
from novacontrol.self_improvement.engine import SelfImprovementEngine
from novacontrol.self_improvement.models import CodeChange

try:
    from fastapi.testclient import TestClient

    from novacontrol.api.app import create_app
    from novacontrol.application import NovaControlApplication
except Exception:  # pragma: no cover - httpx/fastapi missing
    TestClient = None  # type: ignore[assignment]
    create_app = None  # type: ignore[assignment]
    NovaControlApplication = None  # type: ignore[assignment]


def _csv(path: Path, *, columns: list[str] | None = None, rows: list[list[Any]] | None = None) -> None:
    columns = columns or ["age", "score", "name"]
    rows = rows or [
        [20, 10.0, "a"],
        [30, 20.0, "b"],
        [40, 30.0, "c"],
        [50, 40.0, "d"],
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)


def _pipeline(*, authorize: bool = True) -> SpecialistPipeline:
    return SpecialistPipeline(
        permissions=PermissionManager(),
        approvals=ApprovedApprovalGateway() if authorize else None,
    )


def _registry(root: Path) -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(DataAnalysisAgent(root=root, pipeline=_pipeline()))
    registry.register(VerificationAgent(root=root, pipeline=_pipeline()))
    return registry


class _FixedAgent:
    """A test harness agent: it really runs, but its output is controlled."""

    def __init__(
        self,
        name: str,
        role: AgentRole,
        *,
        verified: bool = True,
        claims: tuple[dict[str, Any], ...] = (),
        delay: float = 0.0,
        fail: bool = False,
        evidence: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        self._name = name
        self.role = role
        self.verified = verified
        self.claims = list(claims)
        self.delay = delay
        self.fail = fail
        self.evidence = dict(evidence or {})
        self.usage = dict(usage or {})
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def handle_task(self, task: AgentTask) -> AgentResponse:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError(f"{self.name} crashed")
        return AgentResponse(
            task_id=task.id,
            agent_name=self.name,
            role=self.role,
            status=AgentTaskStatus.COMPLETED,
            content=f"{self.name} completed {task.goal}",
            metadata={
                "verified": self.verified,
                "claims": list(self.claims),
                "report": {
                    "verified": self.verified,
                    "claims": list(self.claims),
                    "evidence": dict(self.evidence),
                    **dict(self.evidence),
                },
                "verifications": {
                    "step": {
                        "status": "pass" if self.verified else "fail",
                        "confidence": 0.9 if self.verified else 0.2,
                        "reason": "scripted harness",
                    }
                },
                "usage": dict(self.usage),
            },
        )


class _UnavailableAgent(_FixedAgent):
    def availability(self) -> dict[str, Any]:
        return {"available": False, "reason": "required resource is missing"}


class _NonCompletedAgent(_FixedAgent):
    async def handle_task(self, task: AgentTask) -> AgentResponse:
        self.calls += 1
        return AgentResponse(
            task_id=task.id,
            agent_name=self.name,
            role=self.role,
            status=AgentTaskStatus.FAILED,
            content="the agent could not complete the work",
            metadata={"verified": False},
        )


class _RateLimitedAgent(_FixedAgent):
    async def handle_task(self, task: AgentTask) -> AgentResponse:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("429 Too Many Requests")
        return await super().handle_task(task)


class _RetryAfterAgent(_FixedAgent):
    async def handle_task(self, task: AgentTask) -> AgentResponse:
        if self.calls == 0:
            self.calls += 1
            raise RuntimeError("429 Too Many Requests; Retry-After: 2")
        return await super().handle_task(task)


class _CapabilityProvider:
    name = "fake-provider"
    model = "fake-model"
    capabilities = {
        "text_generation": True,
        "structured_output": True,
        "context_window": 4096,
        "max_output_tokens": 512,
    }

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "ok"


class _UnmeasuredMonitor:
    def available_ram_bytes(self) -> None:
        return None

    def total_ram_bytes(self) -> None:
        return None

    def cpu_percent(self) -> None:
        return None

    def gpu(self) -> dict[str, Any]:
        return {"available": False}


class _FakeGovernor:
    def __init__(self, level: ResourceLevel) -> None:
        self.assessment = ResourceAssessment(
            level=level,
            reasons=(f"scripted {level.value} reading",),
            available_ram_bytes=2 * 1024 * 1024 * 1024,
            total_ram_bytes=16 * 1024 * 1024 * 1024,
            cpu_percent=10.0,
        )

    def assess(self) -> ResourceAssessment:
        return self.assessment


class _CloneableAgent(_FixedAgent):
    def clone(self, pipeline: SpecialistPipeline) -> "_CloneableAgent":
        return self


class _ContextAgent(_FixedAgent):
    def __init__(self, name: str, role: AgentRole) -> None:
        super().__init__(name, role)
        self.seen: dict[str, Any] = {}

    async def handle_task(self, task: AgentTask) -> AgentResponse:
        self.seen = dict(task.metadata.get("context") or {})
        return await super().handle_task(task)


def _plan(
    *subtasks: CollaborationSubtask,
    mode: CollaborationMode = CollaborationMode.DATA_ANALYSIS,
    objective: str = "test objective",
) -> CollaborationPlan:
    return CollaborationPlan(
        id="plan:test",
        objective=objective,
        mode=mode,
        rationale="test harness plan",
        subtasks=subtasks,
        beneficial=True,
    )


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _csv(self.root / "data.csv")
        self.registry = _registry(self.root)
        self.engine = CollaborationEngine(
            self.registry, policy=CollaborationPolicy(persist=False)
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_simple_tasks_are_declined_not_split(self) -> None:
        plan = self.engine.plan("open calculator")
        self.assertFalse(plan.beneficial)
        self.assertEqual(plan.mode, CollaborationMode.SINGLE)
        self.assertEqual(plan.subtasks, ())

    def test_a_dataset_is_split_into_independent_columns_and_a_verifier(self) -> None:
        plan = self.engine.plan(
            "analyze each column independently and verify the results",
            context={"dataset_path": str(self.root / "data.csv")},
        )
        self.assertTrue(plan.beneficial)
        self.assertEqual(plan.mode, CollaborationMode.DATA_ANALYSIS)
        self.assertEqual(len(plan.subtasks), 4)
        for spec in plan.subtasks[:3]:
            self.assertEqual(spec.role, AgentRole.DATA_ANALYSIS)
            self.assertEqual(spec.depends_on, ())
            self.assertTrue(spec.retryable)
        verifier = plan.subtasks[-1]
        self.assertEqual(verifier.role, AgentRole.VERIFICATION)
        self.assertEqual(verifier.depends_on, ("t1", "t2", "t3"))
        self.assertEqual(verifier.metadata["verification"], "metrics")

    def test_coding_plan_requires_explicit_authorization(self) -> None:
        registry = AgentRegistry()
        registry.register(_FixedAgent("coder", AgentRole.CODING))
        registry.register(_FixedAgent("checker", AgentRole.VERIFICATION))
        plan = CollaborationEngine(registry, policy=CollaborationPolicy(persist=False)).plan(
            "implement the feature and then run the tests to verify it"
        )
        self.assertTrue(plan.beneficial)
        self.assertEqual(plan.mode, CollaborationMode.CODING)
        self.assertTrue(plan.requires_approval)
        self.assertEqual(plan.subtasks[0].role, AgentRole.CODING)
        self.assertEqual(plan.subtasks[1].role, AgentRole.VERIFICATION)

    def test_destructive_objectives_are_refused(self) -> None:
        plan = self.engine.plan("delete the old database and then verify it")
        self.assertFalse(plan.beneficial)
        self.assertIn("destructive", plan.blocked_reason)

    def test_missing_specialist_declines_rather_than_improvising(self) -> None:
        registry = AgentRegistry()
        registry.register(DataAnalysisAgent(root=self.root, pipeline=_pipeline()))
        plan = CollaborationEngine(registry, policy=CollaborationPolicy(persist=False)).plan(
            "analyze each column independently and verify the results",
            context={"dataset_path": str(self.root / "data.csv")},
        )
        self.assertFalse(plan.beneficial)
        self.assertTrue(any("verification" in item for item in plan.limitations))

    def test_a_cycle_is_rejected_before_execution(self) -> None:
        with self.assertRaises(ValueError):
            CollaborationPlan(
                id="plan:cycle",
                objective="cycle",
                mode=CollaborationMode.DATA_ANALYSIS,
                rationale="bad",
                subtasks=(
                    CollaborationSubtask("a", "a", "a", AgentRole.DATA_ANALYSIS, ("b",)),
                    CollaborationSubtask("b", "b", "b", AgentRole.DATA_ANALYSIS, ("a",)),
                ),
            )

    def test_columns_are_capped_leaving_a_verification_slot(self) -> None:
        _csv(self.root / "many.csv", columns=["a", "b", "c", "d", "e"])
        plan = CollaborationEngine(
            self.registry,
            policy=CollaborationPolicy(persist=False, max_subtasks=3),
        ).plan(
            "analyze each column independently and verify the results",
            context={"dataset_path": str(self.root / "many.csv")},
        )
        self.assertLessEqual(len(plan.subtasks), 3)
        self.assertEqual(plan.subtasks[-1].role, AgentRole.VERIFICATION)


class AgentArchitectureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _csv(self.root / "data.csv")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_new_specialists_register_by_role(self) -> None:
        registry = _registry(self.root)
        self.assertIsInstance(registry.first_by_role(AgentRole.DATA_ANALYSIS), DataAnalysisAgent)
        self.assertIsInstance(registry.first_by_role(AgentRole.VERIFICATION), VerificationAgent)

    def test_catalog_reports_tools_limits_and_availability(self) -> None:
        engine = CollaborationEngine(_registry(self.root), policy=CollaborationPolicy(persist=False))
        catalog = {row["name"]: row for row in engine.status()["agents"]}
        self.assertIn("data-analysis-agent", catalog)
        self.assertIn("verification-agent", catalog)
        self.assertTrue(catalog["data-analysis-agent"]["available"])
        self.assertIn("analysis.analyze_dataset", catalog["data-analysis-agent"]["tools"])
        self.assertGreater(catalog["data-analysis-agent"]["constraints"]["timeout_seconds"], 0)

    def test_an_unavailable_agent_is_reported_and_never_a_candidate(self) -> None:
        missing = self.root / "missing-data-root"
        agent = DataAnalysisAgent(root=missing, pipeline=_pipeline())
        registry = AgentRegistry()
        registry.register(agent)
        engine = CollaborationEngine(registry, policy=CollaborationPolicy(persist=False))
        catalog = engine.status()["agents"][0]
        self.assertFalse(catalog["available"])
        self.assertIn("not a directory", catalog["availability_reason"])
        self.assertEqual(engine._candidates(AgentRole.DATA_ANALYSIS), [])  # type: ignore[attr-defined]

    def test_config_accepts_the_resource_controls(self) -> None:
        settings = CollaborationSettings.from_mapping(
            {
                "rate_limit_backoff_seconds": 2.5,
                "rate_limit_backoff_max_seconds": 3.5,
                "token_budget": 1234,
                "hardware_aware": False,
            }
        )
        self.assertEqual(settings.rate_limit_backoff_seconds, 2.5)
        self.assertEqual(settings.rate_limit_backoff_max_seconds, 3.5)
        self.assertEqual(settings.token_budget, 1234)
        self.assertFalse(settings.hardware_aware)
        policy = CollaborationPolicy(
            rate_limit_backoff_seconds=settings.rate_limit_backoff_seconds,
            rate_limit_backoff_max_seconds=settings.rate_limit_backoff_max_seconds,
            token_budget=settings.token_budget,
            hardware_aware=settings.hardware_aware,
        ).to_dict()
        self.assertEqual(policy["rate_limit_backoff_seconds"], 2.5)
        self.assertEqual(policy["rate_limit_backoff_max_seconds"], 3.5)
        self.assertEqual(policy["token_budget"], 1234)
        self.assertFalse(policy["hardware_aware"])

    def test_data_agent_computes_real_statistics(self) -> None:
        agent = DataAnalysisAgent(root=self.root, pipeline=_pipeline())
        run = asyncio.run(
            agent.run_task(
                f"analyze column age in {self.root / 'data.csv'}",
                metadata={"context": {"dataset_path": str(self.root / "data.csv"), "column": "age"}},
            )
        )
        self.assertTrue(run.verified)
        self.assertEqual(run.report["rows"], 4)
        self.assertAlmostEqual(run.report["metrics"]["column:age:mean"], 35.0)

    def test_data_agent_refuses_a_path_outside_its_root(self) -> None:
        with TemporaryDirectory() as other:
            _csv(Path(other) / "outside.csv")
            agent = DataAnalysisAgent(root=self.root, pipeline=_pipeline())
            run = asyncio.run(
                agent.run_task(
                    f"analyze {Path(other) / 'outside.csv'}",
                    metadata={"context": {"dataset_path": str(Path(other) / "outside.csv")}},
                )
            )
        self.assertFalse(run.verified)
        self.assertIn("outside", " ".join(run.errors + [o.detail for o in run.outcomes]).lower())

    def test_verification_agent_recomputes_a_reported_metric(self) -> None:
        agent = VerificationAgent(root=self.root, pipeline=_pipeline())
        upstream = {
            "claims": [
                {
                    "topic": "column:age:mean",
                    "value": 35.0,
                    "kind": "evidence",
                    "text": "mean",
                }
            ],
            "evidence": {"path": str(self.root / "data.csv")},
        }
        run = asyncio.run(
            agent.run_task(
                "verify the data analysis findings independently",
                metadata={
                    "verification": "metrics",
                    "context": {
                        "verification": "metrics",
                        "dataset_path": str(self.root / "data.csv"),
                        "upstream": {"t1": upstream},
                    },
                },
            )
        )
        self.assertTrue(run.verified)
        self.assertTrue(run.report["checks"]["column:age:mean"]["ok"])

    def test_verification_agent_fails_a_wrong_metric(self) -> None:
        agent = VerificationAgent(root=self.root, pipeline=_pipeline())
        upstream = {
            "claims": [
                {"topic": "column:age:mean", "value": 99.0, "kind": "evidence", "text": "wrong"}
            ],
            "evidence": {"path": str(self.root / "data.csv")},
        }
        run = asyncio.run(
            agent.run_task(
                "verify the data analysis findings independently",
                metadata={
                    "verification": "metrics",
                    "context": {
                        "dataset_path": str(self.root / "data.csv"),
                        "upstream": {"t1": upstream},
                    },
                },
            )
        )
        self.assertFalse(run.verified)
        self.assertTrue(run.report["checks"]["column:age:mean"]["actual"] == 35.0)


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _csv(self.root / "data.csv")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_data_agent_analyses_all_columns_when_none_is_named(self) -> None:
        agent = DataAnalysisAgent(root=self.root, pipeline=_pipeline())
        run = await agent.run_task(
            "analyze every column in data.csv", metadata={"dataset": "data.csv"}
        )
        self.assertTrue(run.verified)
        self.assertEqual(run.report["columns"], 3)
        self.assertGreaterEqual(len(run.report["metrics"]), 9)

    async def test_real_parallel_analysis_and_verification(self) -> None:
        engine = CollaborationEngine(
            _registry(self.root), policy=CollaborationPolicy(persist=False, max_concurrency=3)
        )
        task = await engine.run(
            "analyze each column in data.csv independently and verify the results",
            context={"dataset_path": str(self.root / "data.csv")},
        )
        self.assertEqual(task.status, CollaborationStatus.COMPLETED)
        self.assertTrue(task.aggregate["verified"])
        self.assertEqual(task.aggregate["verified_required"], 4)
        self.assertTrue(all(result.verified for result in task.result_list()))
        # The three analyses form one independent wave. Actual overlap is
        # asserted deterministically by the controlled timing test below; this
        # run proves the real CSV path and its independent recomputation.
        self.assertGreater(task.aggregate["timing"]["subtask_ms"], 0)
        self.assertGreaterEqual(task.plan.parallel_width, 3)

    async def test_independent_subtasks_are_actually_concurrent(self) -> None:
        registry = AgentRegistry()
        registry.register(_FixedAgent("slow", AgentRole.DATA_ANALYSIS, delay=0.15))
        plan = _plan(
            CollaborationSubtask("t1", "one", "one", AgentRole.DATA_ANALYSIS, retryable=True),
            CollaborationSubtask("t2", "two", "two", AgentRole.DATA_ANALYSIS, retryable=True),
            CollaborationSubtask("t3", "three", "three", AgentRole.DATA_ANALYSIS, retryable=True),
        )
        engine = CollaborationEngine(
            registry, policy=CollaborationPolicy(persist=False, max_concurrency=3)
        )
        started = time.monotonic()
        task = await engine.run(plan=plan)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.40)
        self.assertEqual(task.status, CollaborationStatus.COMPLETED)
        self.assertGreater(task.aggregate["timing"]["parallelism"], 1.5)

    async def test_dependent_receives_only_declared_upstream_context(self) -> None:
        registry = AgentRegistry()
        registry.register(_FixedAgent("producer", AgentRole.DATA_ANALYSIS))
        consumer = _ContextAgent("consumer", AgentRole.PLANNING)
        registry.register(consumer)
        plan = _plan(
            CollaborationSubtask("t1", "produce", "produce", AgentRole.DATA_ANALYSIS),
            CollaborationSubtask(
                "t2",
                "consume",
                "consume",
                AgentRole.PLANNING,
                depends_on=("t1",),
                context={"only": "this"},
            ),
        )
        engine = CollaborationEngine(registry, policy=CollaborationPolicy(persist=False))
        task = await engine.run(plan=plan, context={"shared": "public"})
        self.assertEqual(task.status, CollaborationStatus.COMPLETED)
        self.assertEqual(consumer.seen["only"], "this")
        self.assertEqual(consumer.seen["shared"], "public")
        self.assertIn("t1", consumer.seen["upstream"])
        self.assertNotIn("t3", consumer.seen["upstream"])

    async def test_unverified_agent_output_never_completes(self) -> None:
        registry = AgentRegistry()
        registry.register(_FixedAgent("unproven", AgentRole.CODING, verified=False))
        plan = _plan(
            CollaborationSubtask("t1", "do work", "do work", AgentRole.CODING),
            mode=CollaborationMode.CODING,
        )
        task = await CollaborationEngine(
            registry, policy=CollaborationPolicy(persist=False)
        ).run(plan=plan)
        self.assertFalse(task.aggregate["verified"])
        self.assertNotEqual(task.status, CollaborationStatus.COMPLETED)
        self.assertIn("t1", task.aggregate["unverified"])

    async def test_timeout_is_reported_not_hidden(self) -> None:
        registry = AgentRegistry()
        registry.register(_FixedAgent("slow", AgentRole.DATA_ANALYSIS, delay=0.5))
        plan = _plan(
            CollaborationSubtask(
                "t1", "slow", "slow", AgentRole.DATA_ANALYSIS, timeout_seconds=0.05
            )
        )
        task = await CollaborationEngine(
            registry, policy=CollaborationPolicy(persist=False, max_retries=0)
        ).run(plan=plan)
        self.assertFalse(task.aggregate["verified"])
        self.assertIn("timed out", task.results["t1"].error)

    async def test_retry_can_reassign_to_another_agent(self) -> None:
        registry = AgentRegistry()
        registry.register(_FixedAgent("a-failing", AgentRole.DATA_ANALYSIS, fail=True))
        registry.register(_FixedAgent("b-working", AgentRole.DATA_ANALYSIS))
        plan = _plan(
            CollaborationSubtask("t1", "retry", "retry", AgentRole.DATA_ANALYSIS, retryable=True)
        )
        task = await CollaborationEngine(
            registry, policy=CollaborationPolicy(persist=False, max_retries=1)
        ).run(plan=plan)
        result = task.results["t1"]
        self.assertEqual(result.status, SubtaskStatus.COMPLETED)
        self.assertEqual(result.agent, "b-working")
        self.assertEqual(result.attempts, 2)

    async def test_unavailable_agent_is_not_scheduled(self) -> None:
        registry = AgentRegistry()
        registry.register(_UnavailableAgent("offline", AgentRole.DATA_ANALYSIS))
        plan = _plan(
            CollaborationSubtask("t1", "one", "one", AgentRole.DATA_ANALYSIS, retryable=True)
        )
        task = await CollaborationEngine(
            registry, policy=CollaborationPolicy(persist=False)
        ).run(plan=plan)
        result = task.results["t1"]
        self.assertEqual(result.status, SubtaskStatus.FAILED)
        self.assertIn("no available agent", result.error)
        self.assertIn("offline", result.error)

    async def test_non_completed_agent_response_is_a_failure(self) -> None:
        registry = AgentRegistry()
        registry.register(_NonCompletedAgent("unfinished", AgentRole.DATA_ANALYSIS))
        plan = _plan(
            CollaborationSubtask("t1", "one", "one", AgentRole.DATA_ANALYSIS)
        )
        task = await CollaborationEngine(
            registry, policy=CollaborationPolicy(persist=False)
        ).run(plan=plan)
        result = task.results["t1"]
        self.assertEqual(result.status, SubtaskStatus.FAILED)
        self.assertIn("could not complete", result.error)
        self.assertFalse(task.aggregate["verified"])
        self.assertIn("t1", task.aggregate["failed"])

    async def test_usage_is_aggregated_across_agents(self) -> None:
        registry = AgentRegistry()
        registry.register(
            _FixedAgent("left", AgentRole.DATA_ANALYSIS, usage={"tokens": 10, "cost": 0.25})
        )
        registry.register(
            _FixedAgent("right", AgentRole.DATA_ANALYSIS, usage={"tokens": 5})
        )
        plan = _plan(
            CollaborationSubtask("t1", "one", "one", AgentRole.DATA_ANALYSIS),
            CollaborationSubtask("t2", "two", "two", AgentRole.DATA_ANALYSIS),
        )
        task = await CollaborationEngine(
            registry, policy=CollaborationPolicy(persist=False)
        ).run(plan=plan)
        self.assertTrue(task.aggregate["usage_available"])
        self.assertEqual(task.aggregate["usage"]["tokens"], 15.0)
        self.assertEqual(task.aggregate["usage"]["cost"], 0.25)

    async def test_rate_limit_failure_retries_with_backoff(self) -> None:
        registry = AgentRegistry()
        agent = _RateLimitedAgent("limited", AgentRole.DATA_ANALYSIS)
        registry.register(agent)
        plan = _plan(
            CollaborationSubtask("t1", "one", "one", AgentRole.DATA_ANALYSIS, retryable=True)
        )
        started = time.monotonic()
        task = await CollaborationEngine(
            registry,
            policy=CollaborationPolicy(
                persist=False, max_retries=1, rate_limit_backoff_seconds=0.05
            ),
        ).run(plan=plan)
        elapsed = time.monotonic() - started
        result = task.results["t1"]
        self.assertEqual(result.status, SubtaskStatus.COMPLETED)
        self.assertEqual(result.attempts, 2)
        self.assertGreaterEqual(elapsed, 0.04)

    async def test_resume_requires_fresh_authorization_and_skips_cancelled_writes(
        self,
    ) -> None:
        registry = AgentRegistry()
        agent = _CloneableAgent("writer", AgentRole.CODING)
        registry.register(agent)
        plan = replace(
            _plan(
                CollaborationSubtask(
                    "t1", "write", "write", AgentRole.CODING, retryable=False
                ),
                mode=CollaborationMode.CODING,
            ),
            requires_approval=True,
        )
        engine = CollaborationEngine(
            registry, policy=CollaborationPolicy(persist=False), authorize_pipeline=_pipeline
        )
        task = CollaborationTask(
            id="collab:resume-auth",
            objective="write once",
            mode=plan.mode,
            plan=plan,
            status=CollaborationStatus.CANCELLED,
            cancelled=True,
            results={
                "t1": SubtaskResult(
                    subtask_id="t1",
                    agent="writer",
                    role=AgentRole.CODING,
                    status=SubtaskStatus.CANCELLED,
                    error="cancelled while running",
                )
            },
        )
        engine._remember(task)  # type: ignore[attr-defined]
        with self.assertRaises(ValueError):
            await engine.resume(task.id)
        resumed = await engine.resume(task.id, authorize=True)
        self.assertEqual(agent.calls, 0)
        self.assertEqual(resumed.results["t1"].status, SubtaskStatus.FAILED)
        self.assertIn("not retried", resumed.results["t1"].error)
        self.assertNotEqual(resumed.status, CollaborationStatus.COMPLETED)

    async def test_authorized_run_fails_when_an_agent_cannot_clone(self) -> None:
        registry = AgentRegistry()
        registry.register(_FixedAgent("no-clone", AgentRole.CODING))
        plan = replace(
            _plan(
                CollaborationSubtask("t1", "write", "write", AgentRole.CODING),
                mode=CollaborationMode.CODING,
            ),
            requires_approval=True,
        )
        task = await CollaborationEngine(
            registry,
            policy=CollaborationPolicy(persist=False),
            authorize_pipeline=_pipeline,
        ).run(plan=plan, authorize=True)
        result = task.results["t1"]
        self.assertEqual(result.status, SubtaskStatus.FAILED)
        self.assertIn("no clone hook", result.error)
        self.assertFalse((task.aggregate or {}).get("verified", False))

    async def test_a_failure_preserves_a_completed_sibling(self) -> None:
        registry = AgentRegistry()
        registry.register(DataAnalysisAgent(root=self.root, pipeline=_pipeline()))
        plan = _plan(
            CollaborationSubtask(
                "t1",
                "good",
                "analyze column age in data.csv",
                AgentRole.DATA_ANALYSIS,
                context={"dataset_path": str(self.root / "data.csv"), "column": "age"},
                retryable=True,
            ),
            CollaborationSubtask(
                "t2",
                "missing",
                "analyze missing.csv",
                AgentRole.DATA_ANALYSIS,
                context={"dataset_path": str(self.root / "missing.csv"), "column": "age"},
                retryable=True,
            ),
        )
        task = await CollaborationEngine(
            registry, policy=CollaborationPolicy(persist=False)
        ).run(plan=plan)
        self.assertEqual(task.results["t1"].status, SubtaskStatus.COMPLETED)
        self.assertTrue(task.results["t1"].verified)
        self.assertEqual(task.results["t2"].status, SubtaskStatus.FAILED)
        self.assertEqual(task.status, CollaborationStatus.PARTIAL)

    async def test_recomputation_resolves_a_conflict(self) -> None:
        registry = AgentRegistry()
        registry.register(
            _FixedAgent(
                "left",
                AgentRole.DATA_ANALYSIS,
                claims=(
                    {"topic": "column:age:mean", "value": 10.0, "kind": "evidence", "text": "left"},
                ),
            )
        )
        registry.register(
            _FixedAgent(
                "right",
                AgentRole.DATA_ANALYSIS,
                claims=(
                    {"topic": "column:age:mean", "value": 20.0, "kind": "evidence", "text": "right"},
                ),
            )
        )
        registry.register(
            _FixedAgent(
                "checker",
                AgentRole.VERIFICATION,
                evidence={"checks": {"column:age:mean": {"ok": True, "actual": 10.0}}},
            )
        )
        plan = _plan(
            CollaborationSubtask("t1", "left", "left", AgentRole.DATA_ANALYSIS),
            CollaborationSubtask("t2", "right", "right", AgentRole.DATA_ANALYSIS),
            CollaborationSubtask(
                "t3",
                "check",
                "check",
                AgentRole.VERIFICATION,
                depends_on=("t1", "t2"),
                metadata={"verification": "metrics"},
            ),
        )
        task = await CollaborationEngine(
            registry, policy=CollaborationPolicy(persist=False)
        ).run(plan=plan)
        self.assertTrue(task.conflicts)
        self.assertTrue(task.conflicts[0].resolved)
        self.assertIn("10.0", task.conflicts[0].resolution)
        self.assertTrue(task.aggregate["verified"])
        self.assertEqual(task.status, CollaborationStatus.COMPLETED)

    async def test_cancelled_task_can_resume_without_rerunning_completed_work(self) -> None:
        registry = AgentRegistry()
        agent = _FixedAgent("slow", AgentRole.DATA_ANALYSIS, delay=0.2)
        registry.register(agent)
        plan = _plan(
            CollaborationSubtask("t1", "one", "one", AgentRole.DATA_ANALYSIS, retryable=True),
            CollaborationSubtask("t2", "two", "two", AgentRole.DATA_ANALYSIS, retryable=True),
        )
        engine = CollaborationEngine(
            registry, policy=CollaborationPolicy(persist=False, max_concurrency=2)
        )
        running = asyncio.create_task(engine.run(plan=plan))
        await asyncio.sleep(0.05)
        listed = engine.tasks()["tasks"]
        engine.cancel(listed[0]["task_id"])
        task = await running
        self.assertEqual(task.status, CollaborationStatus.CANCELLED)
        resumed = await engine.resume(task.id)
        self.assertEqual(resumed.status, CollaborationStatus.COMPLETED)
        self.assertTrue(all(record.verified for record in resumed.result_list()))

    async def test_conflicting_claims_are_surfaced_not_majority_voted(self) -> None:
        registry = AgentRegistry()
        registry.register(
            _FixedAgent(
                "left",
                AgentRole.DATA_ANALYSIS,
                claims=(
                    {"topic": "column:age:mean", "value": 10.0, "kind": "evidence", "text": "left"},
                ),
            )
        )
        registry.register(
            _FixedAgent(
                "right",
                AgentRole.DATA_ANALYSIS,
                claims=(
                    {"topic": "column:age:mean", "value": 20.0, "kind": "evidence", "text": "right"},
                ),
            )
        )
        plan = _plan(
            CollaborationSubtask("t1", "left", "left", AgentRole.DATA_ANALYSIS),
            CollaborationSubtask("t2", "right", "right", AgentRole.DATA_ANALYSIS),
        )
        task = await CollaborationEngine(
            registry, policy=CollaborationPolicy(persist=False)
        ).run(plan=plan)
        self.assertTrue(task.conflicts)
        self.assertFalse(task.conflicts[0].resolved)
        self.assertFalse(task.aggregate["verified"])
        self.assertNotEqual(task.status, CollaborationStatus.COMPLETED)
        self.assertTrue(task.aggregate["unresolved_conflicts"])

    async def test_cancellation_preserves_what_finished(self) -> None:
        registry = AgentRegistry()
        registry.register(_FixedAgent("slow", AgentRole.DATA_ANALYSIS, delay=0.3))
        plan = _plan(
            CollaborationSubtask("t1", "one", "one", AgentRole.DATA_ANALYSIS, retryable=True),
            CollaborationSubtask("t2", "two", "two", AgentRole.DATA_ANALYSIS, retryable=True),
        )
        engine = CollaborationEngine(
            registry, policy=CollaborationPolicy(persist=False, max_concurrency=2)
        )
        running = asyncio.create_task(engine.run(plan=plan))
        await asyncio.sleep(0.05)
        listed = engine.tasks()["tasks"]
        self.assertTrue(listed)
        engine.cancel(listed[0]["task_id"])
        task = await running
        self.assertEqual(task.status, CollaborationStatus.CANCELLED)
        self.assertTrue(task.cancelled)

    async def test_idempotency_key_returns_the_same_task(self) -> None:
        registry = AgentRegistry()
        agent = _FixedAgent("once", AgentRole.DATA_ANALYSIS)
        registry.register(agent)
        plan = _plan(CollaborationSubtask("t1", "one", "one", AgentRole.DATA_ANALYSIS))
        engine = CollaborationEngine(registry, policy=CollaborationPolicy(persist=False))
        first = await engine.run(plan=plan, idempotency_key="same")
        second = await engine.run(plan=plan, idempotency_key="same")
        self.assertEqual(first.id, second.id)
        self.assertEqual(agent.calls, 1)


class TokenAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_reported_tokens_are_centralized_across_agents(self) -> None:
        registry = AgentRegistry()
        registry.register(
            _FixedAgent(
                "left",
                AgentRole.DATA_ANALYSIS,
                usage={"prompt_tokens": 10, "completion_tokens": 5},
            )
        )
        registry.register(
            _FixedAgent("right", AgentRole.DATA_ANALYSIS, usage={"total_tokens": 20})
        )
        plan = _plan(
            CollaborationSubtask("t1", "one", "one", AgentRole.DATA_ANALYSIS),
            CollaborationSubtask("t2", "two", "two", AgentRole.DATA_ANALYSIS),
        )
        task = await CollaborationEngine(
            registry,
            policy=CollaborationPolicy(persist=False, token_budget=100),
        ).run(plan=plan)
        tokens = task.aggregate["tokens"]
        self.assertEqual(tokens["total_tokens"], 35)
        self.assertEqual(tokens["prompt_tokens"], 10)
        self.assertEqual(tokens["completion_tokens"], 5)
        self.assertEqual(tokens["budget"], 100)
        self.assertEqual(tokens["remaining"], 65)
        self.assertEqual(tokens["calls"], 2)
        self.assertEqual(tokens["reported_calls"], 2)
        self.assertEqual(tokens["by_agent"]["left"]["total_tokens"], 15)
        self.assertEqual(tokens["by_agent"]["right"]["total_tokens"], 20)

    async def test_exhausted_budget_skips_dependent_work_instead_of_inventing_it(self) -> None:
        registry = AgentRegistry()
        registry.register(
            _FixedAgent("spender", AgentRole.DATA_ANALYSIS, usage={"tokens": 60})
        )
        registry.register(
            _FixedAgent("second", AgentRole.DATA_ANALYSIS, usage={"tokens": 1})
        )
        plan = _plan(
            CollaborationSubtask("t1", "first", "first", AgentRole.DATA_ANALYSIS),
            CollaborationSubtask(
                "t2",
                "second",
                "second",
                AgentRole.DATA_ANALYSIS,
                depends_on=("t1",),
            ),
        )
        task = await CollaborationEngine(
            registry,
            policy=CollaborationPolicy(persist=False, token_budget=50),
        ).run(plan=plan)
        self.assertEqual(task.results["t1"].status, SubtaskStatus.COMPLETED)
        self.assertEqual(task.results["t2"].status, SubtaskStatus.SKIPPED)
        self.assertIn("token budget exhausted", task.results["t2"].error)
        self.assertEqual(task.aggregate["tokens"]["total_tokens"], 60)
        self.assertTrue(task.aggregate["token_budget_exhausted"])
        self.assertNotEqual(task.status, CollaborationStatus.COMPLETED)

    async def test_retry_attempts_are_charged_once_to_the_task_ledger(self) -> None:
        registry = AgentRegistry()
        registry.register(
            _FixedAgent(
                "retry-usage",
                AgentRole.DATA_ANALYSIS,
                usage={"total_tokens": 12},
            )
        )
        plan = _plan(CollaborationSubtask("t1", "one", "one", AgentRole.DATA_ANALYSIS))
        task = await CollaborationEngine(
            registry,
            policy=CollaborationPolicy(persist=False, token_budget=50),
        ).run(plan=plan)
        self.assertEqual(task.aggregate["tokens"]["total_tokens"], 12)
        self.assertEqual(task.results["t1"].usage["total_tokens"], 12)


class ProviderPreflightTests(unittest.IsolatedAsyncioTestCase):
    def test_capabilities_are_read_again_after_a_provider_hot_swap(self) -> None:
        provider = _CapabilityProvider()
        checker = ProviderPreflightChecker(provider_getter=lambda: provider)
        requirements = {"required_capabilities": ["tool_calling"]}
        self.assertFalse(checker.check(requirements).ok)

        upgraded = _CapabilityProvider()
        upgraded.capabilities = {
            "text_generation": True,
            "tool_calling": True,
        }
        provider = upgraded

        self.assertTrue(checker.check(requirements).ok)
        self.assertTrue(checker.describe()["tool_calling"])

    async def test_an_unsupported_requirement_fails_before_the_agent_runs(self) -> None:
        agent = _FixedAgent("worker", AgentRole.DATA_ANALYSIS)
        registry = AgentRegistry()
        registry.register(agent)
        plan = _plan(
            CollaborationSubtask(
                "t1",
                "needs tools",
                "needs tools",
                AgentRole.DATA_ANALYSIS,
                metadata={
                    "provider_requirements": {
                        "required_capabilities": ["tool_calling"],
                        "require_provider": True,
                    }
                },
            )
        )
        task = await CollaborationEngine(
            registry,
            policy=CollaborationPolicy(persist=False),
            provider=_CapabilityProvider(),
        ).run(plan=plan)
        self.assertEqual(agent.calls, 0)
        self.assertEqual(task.results["t1"].status, SubtaskStatus.FAILED)
        self.assertIn("provider preflight failed", task.results["t1"].error)
        self.assertEqual(task.provider_preflight[0]["missing"], ["tool_calling"])

    async def test_declared_capabilities_and_context_are_accepted(self) -> None:
        agent = _FixedAgent("worker", AgentRole.DATA_ANALYSIS)
        registry = AgentRegistry()
        registry.register(agent)
        plan = _plan(
            CollaborationSubtask(
                "t1",
                "needs text",
                "needs text",
                AgentRole.DATA_ANALYSIS,
                metadata={
                    "provider_requirements": {
                        "required_capabilities": ["text_generation"],
                        "min_context_tokens": 4096,
                        "max_output_tokens": 512,
                    }
                },
            )
        )
        task = await CollaborationEngine(
            registry,
            policy=CollaborationPolicy(persist=False),
            provider=_CapabilityProvider(),
        ).run(plan=plan)
        self.assertEqual(agent.calls, 1)
        self.assertEqual(task.status, CollaborationStatus.COMPLETED)
        self.assertTrue(task.provider_preflight[0]["ok"])

    async def test_an_unknown_context_window_is_not_assumed(self) -> None:
        provider = _CapabilityProvider()
        provider.capabilities = {
            "text_generation": True,
            "context_window": None,
        }
        registry = AgentRegistry()
        registry.register(_FixedAgent("worker", AgentRole.DATA_ANALYSIS))
        plan = _plan(
            CollaborationSubtask(
                "t1",
                "needs context",
                "needs context",
                AgentRole.DATA_ANALYSIS,
                metadata={
                    "provider_requirements": {
                        "required_capabilities": ["text_generation"],
                        "min_context_tokens": 4096,
                    }
                },
            )
        )
        task = await CollaborationEngine(
            registry, policy=CollaborationPolicy(persist=False), provider=provider
        ).run(plan=plan)
        self.assertIn("context_window", task.provider_preflight[0]["missing"])
        self.assertNotEqual(task.results["t1"].status, SubtaskStatus.COMPLETED)


class RateLimitBackoffTests(unittest.IsolatedAsyncioTestCase):
    def test_retry_after_wins_over_exponential_and_is_capped(self) -> None:
        decision = backoff_decision(
            "429 Too Many Requests; Retry-After: 2",
            attempt=1,
            base_seconds=0.5,
            maximum_seconds=1.0,
        )
        self.assertEqual(decision.source, "retry_after")
        self.assertEqual(decision.delay_seconds, 1.0)
        self.assertTrue(decision.rate_limited)

    def test_exponential_backoff_is_bounded(self) -> None:
        decision = backoff_decision(
            "too many requests",
            attempt=4,
            base_seconds=0.5,
            maximum_seconds=2.0,
        )
        self.assertEqual(decision.source, "exponential")
        self.assertEqual(decision.delay_seconds, 2.0)

    async def test_the_engine_backs_off_before_the_retry(self) -> None:
        delays: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            delays.append(seconds)

        registry = AgentRegistry()
        agent = _RetryAfterAgent("limited", AgentRole.DATA_ANALYSIS)
        registry.register(agent)
        plan = _plan(
            CollaborationSubtask("t1", "one", "one", AgentRole.DATA_ANALYSIS, retryable=True)
        )
        task = await CollaborationEngine(
            registry,
            policy=CollaborationPolicy(
                persist=False,
                max_retries=1,
                rate_limit_backoff_seconds=0.1,
                rate_limit_backoff_max_seconds=2.0,
            ),
            sleep=fake_sleep,
        ).run(plan=plan)
        self.assertEqual(agent.calls, 2)
        self.assertEqual(delays, [2.0])
        self.assertEqual(task.backoff_events[0]["subtask_id"], "t1")
        self.assertEqual(task.backoff_events[0]["source"], "retry_after")
        self.assertEqual(task.results["t1"].status, SubtaskStatus.COMPLETED)


class HardwareSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_critical_pressure_reduces_concurrency_to_one(self) -> None:
        registry = AgentRegistry()
        registry.register(_FixedAgent("a", AgentRole.DATA_ANALYSIS))
        registry.register(_FixedAgent("b", AgentRole.DATA_ANALYSIS))
        plan = _plan(
            CollaborationSubtask("t1", "one", "one", AgentRole.DATA_ANALYSIS),
            CollaborationSubtask("t2", "two", "two", AgentRole.DATA_ANALYSIS),
        )
        task = await CollaborationEngine(
            registry,
            policy=CollaborationPolicy(persist=False, max_concurrency=3),
            hardware_governor=_FakeGovernor(ResourceLevel.CRITICAL),
        ).run(plan=plan)
        self.assertEqual(task.scheduling["effective_concurrency"], 1)
        self.assertEqual(task.aggregate["scheduling"]["level"], "critical")
        self.assertEqual(task.status, CollaborationStatus.COMPLETED)

    async def test_ample_capacity_keeps_the_configured_concurrency(self) -> None:
        registry = AgentRegistry()
        registry.register(_FixedAgent("a", AgentRole.DATA_ANALYSIS))
        registry.register(_FixedAgent("b", AgentRole.DATA_ANALYSIS))
        plan = _plan(
            CollaborationSubtask("t1", "one", "one", AgentRole.DATA_ANALYSIS),
            CollaborationSubtask("t2", "two", "two", AgentRole.DATA_ANALYSIS),
        )
        task = await CollaborationEngine(
            registry,
            policy=CollaborationPolicy(persist=False, max_concurrency=3),
            hardware_governor=_FakeGovernor(ResourceLevel.AMPLE),
        ).run(plan=plan)
        self.assertEqual(task.scheduling["effective_concurrency"], 3)

    async def test_unmeasured_hardware_keeps_the_policy_limit_without_guessing(self) -> None:
        registry = AgentRegistry()
        registry.register(_FixedAgent("a", AgentRole.DATA_ANALYSIS))
        plan = _plan(CollaborationSubtask("t1", "one", "one", AgentRole.DATA_ANALYSIS))
        task = await CollaborationEngine(
            registry,
            policy=CollaborationPolicy(persist=False, max_concurrency=3),
            hardware_governor=_UnmeasuredMonitor(),
        ).run(plan=plan)
        self.assertEqual(task.scheduling["level"], "unmeasured")
        self.assertEqual(task.scheduling["effective_concurrency"], 3)
        self.assertIn("no hardware reading", task.scheduling["reasons"][0])

    async def test_tight_pressure_caps_concurrency_at_two(self) -> None:
        registry = AgentRegistry()
        registry.register(_FixedAgent("a", AgentRole.DATA_ANALYSIS))
        plan = _plan(CollaborationSubtask("t1", "one", "one", AgentRole.DATA_ANALYSIS))
        task = await CollaborationEngine(
            registry,
            policy=CollaborationPolicy(persist=False, max_concurrency=4),
            hardware_governor=_FakeGovernor(ResourceLevel.TIGHT),
        ).run(plan=plan)
        self.assertEqual(task.scheduling["effective_concurrency"], 2)

    async def test_disabling_hardware_awareness_keeps_the_policy_limit(self) -> None:
        registry = AgentRegistry()
        registry.register(_FixedAgent("a", AgentRole.DATA_ANALYSIS))
        plan = _plan(CollaborationSubtask("t1", "one", "one", AgentRole.DATA_ANALYSIS))
        task = await CollaborationEngine(
            registry,
            policy=CollaborationPolicy(
                persist=False, max_concurrency=3, hardware_aware=False
            ),
            hardware_governor=_FakeGovernor(ResourceLevel.CRITICAL),
        ).run(plan=plan)
        self.assertEqual(task.scheduling["effective_concurrency"], 3)
        self.assertEqual(task.scheduling["level"], "disabled")


class CodingCollaborationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_example.py").write_text(
            "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8"
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _engine(self) -> CollaborationEngine:
        def pipeline() -> SpecialistPipeline:
            return _pipeline()

        developer = DeveloperAgent(
            root=self.root,
            engine=SelfImprovementEngine(self.root),
            pipeline=pipeline(),
            change_builder=lambda goal: (
                CodeChange("src/example.py", "VALUE = 42\n", "write the example module"),
            ),
            test_command=(sys.executable, "-m", "pytest", "-q", "tests"),
        )
        registry = AgentRegistry()
        registry.register(developer)
        registry.register(VerificationAgent(root=self.root, pipeline=pipeline()))
        return CollaborationEngine(
            registry,
            policy=CollaborationPolicy(persist=False, max_retries=0),
            authorize_pipeline=pipeline,
        )

    async def test_coding_writes_a_file_and_a_separate_agent_runs_the_tests(self) -> None:
        engine = self._engine()
        task = await engine.run(
            "implement the feature and then run the tests to verify it", authorize=True
        )
        self.assertEqual(task.plan.subtasks[0].goal, "apply the change: implement the feature")
        self.assertEqual(task.status, CollaborationStatus.COMPLETED)
        self.assertTrue(task.aggregate["verified"])
        self.assertEqual((self.root / "src" / "example.py").read_text(), "VALUE = 42\n")
        self.assertEqual(task.results["t1"].agent, "developer-agent")
        self.assertEqual(task.results["t2"].agent, "verification-agent")
        self.assertTrue(task.results["t2"].verified)

    async def test_coding_without_authorization_writes_nothing(self) -> None:
        engine = self._engine()
        task = await engine.run(
            "implement the feature and then run the tests to verify it", authorize=False
        )
        self.assertEqual(task.status, CollaborationStatus.DECLINED)
        self.assertFalse((self.root / "src" / "example.py").exists())
        self.assertIn("authorize=true", task.error.lower())


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = JsonStateStore(self.root / "state")
        _csv(self.root / "data.csv")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_completed_results_survive_a_restore(self) -> None:
        registry = AgentRegistry()
        registry.register(DataAnalysisAgent(root=self.root, pipeline=_pipeline()))
        plan = _plan(
            CollaborationSubtask(
                "t1",
                "one",
                "analyze column age in data.csv",
                AgentRole.DATA_ANALYSIS,
                context={"dataset_path": str(self.root / "data.csv"), "column": "age"},
                retryable=True,
            )
        )
        engine = CollaborationEngine(
            registry, policy=CollaborationPolicy(persist=True), store=self.store
        )
        task = await engine.run(plan=plan)
        self.assertEqual(task.status, CollaborationStatus.COMPLETED)
        engine.flush()
        restored = CollaborationEngine(
            AgentRegistry(), policy=CollaborationPolicy(persist=True), store=self.store
        )
        report = restored.restore()
        self.assertEqual(report["restored"], 1)
        loaded = restored.task(task.id)
        self.assertEqual(loaded.status, CollaborationStatus.COMPLETED)
        self.assertTrue(loaded.results["t1"].verified)

    async def test_flush_reports_the_count_that_was_actually_persisted(self) -> None:
        registry = AgentRegistry()
        registry.register(_FixedAgent("worker", AgentRole.DATA_ANALYSIS))
        plan = _plan(
            CollaborationSubtask("t1", "one", "one", AgentRole.DATA_ANALYSIS)
        )
        engine = CollaborationEngine(
            registry,
            policy=CollaborationPolicy(persist=True, max_tasks=2),
            store=self.store,
        )
        for index in range(3):
            await engine.run(plan=plan, source=f"flush-{index}")
        report = engine.flush()
        self.assertTrue(report["saved"])
        self.assertEqual(report["tasks"], 2)
        payload = self.store.read("collaboration")
        self.assertEqual(len(payload["tasks"]), 2)

    async def test_a_running_task_is_restored_interrupted_not_replayed(self) -> None:
        plan = _plan(
            CollaborationSubtask(
                "t1", "one", "one", AgentRole.DATA_ANALYSIS, retryable=False
            )
        )
        task = {
            "task_id": "collab:interrupted",
            "objective": "interrupted",
            "mode": "data_analysis",
            "status": "running",
            "plan": plan.to_dict(),
            "results": [],
            "created_at": "2026-01-01T00:00:00Z",
        }
        self.store.write("collaboration", {"schema_version": "phase28.1", "tasks": [task]})
        engine = CollaborationEngine(
            AgentRegistry(), policy=CollaborationPolicy(persist=True), store=self.store
        )
        report = engine.restore()
        self.assertEqual(report["interrupted"], 1)
        loaded = engine.task("collab:interrupted")
        self.assertEqual(loaded.status, CollaborationStatus.INTERRUPTED)


class LearningIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _csv(self.root / "data.csv")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_a_verified_collaboration_becomes_phase27_evidence(self) -> None:
        learning = ContinualLearningEngine(
            store=InMemoryContinualLearningStore(), persist=False, auto_learn=False
        )
        engine = CollaborationEngine(
            _registry(self.root),
            policy=CollaborationPolicy(persist=False, capture_learning=True),
            learning=learning,
        )
        task = await engine.run(
            "analyze each column in data.csv independently and verify the results",
            context={"dataset_path": str(self.root / "data.csv")},
        )
        self.assertTrue(task.aggregate["verified"])
        rows = learning.experiences(limit=10)["experiences"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["task_kind"], "collaboration.data_analysis")
        self.assertTrue(row["verified"])
        self.assertEqual(row["outcome"], "success")
        self.assertIn("data-analysis-agent", row["context"]["agents"])

    async def test_learning_capture_never_upgrades_unverified_work(self) -> None:
        learning = ContinualLearningEngine(
            store=InMemoryContinualLearningStore(), persist=False, auto_learn=False
        )
        registry = AgentRegistry()
        registry.register(_FixedAgent("unproven", AgentRole.DATA_ANALYSIS, verified=False))
        plan = _plan(
            CollaborationSubtask("t1", "one", "one", AgentRole.DATA_ANALYSIS),
            objective="unproven",
        )
        engine = CollaborationEngine(
            registry, policy=CollaborationPolicy(persist=False), learning=learning
        )
        task = await engine.run(plan=plan)
        record = learning.experiences(limit=10)["experiences"][0]
        self.assertNotEqual(record["outcome"], "success")
        self.assertFalse(record["verified"])
        self.assertFalse(task.aggregate["verified"])

    async def test_verified_history_orders_candidates(self) -> None:
        learning = ContinualLearningEngine(
            store=InMemoryContinualLearningStore(), persist=False, auto_learn=False
        )
        evidence = learning.capture(
            "prior collaboration",
            task_kind="collaboration.data_analysis",
            context={"agents": ["b-preferred"]},
            verification={"passed": True, "status": "passed"},
        )
        learning.record(evidence)
        registry = AgentRegistry()
        registry.register(_FixedAgent("a-default", AgentRole.DATA_ANALYSIS))
        registry.register(_FixedAgent("b-preferred", AgentRole.DATA_ANALYSIS))
        engine = CollaborationEngine(
            registry, policy=CollaborationPolicy(persist=False), learning=learning
        )
        candidates = engine._candidates(AgentRole.DATA_ANALYSIS)  # type: ignore[attr-defined]
        self.assertEqual(candidates[0].name, "b-preferred")


@unittest.skipIf(TestClient is None, "fastapi.testclient.TestClient requires httpx")
class CollaborationHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        if TestClient is None:
            self.skipTest("httpx not installed")
        self._tmp = TemporaryDirectory()
        self._csv_dir = mkdtemp(dir=Path.cwd())
        self._csv_path = Path(self._csv_dir) / "data.csv"
        _csv(self._csv_path)
        self._nova: Any = None
        self._patchers = [
            mock.patch(
                "novacontrol.api.app.NovaControlApplication",
                side_effect=self._isolated_application,
            ),
            mock.patch("novacontrol.application.LocalDesktopRunner", _NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", _NoopBrowserRunner),
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
        for path in Path(self._csv_dir).glob("*"):
            path.unlink(missing_ok=True)
        Path(self._csv_dir).rmdir()

    def _isolated_application(self, **kwargs: object) -> Any:
        kwargs["data_dir"] = self._tmp.name
        app = NovaControlApplication(**kwargs)  # type: ignore[arg-type]
        self._nova = app
        return app

    def test_status_overview_and_simple_plan(self) -> None:
        status = self._client.get("/collaboration/status")
        self.assertEqual(status.status_code, 200)
        self.assertIn("policy", status.json())
        overview = self._client.get("/collaboration/overview")
        self.assertEqual(overview.status_code, 200)
        self.assertIn("limitations", overview.json())
        plan = self._client.post("/collaboration/plan", json={"objective": "open calculator"})
        self.assertEqual(plan.status_code, 200)
        self.assertFalse(plan.json()["beneficial"])

    def test_data_plan_and_run_through_http(self) -> None:
        body = {
            "objective": "analyze each column independently and verify the results",
            "context": {"dataset_path": str(self._csv_path)},
        }
        plan = self._client.post("/collaboration/plan", json=body)
        self.assertEqual(plan.status_code, 200)
        self.assertEqual(plan.json()["mode"], "data_analysis")
        run = self._client.post("/collaboration/run", json=body)
        self.assertEqual(run.status_code, 200)
        payload = run.json()
        self.assertEqual(payload["status"], "completed")
        self.assertTrue(payload["aggregate"]["verified"])
        task_id = payload["task_id"]
        one = self._client.get(f"/collaboration/tasks/{task_id}")
        self.assertEqual(one.status_code, 200)
        self.assertEqual(one.json()["task_id"], task_id)
        listing = self._client.get("/collaboration/tasks")
        self.assertGreaterEqual(listing.json()["total"], 1)
        experiences = self._client.get("/continual/experiences").json()
        self.assertGreaterEqual(experiences["total"], 1)
        self.assertEqual(experiences["experiences"][0]["task_kind"], "collaboration.data_analysis")

    def test_resume_forwards_fresh_authorization(self) -> None:
        resume = mock.AsyncMock(return_value={"task_id": "collab:x"})
        with mock.patch.object(self._nova, "resume_collaboration", resume):
            response = self._client.post(
                "/collaboration/tasks/collab:x/resume?authorize=true"
            )
        self.assertEqual(response.status_code, 200)
        resume.assert_awaited_once_with("collab:x", authorize=True)

    def test_missing_task_and_bad_payload(self) -> None:
        self.assertEqual(self._client.get("/collaboration/tasks/nope").status_code, 404)
        self.assertEqual(
            self._client.post("/collaboration/tasks/nope/cancel").status_code, 404
        )
        self.assertEqual(self._client.post("/collaboration/run", json={}).status_code, 422)
        self.assertEqual(
            self._client.post("/collaboration/plan", json={"objective": ""}).status_code,
            422,
        )


class _NoopDesktopRunner:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    @classmethod
    def is_available(cls) -> bool:
        return False

    async def close(self) -> None:
        return None


class _NoopBrowserRunner:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    @classmethod
    def is_available(cls) -> bool:
        return False

    async def close(self) -> None:
        return None
