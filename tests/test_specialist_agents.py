"""Phase 12 — specialized agents.

Nine required areas, each one carried through the real pipeline rather than a
mock of it: developer task routing, project-context usage, repository
inspection, test execution, failure recovery, research task routing, source
handling, citation preservation, and permission enforcement.

Two things are asserted everywhere because they are the phase's promises:

* every run walks all eight stages (NLU → Context → Decision → Planner → Tool
  Selection → Execution → Verification → Recovery) and records what each one
  did;
* nothing is reported as completed unless a step was verified, and nothing is
  written or executed unless the permission layer allowed it.
"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from conftest import AllowGateway
from novacontrol.agents import (
    STAGE_ORDER,
    AgentRole,
    AgentRun,
    AgentTask,
    Claim,
    ClaimKind,
    DeveloperAgent,
    PipelineStep,
    ResearchAgent,
    SpecialistAgent,
    SpecialistDecision,
    SpecialistPipeline,
    StageName,
    StageStatus,
    StepOutcome,
    StepStatus,
)
from novacontrol.agents.coordinator import CoordinatorAgent
from novacontrol.agents.developer import (
    CommandResult,
    diagnose_failure,
    parse_test_output,
    search_code,
)
from novacontrol.agents.developer import TestReport as ParsedReport
from novacontrol.agents.registry import AgentRegistry
from novacontrol.agents.research import detect_conflicts
from novacontrol.application import NovaControlApplication
from novacontrol.core.events import EventBus, EventType
from novacontrol.core.security import (
    ApprovalDecision,
    DenyByDefaultApprovalGateway,
    RiskLevel,
)
from novacontrol.explore.models import ResearchSource
from novacontrol.reliability.permissions import (
    PermissionDeclaration,
    PermissionManager,
    PermissionPolicy,
)
from novacontrol.self_improvement import CodeChange

PYTEST_GREEN = "3 passed, 1 skipped in 0.42s\n"
PYTEST_RED = (
    "tests/test_widgets.py::test_timeout FAILED\n"
    "Traceback (most recent call last):\n"
    '  File "tests/test_widgets.py", line 5, in test_timeout\n'
    "    assert widget_timeout() == 30\n"
    "E   AssertionError: assert 60 == 30\n"
    "1 failed, 2 passed in 0.51s\n"
)
UNITTEST_GREEN = "Ran 5 tests in 0.012s\n\nOK (skipped=1)\n"
UNITTEST_RED = "Ran 5 tests in 0.014s\n\nFAILED (failures=2, errors=1)\n"


class FakeRunner:
    """A command runner that records what it was asked to run.

    Scripted results are consumed in order and the last one repeats, so
    "fails once, then passes" is expressible without a counter in the test.
    """

    def __init__(self, *results: tuple[int, str]) -> None:
        self.script = list(results) or [(0, PYTEST_GREEN)]
        self.calls: list[tuple[str, ...]] = []

    async def run(
        self, command: Sequence[str], *, cwd: str | Path | None = None, timeout: float = 300.0
    ) -> CommandResult:
        argv = tuple(str(part) for part in command)
        self.calls.append(argv)
        returncode, stdout = self.script[min(len(self.calls), len(self.script)) - 1]
        return CommandResult(command=argv, returncode=returncode, stdout=stdout)


def write_workspace(root: Path) -> Path:
    """A tiny project on disk: sources, tests, a manifest, and a git branch."""
    (root / "src" / "widgets").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "widgets" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "widgets" / "core.py").write_text(
        "DEFAULT_TIMEOUT = 30\n\n\n"
        "def widget_timeout() -> int:\n"
        '    """The timeout a widget request may take."""\n'
        "    return DEFAULT_TIMEOUT\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_widgets.py").write_text(
        "from widgets.core import widget_timeout\n\n\n"
        "def test_timeout() -> None:\n"
        "    assert widget_timeout() == 30\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'widgets'\n\n[project.optional-dependencies]\ndev = ['pytest']\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "# Widgets\n\nThe widget timeout lives in core.py.\n", encoding="utf-8"
    )
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text(
        "ref: refs/heads/feature/widget-timeout\n", encoding="utf-8"
    )
    (root / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://example.invalid/widgets.git\n', encoding="utf-8"
    )
    return root


def tree_fingerprint(root: Path) -> dict[str, int]:
    """Every file's size, so a claimed read-only run can be checked."""
    return {
        str(path.relative_to(root)): path.stat().st_size
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def note_builder(goal: str) -> tuple[CodeChange, ...]:
    """A change-builder that proposes one small, harmless file."""
    return (
        CodeChange(
            relative_path="notes/change.md",
            content=f"planned: {goal}\n",
            description="Record the planned change",
        ),
    )


def build_developer(  # noqa: PLR0913 - a test fixture with explicit knobs
    root: Path,
    *,
    runner: FakeRunner | None = None,
    approvals: Any = None,
    builder: Any = None,
    bug_log: Any = None,
    event_bus: EventBus | None = None,
    permissions: PermissionManager | None = None,
    max_recovery_attempts: int = 2,
) -> DeveloperAgent:
    pipeline = SpecialistPipeline(
        approvals=approvals or DenyByDefaultApprovalGateway(),
        permissions=permissions,
        event_bus=event_bus,
        max_recovery_attempts=max_recovery_attempts,
    )
    return DeveloperAgent(
        root=root,
        runner=runner or FakeRunner(),
        change_builder=builder,
        bug_log=bug_log,
        pipeline=pipeline,
    )


def source(title: str, url: str, content: str) -> ResearchSource:
    return ResearchSource(title=title, url=url, snippet=content[:40], content=content)


CONFLICTING_SOURCES = (
    source(
        "Widget Guide",
        "https://example.invalid/guide",
        "The widget timeout increases to sixty seconds under load. "
        "Latency stays around 30 ms per request.",
    ),
    source(
        "Widget Benchmarks",
        "https://example.invalid/benchmarks",
        "The widget timeout decreases to five seconds under load. "
        "Latency stays around 80 ms per request.",
    ),
)


class StubProvider:
    """The research engine the application supplies in production."""

    def __init__(
        self, sources: Sequence[ResearchSource] = CONFLICTING_SOURCES, *, fail: bool = False
    ) -> None:
        self.sources = tuple(sources)
        self.fail = fail
        self.calls: list[str] = []

    async def research(self, request: Any) -> Any:
        self.calls.append(str(getattr(request, "topic", "")))
        if self.fail:
            raise OSError("search provider is unreachable")
        return SimpleNamespace(
            topic=getattr(request, "topic", ""),
            overview="Widget timeouts trade headroom against queue depth.",
            answer="",
            key_points=("Timeouts are the first thing to tune.",),
            sources=self.sources,
            warnings=(),
            provider_status="stub",
        )


def build_research(
    *,
    provider: Any = None,
    approvals: Any = None,
    allow_web: bool = True,
    permissions: PermissionManager | None = None,
) -> ResearchAgent:
    return ResearchAgent(
        provider=provider if provider is not None else StubProvider(),
        pipeline=SpecialistPipeline(
            approvals=approvals or DenyByDefaultApprovalGateway(), permissions=permissions
        ),
        allow_web=allow_web,
    )


def _interpretation(goal: str = "") -> Any:
    """A minimal interpretation stand-in; the specialists route from the goal."""
    return SimpleNamespace(goal=goal, subtasks=(goal,), required_tools=("reasoning",))


def _result(returncode: int, stdout: str) -> CommandResult:
    return CommandResult(command=("pytest",), returncode=returncode, stdout=stdout)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    """12.0 — the one shared pipeline every specialist runs on."""

    async def test_run_records_all_eight_stages_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            agent = build_developer(write_workspace(Path(temp)))

            run = await agent.run_task("inspect this repository")

        self.assertEqual([record.stage for record in run.stages], list(STAGE_ORDER))
        self.assertEqual([stage.value for stage in STAGE_ORDER][0], "nlu")
        self.assertEqual([stage.value for stage in STAGE_ORDER][-1], "recovery")
        self.assertEqual(run.stages[0].status, StageStatus.OK)
        self.assertEqual(agent.stages, STAGE_ORDER)

    async def test_run_records_one_entry_per_stage_even_when_it_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            runner = FakeRunner((1, PYTEST_RED), (0, PYTEST_GREEN))
            agent = build_developer(root, runner=runner, approvals=AllowGateway())

            run = await agent.run_task("run the tests")

        stages = [record.stage for record in run.stages]
        self.assertEqual(stages, list(STAGE_ORDER))
        self.assertEqual(run.stage(StageName.RECOVERY).status, StageStatus.RECOVERED)
        self.assertEqual(run.status.value, "completed")

    async def test_a_broken_hook_is_contained_and_reported(self) -> None:
        class Broken(SpecialistAgent):
            def __init__(self) -> None:
                super().__init__("broken-agent", AgentRole.CODING, "Fails on purpose.")

            def clone(self, pipeline: SpecialistPipeline) -> SpecialistAgent:
                return Broken()

            def decide(
                self, task: AgentTask, interpretation: Any, context: Mapping[str, Any]
            ) -> SpecialistDecision:
                raise RuntimeError("routing exploded")

            def plan(self, *args: Any, **kwargs: Any) -> tuple[PipelineStep, ...]:
                raise AssertionError("planning must not be reached")

            async def execute(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
                raise AssertionError("execution must not be reached")

        run = await SpecialistPipeline().run(Broken(), AgentTask("do something"))

        self.assertEqual(run.status.value, "failed")
        self.assertEqual(run.stage(StageName.DECISION).status, StageStatus.FAILED)
        self.assertIn("routing exploded", run.stage(StageName.DECISION).detail)
        self.assertEqual([record.stage for record in run.stages], list(STAGE_ORDER))
        self.assertTrue(run.summary)

    async def test_a_raising_hook_degrades_the_run_instead_of_breaking_it(self) -> None:
        class Exploding(SpecialistAgent):
            def __init__(self) -> None:
                super().__init__("exploding-agent", AgentRole.CODING, "Raises in a hook.")

            def clone(self, pipeline: SpecialistPipeline) -> SpecialistAgent:
                return Exploding()

            def local_context(self, task: AgentTask, interpretation: Any) -> Mapping[str, Any]:
                raise ValueError("no context for you")

            def decide(self, *args: Any, **kwargs: Any) -> SpecialistDecision:
                return SpecialistDecision(action="nothing")

            def plan(self, *args: Any, **kwargs: Any) -> tuple[PipelineStep, ...]:
                return ()

            async def execute(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
                raise AssertionError("execution must not be reached")

        run = await SpecialistPipeline().run(Exploding(), AgentTask("do something"))

        self.assertIn("no context for you", " ".join(run.errors))
        self.assertEqual(run.stage(StageName.CONTEXT).status, StageStatus.OK)
        self.assertEqual(run.stage(StageName.PLANNER).status, StageStatus.SKIPPED)
        self.assertEqual(run.status.value, "failed")

    async def test_a_step_that_does_not_apply_is_not_a_failed_execution_stage(self) -> None:
        """A legitimate skip is a SKIPPED stage, never a FAILED one.

        ``_finish`` already documents the reading — a step that was never
        applicable is not a failure either — and the verification stage records
        SKIPPED for it; the stage that ran it must agree, or the run's own table
        contradicts the run's own summary.
        """

        class Skipper(SpecialistAgent):
            def __init__(self) -> None:
                super().__init__(
                    "skipping-agent", AgentRole.CODING, "Skips what does not apply."
                )

            def clone(self, pipeline: SpecialistPipeline) -> SpecialistAgent:
                return Skipper()

            def declarations(self) -> Mapping[str, PermissionDeclaration]:
                return {
                    "probe.skip": PermissionDeclaration(),
                    "probe.work": PermissionDeclaration(),
                }

            def decide(self, *args: Any, **kwargs: Any) -> SpecialistDecision:
                return SpecialistDecision(action="probe")

            def plan(self, *args: Any, **kwargs: Any) -> tuple[PipelineStep, ...]:
                return (
                    PipelineStep(action="probe.skip", expected="nothing applies"),
                    PipelineStep(action="probe.work", expected="the work is done"),
                )

            async def execute(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
                if step.action == "probe.skip":
                    return StepOutcome(
                        step_id=step.id,
                        action=step.action,
                        status=StepStatus.SKIPPED,
                        detail="there is nothing here this step applies to",
                    )
                return StepOutcome(
                    step_id=step.id,
                    action=step.action,
                    status=StepStatus.COMPLETED,
                    detail="the work was done",
                    output={"done": True},
                )

        run = await SpecialistPipeline().run(Skipper(), AgentTask("do the probe"))

        execution = run.stage(StageName.EXECUTION)
        assert execution is not None
        self.assertEqual(execution.status, StageStatus.SKIPPED)
        self.assertNotEqual(execution.status, StageStatus.FAILED)
        self.assertIn("nothing here this step applies to", execution.detail)
        self.assertIn("the work was done", execution.detail)
        # And the skip does not block the run: the work that did run was verified.
        self.assertEqual(run.status.value, "completed")

    async def test_steps_naming_an_undeclared_tool_are_refused(self) -> None:
        class Sneaky(SpecialistAgent):
            def __init__(self) -> None:
                super().__init__("sneaky-agent", AgentRole.CODING, "Uses an undeclared tool.")

            def clone(self, pipeline: SpecialistPipeline) -> SpecialistAgent:
                return Sneaky()

            def decide(self, *args: Any, **kwargs: Any) -> SpecialistDecision:
                return SpecialistDecision(action="wipe")

            def plan(self, *args: Any, **kwargs: Any) -> tuple[PipelineStep, ...]:
                return (PipelineStep(action="wipe", tool="developer.wipe_disk"),)

            async def execute(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
                raise AssertionError("an undeclared tool must never execute")

        run = await SpecialistPipeline().run(Sneaky(), AgentTask("wipe the disk"))

        self.assertEqual(run.stage(StageName.TOOL_SELECTION).status, StageStatus.FAILED)
        self.assertIn("not registered", run.stage(StageName.TOOL_SELECTION).detail)
        # Recorded as a refusal, never attempted: the stub's execute() would have
        # raised AssertionError if the step had been dispatched at all.
        self.assertEqual([outcome.status for outcome in run.outcomes], [StepStatus.DENIED])
        # The stage that considered the refusal says so. Falling through to the
        # end-of-run fill would report "the run ended before this stage", which
        # is a different fact from "this step was turned down".
        execution = run.stage(StageName.EXECUTION)
        assert execution is not None
        self.assertEqual(execution.status, StageStatus.DENIED)
        self.assertIn("not registered", execution.detail)
        self.assertEqual(run.status.value, "failed")

    async def test_declared_permissions_are_what_the_risk_layer_reads(self) -> None:
        permissions = PermissionManager()
        with tempfile.TemporaryDirectory() as temp:
            agent = build_developer(write_workspace(Path(temp)), permissions=permissions)

            await agent.run_task("run the tests")

        declared = permissions.declared_for("developer.run_tests")
        self.assertIsNotNone(declared)
        assert declared is not None
        self.assertEqual(declared.risk_level, RiskLevel.HIGH)
        self.assertEqual(permissions.risk_of("developer.run_tests"), RiskLevel.HIGH)
        self.assertIn("developer.apply_changes", permissions.declared())

    async def test_the_run_publishes_the_typed_lifecycle_vocabulary(self) -> None:
        bus = EventBus()
        watched = (
            EventType.TASK_STARTED,
            EventType.INTENT_DETECTED,
            EventType.CONTEXT_RESOLVED,
            EventType.DECISION_CREATED,
            EventType.PLAN_CREATED,
            EventType.TOOL_SELECTED,
            EventType.TOOL_STARTED,
            EventType.TOOL_COMPLETED,
            EventType.VERIFICATION_STARTED,
            EventType.VERIFICATION_COMPLETED,
            EventType.TASK_COMPLETED,
        )
        seen: list[str] = []

        async def capture(event: Any) -> None:
            seen.append(str(event.type))

        for event_type in watched:
            await bus.subscribe(event_type.value, capture)
        agent = ResearchAgent(
            provider=StubProvider(), pipeline=SpecialistPipeline(event_bus=bus)
        )

        run = await agent.run_task("compare widget timeouts under load")

        indexes = [seen.index(event_type.value) for event_type in watched]
        self.assertEqual(indexes, sorted(indexes), f"events arrived out of order: {seen}")
        self.assertEqual(seen[-1], EventType.TASK_COMPLETED.value)
        self.assertEqual(run.errors, [])
        self.assertEqual(run.events[0], EventType.TASK_STARTED.value)

    async def test_a_failed_run_announces_failure(self) -> None:
        bus = EventBus()
        seen: list[str] = []

        async def capture(event: Any) -> None:
            seen.append(str(event.type))

        await bus.subscribe(EventType.TASK_FAILED.value, capture)
        with tempfile.TemporaryDirectory() as temp:
            agent = build_developer(write_workspace(Path(temp)), event_bus=bus)

            run = await agent.run_task("run the tests")

        self.assertEqual(run.status.value, "failed")
        self.assertEqual(seen, [EventType.TASK_FAILED.value])

    async def test_unverified_steps_are_not_reported_as_success(self) -> None:
        class Empty(SpecialistAgent):
            def __init__(self) -> None:
                super().__init__("empty-agent", AgentRole.CODING, "Produces nothing.")

            def clone(self, pipeline: SpecialistPipeline) -> SpecialistAgent:
                return Empty()

            def declarations(self) -> Mapping[str, PermissionDeclaration]:
                return {"empty.read": PermissionDeclaration()}

            def decide(self, *args: Any, **kwargs: Any) -> SpecialistDecision:
                return SpecialistDecision(action="read")

            def plan(self, *args: Any, **kwargs: Any) -> tuple[PipelineStep, ...]:
                return (PipelineStep(action="read", tool="empty.read", expected="A result"),)

            async def execute(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
                return StepOutcome(step_id=step.id, action=step.action, status=StepStatus.COMPLETED)

        run = await SpecialistPipeline().run(Empty(), AgentTask("read something"))

        self.assertEqual(run.stage(StageName.VERIFICATION).status, StageStatus.FAILED)
        self.assertFalse(run.verified)
        self.assertEqual(run.status.value, "failed")
        step_id = run.outcomes[0].step_id
        self.assertIn("produced nothing to check", run.verifications[step_id]["reason"])


class DeveloperRoutingTests(unittest.IsolatedAsyncioTestCase):
    """12.1 — a developer task reaches the developer agent, and the right action."""

    async def test_the_coordinator_routes_coding_work_to_the_developer(self) -> None:
        registry = AgentRegistry()
        with tempfile.TemporaryDirectory() as temp:
            workspace = write_workspace(Path(temp))
            registry.register(build_developer(workspace, approvals=AllowGateway()))

        response = await CoordinatorAgent().delegate(
            AgentTask("fix the authentication issue"), registry
        )

        self.assertEqual(response.role, AgentRole.CODING)
        self.assertEqual(response.agent_name, "developer-agent")

    async def test_goal_phrasing_selects_the_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            agent = build_developer(root, approvals=AllowGateway())
            cases = (
                ("inspect this repository", "inspect"),
                ("explain the project structure", "inspect"),
                ("search for widget_timeout", "search"),
                ("inspect the open errors", "errors"),
                ("why does the widget test fail", "debug"),
                ("debug the widget timeout", "debug"),
                ("git status", "git"),
                ("git commit these changes", "git_write"),
                ("fix the widget timeout", "plan_change"),
                ("run the tests", "run_tests"),
            )
            for goal, expected in cases:
                with self.subTest(goal=goal):
                    decision = agent.decide(AgentTask(goal), _interpretation(goal), {})
                    self.assertEqual(decision.action, expected)
                    self.assertTrue(decision.reason)

    async def test_apply_is_downgraded_without_a_proposed_change_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            without_builder = build_developer(root, approvals=AllowGateway())

            downgraded = without_builder.decide(
                AgentTask("apply the widget timeout fix"), _interpretation(), {}
            )
            with_builder = build_developer(root, approvals=AllowGateway(), builder=note_builder)
            applied = with_builder.decide(
                AgentTask("apply the widget timeout fix"), _interpretation(), {}
            )

        self.assertEqual(downgraded.action, "plan_change")
        self.assertEqual(downgraded.data["downgraded_from"], "apply_change")
        self.assertEqual(applied.action, "apply_change")

    async def test_git_reads_are_allowed_but_git_writes_need_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            agent = build_developer(write_workspace(Path(temp)), approvals=AllowGateway())

            read = agent.decide(AgentTask("git diff"), _interpretation(), {})
            write = agent.decide(AgentTask("git commit the fix"), _interpretation(), {})

        self.assertEqual(read.action, "git")
        self.assertFalse(read.requires_approval)
        self.assertEqual(write.action, "git_write")
        self.assertTrue(write.requires_approval)

    async def test_the_specialist_replaces_the_placeholder_stub_for_its_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            app = NovaControlApplication(data_dir=temp)
            await app.start()
            try:
                coding = [agent.name for agent in app.agent_registry.by_role(AgentRole.CODING)]
                research = [agent.name for agent in app.agent_registry.by_role(AgentRole.RESEARCH)]
                # A role nobody specializes keeps its deterministic stub.
                testing = [agent.name for agent in app.agent_registry.by_role(AgentRole.TESTING)]
                delegated = await app.coordinator.delegate(
                    AgentTask("create tests for this module"), app.agent_registry
                )
            finally:
                await app.stop()

        self.assertEqual(coding, ["developer-agent"])
        self.assertEqual(research, ["research-agent"])
        self.assertEqual(testing, ["testing-agent"])
        self.assertEqual(delegated.role, AgentRole.TESTING)

    async def test_the_application_runs_the_developer_agent_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            app = NovaControlApplication(data_dir=temp)
            await app.start()
            try:
                report = await app.developer_task("inspect this repository")
                refused = await app.developer_task("run the tests")
                declared = app.specialist_agents()
            finally:
                await app.stop()

        self.assertEqual(report["status"], "completed")
        self.assertEqual(
            [entry["stage"] for entry in report["stages"]], [stage.value for stage in STAGE_ORDER]
        )
        self.assertEqual(refused["status"], "failed")
        self.assertIn(
            "developer.run_tests",
            [grant["tool"] for grant in refused["tools"] if not grant["allowed"]],
        )
        self.assertEqual(declared["stages"], [stage.value for stage in STAGE_ORDER])
        self.assertEqual(
            [entry["agent"] for entry in declared["agents"]], ["developer-agent", "research-agent"]
        )


class DeveloperContextTests(unittest.IsolatedAsyncioTestCase):
    """12.1 — the Context stage, and the no-blind-writes promise."""

    async def test_the_context_stage_carries_the_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            agent = build_developer(root, approvals=AllowGateway())

            run = await agent.run_task("inspect this repository")

        project = run.context["project"]
        self.assertEqual(Path(project["root"]), root.resolve())
        self.assertEqual(project["branch"], "feature/widget-timeout")
        self.assertEqual(project["repository"], "https://example.invalid/widgets.git")
        self.assertEqual(project["language"], "python")
        self.assertEqual(project["name"], root.name)
        self.assertTrue(project["recent_files"])
        self.assertEqual(Path(run.context["root"]), root.resolve())
        self.assertEqual(run.stage(StageName.CONTEXT).status, StageStatus.OK)
        self.assertEqual(run.stage(StageName.CONTEXT).data["project"]["language"], "python")

    async def test_a_read_only_run_never_touches_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            before = tree_fingerprint(root)
            agent = build_developer(root, approvals=AllowGateway())

            for goal in ("inspect this repository", "search for widget_timeout", "git status"):
                run = await agent.run_task(goal)
                self.assertEqual(run.status.value, "completed", run.summary)

            after = tree_fingerprint(root)

        self.assertEqual(before, after)

    async def test_the_report_carries_the_profile_and_the_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            agent = build_developer(root, approvals=AllowGateway())

            run = await agent.run_task("inspect this repository")

        profile = run.report["profile"]
        self.assertEqual(profile["source_files"], 2)
        self.assertEqual(profile["test_files"], 1)
        self.assertGreater(profile["python_lines"], 0)
        self.assertIn("src", run.report["structure"]["directories"])
        self.assertEqual(run.report["structure"]["packages"], ["widgets"])
        self.assertEqual(run.report["structure"]["modules"], [])

    async def test_a_search_reports_real_lines_of_real_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            agent = build_developer(root, approvals=AllowGateway())

            run = await agent.run_task("search for widget_timeout")

        self.assertEqual(run.decision["action"], "search")
        matches = run.report["search"]["matches"]
        self.assertTrue(matches)
        self.assertIn("src/widgets/core.py", {match["path"] for match in matches})
        line = next(match for match in matches if match["path"] == "src/widgets/core.py")
        self.assertIn("widget_timeout", line["text"])
        self.assertEqual(line["line"], 4)

    async def test_a_search_is_bounded_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            (root / "extra.py").write_text("widget_timeout = 1\n", encoding="utf-8")
            result = search_code(root, "widget_timeout", max_files=1)

        self.assertLessEqual(result.files_scanned, 1)
        self.assertTrue(result.truncated)

    async def test_open_errors_come_from_the_bug_log(self) -> None:
        class BugLog:
            def all(self, *, include_fixed: bool = True) -> tuple[Any, ...]:
                return (SimpleNamespace(title="widget timeout overflow"),)

        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            agent = build_developer(root, approvals=AllowGateway(), bug_log=BugLog())

            run = await agent.run_task("inspect the open errors")

        self.assertEqual(run.decision["action"], "errors")
        self.assertIn("widget timeout overflow", run.report["errors"]["errors"])

    async def test_an_unreadable_bug_log_costs_only_the_errors(self) -> None:
        class BrokenLog:
            def all(self, *, include_fixed: bool = True) -> tuple[Any, ...]:
                raise OSError("log is locked")

        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            agent = build_developer(root, approvals=AllowGateway(), bug_log=BrokenLog())

            run = await agent.run_task("inspect the open errors")

            self.assertEqual(agent.open_errors(), ())

        self.assertEqual(run.status.value, "completed")
        self.assertEqual(run.report["errors"]["errors"], [])


class TestExecutionTests(unittest.IsolatedAsyncioTestCase):
    """12.1 — running tests, and reading what the run actually said."""

    async def test_a_green_pytest_run_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            runner = FakeRunner((0, PYTEST_GREEN))
            agent = build_developer(root, runner=runner, approvals=AllowGateway())

            run = await agent.run_task("run the tests")
            recognised = agent.test_command

        self.assertEqual(run.status.value, "completed")
        # The runner is RECOGNISED, not assumed: pytest from the interpreter that
        # owns this process, over the project's own test directory.
        self.assertEqual(runner.calls, [recognised])
        self.assertEqual(recognised[1:3], ("-m", "pytest"))
        self.assertEqual(recognised[-1], "tests")
        tests = run.report["tests"]
        self.assertEqual(tests["passed"], 3)
        self.assertEqual(tests["skipped"], 1)
        self.assertTrue(tests["ok"])
        self.assertTrue(run.verified)

    async def test_a_red_run_fails_the_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            runner = FakeRunner((1, PYTEST_RED))
            agent = build_developer(root, runner=runner, approvals=AllowGateway())

            run = await agent.run_task("run the tests")

        self.assertEqual(run.status.value, "failed")
        self.assertEqual(run.report["tests"]["failed"], 1)
        self.assertFalse(run.verified)

    async def test_a_run_without_a_tally_is_not_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            runner = FakeRunner((0, "collected 0 items\n"))
            agent = build_developer(root, runner=runner, approvals=AllowGateway())

            run = await agent.run_task("run the tests")

        self.assertFalse(run.report["tests"]["recognised"])
        self.assertEqual(run.status.value, "failed")

    async def test_debugging_wants_the_failure_reproduced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            runner = FakeRunner((1, PYTEST_RED))
            agent = build_developer(root, runner=runner, approvals=AllowGateway())

            run = await agent.run_task("debug why the widget test fails")

        self.assertEqual(run.decision["action"], "debug")
        self.assertEqual(run.status.value, "completed")
        diagnosis = run.report["diagnosis"]
        self.assertEqual(diagnosis["kind"], "assertion")
        self.assertIn("assert 60 == 30", diagnosis["message"])
        self.assertIn("test_widgets.py", diagnosis["file"])
        self.assertEqual(diagnosis["line"], 5)

    async def test_debugging_reports_when_the_failure_does_not_reproduce(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            runner = FakeRunner((0, PYTEST_GREEN))
            agent = build_developer(root, runner=runner, approvals=AllowGateway())

            run = await agent.run_task("debug why the widget test fails")

        self.assertEqual(run.status.value, "failed")
        reasons = " ".join(entry["reason"] for entry in run.verifications.values())
        self.assertIn("was not reproduced", reasons)

    async def test_the_configured_command_is_the_one_that_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            runner = FakeRunner((0, UNITTEST_GREEN))
            agent = build_developer(root, runner=runner, approvals=AllowGateway())
            agent.test_command = ("python", "-m", "unittest", "discover")

            run = await agent.run_task("run the tests")

        self.assertEqual(runner.calls[0], ("python", "-m", "unittest", "discover"))
        self.assertEqual(run.report["tests"]["passed"], 4)
        self.assertEqual(run.report["tests"]["skipped"], 1)
        self.assertEqual(run.status.value, "completed")

    def test_unittest_and_pytest_tallies_are_both_understood(self) -> None:
        self.assertTrue(parse_test_output(_result(0, PYTEST_GREEN)).ok)
        self.assertTrue(parse_test_output(_result(0, UNITTEST_GREEN)).ok)
        red = parse_test_output(_result(1, UNITTEST_RED))
        self.assertEqual((red.failed, red.errors), (2, 1))
        self.assertFalse(red.ok)
        unrecognised = ParsedReport(command=("pytest",), returncode=0, recognised=False)
        self.assertFalse(unrecognised.ok)

    def test_failure_signatures_are_named_not_guessed(self) -> None:
        missing = diagnose_failure("ModuleNotFoundError: No module named 'widgets'")
        self.assertEqual(missing["kind"], "missing_dependency")
        self.assertIn("widgets", missing["message"])
        self.assertEqual(diagnose_failure("SyntaxError: invalid syntax")["kind"], "syntax_error")
        self.assertEqual(diagnose_failure("everything is fine")["kind"], "unknown")


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    """12.1 — recover, or say why not."""

    async def test_a_transient_failure_is_retried_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            runner = FakeRunner((1, PYTEST_RED), (0, PYTEST_GREEN))
            agent = build_developer(root, runner=runner, approvals=AllowGateway())

            run = await agent.run_task("run the tests")

        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(len(run.recoveries), 1)
        self.assertEqual(run.recoveries[0]["strategy"], "retry")
        self.assertEqual(run.status.value, "completed")

    async def test_a_missing_dependency_aborts_instead_of_retrying(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            runner = FakeRunner((1, "ModuleNotFoundError: No module named 'widgets'"))
            agent = build_developer(root, runner=runner, approvals=AllowGateway())

            run = await agent.run_task("run the tests")

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(run.recoveries[0]["strategy"], "abort")
        self.assertIn("cannot install it", run.recoveries[0]["text"])
        self.assertEqual(run.status.value, "failed")

    async def test_the_retry_budget_is_finite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            runner = FakeRunner((1, PYTEST_RED))
            agent = build_developer(
                root, runner=runner, approvals=AllowGateway(), max_recovery_attempts=1
            )

            run = await agent.run_task("run the tests")

        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(run.stage(StageName.RECOVERY).status, StageStatus.FAILED)
        self.assertIn("retry budget exhausted", run.stage(StageName.RECOVERY).detail)

    async def test_an_unrecoverable_step_records_a_failed_recovery_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            runner = FakeRunner((1, PYTEST_RED))
            agent = build_developer(
                root, runner=runner, approvals=AllowGateway(), max_recovery_attempts=0
            )

            run = await agent.run_task("run the tests")

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(run.stage(StageName.RECOVERY).status, StageStatus.FAILED)
        self.assertEqual(run.status.value, "failed")

    async def test_a_failed_write_is_never_retried(self) -> None:
        escaping = (
            CodeChange(
                relative_path="../escaped.py",
                content="print('outside')\n",
                description="Escape the project root",
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            agent = build_developer(
                root, approvals=AllowGateway(), builder=lambda goal: escaping
            )

            run = await agent.run_task("apply the widget timeout fix")
            escaped = (Path(temp).parent / "escaped.py").exists()

        applied = [outcome for outcome in run.outcomes if outcome.action == "apply_changes"]
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0].status, StepStatus.FAILED)
        self.assertIn("escapes project root", applied[0].detail)
        self.assertEqual(run.recoveries[-1]["strategy"], "abort")
        self.assertFalse(escaped)
        self.assertEqual(run.status.value, "failed")


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    """12.2 — research task routing and source handling."""

    async def test_the_coordinator_routes_research_work_to_the_research_agent(self) -> None:
        registry = AgentRegistry()
        registry.register(build_research(), prefer=True)

        response = await CoordinatorAgent().delegate(
            AgentTask("research vector database indexing"), registry
        )

        self.assertEqual(response.role, AgentRole.RESEARCH)
        self.assertEqual(response.agent_name, "research-agent")

    async def test_question_phrasing_selects_the_action(self) -> None:
        agent = build_research()
        cases = (
            ("research vector database indexing", "research"),
            ("compare postgres and sqlite for this project", "compare"),
            ("explain this offline only", "local"),
        )
        for goal, expected in cases:
            with self.subTest(goal=goal):
                decision = agent.decide(AgentTask(goal), _interpretation(goal), {})
                self.assertEqual(decision.action, expected)
                self.assertTrue(decision.reason)

    async def test_the_web_path_uses_the_existing_research_engine(self) -> None:
        provider = StubProvider()
        agent = build_research(provider=provider, approvals=AllowGateway())

        run = await agent.run_task("compare widget timeouts under load")

        self.assertEqual(provider.calls, ["compare widget timeouts under load"])
        self.assertEqual(run.status.value, "completed")
        self.assertEqual(len(run.report["sources"]), 2)
        stages = [record.stage.value for record in run.stages]
        self.assertEqual(stages, [stage.value for stage in STAGE_ORDER])
        self.assertIn("compare_sources", [outcome.action for outcome in run.outcomes])
        self.assertEqual(run.report["provider_status"], "stub")

    async def test_the_local_path_never_reaches_the_network(self) -> None:
        provider = StubProvider()
        agent = build_research(provider=provider, allow_web=False)

        run = await agent.run_task("research widget timeout behaviour")

        self.assertEqual(provider.calls, [])
        self.assertEqual(run.decision["action"], "local")
        self.assertNotIn("search_sources", [outcome.action for outcome in run.outcomes])
        self.assertTrue(run.report["local_only"])
        self.assertIn("local knowledge", run.summary)

    async def test_a_provider_failure_is_reported_not_swallowed(self) -> None:
        agent = build_research(provider=StubProvider(fail=True), approvals=AllowGateway())

        run = await agent.run_task("research widget timeouts")

        self.assertEqual(run.status.value, "failed")
        failed = next(outcome for outcome in run.outcomes if outcome.action == "search_sources")
        self.assertEqual(failed.status, StepStatus.FAILED)
        self.assertIn("research provider failed", failed.detail)
        self.assertIn("OSError", failed.detail)

    async def test_sources_are_compared_and_agreement_is_reported(self) -> None:
        agent = build_research(approvals=AllowGateway())

        run = await agent.run_task("compare widget timeouts under load")

        agreement = float(run.report["agreement"])
        self.assertGreaterEqual(agreement, 0.0)
        self.assertLessEqual(agreement, 1.0)
        compared = next(outcome for outcome in run.outcomes if outcome.action == "compare_sources")
        self.assertEqual(len(compared.output["sources"]), 2)
        self.assertTrue(all(entry["claims"] for entry in compared.output["sources"]))

    async def test_a_research_run_with_no_sources_fails_honestly(self) -> None:
        agent = build_research(provider=StubProvider(sources=()), approvals=AllowGateway())

        run = await agent.run_task("research widget timeouts")

        self.assertEqual(run.status.value, "failed")
        details = " ".join(outcome.detail for outcome in run.outcomes)
        self.assertIn("no source was returned", details)

    async def test_the_report_separates_evidence_from_inference(self) -> None:
        agent = build_research(approvals=AllowGateway())

        run = await agent.run_task("compare widget timeouts under load")

        claims = run.report["claims"]
        evidence = [claim for claim in claims if claim["kind"] == "evidence"]
        inference = [claim for claim in claims if claim["kind"] == "inference"]
        self.assertTrue(evidence)
        self.assertTrue(inference)
        self.assertEqual(run.report["evidence_count"], len(evidence))
        self.assertEqual(run.report["inference_count"], len(inference))
        for claim in evidence:
            self.assertTrue(claim["citations"], f"evidence without a citation: {claim}")
            self.assertFalse(claim["basis"])
        for claim in inference:
            self.assertTrue(claim["basis"], f"inference without a basis: {claim}")
            self.assertFalse(claim["citations"])

    async def test_conflicting_claims_are_found_between_sources(self) -> None:
        agent = build_research(approvals=AllowGateway())

        run = await agent.run_task("compare widget timeouts under load")

        conflicts = run.report["conflicts"]
        self.assertTrue(conflicts)
        self.assertEqual({conflict["kind"] for conflict in conflicts}, {"antonym"})
        self.assertEqual({conflicts[0]["left_source"], conflicts[0]["right_source"]}, {1, 2})
        self.assertIn("Conflicting claims:", run.report["answer"])

    def test_sources_that_agree_are_not_reported_as_conflicting(self) -> None:
        left = Claim("The widget timeout is 30 seconds.", ClaimKind.EVIDENCE, (1,))
        agree = Claim("The widget timeout is 30 seconds by default.", ClaimKind.EVIDENCE, (2,))
        differ = Claim("The widget timeout is 5 seconds.", ClaimKind.EVIDENCE, (2,))
        unrelated = Claim("Dogs need daily walks in the park.", ClaimKind.EVIDENCE, (2,))
        self.assertEqual(detect_conflicts([left, agree]), ())
        self.assertEqual(len(detect_conflicts([left, differ])), 1)
        self.assertEqual(detect_conflicts([left, unrelated]), ())

    def test_each_conflict_kind_is_recognised(self) -> None:
        figure = detect_conflicts(
            [
                Claim("Latency stays around 30 ms per request.", ClaimKind.EVIDENCE, (1,)),
                Claim("Latency stays around 80 ms per request.", ClaimKind.EVIDENCE, (2,)),
            ]
        )
        negation = detect_conflicts(
            [
                Claim("Widgets require a dedicated pool.", ClaimKind.EVIDENCE, (1,)),
                Claim("Widgets never require a dedicated pool.", ClaimKind.EVIDENCE, (2,)),
            ]
        )
        antonym = detect_conflicts(
            [
                Claim("HNSW indexing increases query speed.", ClaimKind.EVIDENCE, (1,)),
                Claim(
                    "HNSW indexing decreases query speed for very large collections.",
                    ClaimKind.EVIDENCE,
                    (2,),
                ),
            ]
        )
        self.assertEqual(figure[0].kind, "figure")
        self.assertEqual(negation[0].kind, "negation")
        self.assertEqual(antonym[0].kind, "antonym")

    def test_one_source_does_not_conflict_with_itself(self) -> None:
        same = [
            Claim("Widgets increase throughput under load.", ClaimKind.EVIDENCE, (1,)),
            Claim("Widgets decrease throughput under load.", ClaimKind.EVIDENCE, (1,)),
        ]
        self.assertEqual(detect_conflicts(same), ())

    def test_only_evidence_claims_can_conflict(self) -> None:
        pair = [
            Claim("Widgets increase throughput under load.", ClaimKind.INFERENCE, (1,)),
            Claim("Widgets decrease throughput under load.", ClaimKind.EVIDENCE, (2,)),
        ]
        self.assertEqual(detect_conflicts(pair), ())


class CitationTests(unittest.IsolatedAsyncioTestCase):
    """12.2 — citations survive synthesis, and inference is labelled as such."""

    async def test_every_citation_index_resolves_to_a_gathered_source(self) -> None:
        agent = build_research(approvals=AllowGateway())

        run = await agent.run_task("compare widget timeouts under load")

        indexes = {entry["index"] for entry in run.report["sources"]}
        self.assertEqual(indexes, {1, 2})
        for claim in run.report["claims"]:
            for index in claim["citations"]:
                self.assertIn(index, indexes)
        self.assertTrue(run.report["citations_verified"])

    async def test_the_answer_marks_claims_with_their_sources(self) -> None:
        agent = build_research(approvals=AllowGateway())

        run = await agent.run_task("compare widget timeouts under load")

        answer = run.report["answer"]
        self.assertIn("[1]", answer)
        self.assertIn("Sources:", answer)
        self.assertIn("https://example.invalid/guide", answer)

    async def test_source_titles_and_urls_are_preserved_verbatim(self) -> None:
        agent = build_research(approvals=AllowGateway())

        run = await agent.run_task("research widget timeouts")

        self.assertEqual(
            [entry["title"] for entry in run.report["sources"]],
            ["Widget Guide", "Widget Benchmarks"],
        )
        self.assertEqual(
            [entry["url"] for entry in run.report["sources"]],
            ["https://example.invalid/guide", "https://example.invalid/benchmarks"],
        )

    async def test_a_forged_citation_index_fails_verification(self) -> None:
        class Tampering(ResearchAgent):
            def _evidence_claims(self, run: AgentRun) -> list[Claim]:
                forged = Claim(
                    text="Widgets are always cached.",
                    kind=ClaimKind.EVIDENCE,
                    citations=(99,),
                    id="forged-1",
                )
                return [*super()._evidence_claims(run), forged]  # noqa: SLF001

        agent = Tampering(
            provider=StubProvider(), pipeline=SpecialistPipeline(approvals=AllowGateway())
        )

        run = await agent.run_task("compare widget timeouts under load")

        self.assertFalse(run.report["citations_verified"])
        self.assertEqual(run.status.value, "failed")
        unresolved = next(
            outcome for outcome in run.outcomes if outcome.action == "verify_citations"
        ).output["unresolved"]
        self.assertEqual(unresolved[0]["reason"], "no such source")
        self.assertEqual(unresolved[0]["citation"], 99)

    async def test_a_local_answer_cites_the_local_index_or_fails_honestly(self) -> None:
        agent = build_research(provider=StubProvider(), allow_web=False)

        run = await agent.run_task("research widget timeouts")

        # No local knowledge is indexed in this build, so the honest answer is a
        # failed run with the reason — not an answer invented from nothing.
        self.assertTrue(run.report["local_only"])
        self.assertEqual(run.status.value, "failed")
        self.assertEqual(run.report["sources"], [])


class PermissionTests(unittest.IsolatedAsyncioTestCase):
    """12.1/12.2 — nothing writes or executes without authorization."""

    async def test_writes_and_tests_are_refused_without_an_approval_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            before = tree_fingerprint(root)
            runner = FakeRunner()
            agent = build_developer(root, runner=runner, builder=note_builder)

            run = await agent.run_task("apply the widget timeout fix")

            after = tree_fingerprint(root)

        self.assertEqual(before, after)
        self.assertEqual(runner.calls, [])
        self.assertEqual(run.status.value, "failed")
        denied = {grant.tool: grant for grant in run.denied}
        self.assertEqual(set(denied), {"developer.apply_changes", "developer.run_tests"})
        self.assertEqual(denied["developer.apply_changes"].risk, RiskLevel.HIGH)
        self.assertEqual(denied["developer.apply_changes"].source, "declared")
        self.assertIn("authorized", run.summary)
        self.assertIn("cannot be undone", denied["developer.apply_changes"].reason)
        self.assertFalse(run.report["wrote_files"])

    async def test_an_authorized_run_writes_tests_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            runner = FakeRunner((0, PYTEST_GREEN))
            agent = build_developer(
                root, runner=runner, approvals=AllowGateway(), builder=note_builder
            )

            run = await agent.run_task("apply the widget timeout fix")

            written = root / "notes" / "change.md"
            content = written.read_text(encoding="utf-8") if written.is_file() else ""
            recognised = agent.test_command

        self.assertEqual(run.decision["action"], "apply_change")
        self.assertEqual(run.status.value, "completed")
        self.assertEqual(content, "planned: apply the widget timeout fix\n")
        self.assertEqual(runner.calls, [recognised])
        self.assertTrue(run.report["verification"]["verified"])
        self.assertTrue(run.report["wrote_files"])
        self.assertEqual(run.report["changes"]["applied"], 1)

    async def test_the_write_path_asks_for_approval_once_per_step(self) -> None:
        asked: list[str] = []

        class CountingGateway:
            async def request_approval(self, request: Any) -> ApprovalDecision:
                asked.append(str(request.action))
                return ApprovalDecision(str(request.id), approved=True, decided_by="test")

        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            agent = build_developer(
                root,
                runner=FakeRunner((0, PYTEST_GREEN)),
                approvals=CountingGateway(),
                builder=note_builder,
            )

            run = await agent.run_task("apply the widget timeout fix")

        self.assertEqual(run.status.value, "completed")
        self.assertEqual(len(asked), 2)
        self.assertTrue(any("Write the proposed changes" in action for action in asked))
        self.assertTrue(any("Run the project's tests" in action for action in asked))

    async def test_a_stricter_policy_can_refuse_the_web_search(self) -> None:
        policy = PermissionPolicy(confirm_at=frozenset(RiskLevel))
        permissions = PermissionManager(policy=policy)
        provider = StubProvider()
        agent = build_research(provider=provider, permissions=permissions)

        run = await agent.run_task("research widget timeouts")

        self.assertEqual(provider.calls, [])
        self.assertEqual(run.status.value, "failed")
        # Every step of a research run needs confirmation under this policy, and
        # the first thing it asks about is the network.
        self.assertEqual(run.denied[0].tool, "research.search_web")
        self.assertEqual(run.denied[0].to_dict()["permissions"], ["network:access"])
        self.assertTrue(all(not grant.allowed for grant in run.denied))

    async def test_network_access_is_declared_for_web_research(self) -> None:
        declaration = build_research().declarations()["research.search_web"]

        self.assertEqual(declaration.required_permission.value, "network:access")
        self.assertEqual(declaration.risk_level, RiskLevel.LOW)
        self.assertFalse(declaration.destructive)
        self.assertTrue(declaration.reversible)

    async def test_git_writes_are_refused_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            runner = FakeRunner()
            agent = build_developer(root, runner=runner)

            run = await agent.run_task("git commit the widget fix")

        self.assertEqual(runner.calls, [])
        self.assertEqual(run.status.value, "failed")
        self.assertEqual([grant.tool for grant in run.denied], ["developer.git_write"])

    async def test_git_reads_run_without_an_approval_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            runner = FakeRunner((0, "## main\n"))
            agent = build_developer(root, runner=runner)

            run = await agent.run_task("git status")

        self.assertEqual(runner.calls, [("git", "status")])
        self.assertEqual(run.status.value, "completed")

    async def test_an_approval_flow_that_raises_is_a_refusal(self) -> None:
        class BrokenGateway:
            async def request_approval(self, request: Any) -> ApprovalDecision:
                raise RuntimeError("approval service is down")

        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            runner = FakeRunner()
            agent = build_developer(
                root, runner=runner, approvals=BrokenGateway(), builder=note_builder
            )

            run = await agent.run_task("apply the widget timeout fix")
            written = (root / "notes" / "change.md").exists()

        self.assertEqual(runner.calls, [])
        self.assertFalse(written)
        self.assertEqual(run.status.value, "failed")
        self.assertFalse(run.report["wrote_files"])

    async def test_a_clone_keeps_the_work_but_gets_its_own_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = write_workspace(Path(temp))
            agent = build_developer(
                root, runner=FakeRunner((0, PYTEST_GREEN)), builder=note_builder
            )
            authorized = agent.clone(SpecialistPipeline(approvals=AllowGateway()))

            refused = await agent.run_task("apply the widget timeout fix")
            allowed = await authorized.run_task("apply the widget timeout fix")

        self.assertEqual(refused.status.value, "failed")
        self.assertEqual(allowed.status.value, "completed")
        self.assertIsNot(agent.pipeline, authorized.pipeline)
        self.assertEqual(authorized.root, agent.root)
        self.assertEqual(refused.report["root"], allowed.report["root"])


if __name__ == "__main__":
    unittest.main()
