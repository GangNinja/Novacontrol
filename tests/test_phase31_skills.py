"""Phase 31 — transfer learning and general-purpose skills."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from novacontrol.agents.models import AgentRole, AgentTaskStatus
from novacontrol.agents.pipeline import (
    SKILL_TOOL,
    PipelineStep,
    SpecialistAgent,
    SpecialistDecision,
    SpecialistPipeline,
    StageName,
    StepOutcome as PipelineOutcome,
    StepStatus,
)
from novacontrol.application import NovaControlApplication
from novacontrol.reliability.permissions import (
    PermissionDeclaration,
    PermissionManager,
    RiskLevel,
)
from novacontrol.skills import (
    DuplicateSkillError,
    Skill,
    SkillInput,
    SkillLifecycle,
    SkillObservation,
    SkillService,
    SkillStep,
    SkillStepKind,
    StepOutcome,
)
from novacontrol.skills.models import SkillReliability
from novacontrol.tools.models import ToolRequest, ToolStatus

WRITE = "test.skill.copy"
READ = "test.skill.check"


def permissions(*, confirm: bool = False) -> PermissionManager:
    manager = PermissionManager()
    for scope in (WRITE, READ):
        manager.declare(
            scope,
            risk_level=RiskLevel.HIGH if confirm else RiskLevel.LOW,
            requires_confirmation=confirm,
            reversible=not confirm,
            destructive=False,
            external_side_effect=False,
        )
    return manager


def copy_steps() -> tuple[SkillStep, ...]:
    return (
        SkillStep(
            "copy {source} to {destination}",
            kind=SkillStepKind.TOOL,
            tool="copy_file",
            permission=WRITE,
        ),
        SkillStep(
            "verify {destination} exists",
            kind=SkillStepKind.VERIFY,
            tool="file_exists",
            permission=READ,
            verify=True,
        ),
    )


def observations(root: Path) -> list[SkillObservation]:
    rows: list[SkillObservation] = []
    for index, name in enumerate(("alpha", "beta")):
        source = root / f"source-{name}.txt"
        destination = root / f"copy-{name}.txt"
        source.write_text(f"payload {name}", encoding="utf-8")
        rows.append(
            SkillObservation(
                goal="copy a text file to a destination",
                steps=copy_steps(),
                verified=True,
                environment="windows",
                inputs={"source": str(source), "destination": str(destination)},
                tools=("copy_file", "file_exists"),
                duration_ms=12.0 + index,
            )
        )
    return rows


def make_executor(counter: dict[str, int]):
    async def execute(step, inputs, context) -> StepOutcome:
        counter[step.tool] = counter.get(step.tool, 0) + 1
        if step.tool == "copy_file":
            try:
                shutil.copyfile(inputs["source"], inputs["destination"])
            except Exception as exc:
                return StepOutcome(False, error=f"{type(exc).__name__}: {exc}")
            return StepOutcome(True, "copied", (str(inputs["destination"]),))
        if step.tool == "file_exists":
            exists = Path(inputs["destination"]).is_file()
            return StepOutcome(exists, "exists" if exists else "missing")
        if step.tool == "always_fail":
            return StepOutcome(False, error="synthetic failure")
        return StepOutcome(True, "step completed")

    return execute


class AcquisitionTests(unittest.TestCase):
    def test_one_success_is_not_a_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = SkillService(permissions=permissions())
            rows = observations(root)[:1]
            result = service.acquire("copy a text file", rows)
            self.assertFalse(result.created)
            self.assertTrue(result.rejected)
            self.assertIn("one verified success", result.reasons[0])

    def test_repeated_verified_procedure_is_acquired_with_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = SkillService(permissions=permissions())
            result = service.acquire("copy a text file", observations(root))
            self.assertTrue(result.created)
            skill = result.skill
            assert skill is not None
            self.assertEqual(skill.lifecycle, SkillLifecycle.ACQUIRED)
            self.assertEqual(skill.reliability.trials, 2)
            self.assertEqual(skill.input_names, ("destination", "source"))
            self.assertTrue(any(item.startswith("observation:") for item in skill.provenance))
            self.assertFalse(skill.executable)

    def test_demonstration_still_needs_a_verified_execution(self) -> None:
        service = SkillService(permissions=permissions())
        result = service.acquire("copy a text file", [], demonstrated=True)
        self.assertFalse(result.created)
        self.assertTrue(result.rejected)

    def test_different_procedures_are_not_merged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = observations(root)
            rows[1] = SkillObservation(
                goal=rows[1].goal,
                steps=(SkillStep("delete {destination}"),),
                verified=True,
                environment="windows",
                inputs=rows[1].inputs,
            )
            service = SkillService(permissions=permissions())
            result = service.acquire("copy a text file", rows)
            self.assertFalse(result.created)
            self.assertIn("different procedures", result.reasons[0])

    def test_acquisition_is_not_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SkillService(permissions=permissions())
            result = service.acquire("copy a text file", observations(Path(directory)))
            assert result.skill is not None
            with self.assertRaises(KeyError):
                service.validate("missing-skill")
            validated = service.validate(result.skill.id)
            self.assertEqual(validated.lifecycle, SkillLifecycle.VALIDATED)


class RetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.service = SkillService(permissions=permissions())
        result = self.service.acquire("copy a text file", observations(self.root))
        assert result.skill is not None
        self.skill = self.service.validate(result.skill.id)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_relevant_skill_is_ranked_and_irrelevant_is_rejected(self) -> None:
        hits = self.service.retrieve("copy a file for the report")
        self.assertTrue(hits)
        self.assertEqual(hits[0].skill_id, self.skill.id)
        self.assertEqual(self.service.retrieve("bake sourdough bread"), ())

    def test_environment_mismatch_is_named_not_hidden(self) -> None:
        hits = self.service.retrieve(
            "copy a file", environment="plan9", include_incompatible=True
        )
        self.assertTrue(hits)
        self.assertFalse(hits[0].compatible)
        self.assertTrue(hits[0].missing_capabilities == ())

    def test_duplicate_definitions_are_refused_without_a_phantom_entry(self) -> None:
        before = self.service.catalog.ids()
        duplicate = self.skill.with_(id="duplicate-id", name=self.skill.name)
        with self.assertRaises(DuplicateSkillError):
            self.service.catalog.add(duplicate)
        self.assertEqual(self.service.catalog.ids(), before)
        self.assertNotIn("duplicate-id", self.service.catalog.ids())


class TransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.counter: dict[str, int] = {}
        self.service = SkillService(
            permissions=permissions(), execute_step=make_executor(self.counter)
        )
        result = self.service.acquire("copy a text file", observations(self.root))
        assert result.skill is not None
        self.skill = self.service.validate(result.skill.id)

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def test_adaptation_preserves_the_original_and_executes_new_inputs(self) -> None:
        source = self.root / "transfer-source.txt"
        target = self.root / "transfer-target.txt"
        source.write_text("new payload", encoding="utf-8")
        adapted = self.service.adapt(
            self.skill.id,
            goal="copy the quarterly file",
            inputs={"source": str(source), "destination": str(target)},
            environment="windows",
        )
        self.assertTrue(adapted.compatible)
        assert adapted.skill is not None
        self.assertEqual(adapted.skill.version, 2)
        self.assertEqual(adapted.skill.lifecycle, SkillLifecycle.ADAPTED)
        self.assertEqual(adapted.skill.reliability.trials, 0)
        self.assertEqual(self.service.catalog.get(self.skill.id, 1).lifecycle, SkillLifecycle.VALIDATED)

        execution = await self.service.execute(
            self.skill.id,
            version=2,
            inputs={"source": str(source), "destination": str(target)},
        )
        self.assertEqual(execution["status"], "completed")
        self.assertTrue(target.is_file())
        self.assertIn("copy", self.service.catalog.get(self.skill.id, 2).objective)
        self.assertTrue(self.service.catalog.get(self.skill.id, 2).executable)
        self.assertEqual(self.service.catalog.get(self.skill.id, 1).version, 1)

    async def test_adapted_version_requires_its_own_validation(self) -> None:
        adapted = self.service.adapt(
            self.skill.id, goal="copy another file", environment="windows"
        )
        assert adapted.skill is not None
        with self.assertRaises(ValueError):
            self.service.validate(self.skill.id, 2)
        source = self.root / "again-source.txt"
        source.write_text("again", encoding="utf-8")
        for index in range(2):
            target = self.root / f"again-{index}.txt"
            result = await self.service.execute(
                self.skill.id,
                version=2,
                inputs={"source": str(source), "destination": str(target)},
            )
            self.assertEqual(result["status"], "completed")
        validated = self.service.validate(self.skill.id, 2)
        self.assertEqual(validated.lifecycle, SkillLifecycle.VALIDATED)

    async def test_incompatible_environment_is_refused_and_recorded(self) -> None:
        adapted = self.service.adapt(
            self.skill.id, goal="copy elsewhere", environment="plan9"
        )
        self.assertFalse(adapted.compatible)
        self.assertIsNone(adapted.skill)
        self.assertEqual(len(self.service.adaptation_failures), 1)
        self.assertEqual(self.service.catalog.get(self.skill.id, 1).lifecycle, SkillLifecycle.VALIDATED)

    async def test_environment_target_map_cannot_relabel_an_unsupported_os(self) -> None:
        skill = Skill(
            name="Windows-only procedure",
            objective="open a Windows application",
            steps=(
                SkillStep(
                    "open calculator",
                    kind=SkillStepKind.DESKTOP,
                    environment="windows",
                ),
            ),
            lifecycle=SkillLifecycle.VALIDATED,
            environments=("windows",),
        )
        self.service.catalog.add(skill)
        adapted = self.service.adapt(
            skill.id,
            goal="open it elsewhere",
            environment="plan9",
            target_map={"windows": "plan9"},
        )
        self.assertFalse(adapted.compatible)
        self.assertIsNone(adapted.skill)
        self.assertTrue(any("unsupported" in reason for reason in adapted.reasons))

    async def test_cross_application_target_is_replaced_only_when_supported(self) -> None:
        skill = Skill(
            name="Open calculator",
            objective="open a desktop application",
            steps=(
                SkillStep(
                    "open calculator",
                    kind=SkillStepKind.DESKTOP,
                    environment="windows",
                ),
                SkillStep("verify the window is open", kind=SkillStepKind.VERIFY, verify=True),
            ),
            lifecycle=SkillLifecycle.VALIDATED,
            environments=("windows",),
        )
        self.service.catalog.add(skill)
        adapted = self.service.adapt(
            skill.id,
            goal="open notepad",
            environment="windows",
            target_map={"calculator": "notepad"},
        )
        self.assertTrue(adapted.compatible)
        assert adapted.skill is not None
        self.assertIn("notepad", adapted.skill.steps[0].instruction)
        self.assertNotIn("calculator", adapted.skill.steps[0].instruction)


class CompositionTests(unittest.IsolatedAsyncioTestCase):
    def _skill(self, name: str, *, outputs: tuple[str, ...], dependencies: tuple[str, ...] = (), skill_id: str = "") -> Skill:
        return Skill(
            id=skill_id or name.lower().replace(" ", "-"),
            name=name,
            objective=f"do {name}",
            steps=(SkillStep(f"perform {name}"),),
            outputs=outputs,
            dependencies=dependencies,
            lifecycle=SkillLifecycle.VALIDATED,
        )

    def test_composition_orders_by_dependencies_and_checks_inputs(self) -> None:
        first = self._skill("Extract", outputs=("rows",))
        second = self._skill("Report", outputs=("report",), dependencies=("extract",))
        composer = SkillService(permissions=permissions())
        result = composer.compose(
            "produce a report",
            [second, first],
            inputs_by_skill={first.id: {}, second.id: {"rows": "supplied"}},
        )
        self.assertTrue(result.compatible)
        self.assertEqual(result.ordered_skill_ids, (first.id, second.id))

    def test_duplicate_outputs_and_missing_dependencies_are_conflicts(self) -> None:
        first = self._skill("Extract A", outputs=("rows",))
        second = self._skill("Extract B", outputs=("rows",))
        third = self._skill("Report", outputs=("report",), dependencies=("missing-skill",))
        result = SkillService(permissions=permissions()).compose(
            "produce a report", [first, second, third]
        )
        self.assertFalse(result.compatible)
        self.assertTrue(any("produced by both" in item for item in result.conflicts))
        self.assertTrue(any("not part of the composition" in item for item in result.conflicts))

    async def test_composed_execution_binds_a_prior_skills_declared_output(self) -> None:
        calls: list[dict] = []

        async def executor(step, inputs, context) -> StepOutcome:
            if step.instruction == "extract rows":
                return StepOutcome(True, "rows ready", data={"rows": [3, 4]})
            calls.append(dict(inputs))
            return StepOutcome(True, "report ready", data={"report": "ok"})

        service = SkillService(permissions=permissions(), execute_step=executor)
        first = Skill(
            id="extract",
            name="Extract",
            objective="extract rows",
            steps=(SkillStep("extract rows"),),
            outputs=("rows",),
            lifecycle=SkillLifecycle.VALIDATED,
        )
        second = Skill(
            id="report",
            name="Report",
            objective="write a report",
            steps=(SkillStep("render {rows}"),),
            inputs=(SkillInput("rows"),),
            outputs=("report",),
            dependencies=("extract",),
            lifecycle=SkillLifecycle.VALIDATED,
        )
        service.catalog.add(first)
        service.catalog.add(second)
        result = await service.compose_and_execute(
            "extract and report", [first.id, second.id], approved=True
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(calls, [{"rows": [3, 4]}])
        self.assertEqual(result["outputs"], {"rows": [3, 4], "report": "ok"})

    async def test_composed_execution_stops_on_failure(self) -> None:
        calls: list[str] = []

        async def executor(step, inputs, context) -> StepOutcome:
            calls.append(step.instruction)
            if "explode" in step.instruction.lower():
                return StepOutcome(False, error="synthetic failure")
            return StepOutcome(True, "ok")

        service = SkillService(permissions=permissions(), execute_step=executor)
        first = self._skill("Prepare", outputs=("value",))
        second = self._skill(
            "Explode", outputs=("report",), dependencies=("prepare",)
        )
        service.catalog.add(first)
        service.catalog.add(second)
        result = await service.compose_and_execute(
            "prepare and report", [first.id, second.id], approved=True
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(calls, ["perform Prepare", "perform Explode"])


class ExecutionSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_input_blocks_before_any_step(self) -> None:
        calls: list[str] = []

        async def executor(step, inputs, context) -> StepOutcome:
            calls.append(step.tool)
            return StepOutcome(True)

        service = SkillService(permissions=permissions(), execute_step=executor)
        skill = Skill(
            name="Needs input",
            objective="needs a real input",
            steps=(SkillStep("do {required}", tool="step"),),
            inputs=(SkillInput("required"),),
            lifecycle=SkillLifecycle.VALIDATED,
        )
        service.catalog.add(skill)
        result = await service.execute(skill.id)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(calls, [])

    async def test_confirmation_required_parks_without_execution(self) -> None:
        calls: list[str] = []

        async def executor(step, inputs, context) -> StepOutcome:
            calls.append(step.tool)
            return StepOutcome(True)

        service = SkillService(permissions=permissions(confirm=True), execute_step=executor)
        skill = Skill(
            name="Sensitive",
            objective="change something",
            steps=(SkillStep("change it", tool="step", permission=WRITE),),
            permissions=(WRITE,),
            lifecycle=SkillLifecycle.VALIDATED,
        )
        service.catalog.add(skill)
        result = await service.execute(skill.id)
        self.assertEqual(result["status"], "waiting_approval")
        self.assertEqual(calls, [])
        approved = await service.execute(skill.id, approved=True)
        self.assertEqual(approved["status"], "completed")
        self.assertEqual(calls, ["step"])

    async def test_step_environment_mismatch_blocks_before_any_step(self) -> None:
        calls: list[str] = []

        async def executor(step, inputs, context) -> StepOutcome:
            calls.append(step.tool)
            return StepOutcome(True)

        service = SkillService(
            permissions=permissions(), execute_step=executor, environment="windows"
        )
        skill = Skill(
            name="Wrong platform step",
            objective="run only on its declared platform",
            steps=(
                SkillStep(
                    "copy file", kind=SkillStepKind.TOOL, tool="copy_file",
                    permission=WRITE, environment="linux",
                ),
            ),
            environments=("windows",),
            lifecycle=SkillLifecycle.VALIDATED,
        )
        service.catalog.add(skill)
        result = await service.execute(skill.id)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("step 1 is declared for 'linux'", result["reason"])
        self.assertEqual(calls, [])

    async def test_required_input_is_not_silently_filled_from_its_example(self) -> None:
        calls: list[str] = []

        async def executor(step, inputs, context) -> StepOutcome:
            calls.append(step.tool)
            return StepOutcome(True)

        service = SkillService(permissions=permissions(), execute_step=executor)
        skill = Skill(
            name="Example is documentation",
            objective="use a real binding",
            steps=(SkillStep("do {required}", tool="step"),),
            inputs=(SkillInput("required", example="value-from-acquisition"),),
            lifecycle=SkillLifecycle.VALIDATED,
        )
        service.catalog.add(skill)
        result = await service.execute(skill.id)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("missing required inputs", result["reason"])
        self.assertEqual(calls, [])

    async def test_no_permission_layer_never_executes(self) -> None:
        calls: list[str] = []

        async def executor(step, inputs, context) -> StepOutcome:
            calls.append(step.tool)
            return StepOutcome(True)

        service = SkillService(execute_step=executor)
        skill = Skill(
            name="Ungated",
            objective="must not run",
            steps=(SkillStep("run", tool="step"),),
            lifecycle=SkillLifecycle.VALIDATED,
        )
        service.catalog.add(skill)
        result = await service.execute(skill.id)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(calls, [])


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_skips_verified_steps(self) -> None:
        calls: list[int] = []
        fail_once = {"value": True}

        async def executor(step, inputs, context) -> StepOutcome:
            index = int(context["step_index"])
            calls.append(index)
            if index == 1 and fail_once["value"]:
                fail_once["value"] = False
                return StepOutcome(False, error="temporary")
            return StepOutcome(True, "ok")

        service = SkillService(permissions=permissions(), execute_step=executor)
        skill = Skill(
            name="Two steps",
            objective="run two steps",
            steps=(
                SkillStep("first", tool="one"),
                SkillStep("second", tool="two"),
                SkillStep("verify", tool="three", verify=True),
            ),
            lifecycle=SkillLifecycle.VALIDATED,
        )
        service.catalog.add(skill)
        first = await service.execute(skill.id)
        self.assertEqual(first["status"], "failed")
        execution_id = first["id"]
        resumed = await service.resume(execution_id)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(calls, [0, 1, 1, 2])

    async def test_deprecation_does_not_activate_an_unvalidated_adaptation(self) -> None:
        service = SkillService(permissions=permissions())
        with tempfile.TemporaryDirectory() as directory:
            result = service.acquire("copy a text file", observations(Path(directory)))
            assert result.skill is not None
            first = service.validate(result.skill.id)
            adapted = service.adapt(first.id, goal="copy a report")
            assert adapted.skill is not None
            service.deprecate(first.id, reason="replaced")
            self.assertEqual(service.catalog.stats()["active"][first.id], 0)
            with self.assertRaises(KeyError):
                service.catalog.get(first.id)
            with self.assertRaises(ValueError):
                service.validate(first.id, 1)

    async def test_promote_and_rollback_refuse_an_unvalidated_adaptation(self) -> None:
        service = SkillService(permissions=permissions())
        with tempfile.TemporaryDirectory() as directory:
            result = service.acquire("copy a text file", observations(Path(directory)))
            assert result.skill is not None
            first = service.validate(result.skill.id)
            adapted = service.adapt(first.id, goal="copy a report")
            assert adapted.skill is not None
            with self.assertRaises(ValueError):
                service.promote(first.id, adapted.skill.version)
            with self.assertRaises(ValueError):
                service.rollback(first.id, adapted.skill.version)
            service.catalog.update(
                adapted.skill.with_(lifecycle=SkillLifecycle.VALIDATED)
            )
            promoted = service.promote(first.id, adapted.skill.version)
            self.assertEqual(service.catalog.get(first.id).version, adapted.skill.version)
            self.assertEqual(promoted.version, adapted.skill.version)

    async def test_deprecation_and_rollback_preserve_validated_versions(self) -> None:
        service = SkillService(permissions=permissions())
        with tempfile.TemporaryDirectory() as directory:
            result = service.acquire("copy a text file", observations(Path(directory)))
            assert result.skill is not None
            first = service.validate(result.skill.id)
            adapted = service.adapt(first.id, goal="copy a report")
            assert adapted.skill is not None
            service.catalog.update(
                adapted.skill.with_(lifecycle=SkillLifecycle.VALIDATED)
            )
            service.catalog.promote(first.id, adapted.skill.version)
            service.rollback(first.id, 1)
            self.assertEqual(service.catalog.get(first.id).version, 1)
            self.assertEqual(service.catalog.get(first.id, 2).lifecycle, SkillLifecycle.DEPRECATED)
            self.assertEqual(service.catalog.get(first.id, 1).lifecycle, SkillLifecycle.VALIDATED)

    def test_reliability_is_derived_from_evidence(self) -> None:
        reliability = SkillReliability(trials=2, successes=2)
        self.assertEqual(reliability.success_rate, 1.0)
        failed = reliability.record(verified=False, duration_ms=10)
        self.assertEqual(failed.failures, 1)
        self.assertEqual(failed.success_rate, 0.6667)
        self.assertGreater(failed.confidence, 0)


class ApplicationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """The real application, real tools and the Phase 30 objective executor."""

    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.data = Path(self.directory.name) / "data"
        self.app = NovaControlApplication(data_dir=self.data)

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def _observations(self) -> list[dict]:
        rows: list[dict] = []
        for _ in range(2):
            result = await self.app.tool_executor.execute(
                ToolRequest("machine_facts", {"scope": "all"})
            )
            self.assertIs(result.status, ToolStatus.COMPLETED)
            rows.append(
                {
                    "goal": "report stable machine facts",
                    "steps": [
                        {
                            "instruction": "report stable machine facts",
                            "kind": "tool",
                            "tool": "machine_facts",
                        }
                    ],
                    "verified": True,
                    "environment": self.app.skill_service.environment,
                    "inputs": {"scope": "all"},
                    "tools": ["machine_facts"],
                    "duration_ms": 1.0,
                }
            )
        return rows

    async def test_acquire_validate_execute_and_phase30_objective(self) -> None:
        acquired = self.app.acquire_skill(
            "report stable machine facts",
            await self._observations(),
            name="Report machine facts",
        )
        self.assertTrue(acquired["created"])
        skill_id = acquired["skill"]["id"]
        validated = self.app.validate_skill(skill_id)
        self.assertEqual(validated["lifecycle"], "validated")

        executed = await self.app.execute_skill(
            skill_id, inputs={"scope": "all"}
        )
        self.assertEqual(executed["status"], "completed")
        self.assertTrue(executed["verified"])

        objective = self.app.create_objective(
            "Machine report",
            "report stable machine facts",
            autonomy="execute",
        )
        task = self.app.add_objective_task(
            objective["id"],
            "Skill-backed report",
            "report stable machine facts",
            agent=f"skill:{skill_id}",
            metadata={"skill_inputs": {"scope": "all"}},
        )
        result = await self.app.run_objective(objective["id"])
        self.assertEqual(result["results"][0]["status"], "completed", result)
        stored = self.app.operations.manager.get(objective["id"]).task(task["id"])
        self.assertEqual(stored.state.value, "completed")
        self.assertTrue(stored.verified)

    async def test_catalog_survives_restart_with_its_lifecycle(self) -> None:
        acquired = self.app.acquire_skill(
            "report stable machine facts",
            await self._observations(),
            name="Persistent machine facts",
        )
        skill_id = acquired["skill"]["id"]
        self.app.validate_skill(skill_id)
        restarted = NovaControlApplication(data_dir=self.data)
        restored = restarted.skill_service.catalog.get(skill_id)
        self.assertEqual(restored.lifecycle, SkillLifecycle.VALIDATED)
        self.assertEqual(restored.reliability.trials, 2)


class LearningIntegrationTests(unittest.TestCase):
    def test_continual_suggestion_is_reported_without_changing_the_skill(self) -> None:
        class Learning:
            def recommend(self, goal: str):
                return type("R", (), {"source": "strategy"})()

        with tempfile.TemporaryDirectory() as directory:
            service = SkillService(permissions=permissions(), learning=Learning())
            result = service.acquire("copy a text file", observations(Path(directory)))
            assert result.skill is not None
            service.validate(result.skill.id)
            hits = service.retrieve("copy a text file")
            self.assertTrue(hits)
            self.assertTrue(any("continual learning" in reason for reason in hits[0].reasons))

    def test_validated_learning_preference_reranks_and_discourages(self) -> None:
        class Learning:
            def recommend(self, goal: str):
                return type(
                    "R",
                    (),
                    {
                        "source": "strategy",
                        "compatible": True,
                        "strategy_id": "copy#v2",
                        "prefer_tools": ("beta",),
                        "avoid_tools": ("alpha",),
                        "evidence_ids": ("exp:1", "exp:2"),
                    },
                )()

        service = SkillService(permissions=permissions(), learning=Learning())
        alpha = Skill(
            name="Alpha copy",
            objective="copy a file",
            steps=(SkillStep("use alpha", tool="alpha"),),
            tools=("alpha",),
            lifecycle=SkillLifecycle.VALIDATED,
        )
        beta = Skill(
            name="Beta copy",
            objective="copy a file",
            steps=(SkillStep("use beta", tool="beta"),),
            tools=("beta",),
            lifecycle=SkillLifecycle.VALIDATED,
        )
        service.catalog.add(alpha)
        service.catalog.add(beta)
        hits = service.retrieve("copy a file")
        self.assertEqual(hits[0].skill_id, beta.id)
        self.assertTrue(any("prefers: beta" in reason for reason in hits[0].reasons))
        avoided = next(hit for hit in hits if hit.skill_id == alpha.id)
        self.assertTrue(any("discourages: alpha" in reason for reason in avoided.reasons))


class _NoProcedureAgent(SpecialistAgent):
    """A specialist with no procedure of its own for the goal it is given."""

    def __init__(self, pipeline=None, steps=()) -> None:
        super().__init__(
            "probe-agent", AgentRole.PLANNING, "has no local procedure", pipeline=pipeline
        )
        self._steps = tuple(steps)
        self.executed: list[str] = []

    def declarations(self):
        return {"own_tool": PermissionDeclaration()}

    def clone(self, pipeline):
        return _NoProcedureAgent(pipeline, self._steps)

    def decide(self, task, interpretation, context):
        return SpecialistDecision(
            action="select_procedure", reason="no local procedure", confidence=0.4
        )

    def plan(self, task, interpretation, decision, context):
        return self._steps

    async def execute(self, step, run):
        self.executed.append(step.action)
        return PipelineOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED,
            detail="the agent's own step ran",
            output={"ok": True},
            tool=step.tool,
        )


class _FakeSkillProvider:
    """A provider that records what the pipeline asked it."""

    def __init__(self, rows=(), result=None) -> None:
        self.rows = tuple(rows)
        self.result = result or {
            "status": "completed",
            "verified": True,
            "results": [{"index": 0, "verified": True, "detail": "the skill step verified"}],
        }
        self.select_calls: list[dict] = []
        self.executed: list[dict] = []

    def select(self, goal, *, inputs=None, skill="", limit=5):
        self.select_calls.append(
            {"goal": goal, "inputs": dict(inputs or {}), "skill": skill, "limit": limit}
        )
        return self.rows

    async def execute(self, skill_id, *, goal, inputs, version=None):
        self.executed.append(
            {"skill_id": skill_id, "goal": goal, "inputs": dict(inputs), "version": version}
        )
        return dict(self.result)


def _match(**overrides):
    row = {
        "skill_id": "skill-1",
        "name": "Report machine facts",
        "version": 2,
        "score": 0.8,
        "lifecycle": "validated",
        "compatible": True,
        "missing_inputs": [],
        "missing_capabilities": [],
    }
    row.update(overrides)
    return row


class InPipelineSkillSelectionTests(unittest.IsolatedAsyncioTestCase):
    """Phase 28 x 31: a specialist selects a validated skill inside its pipeline."""

    async def test_a_validated_skill_is_selected_executed_and_verified(self) -> None:
        provider = _FakeSkillProvider([_match()])
        agent = _NoProcedureAgent(SpecialistPipeline(skills=provider))
        run = await agent.run_task(
            "report the machine facts", metadata={"skill_inputs": {"scope": "all"}}
        )
        self.assertIs(run.status, AgentTaskStatus.COMPLETED, run.stages)
        self.assertTrue(run.verified)
        self.assertEqual(agent.executed, [])
        self.assertEqual(provider.executed[0]["skill_id"], "skill-1")
        self.assertEqual(provider.executed[0]["inputs"], {"scope": "all"})
        self.assertEqual(provider.executed[0]["version"], 2)
        self.assertEqual(run.plan[0]["tool"], SKILL_TOOL)
        self.assertTrue(provider.select_calls[0]["inputs"] == {"scope": "all"})

    async def test_an_unvalidated_skill_is_not_selected(self) -> None:
        provider = _FakeSkillProvider([_match(lifecycle="acquired")])
        agent = _NoProcedureAgent(SpecialistPipeline(skills=provider))
        run = await agent.run_task("report the machine facts")
        self.assertIs(run.status, AgentTaskStatus.FAILED)
        self.assertEqual(provider.executed, [])
        self.assertIn("nothing to do", run.stage(StageName.PLANNER).detail)

    async def test_an_incompatible_skill_is_not_selected(self) -> None:
        provider = _FakeSkillProvider([_match(compatible=False)])
        agent = _NoProcedureAgent(SpecialistPipeline(skills=provider))
        run = await agent.run_task("report the machine facts")
        self.assertIs(run.status, AgentTaskStatus.FAILED)
        self.assertEqual(provider.executed, [])

    async def test_an_unbound_required_input_is_not_silently_substituted(self) -> None:
        provider = _FakeSkillProvider([_match(missing_inputs=["scope"])])
        agent = _NoProcedureAgent(SpecialistPipeline(skills=provider))
        run = await agent.run_task("report the machine facts")
        self.assertIs(run.status, AgentTaskStatus.FAILED)
        self.assertEqual(provider.executed, [])

    async def test_no_provider_keeps_the_previous_behaviour(self) -> None:
        agent = _NoProcedureAgent(SpecialistPipeline())
        run = await agent.run_task("report the machine facts")
        self.assertIs(run.status, AgentTaskStatus.FAILED)
        self.assertIn("nothing to do", run.stage(StageName.PLANNER).detail)

    async def test_an_existing_plan_is_never_replaced_by_a_skill(self) -> None:
        provider = _FakeSkillProvider([_match()])
        agent = _NoProcedureAgent(
            SpecialistPipeline(skills=provider),
            steps=(PipelineStep(action="own_step", tool="own_tool"),),
        )
        run = await agent.run_task("report the machine facts")
        self.assertIs(run.status, AgentTaskStatus.COMPLETED, run.stages)
        self.assertEqual(provider.select_calls, [])
        self.assertEqual(agent.executed, ["own_step"])

    async def test_a_refused_skill_step_is_not_retried(self) -> None:
        # Retrying a refusal reuses a verdict that will not change, and for a
        # skill it starts a second execution with a second approval checkpoint.
        provider = _FakeSkillProvider(
            [_match()],
            result={
                "status": "waiting_approval",
                "reason": "the skill declares a sensitive scope",
                "verified": False,
            },
        )
        agent = _NoProcedureAgent(SpecialistPipeline(skills=provider))
        run = await agent.run_task("report the machine facts")
        self.assertIs(run.status, AgentTaskStatus.FAILED)
        self.assertEqual(len(provider.executed), 1)
        self.assertIn("not retried", run.stage(StageName.RECOVERY).detail)

    async def test_an_explicitly_requested_skill_wins_over_the_agents_own_plan(self) -> None:
        # Every shipped specialist returns a plan of its own, so an explicit
        # request that could never be honoured was a silent no-op.
        provider = _FakeSkillProvider([_match(name="Report machine facts")])
        agent = _NoProcedureAgent(
            SpecialistPipeline(skills=provider),
            steps=(PipelineStep(action="own_step", tool="own_tool"),),
        )
        run = await agent.run_task(
            "report the machine facts", metadata={"skill": "Report machine facts"}
        )
        self.assertIs(run.status, AgentTaskStatus.COMPLETED, run.stages)
        self.assertEqual(provider.select_calls[0]["skill"], "Report machine facts")
        self.assertEqual(run.plan[0]["tool"], SKILL_TOOL)
        self.assertEqual(agent.executed, [])

    async def test_an_explicit_request_for_an_unusable_skill_keeps_the_own_plan(self) -> None:
        provider = _FakeSkillProvider([_match(lifecycle="acquired")])
        agent = _NoProcedureAgent(
            SpecialistPipeline(skills=provider),
            steps=(PipelineStep(action="own_step", tool="own_tool"),),
        )
        run = await agent.run_task(
            "report the machine facts", metadata={"skill": "Report machine facts"}
        )
        self.assertEqual(agent.executed, ["own_step"])
        self.assertEqual(provider.executed, [])
        self.assertNotEqual(run.plan[0]["tool"], SKILL_TOOL)

    async def test_a_skill_that_did_not_verify_cannot_complete_the_run(self) -> None:
        provider = _FakeSkillProvider(
            [_match()],
            result={
                "status": "completed",
                "verified": False,
                "results": [{"index": 0, "verified": False, "detail": "no read-back"}],
            },
        )
        agent = _NoProcedureAgent(SpecialistPipeline(skills=provider))
        run = await agent.run_task("report the machine facts")
        self.assertIs(run.status, AgentTaskStatus.FAILED)
        self.assertFalse(run.verified)
        verdict = run.verifications[run.plan[0]["id"]]
        self.assertEqual(verdict["status"], "fail")

    async def test_a_skill_waiting_for_approval_is_a_refusal_not_a_success(self) -> None:
        provider = _FakeSkillProvider(
            [_match()],
            result={
                "status": "waiting_approval",
                "reason": "the skill declares a sensitive scope",
                "verified": False,
            },
        )
        agent = _NoProcedureAgent(SpecialistPipeline(skills=provider))
        run = await agent.run_task("report the machine facts")
        self.assertIs(run.status, AgentTaskStatus.FAILED)
        self.assertEqual(run.outcomes[0].status.value, "denied")
        self.assertIn("sensitive scope", run.outcomes[0].detail)

    async def test_an_explicitly_requested_skill_is_forwarded(self) -> None:
        provider = _FakeSkillProvider([_match(name="Report machine facts")])
        agent = _NoProcedureAgent(SpecialistPipeline(skills=provider))
        run = await agent.run_task(
            "do the thing", metadata={"skill": "Report machine facts"}
        )
        self.assertIs(run.status, AgentTaskStatus.COMPLETED, run.stages)
        self.assertEqual(provider.select_calls[0]["skill"], "Report machine facts")


class InPipelineSkillApplicationTests(unittest.IsolatedAsyncioTestCase):
    """The real application's own skill library, selected inside the pipeline."""

    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.data = Path(self.directory.name) / "data"
        self.app = NovaControlApplication(data_dir=self.data)

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def _observations(self) -> list[dict]:
        rows: list[dict] = []
        for _ in range(2):
            result = await self.app.tool_executor.execute(
                ToolRequest("machine_facts", {"scope": "all"})
            )
            self.assertIs(result.status, ToolStatus.COMPLETED)
            rows.append(
                {
                    "goal": "report stable machine facts",
                    "steps": [
                        {
                            "instruction": "report stable machine facts",
                            "kind": "tool",
                            "tool": "machine_facts",
                        }
                    ],
                    "verified": True,
                    "environment": self.app.skill_service.environment,
                    "inputs": {"scope": "all"},
                    "tools": ["machine_facts"],
                    "duration_ms": 1.0,
                }
            )
        return rows

    async def test_the_pipeline_selects_from_the_applications_own_library(self) -> None:
        acquired = self.app.acquire_skill(
            "report stable machine facts",
            await self._observations(),
            name="Pipeline machine facts",
        )
        self.assertTrue(acquired["created"])
        skill_id = acquired["skill"]["id"]
        self.app.validate_skill(skill_id)

        agent = _NoProcedureAgent(self.app._specialist_pipeline)
        run = await agent.run_task(
            "report stable machine facts",
            metadata={"skill_inputs": {"scope": "all"}},
        )
        self.assertIs(run.status, AgentTaskStatus.COMPLETED, run.stages)
        self.assertTrue(run.verified)
        # The agent's own execute hook never ran: the library did the work.
        self.assertEqual(agent.executed, [])
        self.assertEqual(run.plan[0]["tool"], SKILL_TOOL)
        self.assertEqual(run.plan[0]["target"], skill_id)

    async def test_an_unvalidated_library_skill_is_not_executed_in_pipeline(self) -> None:
        acquired = self.app.acquire_skill(
            "report stable machine facts",
            await self._observations(),
            name="Unvalidated pipeline facts",
        )
        self.assertTrue(acquired["created"])
        agent = _NoProcedureAgent(self.app._specialist_pipeline)
        run = await agent.run_task("report stable machine facts")
        self.assertIs(run.status, AgentTaskStatus.FAILED)
        self.assertEqual(agent.executed, [])
