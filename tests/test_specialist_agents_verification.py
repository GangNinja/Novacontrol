"""Phase 12 — verified clause by clause against the phase's own specification.

`tests/test_specialist_agents.py` covers the nine areas the phase lists under
"PHASE 12 TESTS". This file goes the other way: it walks the *requirements* text
and drives each clause through the real specialists, so a clause that is only
true of the test doubles — or true only because nothing exercised it — is caught
here.

The phase states two constraints that are easy to satisfy on paper and hard to
satisfy in fact, so they are asserted everywhere:

* **Do NOT create separate independent agent frameworks.** Every specialist must
  run the one pipeline, in the one stage order, through the components that
  already exist. The stage tests below assert the *mechanics* of each stage —
  that NLU really is the interpreter, that Tool Selection really is the risk
  layer, that Verification really is the Phase 8 vocabulary — not just that a
  stage named "nlu" appears in a list.
* **Never claim success without verification.** Both directions: a run that
  nothing verified is FAILED, and a run reported COMPLETED has a PASS behind it.

The clausal tests are grouped by the numbering in the phase text:

    12.1 developer agent      -> DeveloperResponsibilityTests, GitOperationTests
    12.2 research agent       -> ResearchResponsibilityTests
    the shared pipeline       -> SharedPipelineTests, StageVocabularyTests
    the phase's two rules     -> SafetyTests

Every defect this pass found has a regression test here, named after what broke:
a git command's arguments were silently discarded, a branch deletion was
classified as a read, a skipped step was reported as a verification failure, "is
X faster than Y?" was not read as a comparison, a retry was spent on a step whose
inputs cannot change, the test command was a hard-coded pytest inside whatever
project the agent happened to be standing in, an agent's own refusal was
presented as the diagnosis of a failure, and a refused step was retried.
"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from conftest import AllowGateway
from novacontrol.agentcore.recovery import RecoveryStrategy
from novacontrol.agentcore.verifier import VerificationStatus
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
from novacontrol.agents.base import build_default_agents
from novacontrol.agents.developer import (
    CommandResult,
    diagnose_failure,
    git_is_mutating,
    read_git_command,
    recognise_test_command,
    search_code,
)
from novacontrol.agents.registry import AgentRegistry
from novacontrol.agents.research import detect_conflicts
from novacontrol.application import NovaControlApplication
from novacontrol.core.events import EventBus, EventType
from novacontrol.core.security import ApprovalDecision, DenyByDefaultApprovalGateway
from novacontrol.explore.models import ResearchSource
from novacontrol.reliability.permissions import PermissionDeclaration
from novacontrol.self_improvement import CodeChange

#: The phase's stage list, written the way the specification spells it. The
#: pipeline's own order is checked against this, so the contract is the phase's
#: words rather than whatever the enum happens to contain.
SPEC_STAGES: tuple[tuple[str, StageName], ...] = (
    ("NLU", StageName.NLU),
    ("Context", StageName.CONTEXT),
    ("Decision", StageName.DECISION),
    ("Planner", StageName.PLANNER),
    ("Tool Selection", StageName.TOOL_SELECTION),
    ("Execution", StageName.EXECUTION),
    ("Verification", StageName.VERIFICATION),
    ("Recovery", StageName.RECOVERY),
)

PYTEST_GREEN = "5 passed, 1 skipped in 0.31s\n"
PYTEST_RED = (
    "tests/test_widgets.py::test_timeout FAILED\n"
    "Traceback (most recent call last):\n"
    '  File "tests/test_widgets.py", line 5, in test_timeout\n'
    "    assert widget_timeout() == 30\n"
    "E   AssertionError: assert 60 == 30\n"
    "1 failed, 4 passed in 0.44s\n"
)


class FakeRunner:
    """A command runner that records every argv it was asked to run."""

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


def workspace(root: Path, *, git: bool = True) -> Path:
    """A small project with sources, tests, a manifest and real git metadata."""
    (root / "src" / "widgets").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "src" / "widgets" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "widgets" / "core.py").write_text(
        "DEFAULT_TIMEOUT = 30\n\n\ndef widget_timeout() -> int:\n"
        '    """How long a widget request may take."""\n'
        "    return DEFAULT_TIMEOUT\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_widgets.py").write_text(
        "from widgets.core import widget_timeout\n\n\ndef test_timeout() -> None:\n"
        "    assert widget_timeout() == 30\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text("[project]\nname = 'widgets'\n", encoding="utf-8")
    if git:
        (root / ".git" / "refs" / "heads" / "feature").mkdir(parents=True, exist_ok=True)
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (root / ".git" / "config").write_text(
            '[remote "origin"]\n\turl = https://example.invalid/widgets.git\n', encoding="utf-8"
        )
        (root / ".git" / "refs" / "heads" / "main").write_text("0" * 40 + "\n", encoding="utf-8")
        (root / ".git" / "refs" / "heads" / "feature" / "widget-timeout").write_text(
            "0" * 40 + "\n", encoding="utf-8"
        )
        (root / ".git" / "packed-refs").write_text(
            "0" * 40 + " refs/remotes/origin/main\n", encoding="utf-8"
        )
    return root


def fingerprint(root: Path) -> dict[str, int]:
    """Every file's size, so a claimed read-only run can be checked."""
    return {
        str(path.relative_to(root)): path.stat().st_size
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def note_builder(goal: str) -> tuple[CodeChange, ...]:
    return (
        CodeChange(
            relative_path="notes/change.md",
            content=f"planned: {goal}\n",
            description="Record the planned change",
        ),
    )


def build_developer(  # noqa: PLR0913 - explicit knobs beat a parameter object here
    root: Path,
    *,
    runner: FakeRunner | None = None,
    approvals: Any = None,
    builder: Any = None,
    bug_log: Any = None,
    permissions: Any = None,
    event_bus: EventBus | None = None,
    knowledge: Any = None,
    tools: Any = None,
    max_recovery_attempts: int = 2,
) -> DeveloperAgent:
    pipeline = SpecialistPipeline(
        approvals=approvals if approvals is not None else DenyByDefaultApprovalGateway(),
        permissions=permissions,
        knowledge=knowledge,
        tools=tools,
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


AGREEING_SOURCES = (
    source(
        "Widget Guide",
        "https://example.invalid/guide",
        "The default widget timeout is 30 seconds. A widget request waits for a worker. "
        "The pool size governs how many widgets run at once.",
    ),
    source(
        "Widget Notes",
        "https://example.invalid/notes",
        "The default widget timeout is 30 seconds. A widget request waits for a worker. "
        "The pool size limits concurrency during a burst.",
    ),
)

DISAGREEING_SOURCES = (
    source(
        "Widget Guide",
        "https://example.invalid/guide",
        "The widget timeout increases to sixty seconds under load. "
        "Latency stays around 30 ms per request while the queue drains.",
    ),
    source(
        "Widget Benchmarks",
        "https://example.invalid/benchmarks",
        "The widget timeout decreases to five seconds under load. "
        "Latency stays around 80 ms per request while the queue drains.",
    ),
)


class StubProvider:
    """The research engine the application supplies (the real one is ExploreService)."""

    def __init__(self, sources: Any = AGREEING_SOURCES, *, fail: bool = False) -> None:
        self.sources = tuple(sources)
        self.fail = fail
        self.calls: list[str] = []

    async def research(self, request: Any) -> Any:
        self.calls.append(str(getattr(request, "topic", "")))
        if self.fail:
            raise OSError("the search provider is unreachable")
        return SimpleNamespace(
            topic=getattr(request, "topic", ""),
            overview="Widget timeouts trade headroom against queue depth.",
            answer="",
            key_points=("Timeouts are the first thing to tune.",),
            sources=self.sources,
            warnings=("cached result",),
            provider_status="stub",
        )


def build_research(
    *,
    provider: Any = None,
    approvals: Any = None,
    allow_web: bool = True,
    permissions: Any = None,
    knowledge: Any = None,
    event_bus: EventBus | None = None,
) -> ResearchAgent:
    return ResearchAgent(
        provider=provider if provider is not None else StubProvider(),
        pipeline=SpecialistPipeline(
            approvals=approvals if approvals is not None else DenyByDefaultApprovalGateway(),
            permissions=permissions,
            knowledge=knowledge,
            event_bus=event_bus,
        ),
        allow_web=allow_web,
    )


class KnowledgeEngine:
    """The Phase 11 knowledge engine, as the Context stage consumes it."""

    def __init__(self, text: str = "The widget timeout is 30 seconds by default.\n") -> None:
        self.text = text
        self.queries: list[str] = []

    async def context_for(self, question: str, *, active_task: str = "") -> Any:
        self.queries.append(question)
        return SimpleNamespace(
            text=self.text, tokens=len(self.text.split()), embedding_backend="bm25"
        )


class StageVocabularyTests(unittest.TestCase):
    """The phase's architecture clause: all agents use THE pipeline, in THE order."""

    def test_the_stage_order_is_the_specifications_order(self) -> None:
        self.assertEqual([stage for _, stage in SPEC_STAGES], list(STAGE_ORDER))
        self.assertEqual(
            [stage.value for stage in STAGE_ORDER],
            [
                "nlu",
                "context",
                "decision",
                "planner",
                "tool_selection",
                "execution",
                "verification",
                "recovery",
            ],
        )

    def test_both_specialists_declare_exactly_those_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            developer = build_developer(workspace(Path(temp)))
            research = build_research()
            declared = (
                developer.declares(),
                research.declares(),
                developer.declares()["stages"],
                research.declares()["stages"],
            )

        expected = [stage.value for stage in STAGE_ORDER]
        self.assertEqual(declared[0]["stages"], expected)
        self.assertEqual(declared[1]["stages"], expected)
        self.assertEqual(declared[2], expected)
        self.assertEqual(declared[3], expected)
        self.assertEqual(developer.stages, STAGE_ORDER)
        self.assertEqual(SpecialistAgent.stages, STAGE_ORDER)

    def test_the_application_installs_only_pipeline_specialists(self) -> None:
        import asyncio

        async def installed() -> list[tuple[str, list[str]]]:
            with tempfile.TemporaryDirectory() as temp:
                app = NovaControlApplication(data_dir=temp)
                await app.start()
                try:
                    return [
                        (agent.name, agent.declares()["stages"])
                        for agent in app.agent_registry.list()
                        if isinstance(agent, SpecialistAgent)
                    ]
                finally:
                    await app.stop()

        specialists = asyncio.run(installed())

        self.assertEqual([name for name, _ in specialists], ["developer-agent", "research-agent"])
        for name, stages in specialists:
            with self.subTest(agent=name):
                self.assertEqual(stages, [stage.value for stage in STAGE_ORDER])


class SharedPipelineTests(unittest.IsolatedAsyncioTestCase):
    """Each of the eight stages, checked for the mechanic it is supposed to be."""

    async def test_nlu_is_the_existing_task_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            agent = build_developer(workspace(Path(temp)))

            run = await agent.run_task("inspect this repository")

        self.assertEqual(run.interpretation["goal"], "inspect this repository")
        self.assertEqual(run.interpretation["subtasks"], ["inspect this repository"])
        self.assertIn("required_tools", run.interpretation)
        self.assertEqual(run.stage(StageName.NLU).status, StageStatus.OK)

    async def test_context_comes_from_the_phase_11_knowledge_engine(self) -> None:
        engine = KnowledgeEngine()
        with tempfile.TemporaryDirectory() as temp:
            agent = build_developer(workspace(Path(temp)), knowledge=engine)

            run = await agent.run_task("search for widget_timeout")

        # A search asks the engine for the *query*, not for the whole sentence.
        self.assertEqual(engine.queries, ["widget_timeout"])
        self.assertEqual(run.context["knowledge_tokens"], 8)
        self.assertIn("widget timeout", run.context_text.lower())
        self.assertEqual(run.stage(StageName.CONTEXT).data["query"], "widget_timeout")

    async def test_context_degrades_when_the_knowledge_engine_is_broken(self) -> None:
        class Broken:
            async def context_for(self, question: str, *, active_task: str = "") -> Any:
                raise RuntimeError("the index is still building")

        with tempfile.TemporaryDirectory() as temp:
            agent = build_developer(workspace(Path(temp)), knowledge=Broken())

            run = await agent.run_task("inspect this repository")

        self.assertEqual(run.status.value, "completed")
        self.assertEqual(run.stage(StageName.CONTEXT).status, StageStatus.SKIPPED)
        self.assertIn("the index is still building", " ".join(run.errors))

    async def test_tool_selection_is_the_centralized_permission_layer(self) -> None:
        from novacontrol.reliability.permissions import PermissionManager

        permissions = PermissionManager()
        with tempfile.TemporaryDirectory() as temp:
            agent = build_developer(workspace(Path(temp)), permissions=permissions)

            run = await agent.run_task("run the tests")

        # What the layer read is the declaration the agent made, not a guess.
        self.assertIsNotNone(permissions.declared_for("developer.run_tests"))
        self.assertEqual(run.decision["action"], "run_tests")
        self.assertEqual(len(run.grants), 1)
        self.assertEqual(run.grants[0].tool, "developer.run_tests")
        self.assertFalse(run.grants[0].allowed)
        self.assertEqual(run.grants[0].source, "declared")

    async def test_tool_selection_refuses_a_tool_the_build_does_not_have(self) -> None:
        catalog = SimpleNamespace(
            list=lambda: (SimpleNamespace(tool=SimpleNamespace(name="file.read")),)
        )

        class Absent(SpecialistAgent):
            def __init__(self) -> None:
                super().__init__("absent-agent", AgentRole.CODING, "Names a missing tool.")

            def clone(self, pipeline: SpecialistPipeline) -> SpecialistAgent:
                return Absent()

            def declarations(self) -> Mapping[str, PermissionDeclaration]:
                return {"absent.act": PermissionDeclaration()}

            def decide(self, *args: Any, **kwargs: Any) -> SpecialistDecision:
                return SpecialistDecision(action="act")

            def plan(self, *args: Any, **kwargs: Any) -> tuple[PipelineStep, ...]:
                return (PipelineStep(action="act", tool="ghost.tool"),)

            async def execute(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
                raise AssertionError("a tool the build does not have must never run")

        run = await SpecialistPipeline(tools=catalog).run(Absent(), AgentTask("act"))

        self.assertEqual(run.stage(StageName.TOOL_SELECTION).status, StageStatus.FAILED)
        self.assertIn("not registered with this build", run.stage(StageName.TOOL_SELECTION).detail)
        self.assertEqual(run.status.value, "failed")

    async def test_verification_uses_the_phase_8_result_vocabulary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            agent = build_developer(workspace(Path(temp)), approvals=AllowGateway())

            run = await agent.run_task("run the tests")

        results = [run.verifications[outcome.step_id] for outcome in run.outcomes]
        self.assertTrue(results)
        for result in results:
            self.assertIn(result["status"], {status.value for status in VerificationStatus})
        self.assertEqual(run.stage(StageName.VERIFICATION).status, StageStatus.OK)
        self.assertTrue(run.verified)

    async def test_recovery_uses_the_phase_8_strategy_vocabulary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            runner = FakeRunner((1, PYTEST_RED), (0, PYTEST_GREEN))
            agent = build_developer(root, runner=runner, approvals=AllowGateway())

            run = await agent.run_task("run the tests")

        strategies = {entry["strategy"] for entry in run.recoveries}
        self.assertTrue(strategies <= {strategy.value for strategy in RecoveryStrategy})
        self.assertEqual(run.stage(StageName.RECOVERY).status, StageStatus.RECOVERED)

    async def test_every_stage_is_recorded_even_when_the_run_stops_early(self) -> None:
        class Nothing(SpecialistAgent):
            def __init__(self) -> None:
                super().__init__("nothing-agent", AgentRole.CODING, "Plans nothing.")

            def clone(self, pipeline: SpecialistPipeline) -> SpecialistAgent:
                return Nothing()

            def decide(self, *args: Any, **kwargs: Any) -> SpecialistDecision:
                return SpecialistDecision(action="idle")

            def plan(self, *args: Any, **kwargs: Any) -> tuple[PipelineStep, ...]:
                return ()

            async def execute(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
                raise AssertionError("nothing was planned")

        run = await SpecialistPipeline().run(Nothing(), AgentTask("do nothing"))

        self.assertEqual([record.stage for record in run.stages], list(STAGE_ORDER))
        self.assertEqual(run.stage(StageName.PLANNER).status, StageStatus.SKIPPED)
        self.assertEqual(run.stage(StageName.RECOVERY).status, StageStatus.SKIPPED)
        self.assertEqual(run.status.value, "failed")

    async def test_the_run_publishes_the_phase_9_typed_events(self) -> None:
        bus = EventBus()
        seen: list[str] = []

        async def capture(event: Any) -> None:
            seen.append(str(event.type))

        expected = (
            EventType.TASK_STARTED.value,
            EventType.INTENT_DETECTED.value,
            EventType.CONTEXT_RESOLVED.value,
            EventType.DECISION_CREATED.value,
            EventType.PLAN_CREATED.value,
            EventType.TOOL_SELECTED.value,
            EventType.TOOL_STARTED.value,
            EventType.TOOL_COMPLETED.value,
            EventType.VERIFICATION_STARTED.value,
            EventType.VERIFICATION_COMPLETED.value,
            EventType.TASK_COMPLETED.value,
        )
        for name in expected:
            await bus.subscribe(name, capture)
        agent = build_research(approvals=AllowGateway(), event_bus=bus)

        run = await agent.run_task("compare widget timeouts under load")

        for name in expected:
            with self.subTest(event=name):
                self.assertIn(name, seen)
        first = [seen.index(name) for name in expected]
        self.assertEqual(first, sorted(first), f"events arrived out of order: {seen}")
        self.assertEqual(seen[0], EventType.TASK_STARTED.value)
        self.assertEqual(seen.count(EventType.TASK_COMPLETED.value), 1)
        self.assertEqual(run.status.value, "completed")
        self.assertEqual(run.errors, [])


class SkippedStepTests(unittest.IsolatedAsyncioTestCase):
    """Regression: a step that never ran is not a verification failure.

    It used to be recorded as FAILED while recovery — for that same step — said
    there was nothing to recover, so two stages disagreed about one non-event,
    and the run's record implied a failure that never happened.
    """

    async def test_a_skipped_step_is_inconclusive_and_its_stage_says_skipped(self) -> None:
        agent = build_research(provider=StubProvider(), allow_web=False)

        run = await agent.run_task("research widget timeouts offline")

        skipped = [outcome for outcome in run.outcomes if outcome.status is StepStatus.SKIPPED]
        self.assertTrue(skipped, [(o.action, o.status) for o in run.outcomes])
        for outcome in skipped:
            with self.subTest(action=outcome.action):
                verification = run.verifications[outcome.step_id]
                self.assertEqual(verification["status"], VerificationStatus.INCONCLUSIVE.value)
                self.assertIn("did not run", verification["reason"])

    async def test_the_verification_stage_says_skipped_when_nothing_ran(self) -> None:
        class Skipper(SpecialistAgent):
            def __init__(self) -> None:
                super().__init__("skipper-agent", AgentRole.CODING, "Skips its only step.")

            def clone(self, pipeline: SpecialistPipeline) -> SpecialistAgent:
                return Skipper()

            def declarations(self) -> Mapping[str, PermissionDeclaration]:
                return {"skipper.act": PermissionDeclaration()}

            def decide(self, *args: Any, **kwargs: Any) -> SpecialistDecision:
                return SpecialistDecision(action="act")

            def plan(self, *args: Any, **kwargs: Any) -> tuple[PipelineStep, ...]:
                return (PipelineStep(action="act", tool="skipper.act", expected="A result"),)

            async def execute(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
                return StepOutcome(
                    step_id=step.id,
                    action=step.action,
                    status=StepStatus.SKIPPED,
                    detail="there was nothing to do",
                )

        run = await SpecialistPipeline().run(Skipper(), AgentTask("skip it"))

        self.assertEqual(run.stage(StageName.VERIFICATION).status, StageStatus.SKIPPED)
        self.assertEqual(
            run.verifications[run.outcomes[0].step_id]["status"],
            VerificationStatus.INCONCLUSIVE.value,
        )
        self.assertEqual(run.status.value, "failed")

    async def test_a_skipped_step_never_becomes_a_pass_or_gets_a_retry(self) -> None:
        agent = build_research(provider=StubProvider(), allow_web=False)

        run = await agent.run_task("research widget timeouts offline")

        skipped = {
            outcome.action
            for outcome in run.outcomes
            if outcome.status is StepStatus.SKIPPED
        }
        self.assertIn("retrieve_evidence", skipped)
        self.assertFalse(run.verified)
        self.assertEqual(run.status.value, "failed")
        # Recovery is only ever asked about a step that genuinely failed.
        self.assertTrue(run.recoveries)
        for entry in run.recoveries:
            with self.subTest(recovery=entry["action"]):
                self.assertNotIn(entry["action"], skipped)


class DeveloperResponsibilityTests(unittest.IsolatedAsyncioTestCase):
    """12.1 — every responsibility the phase lists, one test each."""

    async def test_inspect_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            agent = build_developer(root, approvals=AllowGateway())

            run = await agent.run_task("inspect this repository")

        profile = run.report["profile"]
        self.assertEqual(profile["source_files"], 2)
        self.assertEqual(profile["test_files"], 1)
        self.assertGreater(profile["python_lines"], 0)
        outcomes = [outcome.action for outcome in run.outcomes]
        self.assertIn("inspect_repository", outcomes)
        self.assertIn("inspect_structure", outcomes)
        self.assertEqual(run.status.value, "completed")

    async def test_understand_project_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            agent = build_developer(root, approvals=AllowGateway())

            run = await agent.run_task("explain the project structure")

        structure = run.report["structure"]
        self.assertIn("src", structure["directories"])
        self.assertEqual(structure["packages"], ["widgets"])
        self.assertEqual(structure["modules"], [])
        self.assertEqual(structure["project"]["language"], "python")
        self.assertEqual(structure["project"]["branch"], "main")
        self.assertEqual(
            structure["project"]["repository"], "https://example.invalid/widgets.git"
        )

    async def test_understand_structure_of_a_flat_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package").mkdir()
            (root / "package" / "alpha.py").write_text("x = 1\n", encoding="utf-8")
            (root / "notes.md").write_text("# notes\n", encoding="utf-8")
            agent = build_developer(root, approvals=AllowGateway())

            run = await agent.run_task("explain the project structure")

        self.assertEqual(run.report["structure"]["packages"], ["package"])
        self.assertEqual(run.report["structure"]["directories"], ["package"])

    async def test_search_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            agent = build_developer(root, approvals=AllowGateway())

            run = await agent.run_task("search for widget_timeout")

        self.assertEqual(run.decision["action"], "search")
        matches = run.report["search"]["matches"]
        hit = next(match for match in matches if match["path"] == "src/widgets/core.py")
        self.assertEqual(hit["line"], 4)
        self.assertIn("widget_timeout", hit["text"])

    async def test_search_code_is_bounded_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            (root / "extra.py").write_text("widget_timeout = 1\n", encoding="utf-8")
            result = search_code(root, "widget_timeout", max_files=1, max_matches=1)

        self.assertLessEqual(result.files_scanned, 1)
        self.assertTrue(result.truncated)

    async def test_inspect_errors(self) -> None:
        class BugLog:
            def all(self, *, include_fixed: bool = True) -> tuple[Any, ...]:
                return (SimpleNamespace(title="widget timeout overflow"),)

        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            agent = build_developer(root, approvals=AllowGateway(), bug_log=BugLog())

            run = await agent.run_task("inspect the open errors")

        self.assertEqual(run.decision["action"], "errors")
        self.assertIn("widget timeout overflow", run.report["errors"]["errors"])
        self.assertEqual(run.status.value, "completed")

    async def test_inspect_errors_survives_an_unreadable_log(self) -> None:
        class BrokenLog:
            def all(self, *, include_fixed: bool = True) -> tuple[Any, ...]:
                raise OSError("the log is locked")

        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            agent = build_developer(root, approvals=AllowGateway(), bug_log=BrokenLog())

            run = await agent.run_task("inspect the open errors")

        self.assertEqual(agent.open_errors(), ())
        self.assertEqual(run.status.value, "completed")

    async def test_propose_changes_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            before = fingerprint(root)
            agent = build_developer(root, approvals=AllowGateway(), builder=note_builder)

            run = await agent.run_task("fix the widget timeout")

            after = fingerprint(root)

        self.assertEqual(run.decision["action"], "plan_change")
        self.assertEqual(before, after)
        proposal = run.report["plan"]
        self.assertTrue(proposal["writes_require_approval"])
        self.assertEqual(proposal["changes"][0]["relative_path"], "notes/change.md")
        self.assertNotIn("changes", run.report)  # nothing was applied
        self.assertFalse(run.report["wrote_files"])

    async def test_modify_files_when_authorized(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            agent = build_developer(
                root,
                runner=FakeRunner((0, PYTEST_GREEN)),
                approvals=AllowGateway(),
                builder=note_builder,
            )

            run = await agent.run_task("apply the widget timeout fix")

            written = root / "notes" / "change.md"
            content = written.read_text(encoding="utf-8") if written.is_file() else ""

        self.assertEqual(run.decision["action"], "apply_change")
        self.assertEqual(run.status.value, "completed")
        self.assertEqual(content, "planned: apply the widget timeout fix\n")
        self.assertTrue(run.report["wrote_files"])
        self.assertTrue(run.report["verification"]["verified"])

    async def test_run_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            runner = FakeRunner((0, PYTEST_GREEN))
            agent = build_developer(root, runner=runner, approvals=AllowGateway())

            run = await agent.run_task("run the tests")
            # The command is read while the workspace still exists: the property
            # reflects the project at the moment it is read, and a plan carries
            # that answer as a snapshot.
            recognised = agent.test_command

        self.assertEqual(runner.calls, [recognised])
        self.assertEqual(recognised[1:3], ("-m", "pytest"))
        self.assertEqual(recognised[-1], "tests")
        self.assertNotEqual(recognised[0], "python")  # this interpreter, not PATH
        tests = run.report["tests"]
        self.assertEqual(tests["passed"], 5)
        self.assertEqual(tests["skipped"], 1)
        self.assertTrue(tests["ok"])
        self.assertEqual(run.status.value, "completed")

    async def test_run_tests_keeps_a_command_that_contains_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            runner = FakeRunner((0, PYTEST_GREEN))
            agent = build_developer(root, runner=runner, approvals=AllowGateway())
            agent.test_command = ("/opt/py 3/bin/python", "-m", "pytest", "tests")

            run = await agent.run_task("run the tests")

        # The display string is never parsed back into argv.
        self.assertEqual(runner.calls, [("/opt/py 3/bin/python", "-m", "pytest", "tests")])
        self.assertEqual(run.report["tests"]["passed"], 5)

    async def test_inspect_failures_and_debug(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            runner = FakeRunner((1, PYTEST_RED))
            agent = build_developer(root, runner=runner, approvals=AllowGateway())

            run = await agent.run_task("why is the widget test failing")

        self.assertEqual(run.decision["action"], "debug")
        self.assertEqual(run.status.value, "completed")
        diagnosis = run.report["diagnosis"]
        self.assertEqual(diagnosis["kind"], "assertion")
        self.assertIn("assert 60 == 30", diagnosis["message"])
        self.assertEqual(diagnosis["line"], 5)
        # The failure signature is named, never guessed.
        self.assertEqual(diagnose_failure("everything is fine")["kind"], "unknown")

    async def test_verify_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            agent = build_developer(
                root,
                runner=FakeRunner((0, PYTEST_GREEN)),
                approvals=AllowGateway(),
                builder=note_builder,
            )

            run = await agent.run_task("apply the widget timeout fix")

        verification = run.report["verification"]
        self.assertTrue(verification["verified"])
        self.assertEqual(verification["files"][0]["path"], "notes/change.md")
        self.assertTrue(verification["files"][0]["matches"])
        self.assertIn("verify_changes", [outcome.action for outcome in run.outcomes])

    async def test_a_run_reported_completed_has_a_pass_behind_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            agent = build_developer(workspace(Path(temp)), approvals=AllowGateway())

            run = await agent.run_task("inspect this repository")

        self.assertEqual(run.status.value, "completed")
        self.assertTrue(run.verified)
        self.assertTrue(
            any(
                entry["status"] == VerificationStatus.PASS.value
                for entry in run.verifications.values()
            )
        )

    async def test_a_run_nothing_verified_is_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            agent = build_developer(root, runner=FakeRunner((1, PYTEST_RED)))

            run = await agent.run_task("run the tests")

        self.assertFalse(run.verified)
        self.assertEqual(run.status.value, "failed")

    async def test_report_is_the_last_thing_a_run_produces(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            agent = build_developer(workspace(Path(temp)), approvals=AllowGateway())

            run = await agent.run_task("inspect this repository")

        for key in ("action", "project", "root", "verified", "wrote_files", "authorized", "denied"):
            with self.subTest(key=key):
                self.assertIn(key, run.report)
        self.assertEqual(run.report["action"], "inspect")
        self.assertTrue(run.summary)
        self.assertEqual(run.stage(StageName.RECOVERY).status, StageStatus.SKIPPED)

    async def test_the_workflow_runs_in_order(self) -> None:
        """Understand → Inspect → Plan → Modify → Test → Verify → Recover → Report."""
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            runner = FakeRunner((1, PYTEST_RED), (0, PYTEST_GREEN))
            agent = build_developer(
                root, runner=runner, approvals=AllowGateway(), builder=note_builder
            )

            run = await agent.run_task("apply the widget timeout fix")

        actions = [outcome.action for outcome in run.outcomes]
        self.assertEqual(
            list(dict.fromkeys(actions)),
            [
                "inspect_repository",
                "plan_changes",
                "apply_changes",
                "run_tests",
                "verify_changes",
            ],
        )
        # Test failed, was recovered, and the same step ended up green.
        attempts = [
            outcome.status for outcome in run.outcomes if outcome.action == "run_tests"
        ]
        self.assertEqual(attempts, [StepStatus.FAILED, StepStatus.COMPLETED])
        records = {record.stage: record.status for record in run.stages}
        self.assertEqual(records[StageName.NLU], StageStatus.OK)
        self.assertEqual(records[StageName.RECOVERY], StageStatus.RECOVERED)
        self.assertEqual(run.status.value, "completed")
        self.assertTrue(run.report["verified"])


class RunnerRecognitionTests(unittest.IsolatedAsyncioTestCase):
    """Regression: the test command is the PROJECT's runner, not a guess.

    The agent used to hard-code pytest, so "run the tests" inside a JavaScript
    repository ran `python -m pytest tests -q` — a command that cannot exist
    there. The recognition rule is shared with the planning path's test step, so
    a project cannot be a Node project to the planner and a Python project to
    the agent.
    """

    def test_a_node_project_gets_npm_test(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text('{"scripts": {"test": "jest"}}\n', encoding="utf-8")
            (root / "src").mkdir()
            with patch(
                "novacontrol.agents.developer.shutil.which", return_value="/usr/local/bin/npm"
            ):
                command = recognise_test_command(root)

        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command.argv, ("/usr/local/bin/npm", "test"))
        self.assertEqual(command.label, "npm test")

    async def test_a_node_project_runs_its_own_suite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text('{"scripts": {"test": "jest"}}\n', encoding="utf-8")
            (root / "src").mkdir()
            runner = FakeRunner((0, "Tests: 3 passed, 3 total\n"))
            agent = build_developer(root, runner=runner, approvals=AllowGateway())
            with patch(
                "novacontrol.agents.developer.shutil.which", return_value="/usr/local/bin/npm"
            ):
                run = await agent.run_task("run the tests")

        self.assertEqual(runner.calls, [("/usr/local/bin/npm", "test")])
        self.assertEqual(run.status.value, "completed")

    def test_a_python_project_gets_this_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            command = recognise_test_command(root)
            agent_command = build_developer(root).test_command

        assert command is not None
        self.assertEqual(command.argv, agent_command)
        self.assertEqual(command.argv[1:3], ("-m", "pytest"))
        self.assertEqual(command.argv[-1], "tests")
        self.assertNotEqual(command.argv[0], "python")

    def test_a_project_with_no_runner_falls_back_to_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "notes.txt").write_text("no code here\n", encoding="utf-8")
            with patch("novacontrol.agents.developer.shutil.which", return_value=None):
                self.assertIsNone(recognise_test_command(root))
                # The fallback is the interpreter's pytest, and verification will
                # refuse to call that run a pass when it finds no tally.
                self.assertEqual(
                    build_developer(root).test_command,
                    ("python", "-m", "pytest", "tests", "-q"),
                )

    def test_the_planning_path_and_the_agent_agree(self) -> None:
        from novacontrol.application import _test_argv

        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            planned = _test_argv(str(root), "tests")
            agent_command = build_developer(root).test_command

        self.assertIsNotNone(planned)
        assert planned is not None
        argv, label = planned
        self.assertEqual(tuple(argv), agent_command)
        self.assertEqual(label, "python -m pytest")

    async def test_an_explicit_command_still_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            runner = FakeRunner((0, PYTEST_GREEN))
            agent = DeveloperAgent(
                root=root,
                runner=runner,
                test_command=("pytest", "-q", "tests/unit"),
                pipeline=SpecialistPipeline(approvals=AllowGateway()),
            )

            run = await agent.run_task("run the tests")

        self.assertEqual(runner.calls, [("pytest", "-q", "tests/unit")])
        self.assertEqual(run.status.value, "completed")


class RefusedStepTests(unittest.IsolatedAsyncioTestCase):
    """Regression: a refusal is not a failure to diagnose, and not a retry.

    A debug run whose test step was refused used to report the refusal itself as
    the diagnosis — "unknown: developer.run_tests is high risk, so it needs
    confirmation" — the agent's own permission decision presented as the cause of
    a failure nobody ever reproduced. Recovery also spent a retry on the step,
    re-queueing a command that reuses the very grant that denied it.
    """

    async def test_a_refused_test_step_is_not_diagnosed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            agent = build_developer(root)  # no approval flow: read-only in practice

            run = await agent.run_task("debug the failing test")

        self.assertEqual(run.status.value, "failed")
        self.assertNotIn("diagnosis", run.report)
        diagnose = next(outcome for outcome in run.outcomes if outcome.action == "diagnose")
        self.assertEqual(diagnose.status, StepStatus.SKIPPED)
        self.assertIn("no failing output was recorded", diagnose.detail)
        # The refusal is still reported — it just is not dressed up as a cause.
        self.assertEqual([grant.tool for grant in run.denied], ["developer.run_tests"])
        self.assertIn("developer.run_tests", run.summary)
        self.assertNotIn("high risk", run.summary)

    async def test_a_refused_step_is_neither_executed_nor_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            runner = FakeRunner()
            agent = build_developer(root, runner=runner)

            run = await agent.run_task("run the tests")

        self.assertEqual(runner.calls, [])
        self.assertEqual(run.recoveries, [])
        # A refusal ends the step: nothing to verify, nothing to retry.
        self.assertEqual(run.verifications, {})
        self.assertEqual(run.stage(StageName.RECOVERY).status, StageStatus.SKIPPED)
        self.assertFalse(run.verified)
        self.assertEqual(run.status.value, "failed")

    async def test_a_diagnosis_only_comes_from_a_step_that_ran(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            runner = FakeRunner((1, PYTEST_RED))
            agent = build_developer(root, runner=runner, approvals=AllowGateway())

            run = await agent.run_task("debug the failing test")

        diagnosis = run.report["diagnosis"]
        self.assertEqual(diagnosis["kind"], "assertion")
        self.assertIn("test_widgets.py", diagnosis["file"])
        self.assertGreater(diagnosis["line"], 0)


class GitOperationTests(unittest.IsolatedAsyncioTestCase):
    """12.1 "perform Git operations" — read back, and never guess a mutation.

    Regression: the agent read only the *verb*, so `git diff HEAD~1 -- src` ran a
    bare `git diff`, `git remote add …` ran `git status`, and `git branch -D
    feature/x` was classified read-only. That last one is the dangerous shape: a
    deletion the classifier called a read.
    """

    def test_the_command_is_read_back_word_for_word(self) -> None:
        cases = (
            ("git status", ("status",)),
            ("git diff HEAD~1 -- src", ("diff", "HEAD~1", "--", "src")),
            ("git log --oneline -5", ("log", "--oneline", "-5")),
            ("show me git history", ("log",)),
            ("git commit -m 'fix the widget'", ("commit", "-m", "fix the widget")),
            ("git branch -D feature/widget-timeout", ("branch", "-D", "feature/widget-timeout")),
            ("rebase onto main", ("rebase", "main")),
            ("git push origin main", ("push", "origin", "main")),
        )
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            for goal, expected in cases:
                with self.subTest(goal=goal):
                    command = read_git_command(goal, root=root)
                    self.assertEqual(command.argv, expected)
                    self.assertEqual(command.unreadable, ())

    def test_words_that_cannot_be_read_are_reported_not_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            command = read_git_command("git commit the widget fix", root=root)

        self.assertEqual(command.argv, ("commit",))
        self.assertEqual(command.unused, ("the",))
        self.assertEqual(command.unreadable, ("widget", "fix"))

    def test_only_a_verb_that_reads_is_a_read(self) -> None:
        cases = {
            ("status",): False,
            ("diff", "HEAD~1"): False,
            ("branch",): False,
            ("branch", "-a"): False,
            ("branch", "-D", "feature/widget-timeout"): True,
            ("remote", "-v"): False,
            ("remote", "add", "origin", "url"): True,
            ("remote", "remove", "origin"): True,
            ("stash", "list"): False,
            ("stash", "pop"): True,
            ("config", "--get", "user.name"): False,
            ("config", "user.name", "someone"): True,
            ("tag", "-l"): False,
            ("tag", "-d", "v1"): True,
            ("worktree", "list"): False,
            ("push", "origin", "main"): True,
            ("commit", "-m", "message"): True,
            ("reset", "--hard"): True,
            ("nonsense-verb",): True,
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(git_is_mutating(argv), expected)

    async def test_a_read_only_command_runs_exactly_as_asked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            runner = FakeRunner((0, "diff --git a/x b/x\n"))
            agent = build_developer(root, runner=runner)

            run = await agent.run_task("git diff HEAD~1 -- src")

        self.assertEqual(runner.calls, [("git", "diff", "HEAD~1", "--", "src")])
        self.assertEqual(run.decision["action"], "git")
        self.assertFalse(run.decision["requires_approval"])
        self.assertEqual(run.status.value, "completed")

    async def test_a_branch_deletion_needs_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            runner = FakeRunner((0, ""))
            agent = build_developer(root, runner=runner)

            run = await agent.run_task("git branch -D feature/widget-timeout")

        self.assertEqual(run.decision["action"], "git_write")
        self.assertTrue(run.decision["requires_approval"])
        self.assertEqual(runner.calls, [])
        self.assertEqual([grant.tool for grant in run.denied], ["developer.git_write"])
        self.assertEqual(run.status.value, "failed")

    async def test_an_approved_mutation_runs_the_command_that_was_asked_for(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            runner = FakeRunner((0, "Deleted branch feature/widget-timeout\n"))
            agent = build_developer(root, runner=runner, approvals=AllowGateway())

            run = await agent.run_task("git branch -D feature/widget-timeout")

        self.assertEqual(runner.calls, [("git", "branch", "-D", "feature/widget-timeout")])
        self.assertEqual(run.status.value, "completed")

    async def test_a_mutation_with_unreadable_words_is_refused_unrun(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            runner = FakeRunner((0, ""))
            agent = build_developer(root, runner=runner, approvals=AllowGateway())

            run = await agent.run_task("git commit the widget fix")

        self.assertEqual(runner.calls, [])
        self.assertEqual(run.status.value, "failed")
        step = next(outcome for outcome in run.outcomes if outcome.action == "git")
        self.assertEqual(step.status, StepStatus.FAILED)
        self.assertIn("was not run", step.detail)
        self.assertIn("'widget'", step.detail)
        self.assertEqual([entry["strategy"] for entry in run.recoveries], ["abort"])

    async def test_git_reads_run_without_any_approval_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            runner = FakeRunner((0, "## main\n"))
            agent = build_developer(root, runner=runner)

            run = await agent.run_task("git status")

        self.assertEqual(runner.calls, [("git", "status")])
        self.assertEqual(run.status.value, "completed")

    async def test_words_that_were_not_used_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            runner = FakeRunner((0, "diff\n"))
            agent = build_developer(root, runner=runner)

            run = await agent.run_task("git diff the widget timeout")

        self.assertEqual(run.report["git"]["unused"], ["the"])
        self.assertEqual(run.report["git"]["unreadable"], ["widget", "timeout"])
        self.assertIn("Words not used in the git command", run.summary)

    async def test_a_mutation_that_fails_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            runner = FakeRunner((1, "error: failed to push\n"))
            agent = build_developer(root, runner=runner, approvals=AllowGateway())

            run = await agent.run_task("git push origin main")

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual([entry["strategy"] for entry in run.recoveries], ["abort"])
        self.assertEqual(run.status.value, "failed")


class ResearchResponsibilityTests(unittest.IsolatedAsyncioTestCase):
    """12.2 — every responsibility the phase lists, one test each."""

    async def test_understand_the_research_question(self) -> None:
        agent = build_research(approvals=AllowGateway())

        run = await agent.run_task("compare widget timeouts under load")

        frame = run.report["frame"]
        self.assertEqual(frame["depth"], "deep")
        self.assertTrue(frame["subject"])
        self.assertEqual(run.decision["action"], "compare")
        self.assertIn(frame["frame"], run.decision["reason"])

    async def test_a_quick_question_is_read_as_quick(self) -> None:
        agent = build_research(approvals=AllowGateway())

        run = await agent.run_task("summarize widget timeouts in a nutshell")

        self.assertEqual(run.report["frame"]["depth"], "quick")

    async def test_determine_required_sources(self) -> None:
        web = build_research(approvals=AllowGateway())
        local = build_research(provider=StubProvider(), allow_web=False)
        offline = build_research(approvals=AllowGateway())

        web_run = await web.run_task("research widget timeouts")
        local_run = await local.run_task("research widget timeouts")
        offline_run = await offline.run_task("research widget timeouts, offline only")

        self.assertEqual(web_run.decision["data"]["sources"], "web")
        self.assertEqual(local_run.decision["data"]["sources"], "local knowledge")
        self.assertEqual(local_run.decision["action"], "local")
        # The same decision from a sentence that asks for no web.
        self.assertEqual(offline_run.decision["action"], "local")

    async def test_search_web_uses_the_existing_research_engine(self) -> None:
        provider = StubProvider()
        agent = build_research(provider=provider, approvals=AllowGateway())

        run = await agent.run_task("research widget timeouts")

        self.assertEqual(provider.calls, ["research widget timeouts"])
        self.assertEqual(run.report["provider_status"], "stub")
        self.assertEqual(run.report["warnings"], ["cached result"])
        self.assertIn("search_sources", [outcome.action for outcome in run.outcomes])

    async def test_search_web_is_not_used_when_it_is_not_available(self) -> None:
        provider = StubProvider()
        agent = build_research(provider=provider, allow_web=False)

        run = await agent.run_task("research widget timeouts")

        self.assertEqual(provider.calls, [])
        self.assertNotIn("search_sources", [outcome.action for outcome in run.outcomes])
        self.assertTrue(run.report["local_only"])

    async def test_retrieve_information(self) -> None:
        agent = build_research(approvals=AllowGateway())

        run = await agent.run_task("how long is the widget timeout")

        retrieved = next(
            outcome for outcome in run.outcomes if outcome.action == "retrieve_evidence"
        )
        self.assertEqual(retrieved.output["sources"], 2)
        self.assertGreater(retrieved.output["pages_read"], 0)
        self.assertTrue(retrieved.output["evidence"])
        for sentence in retrieved.output["evidence"]:
            with self.subTest(sentence=sentence["text"]):
                self.assertIn(sentence["source_index"], (1, 2))
                self.assertGreater(sentence["score"], 0)

    async def test_compare_sources(self) -> None:
        agent = build_research(
            provider=StubProvider(DISAGREEING_SOURCES), approvals=AllowGateway()
        )

        run = await agent.run_task("compare widget timeouts under load")

        compared = next(
            outcome for outcome in run.outcomes if outcome.action == "compare_sources"
        )
        self.assertEqual(len(compared.output["sources"]), 2)
        self.assertEqual([entry["source_index"] for entry in compared.output["sources"]], [1, 2])
        self.assertEqual(
            [entry["title"] for entry in compared.output["sources"]],
            ["Widget Guide", "Widget Benchmarks"],
        )
        self.assertTrue(all(entry["claims"] for entry in compared.output["sources"]))
        self.assertGreater(run.report["agreement"], 0.0)

    async def test_a_comparison_is_recognised_without_a_comparison_keyword(self) -> None:
        # Regression: "is X faster than Y?" framed as an explanation skipped the
        # comparison step entirely, so the run reported agreement it never
        # measured.
        agent = build_research(approvals=AllowGateway())

        run = await agent.run_task("is rust faster than go")

        self.assertEqual(run.decision["action"], "compare")
        self.assertIn("compare_sources", [outcome.action for outcome in run.outcomes])

    async def test_identify_conflicting_claims(self) -> None:
        agent = build_research(
            provider=StubProvider(DISAGREEING_SOURCES), approvals=AllowGateway()
        )

        run = await agent.run_task("compare widget timeouts under load")

        conflicts = run.report["conflicts"]
        self.assertTrue(conflicts)
        for conflict in conflicts:
            with self.subTest(conflict=conflict):
                self.assertNotEqual(conflict["left_source"], conflict["right_source"])
                self.assertIn(conflict["kind"], {"negation", "figure", "antonym"})
                self.assertTrue(conflict["topic"])
        self.assertIn("Conflicting claims:", run.report["answer"])

    async def test_agreement_is_not_reported_as_a_conflict(self) -> None:
        agent = build_research(
            provider=StubProvider(AGREEING_SOURCES), approvals=AllowGateway()
        )

        run = await agent.run_task("compare widget timeouts under load")

        self.assertEqual(run.report["conflicts"], [])
        self.assertEqual(
            detect_conflicts(
                [
                    Claim("The widget timeout is 30 seconds.", ClaimKind.EVIDENCE, (1,)),
                    Claim(
                        "The widget timeout is 30 seconds by default.",
                        ClaimKind.EVIDENCE,
                        (2,),
                    ),
                ]
            ),
            (),
        )

    async def test_synthesize(self) -> None:
        agent = build_research(approvals=AllowGateway())

        run = await agent.run_task("compare widget timeouts under load")

        synthesized = next(outcome for outcome in run.outcomes if outcome.action == "synthesize")
        self.assertEqual(synthesized.status, StepStatus.COMPLETED)
        self.assertIn("answer", synthesized.output)
        self.assertEqual(len(synthesized.output["claims"]), len(run.report["claims"]))
        self.assertTrue(run.report["answer"].strip())

    async def test_provide_citations(self) -> None:
        agent = build_research(approvals=AllowGateway())

        run = await agent.run_task("compare widget timeouts under load")

        indexes = {entry["index"] for entry in run.report["sources"]}
        self.assertEqual(indexes, {1, 2})
        for claim in run.report["claims"]:
            for index in claim["citations"]:
                with self.subTest(claim=claim["id"], index=index):
                    self.assertIn(index, indexes)
        self.assertTrue(run.report["citations_verified"])
        self.assertIn("[1]", run.report["answer"])
        self.assertIn("https://example.invalid/guide", run.report["answer"])

    async def test_citations_are_preserved_from_the_source_objects(self) -> None:
        agent = build_research(
            provider=StubProvider(DISAGREEING_SOURCES), approvals=AllowGateway()
        )

        run = await agent.run_task("research widget timeouts")

        self.assertEqual(
            [entry["title"] for entry in run.report["sources"]],
            ["Widget Guide", "Widget Benchmarks"],
        )
        self.assertEqual(
            [entry["url"] for entry in run.report["sources"]],
            ["https://example.invalid/guide", "https://example.invalid/benchmarks"],
        )
        self.assertEqual([entry["source_type"] for entry in run.report["sources"]], ["web", "web"])

    async def test_a_fabricated_citation_fails_verification(self) -> None:
        class Forging(ResearchAgent):
            def _evidence_claims(self, run: AgentRun) -> list[Claim]:
                forged = Claim(
                    text="Widgets are always cached.",
                    kind=ClaimKind.EVIDENCE,
                    citations=(42,),
                    id="forged-1",
                )
                return [*super()._evidence_claims(run), forged]  # noqa: SLF001

        agent = Forging(
            provider=StubProvider(), pipeline=SpecialistPipeline(approvals=AllowGateway())
        )

        run = await agent.run_task("research widget timeouts")

        self.assertFalse(run.report["citations_verified"])
        self.assertEqual(run.status.value, "failed")
        step = next(outcome for outcome in run.outcomes if outcome.action == "verify_citations")
        self.assertEqual(step.output["unresolved"][0]["reason"], "no such source")
        self.assertEqual(step.output["unresolved"][0]["citation"], 42)

    async def test_distinguish_evidence_from_inference(self) -> None:
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
            with self.subTest(claim=claim["id"]):
                self.assertTrue(claim["citations"])
                self.assertFalse(claim["basis"])
        for claim in inference:
            with self.subTest(claim=claim["id"]):
                self.assertTrue(claim["basis"])
                self.assertFalse(claim["citations"])

    async def test_a_local_answer_cites_the_local_index(self) -> None:
        knowledge = KnowledgeEngine("Widget timeouts are configured per project.\n")
        agent = build_research(provider=StubProvider(), allow_web=False, knowledge=knowledge)

        run = await agent.run_task("research widget timeouts offline")

        self.assertTrue(run.report["local_only"])
        self.assertEqual(
            run.report["sources"],
            [
                {
                    "index": 1,
                    "title": "Local knowledge index",
                    "url": "",
                    "source_type": "local",
                }
            ],
        )
        evidence = [claim for claim in run.report["claims"] if claim["kind"] == "evidence"]
        self.assertTrue(evidence)
        for claim in evidence:
            with self.subTest(claim=claim["text"]):
                self.assertEqual(claim["citations"], [1])
        self.assertTrue(run.report["citations_verified"])
        self.assertEqual(run.status.value, "completed")

    async def test_recovery_retries_a_fetch_and_never_a_computation(self) -> None:
        # Regression: a failed `synthesize` was retried, which cannot change the
        # evidence it works from and so spends time to reach the same failure.
        failing = StubProvider(fail=True)
        online = build_research(provider=failing, approvals=AllowGateway())
        offline = build_research(provider=StubProvider(), allow_web=False)

        fetched = await online.run_task("research widget timeouts")
        computed = await offline.run_task("research widget timeouts offline")

        self.assertEqual(len(failing.calls), 2)  # a fetch is worth one retry
        strategies = [entry["strategy"] for entry in fetched.recoveries]
        self.assertEqual(strategies[0], "retry")
        self.assertEqual(
            {entry["action"] for entry in fetched.recoveries if entry["strategy"] == "retry"},
            {"search_sources"},
        )
        self.assertTrue(all(strategy == "abort" for strategy in strategies[1:]))
        self.assertIn("already tried twice", fetched.recoveries[1]["text"])
        self.assertTrue(computed.recoveries)
        for entry in computed.recoveries:
            with self.subTest(recovery=entry["action"]):
                self.assertEqual(entry["strategy"], "abort")
                self.assertIn("repeat the same failure", entry["text"])

    async def test_a_failed_source_step_fails_the_run(self) -> None:
        agent = build_research(provider=StubProvider(fail=True), approvals=AllowGateway())

        run = await agent.run_task("research widget timeouts")

        self.assertEqual(run.status.value, "failed")
        self.assertFalse(run.verified)


class SafetyTests(unittest.IsolatedAsyncioTestCase):
    """The phase's two rules, asserted rather than assumed."""

    async def test_writes_and_execution_are_refused_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            before = fingerprint(root)
            runner = FakeRunner()
            agent = build_developer(root, runner=runner, builder=note_builder)

            run = await agent.run_task("apply the widget timeout fix")

            after = fingerprint(root)

        self.assertEqual(before, after)
        self.assertEqual(runner.calls, [])
        self.assertEqual(
            {grant.tool for grant in run.denied},
            {"developer.apply_changes", "developer.run_tests"},
        )
        self.assertIn("authorized", run.summary)

    async def test_no_repository_mutation_is_run_without_permission(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            agent = build_developer(root)
            for goal in (
                "git commit the widget fix",
                "git push origin main",
                "git reset --hard HEAD~1",
                "git branch -D feature/widget-timeout",
                "git clean -fd",
            ):
                with self.subTest(goal=goal):
                    # A fresh runner per goal, so the count is that goal's.
                    agent.runner = runner = FakeRunner()
                    run = await agent.run_task(goal)
                    self.assertEqual(runner.calls, [])
                    self.assertTrue(run.denied)
                    self.assertEqual(run.status.value, "failed")

    async def test_an_approval_flow_that_breaks_is_a_refusal(self) -> None:
        class Broken:
            async def request_approval(self, request: Any) -> ApprovalDecision:
                raise RuntimeError("the approval service is down")

        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
            runner = FakeRunner()
            agent = build_developer(root, runner=runner, approvals=Broken(), builder=note_builder)

            run = await agent.run_task("apply the widget timeout fix")
            written = (root / "notes" / "change.md").exists()

        self.assertEqual(runner.calls, [])
        self.assertFalse(written)
        self.assertEqual(run.status.value, "failed")

    async def test_an_approval_flow_that_breaks_still_allows_reads(self) -> None:
        class Broken:
            async def request_approval(self, request: Any) -> ApprovalDecision:
                raise RuntimeError("the approval service is down")

        with tempfile.TemporaryDirectory() as temp:
            agent = build_developer(workspace(Path(temp)), approvals=Broken())

            run = await agent.run_task("inspect this repository")

        self.assertEqual(run.status.value, "completed")

    async def test_authorization_is_a_per_run_scope_not_a_build_wide_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = workspace(Path(temp))
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

    async def test_the_registry_keeps_the_stubs_for_the_roles_nobody_specializes(self) -> None:
        registry = AgentRegistry()
        for stub in build_default_agents():
            registry.register(stub)
        with tempfile.TemporaryDirectory() as temp:
            registry.register(build_developer(workspace(Path(temp))), prefer=True)

            coding = [agent.name for agent in registry.by_role(AgentRole.CODING)]
            documentation = [agent.name for agent in registry.by_role(AgentRole.DOCUMENTATION)]

        self.assertEqual(coding, ["developer-agent", "coding-agent"])
        self.assertEqual(documentation, ["documentation-agent"])

    async def test_the_application_grants_nothing_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            app = NovaControlApplication(data_dir=temp)
            await app.start()
            try:
                report = await app.developer_task("fix the widget timeout")
                declared = app.specialist_agents()
            finally:
                await app.stop()

        self.assertFalse(report["report"]["wrote_files"])
        self.assertIn(report["status"], {"failed", "completed"})
        self.assertIn("developer-agent", [entry["agent"] for entry in declared["agents"]])
        for agent in declared["agents"]:
            with self.subTest(agent=agent["agent"]):
                self.assertEqual(agent["stages"], [stage.value for stage in STAGE_ORDER])
                for name, declaration in agent["declarations"].items():
                    self.assertIn("risk_level", declaration)
                    self.assertIn(name.split(".", 1)[0], {"developer", "research"})


if __name__ == "__main__":
    unittest.main()
