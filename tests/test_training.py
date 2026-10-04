"""Phase 16: supervised fine-tuning — datasets, runs, checkpoints, evaluation, registry.

The suite runs entirely on deterministic fixtures, mocks and dry-run mode: no
model is downloaded, no GPU is required and nothing here trains for real. That
is a requirement of the phase rather than a convenience — a 16 GB Windows
desktop with an Intel iGPU and an NPU must be able to prove the training
subsystem works without loading Qwen3 8B, so every dataset is built from
synthetic Phase 15 trajectories, every trainer is the DRY-RUN backend or a mock,
and the PEFT/LoRA boundary is exercised through an injected runner.

Two policies get their own tests because they are the phase's safety spine:
a real (non-dry-run) run needs confirmation AND a deployment that permits it,
and a model is never approved from a loss curve — only from a recorded passing
before/after evaluation.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
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
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.evaluation import (
    AgentTrajectory,
    DimensionScore,
    EvaluationDimension,
    EvaluationResult,
    ExecutionStep,
    LatencyMetrics,
    RecoveryRecord,
    ResourceUsage,
    ToolCallRecord,
    VerificationRecord,
)
from novacontrol.training import (
    CHECKPOINT_COMPLETE,
    CHECKPOINT_CORRUPT,
    CHECKPOINT_INCOMPLETE,
    FORBIDDEN_REASONING_KEYS,
    PREPROCESSING_VERSION,
    SPLIT_NAMES,
    TRACKED_METRICS,
    CallablePredictor,
    CheckpointManager,
    CheckpointRecord,
    DatasetStatistics,
    DatasetType,
    DryRunTrainer,
    HardwareCapabilities,
    HardwarePolicy,
    ModelStatus,
    PeftLoraBackend,
    ResourceEstimator,
    ResourceVerdict,
    SFTDatasetBuilder,
    SFTDatasetVersion,
    SFTModelRegistry,
    SFTTrainingExample,
    SelectionRules,
    SplitConfig,
    TrainingBackendUnavailable,
    TrainingCallbacks,
    TrainingConfig,
    TrainingEvaluation,
    TrainingEvaluator,
    TrainingManager,
    TrainingMethod,
    TrainingRun,
    TrainingRunStatus,
    dataset_version_id,
    detect_hardware,
    next_version,
    reasoning_violations,
    resolve_backend,
    source_data_version,
)
from novacontrol.training import datasets as builder_module
from novacontrol.training.evaluation import LOWER_IS_BETTER, NOISE_FLOOR_METRICS
from novacontrol.training.backends import SFTTrainer
from novacontrol.training.manager import (
    DATASET_BUILT,
    EVALUATION_COMPLETED,
    MODEL_REGISTERED,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_CREATED,
    RUN_FAILED,
    RUN_STARTED,
)
from novacontrol.training.models import ALLOWED_STATUS_TRANSITIONS
from novacontrol.training.runtime import TrainingModule

SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123"


# ── fixtures ────────────────────────────────────────────────────────────────


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
            "steps": [{"step_id": "s1", "action": "launch", "description": "launch"}],
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
            ToolCallRecord(
                tool="desktop.launch",
                step_id="s1",
                capability="desktop",
                arguments={"app": "calculator"},
                status="completed",
                duration_ms=120.0,
            ),
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


def make_result(**overrides: Any) -> EvaluationResult:
    """A passing Phase 15 evaluation with every dimension scored."""
    fields: dict[str, Any] = {
        "evaluation_id": "ev-1",
        "trajectory_id": "traj-1",
        "task_id": "task-1",
        "overall_status": "ok",
        "task_success": True,
        "dimensions": tuple(
            DimensionScore(dimension=member.value, score=0.9, status="ok")
            for member in EvaluationDimension
        ),
    }
    fields.update(overrides)
    return EvaluationResult(**fields)


def make_example(index: int = 0, *, group: str = "g1", **overrides: Any) -> SFTTrainingExample:
    """One NLU example by hand, for tests that do not need a trajectory."""
    fields: dict[str, Any] = {
        "example_id": f"ex-{index}",
        "dataset_type": DatasetType.NLU.value,
        "input": {"request": f"request {index}"},
        "context": {},
        "target": {"intent": "open_application", "confidence": 0.9},
        "metadata": {"group_key": group},
        "source_trajectory_id": f"traj-{index}",
        "tags": ("nlu",),
    }
    fields.update(overrides)
    return SFTTrainingExample(**fields)


def make_built_dataset(examples: list[SFTTrainingExample] | None = None, **overrides: Any) -> SFTDatasetVersion:
    """A structurally valid dataset built from hand-made examples."""
    items = (
        list(examples)
        if examples is not None
        else [make_example(index, group=f"g{index}") for index in range(10)]
    )
    builder = SFTDatasetBuilder()
    splits = builder.split_examples(items, SplitConfig())
    fields: dict[str, Any] = {
        "dataset_version_id": dataset_version_id("handmade", "1.0.0"),
        "name": "handmade",
        "version": "1.0.0",
        "dataset_type": DatasetType.NLU.value,
        "examples": tuple(items),
        "splits": splits,
        "statistics": DatasetStatistics(
            total=len(items),
            by_split={name: len(ids) for name, ids in splits.items()},
            estimated_tokens=200,
        ),
        "source_data_version": "handmade=1.0.0",
        "preprocessing_version": PREPROCESSING_VERSION,
    }
    fields.update(overrides)
    return SFTDatasetVersion(**fields)


class GoodPredictor:
    """A model that answers every example exactly as the dataset does."""

    name = "candidate"

    def predict(self, example: SFTTrainingExample) -> dict[str, Any]:
        return dict(example.target)


class BadPredictor:
    """A model that answers confidently and wrongly."""

    name = "base"

    def predict(self, example: SFTTrainingExample) -> dict[str, Any]:
        return {"intent": "chat", "confidence": 0.1}


class DeadPredictor:
    """A model that cannot answer at all: the case that must NOT read as a pass."""

    name = "broken"

    def predict(self, example: SFTTrainingExample) -> dict[str, Any]:
        raise RuntimeError("no weights loaded")


def make_manager(root: str | Path, **overrides: Any) -> TrainingManager:
    """A manager wired to JSONL repositories under a temporary directory."""
    datasets, runs, checkpoints, models, evaluations = __import__(
        "novacontrol.training", fromlist=["build_training_repositories"]
    ).build_training_repositories(root)
    kwargs: dict[str, Any] = {
        "datasets": datasets,
        "runs": runs,
        "checkpoints": checkpoints,
        "models": models,
        "evaluations": evaluations,
        "output_root": Path(root) / "training_output",
    }
    kwargs.update(overrides)
    return TrainingManager(**kwargs)


def as_dry_run(root: str | Path, **overrides: Any) -> TrainingManager:
    """A manager whose deployment default is a DRY RUN — the safe configuration."""
    config = TrainingConfig(
        base_model="tinyllama",
        dataset_version="unset@1.0.0",
        output_directory=str(Path(root) / "training_output"),
        dry_run=True,
    )
    return make_manager(root, default_config=config, **overrides)


# ── the schema ──────────────────────────────────────────────────────────────


class TrainingSchemaTests(unittest.TestCase):
    """The vocabulary: types, statuses, transitions and the no-reasoning rule."""

    def test_seven_dataset_types_one_target_schema_each(self) -> None:
        self.assertEqual(
            [member.value for member in DatasetType],
            [
                "nlu",
                "decision",
                "tool_selection",
                "planning",
                "recovery",
                "developer",
                "research",
            ],
        )

    def test_nine_run_statuses_and_only_three_are_terminal(self) -> None:
        statuses = list(TrainingRunStatus)
        self.assertEqual(len(statuses), 9)
        terminal = {member.value for member in statuses if member.terminal}
        self.assertEqual(terminal, {"completed", "failed", "cancelled"})

    def test_six_model_statuses(self) -> None:
        self.assertEqual(
            [member.value for member in ModelStatus],
            [
                "experimental",
                "evaluating",
                "approved",
                "production",
                "deprecated",
                "rejected",
            ],
        )

    def test_a_model_cannot_jump_from_experimental_to_production(self) -> None:
        self.assertNotIn("production", ALLOWED_STATUS_TRANSITIONS["experimental"])
        self.assertIn("production", ALLOWED_STATUS_TRANSITIONS["approved"])
        self.assertEqual(ALLOWED_STATUS_TRANSITIONS["deprecated"], frozenset())

    def test_three_resource_verdicts_and_four_hardware_policies(self) -> None:
        self.assertEqual([member.value for member in ResourceVerdict], ["safe", "warning", "unsafe"])
        self.assertEqual(
            [member.value for member in HardwarePolicy],
            ["auto", "local_cpu", "local_gpu", "local_npu"],
        )

    def test_lora_is_the_default_method_and_full_needs_use_lora_off(self) -> None:
        self.assertEqual(
            [member.value for member in TrainingMethod], ["lora", "qlora", "full"]
        )
        self.assertEqual(TrainingConfig().effective_method, TrainingMethod.LORA.value)
        self.assertEqual(
            TrainingConfig(use_lora=False).effective_method, TrainingMethod.FULL.value
        )

    def test_hidden_reasoning_keys_are_named_and_found_anywhere_in_a_payload(self) -> None:
        self.assertIn("chainofthought", FORBIDDEN_REASONING_KEYS)
        self.assertIn("reasoning", FORBIDDEN_REASONING_KEYS)

        found = reasoning_violations(
            {"input": {"chain_of_thought": "I think..."}, "context": {"steps": [{"thinking": 1}]}}
        )

        self.assertEqual(len(found), 2)
        self.assertIn("input.chain_of_thought", found[0])
        self.assertEqual(reasoning_violations({"input": {"request": "open calculator"}}), ())

    def test_a_dataset_version_id_is_name_at_version(self) -> None:
        self.assertEqual(dataset_version_id("nlu", "1.2.3"), "nlu@1.2.3")
        self.assertEqual(dataset_version_id(" nlu ", ""), "nlu@")

    def test_source_data_version_names_what_the_dataset_was_built_from(self) -> None:
        text = source_data_version(
            trajectories=12, evaluations=12, rewards=11, extra="dataset_type=nlu"
        )

        self.assertIn("trajectory-schema=", text)
        self.assertIn("trajectories=12", text)
        self.assertIn("dataset_type=nlu", text)

    def test_the_next_version_never_reuses_one(self) -> None:
        self.assertEqual(next_version([], ""), "1.0.0")
        self.assertEqual(next_version(["1.0.0"], ""), "1.0.1")
        self.assertEqual(next_version(["1.0.0", "1.0.1"], ""), "1.0.2")
        self.assertEqual(next_version(["1.0.0"], "2.0.0"), "2.0.0")

    def test_tracked_metrics_are_one_vocabulary(self) -> None:
        self.assertIn("intent_accuracy", TRACKED_METRICS)
        self.assertIn("task_success", TRACKED_METRICS)
        self.assertIn("average_latency_ms", TRACKED_METRICS)
        self.assertEqual(SPLIT_NAMES, ("train", "validation", "test"))

    def test_every_record_round_trips_through_a_mapping(self) -> None:
        examples = [make_example(index) for index in range(4)]
        dataset = make_built_dataset(examples)
        run = TrainingRun(
            run_id="r1",
            name="probe",
            model="tinyllama",
            dataset_version=dataset.dataset_version_id,
            dataset_type=dataset.dataset_type,
            training_config=TrainingConfig(base_model="tinyllama").to_mapping(),
            status=TrainingRunStatus.COMPLETED.value,
            backend="dry_run",
            training_loss=0.4,
            total_steps=4,
        )
        checkpoint = CheckpointRecord(run_id="r1", path="x.json", kind="periodic", step=1)
        evaluation = TrainingEvaluation(run_id="r1", verdict="pass", reason="", tolerance=0.02)

        for original, restored in (
            (examples[0], SFTTrainingExample.from_dict(examples[0].to_dict())),
            (dataset, SFTDatasetVersion.from_dict(dataset.to_dict())),
            (run, TrainingRun.from_dict(run.to_dict())),
            (checkpoint, CheckpointRecord.from_dict(checkpoint.to_dict())),
            (evaluation, TrainingEvaluation.from_dict(evaluation.to_dict())),
        ):
            self.assertEqual(restored.to_dict(), original.to_dict())
            self.assertNotIn("reasoning", json.dumps(restored.to_dict(), default=str).lower())

    def test_an_empty_mapping_is_a_usable_record(self) -> None:
        self.assertEqual(TrainingRun.from_dict({}).status, TrainingRunStatus.CREATED.value)
        self.assertEqual(TrainingEvaluation.from_dict({}).verdict, "inconclusive")
        self.assertEqual(TrainingEvaluation.from_dict({}).reason, "")
        self.assertEqual(SFTTrainingExample.from_dict({}).dataset_type, DatasetType.NLU.value)
        self.assertEqual(SFTDatasetVersion.from_dict({}).examples, ())

    def test_selection_rules_round_trip(self) -> None:
        rules = SelectionRules(
            quality=("accepted",),
            require_success=True,
            min_reward=0.5,
            model="qwen",
            tags=("nlu",),
            max_examples=7,
        )

        restored = SelectionRules.from_mapping(rules.to_mapping())

        self.assertEqual(restored.min_reward, 0.5)
        self.assertEqual(restored.max_examples, 7)
        self.assertEqual(restored.tags, ("nlu",))
        self.assertTrue(restored.require_success)

    def test_split_ratios_and_their_issues(self) -> None:
        good = SplitConfig()
        bad = SplitConfig(train_ratio=0.5, validation_ratio=0.1, test_ratio=0.1)

        self.assertAlmostEqual(sum(good.ratios().values()), 1.0)
        self.assertEqual(good.issues(), ())
        self.assertTrue(bad.issues(), "ratios that do not add up must say so")
        self.assertEqual(
            SplitConfig.from_mapping(bad.to_mapping()).issues(), bad.issues()
        )

    def test_the_dataset_fingerprint_changes_with_its_content(self) -> None:
        first = make_built_dataset()
        second = make_built_dataset(
            examples=[make_example(index, target={"intent": "other"}) for index in range(10)]
        )

        self.assertEqual(first.fingerprint(), make_built_dataset().fingerprint())
        self.assertNotEqual(first.fingerprint(), second.fingerprint())


# ── configuration ───────────────────────────────────────────────────────────


class TrainingConfigTests(unittest.TestCase):
    """One validator, safe defaults, and no silent fallback that hides a mistake."""

    def test_defaults_are_a_dry_run_with_a_small_sequence_length(self) -> None:
        config = TrainingConfig()

        self.assertTrue(config.dry_run)
        self.assertEqual(config.epochs, 1)
        self.assertEqual(config.batch_size, 1)
        self.assertEqual(config.gradient_accumulation_steps, 8)
        self.assertEqual(config.learning_rate, 2e-4)
        self.assertLessEqual(config.max_sequence_length, 1024)
        self.assertTrue(config.gradient_checkpointing)
        self.assertTrue(config.use_lora)
        self.assertEqual(config.hardware_policy, HardwarePolicy.AUTO.value)
        self.assertLessEqual(config.max_checkpoints, 10)

    def test_validation_names_every_missing_field(self) -> None:
        report = TrainingConfig().validate()

        self.assertFalse(report.valid)
        self.assertIn("base_model is required", report.errors)
        self.assertEqual(TrainingConfig().validate().warnings, ())
        self.assertIn("dataset_version is required (name@version)", report.errors)
        self.assertIn("output_directory is required", report.errors)

    def test_validation_refuses_nonsense_numbers(self) -> None:
        config = TrainingConfig(
            base_model="tinyllama",
            dataset_version="nlu@1.0.0",
            output_directory="out",
            epochs=0,
            batch_size=0,
            learning_rate=0.0,
            lora_rank=0,
            lora_alpha=0,
        )

        errors = config.validate().errors

        self.assertTrue(any(error.startswith("epochs must be between") for error in errors))
        self.assertTrue(any(error.startswith("batch_size must be between") for error in errors))
        self.assertTrue(any(error.startswith("learning_rate must be in") for error in errors))
        self.assertTrue(any(error.startswith("lora_rank must be between") for error in errors))
        self.assertFalse(config.validate().valid)

    def test_an_unknown_hardware_policy_is_an_error_not_a_guess(self) -> None:
        config = TrainingConfig(
            base_model="tinyllama",
            dataset_version="nlu@1.0.0",
            output_directory="out",
            hardware_policy="cuda-or-bust",
        )

        self.assertTrue(
            any(error.startswith("hardware_policy must be one of") for error in config.validate().errors)
        )

    def test_an_unknown_precision_is_an_error(self) -> None:
        config = TrainingConfig(
            base_model="tinyllama",
            dataset_version="nlu@1.0.0",
            output_directory="out",
            precision="int3",
        )

        errors = config.validate().errors

        self.assertTrue(any(error.startswith("precision must be one of") for error in errors))
        self.assertIn("fp32", errors[0], "the message lists what is supported")

    def test_from_mapping_keeps_defaults_for_unusable_values(self) -> None:
        config = TrainingConfig.from_mapping(
            {
                "base_model": "tinyllama",
                "dataset_version": "nlu@1.0.0",
                "epochs": "many",
                "learning_rate": "fast",
                "dry_run": "yes please",
                "max_checkpoints": None,
            }
        )

        self.assertEqual(config.epochs, 1)
        self.assertEqual(config.learning_rate, 2e-4)
        self.assertTrue(config.dry_run)
        self.assertEqual(config.max_checkpoints, TrainingConfig().max_checkpoints)

    def test_a_number_out_of_range_is_kept_for_the_validator_to_reject(self) -> None:
        config = TrainingConfig.from_mapping(
            {"base_model": "tinyllama", "dataset_version": "nlu@1.0.0", "batch_size": -4}
        )

        self.assertEqual(config.batch_size, -4, "a parse is not a validation")
        self.assertFalse(config.validate().valid)

    def test_from_mapping_reads_the_values_it_can(self) -> None:
        config = TrainingConfig.from_mapping(
            {
                "base_model": "tinyllama",
                "dataset_version": "nlu@1.0.0",
                "output_directory": "out",
                "epochs": 3,
                "batch_size": 2,
                "learning_rate": 0.0001,
                "dry_run": False,
                "use_lora": False,
                "training_method": "full",
                "target_modules": ["q_proj", "v_proj"],
                "hardware_policy": "local_cpu",
            }
        )

        self.assertEqual(config.epochs, 3)
        self.assertEqual(config.batch_size, 2)
        self.assertAlmostEqual(config.learning_rate, 0.0001)
        self.assertFalse(config.dry_run)
        self.assertFalse(config.use_lora)
        self.assertEqual(config.target_modules, ("q_proj", "v_proj"))
        self.assertEqual(config.hardware_policy, "local_cpu")
        self.assertTrue(config.validate().valid)

    def test_lora_off_with_the_lora_method_is_an_inconsistency_not_a_silent_fix(self) -> None:
        config = TrainingConfig(
            base_model="tinyllama",
            dataset_version="nlu@1.0.0",
            output_directory="out",
            use_lora=False,
            training_method="lora",
        )

        self.assertTrue(any("requires use_lora" in error for error in config.validate().errors))

    def test_with_defaults_only_fills_a_blank_output_directory(self) -> None:
        blank = TrainingConfig(base_model="tinyllama", dataset_version="nlu@1.0.0")
        named = TrainingConfig(
            base_model="tinyllama", dataset_version="nlu@1.0.0", output_directory="mine"
        )

        self.assertEqual(blank.with_defaults(output_directory="fallback").output_directory, "fallback")
        self.assertEqual(named.with_defaults(output_directory="fallback").output_directory, "mine")

    def test_the_fingerprint_identifies_the_configuration(self) -> None:
        first = TrainingConfig(base_model="tinyllama", dataset_version="nlu@1.0.0")
        same = TrainingConfig(base_model="tinyllama", dataset_version="nlu@1.0.0")
        other = TrainingConfig(
            base_model="tinyllama", dataset_version="nlu@1.0.0", learning_rate=1e-3
        )

        self.assertEqual(first.fingerprint(), same.fingerprint())
        self.assertNotEqual(first.fingerprint(), other.fingerprint())

    def test_a_real_run_warns_that_it_will_use_the_machine(self) -> None:
        config = TrainingConfig(
            base_model="tinyllama",
            dataset_version="nlu@1.0.0",
            output_directory="out",
            dry_run=False,
            base_model_size_bytes=4_000_000_000,
        )

        report = config.validate()

        self.assertTrue(report.valid)
        self.assertTrue(report.warnings, "a real run should say what it is about to do")


# ── hardware ────────────────────────────────────────────────────────────────


class HardwareTests(unittest.TestCase):
    """Capability detection that never requires CUDA, torch or an NVIDIA card."""

    def test_detection_reports_what_this_machine_has(self) -> None:
        capabilities = detect_hardware()

        self.assertGreater(capabilities.cpu_count, 0)
        self.assertIsInstance(capabilities.backends, tuple)
        self.assertIn("cpu", capabilities.backends)
        self.assertIsInstance(capabilities.gpu_available, bool)
        self.assertIsInstance(capabilities.npu_available, bool)
        self.assertFalse(capabilities.probed_runtime)

    def test_memory_is_read_from_the_application_monitor_when_present(self) -> None:
        class Monitor:
            def total_ram_bytes(self) -> int:
                return 8_000_000_000

            def available_ram_bytes(self) -> int:
                return 5_000_000_000

        capabilities = detect_hardware(monitor=Monitor())

        self.assertEqual(capabilities.available_ram_bytes, 5_000_000_000)
        self.assertEqual(capabilities.total_ram_bytes, 8_000_000_000)

    def test_detection_survives_every_optional_dependency_being_absent(self) -> None:
        blocked = {
            name: None
            for name in ("torch", "transformers", "peft", "accelerate", "bitsandbytes")
        }
        with mock.patch.dict(sys.modules, blocked):
            capabilities = detect_hardware()

        self.assertFalse(capabilities.torch_available)
        self.assertFalse(capabilities.transformers_available)
        self.assertFalse(capabilities.peft_available)
        self.assertIn("cpu", capabilities.backends, "the CPU is always available")
        self.assertTrue(
            any("not installed" in note or "missing" in note for note in capabilities.notes)
            or capabilities.notes == (),
            "what is missing is reported, not raised",
        )

    def test_the_cpu_policy_is_honoured_even_when_an_accelerator_exists(self) -> None:
        capabilities = HardwareCapabilities(
            cpu_count=8, backends=("cpu", "cuda"), gpu_available=True, gpu_name="Fake GPU"
        )

        choice = resolve_backend(HardwarePolicy.LOCAL_CPU.value, capabilities)

        self.assertEqual(choice.device, "cpu")
        self.assertTrue(choice.available)

    def test_a_gpu_policy_without_a_gpu_refuses_instead_of_falling_back(self) -> None:
        capabilities = HardwareCapabilities(cpu_count=8, backends=("cpu",))

        choice = resolve_backend(HardwarePolicy.LOCAL_GPU.value, capabilities)

        self.assertFalse(choice.available)
        self.assertEqual(choice.device, "")
        self.assertIn("CUDA", choice.reason)

    def test_auto_uses_an_accelerator_when_one_is_reported(self) -> None:
        capabilities = HardwareCapabilities(
            cpu_count=8,
            backends=("cpu", "cuda"),
            gpu_available=True,
            gpu_name="Fake GPU",
        )

        choice = resolve_backend(HardwarePolicy.AUTO.value, capabilities)

        self.assertEqual(choice.device, "cuda")
        self.assertTrue(choice.available)

    def test_auto_falls_back_to_the_cpu_and_says_so(self) -> None:
        choice = resolve_backend(HardwarePolicy.AUTO.value, HardwareCapabilities(cpu_count=4))

        self.assertEqual(choice.device, "cpu")
        self.assertTrue(choice.available)
        self.assertIn("CPU", choice.reason)

    def test_an_npu_can_be_selected_without_a_gpu(self) -> None:
        capabilities = HardwareCapabilities(
            cpu_count=8, backends=("cpu", "npu"), npu_available=True, npu_name="Intel NPU"
        )

        choice = resolve_backend(HardwarePolicy.LOCAL_NPU.value, capabilities)

        self.assertEqual(choice.device, "npu")
        self.assertTrue(choice.available)

    def test_an_unknown_policy_is_treated_as_auto(self) -> None:
        choice = resolve_backend("whatever", HardwareCapabilities(cpu_count=4))

        self.assertEqual(choice.policy, HardwarePolicy.AUTO.value)


# ── resource estimation ─────────────────────────────────────────────────────


def config_for(dataset_version: str, **overrides: Any) -> TrainingConfig:
    fields: dict[str, Any] = {
        "base_model": "tinyllama",
        "dataset_version": dataset_version,
        "output_directory": "out",
    }
    fields.update(overrides)
    return TrainingConfig(**fields)


class ResourceEstimationTests(unittest.TestCase):
    """Refusing to pretend: an unmeasured machine is never SAFE."""

    def test_unmeasured_memory_is_never_safe(self) -> None:
        dataset = make_built_dataset()
        estimator = ResourceEstimator()

        estimate = estimator.estimate(config_for(dataset.dataset_version_id), dataset=dataset)

        self.assertNotEqual(estimate.level, ResourceVerdict.SAFE.value)
        self.assertTrue(estimate.reasons)
        self.assertTrue(
            any("memory" in reason.lower() for reason in estimate.reasons),
            "the reason has to say what is missing",
        )

    def test_plenty_of_memory_is_safe_and_plenty_of_demand_is_unsafe(self) -> None:
        dataset = make_built_dataset()
        capabilities = HardwareCapabilities(
            cpu_count=8, available_ram_bytes=64_000_000_000, total_ram_bytes=64_000_000_000
        )
        estimator = ResourceEstimator(hardware=capabilities)

        roomy = estimator.estimate(
            config_for(dataset.dataset_version_id, base_model_size_bytes=100_000_000),
            dataset=dataset,
        )
        cramped = estimator.estimate(
            config_for(dataset.dataset_version_id, base_model_size_bytes=200_000_000_000),
            dataset=dataset,
        )

        self.assertEqual(roomy.level, ResourceVerdict.SAFE.value)
        self.assertEqual(cramped.level, ResourceVerdict.UNSAFE.value)
        self.assertTrue(cramped.reasons)

    def test_the_components_add_up_to_what_is_required(self) -> None:
        dataset = make_built_dataset()
        estimator = ResourceEstimator(
            hardware=HardwareCapabilities(cpu_count=8, available_ram_bytes=32_000_000_000)
        )

        estimate = estimator.estimate(
            config_for(dataset.dataset_version_id, base_model_size_bytes=500_000_000),
            dataset=dataset,
        )

        self.assertEqual(
            set(estimate.components),
            {"weights", "adapter", "activations", "dataset", "checkpoints"},
            "a LoRA estimate costs a base model, an adapter, its activations and the dataset",
        )
        self.assertGreater(estimate.components["weights"], 0)
        self.assertGreater(estimate.components["dataset"], 0)
        self.assertEqual(estimate.required_bytes, sum(estimate.components.values()))

    def test_a_tiny_model_needs_far_less_than_a_large_one(self) -> None:
        dataset = make_built_dataset()
        estimator = ResourceEstimator(
            hardware=HardwareCapabilities(cpu_count=8, available_ram_bytes=32_000_000_000)
        )

        small = estimator.estimate(
            config_for(dataset.dataset_version_id, base_model_size_bytes=100_000_000),
            dataset=dataset,
        )
        large = estimator.estimate(
            config_for(dataset.dataset_version_id, base_model_size_bytes=8_000_000_000),
            dataset=dataset,
        )

        self.assertLess(small.required_bytes, large.required_bytes)

    def test_a_full_fine_tune_costs_more_than_a_lora_adapter(self) -> None:
        dataset = make_built_dataset()
        estimator = ResourceEstimator(
            hardware=HardwareCapabilities(cpu_count=8, available_ram_bytes=64_000_000_000)
        )

        lora = estimator.estimate(
            config_for(dataset.dataset_version_id, base_model_size_bytes=1_000_000_000),
            dataset=dataset,
        )
        full = estimator.estimate(
            config_for(
                dataset.dataset_version_id,
                base_model_size_bytes=1_000_000_000,
                use_lora=False,
                training_method=TrainingMethod.FULL.value,
            ),
            dataset=dataset,
        )

        self.assertLess(lora.required_bytes, full.required_bytes)
        self.assertGreater(full.components["gradients"], 0)
        self.assertGreater(full.components["optimizer"], 0)
        self.assertNotIn("gradients", lora.components)

    def test_the_estimate_reports_the_dataset_it_was_measured_with(self) -> None:
        dataset = make_built_dataset()
        estimator = ResourceEstimator(
            hardware=HardwareCapabilities(cpu_count=8, available_ram_bytes=32_000_000_000)
        )

        estimate = estimator.estimate(
            config_for(dataset.dataset_version_id, base_model_size_bytes=100_000_000),
            dataset=dataset,
        )

        self.assertEqual(estimate.example_count, len(dataset.examples))
        self.assertGreater(estimate.estimated_tokens, 0)
        self.assertEqual(estimate.policy, HardwarePolicy.AUTO.value)

    def test_a_dry_run_is_declared_as_one(self) -> None:
        dataset = make_built_dataset()
        estimator = ResourceEstimator(
            hardware=HardwareCapabilities(cpu_count=8, available_ram_bytes=32_000_000_000)
        )

        estimate = estimator.estimate(config_for(dataset.dataset_version_id), dataset=dataset)

        self.assertTrue(estimate.dry_run)
        self.assertEqual(estimate.backend, "dry_run")

    def test_capabilities_and_the_summary_describe_the_same_machine(self) -> None:
        estimator = ResourceEstimator()

        capabilities = estimator.capabilities()

        self.assertEqual(capabilities.cpu_count, estimator.capabilities().cpu_count)
        self.assertEqual(capabilities.backends, estimator.capabilities().backends)
        summary = estimator.summary()
        self.assertIn("hardware", summary)
        self.assertIn("device", summary)
        self.assertIn("dependencies", summary)
        self.assertEqual(
            summary["dependencies"]["training_ready"],
            capabilities.training_dependencies_ready,
        )
        self.assertEqual(summary["dependencies"]["missing"], list(capabilities.missing_dependencies()))

    def test_with_capabilities_is_a_copy_that_does_not_mutate_the_original(self) -> None:
        original = ResourceEstimator()
        cloned = original.with_capabilities(
            HardwareCapabilities(cpu_count=2, available_ram_bytes=1_000_000)
        )

        self.assertIsNot(original, cloned)
        self.assertEqual(cloned.capabilities().cpu_count, 2)
        self.assertNotEqual(original.capabilities().cpu_count, 2)


# ── dataset construction ────────────────────────────────────────────────────


class DatasetBuilderTests(unittest.TestCase):
    """Seven dataset types built from what Phase 15 recorded, filtered and split."""

    def setUp(self) -> None:
        self.builder = SFTDatasetBuilder()

    def test_the_nlu_example_is_request_to_structured_intent(self) -> None:
        dataset = self.builder.build(
            name="nlu", dataset_type=DatasetType.NLU.value, trajectories=[make_trajectory()]
        )

        example = dataset.examples[0]
        self.assertEqual(example.input["request"], "open calculator")
        self.assertEqual(example.target["intent"], "open_application")
        self.assertEqual(dataset.dataset_type, "nlu")
        self.assertEqual(dataset.dataset_version_id, "nlu@1.0.0")

    def test_the_decision_example_is_intent_to_route(self) -> None:
        dataset = self.builder.build(
            name="decision",
            dataset_type=DatasetType.DECISION.value,
            trajectories=[make_trajectory()],
        )

        example = dataset.examples[0]
        self.assertEqual(example.input["intent"]["intent"], "open_application")
        self.assertEqual(example.target["route"], "direct_tool")

    def test_the_tool_selection_example_names_the_tool_and_its_arguments(self) -> None:
        dataset = self.builder.build(
            name="tools",
            dataset_type=DatasetType.TOOL_SELECTION.value,
            trajectories=[make_trajectory()],
        )

        example = dataset.examples[0]
        self.assertEqual(example.target["tool"], "desktop.launch")
        self.assertEqual(example.target["arguments"], {"app": "calculator"})
        self.assertEqual(example.input["available_tools"], ["desktop.launch"])

    def test_the_planning_example_is_goal_to_steps(self) -> None:
        dataset = self.builder.build(
            name="plans",
            dataset_type=DatasetType.PLANNING.value,
            trajectories=[make_trajectory()],
        )

        example = dataset.examples[0]
        self.assertEqual(example.target["goal"], "open calculator")
        self.assertEqual(example.target["steps"][0]["action"], "launch")

    def test_the_recovery_example_is_a_failure_to_a_corrected_strategy(self) -> None:
        failed = make_trajectory(
            trajectory_id="traj-2",
            task_id="task-2",
            # Recorded as reviewed: a failed call that recovered is exactly the
            # evidence this dataset type exists to keep.
            quality={"verdict": "accepted"},
            tool_calls=(
                ToolCallRecord(
                    tool="desktop.launch",
                    step_id="s1",
                    status="failed",
                    error="no such app",
                    arguments={"app": "calculator"},
                ),
            ),
            recovery_events=(
                RecoveryRecord(
                    step_id="s1",
                    outcome="opened the app by its shortcut",
                    strategy="retry_with_alternate_tool",
                    attempts=2,
                ),
            ),
        )

        dataset = self.builder.build(
            name="recovery",
            dataset_type=DatasetType.RECOVERY.value,
            trajectories=[failed],
        )

        example = dataset.examples[0]
        self.assertEqual(example.input["error"], "no such app")
        self.assertEqual(example.target["strategy"], "retry_with_alternate_tool")
        self.assertEqual(example.target["outcome"], "opened the app by its shortcut")

    def test_the_developer_example_is_a_task_to_patch_actions(self) -> None:
        coding = make_trajectory(
            trajectory_id="traj-3",
            task_id="task-3",
            user_request="fix the failing test in the parser",
            structured_intent={"intent": "code", "confidence": 0.8},
            metadata={"tags": ["developer"]},
            tool_calls=(
                ToolCallRecord(
                    tool="code.apply_patch",
                    step_id="s1",
                    capability="code",
                    status="completed",
                    output_summary="patched parser.py",
                    arguments={"path": "parser.py"},
                ),
            ),
        )

        dataset = self.builder.build(
            name="dev",
            dataset_type=DatasetType.DEVELOPER.value,
            trajectories=[coding],
            rules=SelectionRules(include_developer=True),
        )

        example = dataset.examples[0]
        self.assertEqual(example.input["task"], "fix the failing test in the parser")
        self.assertEqual(example.target["actions"][0]["tool"], "code.apply_patch")

    def test_the_research_example_is_a_request_to_evidence(self) -> None:
        research = make_trajectory(
            trajectory_id="traj-4",
            task_id="task-4",
            user_request="research local vector databases",
            source="explore",
            metadata={"tags": ["research"]},
            structured_intent={"intent": "research", "confidence": 0.7},
        )

        dataset = self.builder.build(
            name="research",
            dataset_type=DatasetType.RESEARCH.value,
            trajectories=[research],
            rules=SelectionRules(include_research=True),
        )

        example = dataset.examples[0]
        self.assertEqual(example.input["request"], "research local vector databases")
        self.assertIn("evidence", example.target)

    def test_a_research_row_can_be_kept_out_of_a_research_dataset_on_request(self) -> None:
        research = make_trajectory(
            trajectory_id="traj-5",
            task_id="task-5",
            user_request="research local vector databases",
            metadata={"tags": ["research"]},
            structured_intent={"intent": "research"},
        )

        wanted = self.builder.build(
            name="research",
            dataset_type=DatasetType.RESEARCH.value,
            trajectories=[research],
        )
        excluded = self.builder.build(
            name="research",
            dataset_type=DatasetType.RESEARCH.value,
            trajectories=[research],
            rules=SelectionRules(include_research=False),
        )

        self.assertEqual(len(wanted.examples), 1)
        self.assertEqual(excluded.examples, ())
        self.assertEqual(
            excluded.statistics.skipped.get(builder_module.REASON_RESEARCH_EXCLUDED), 1
        )

    def test_a_code_row_can_be_kept_out_of_a_developer_dataset_on_request(self) -> None:
        coding = make_trajectory(
            user_request="fix the parser",
            structured_intent={"intent": "code"},
            metadata={"tags": ["developer"]},
            final_result={"summary": "patched"},
        )

        wanted = self.builder.build(
            name="dev",
            dataset_type=DatasetType.DEVELOPER.value,
            trajectories=[coding],
        )
        excluded = self.builder.build(
            name="dev",
            dataset_type=DatasetType.DEVELOPER.value,
            trajectories=[coding],
            rules=SelectionRules(include_developer=False),
        )

        self.assertEqual(len(wanted.examples), 1)
        self.assertEqual(excluded.examples, ())
        self.assertEqual(
            excluded.statistics.skipped.get(builder_module.REASON_DEVELOPER_EXCLUDED), 1
        )

    def test_a_row_without_the_data_the_type_needs_is_skipped_not_faked(self) -> None:
        dataset = self.builder.build(
            name="nlu",
            dataset_type=DatasetType.NLU.value,
            trajectories=[make_trajectory(structured_intent={})],
        )

        self.assertEqual(dataset.examples, ())
        self.assertEqual(
            dataset.statistics.skipped.get(builder_module.REASON_MISSING_DATA), 1
        )

    def test_only_accepted_rows_are_built_by_default(self) -> None:
        untouched = make_trajectory()
        reviewed = make_trajectory(
            trajectory_id="traj-2",
            task_id="task-2",
            quality={"verdict": "needs_review"},
        )

        dataset = self.builder.build(
            name="nlu",
            dataset_type=DatasetType.NLU.value,
            trajectories=[untouched, reviewed],
        )

        self.assertEqual(len(dataset.examples), 1)
        self.assertEqual(
            dataset.statistics.skipped.get(builder_module.REASON_QUALITY), 1
        )

    def test_a_reviewed_row_can_be_opted_in_explicitly(self) -> None:
        reviewed = make_trajectory(quality={"verdict": "needs_review"})

        dataset = self.builder.build(
            name="nlu",
            dataset_type=DatasetType.NLU.value,
            trajectories=[reviewed],
            rules=SelectionRules(quality=("accepted", "needs_review")),
        )

        self.assertEqual(len(dataset.examples), 1)

    def test_a_failed_row_is_not_evidence_of_success(self) -> None:
        failed = make_trajectory(
            trajectory_id="traj-2",
            task_id="task-2",
            success=False,
            status="failed",
            failure_reason="the app was not installed",
        )

        dataset = self.builder.build(
            name="nlu", dataset_type=DatasetType.NLU.value, trajectories=[failed]
        )

        self.assertEqual(dataset.examples, ())
        self.assertEqual(
            dataset.statistics.skipped.get(builder_module.REASON_NOT_SUCCESSFUL), 1
        )

    def test_requiring_a_verification_drops_rows_that_have_none(self) -> None:
        unverified = make_trajectory(verification_results=())

        dataset = self.builder.build(
            name="nlu",
            dataset_type=DatasetType.NLU.value,
            trajectories=[unverified],
            rules=SelectionRules(require_verification=True),
        )

        self.assertEqual(dataset.examples, ())
        self.assertEqual(
            dataset.statistics.skipped.get(builder_module.REASON_VERIFICATION_MISSING), 1
        )

    def test_a_failed_verification_disqualifies_a_row_that_claims_success(self) -> None:
        conflicting = make_trajectory(
            verification_results=(
                VerificationRecord(step_id="s1", status="verified"),
                VerificationRecord(step_id="s2", status="failed"),
            )
        )

        dataset = self.builder.build(
            name="nlu",
            dataset_type=DatasetType.NLU.value,
            trajectories=[conflicting],
            # The quality filter calls a claimed success with a failed check a
            # contradiction and holds it; the verification rule is what this
            # test is about, so the row is admitted at the quality gate first.
            rules=SelectionRules(
                quality=("accepted", "needs_review"), verification_must_pass=True
            ),
        )

        self.assertEqual(dataset.examples, ())
        self.assertEqual(
            dataset.statistics.skipped.get(builder_module.REASON_VERIFICATION_FAILED), 1
        )

    def test_a_reward_floor_is_enforced(self) -> None:
        low = make_trajectory(reward={"total_reward": 0.1})

        dataset = self.builder.build(
            name="nlu",
            dataset_type=DatasetType.NLU.value,
            trajectories=[low],
            rules=SelectionRules(min_reward=0.5),
        )

        self.assertEqual(dataset.examples, ())
        self.assertEqual(
            dataset.statistics.skipped.get(builder_module.REASON_BELOW_REWARD), 1
        )

    def test_an_evaluation_score_floor_uses_the_named_dimension(self) -> None:
        trajectory = make_trajectory(evaluation_id="ev-1")
        weak = make_result(
            dimensions=(
                DimensionScore(dimension="nlu", score=0.2, status="fail"),
                DimensionScore(dimension="planning", score=0.9, status="ok"),
            )
        )

        dataset = self.builder.build(
            name="nlu",
            dataset_type=DatasetType.NLU.value,
            trajectories=[trajectory],
            evaluations=[weak],
            rules=SelectionRules(min_evaluation_score=0.5, evaluation_dimension="nlu"),
        )

        self.assertEqual(dataset.examples, ())
        self.assertEqual(
            dataset.statistics.skipped.get(builder_module.REASON_BELOW_SCORE), 1
        )

    def test_the_model_source_category_tag_and_date_filters_each_bite(self) -> None:
        row = make_trajectory(
            source="application",
            model_information={"model": "qwen3:8b"},
            metadata={"category": "application"},
            timestamp="2026-01-01T00:00:00+00:00",
        )

        def kept(**rules: Any) -> int:
            return len(
                self.builder.build(
                    name="nlu",
                    dataset_type=DatasetType.NLU.value,
                    trajectories=[row],
                    rules=SelectionRules(**rules),
                ).examples
            )

        self.assertEqual(kept(model="qwen3:8b"), 1)
        self.assertEqual(kept(model="llama3"), 0)
        self.assertEqual(kept(source="application"), 1)
        self.assertEqual(kept(source="vision"), 0)
        self.assertEqual(kept(task_category="application"), 1)
        self.assertEqual(kept(task_category="research"), 0)
        self.assertEqual(kept(tags=("application",)), 1)
        self.assertEqual(kept(tags=("nope",)), 0)
        self.assertEqual(kept(since="2025-01-01T00:00:00+00:00"), 1)
        self.assertEqual(kept(until="2025-12-31T00:00:00+00:00"), 0)

    def test_max_examples_caps_the_dataset(self) -> None:
        rows = [
            make_trajectory(
                trajectory_id=f"traj-{index}",
                task_id=f"task-{index}",
                user_request=f"open application {index}",
                structured_intent={"intent": f"open_application_{index}"},
            )
            for index in range(5)
        ]

        dataset = self.builder.build(
            name="nlu",
            dataset_type=DatasetType.NLU.value,
            trajectories=rows,
            rules=SelectionRules(max_examples=3),
        )

        self.assertEqual(len(dataset.examples), 3)

    def test_duplicates_are_dropped_and_counted(self) -> None:
        first = make_trajectory()
        # A different run that produced the SAME training example: two tool
        # calls, a longer latency, the same request and target.
        twin = make_trajectory(
            trajectory_id="traj-9",
            task_id="task-9",
            tool_calls=(
                ToolCallRecord(
                    tool="desktop.launch",
                    step_id="s1",
                    arguments={"app": "calculator"},
                    status="completed",
                    duration_ms=999.0,
                ),
                ToolCallRecord(tool="desktop.focus", step_id="s2", status="completed"),
            ),
            verification_results=(
                VerificationRecord(step_id="s1", status="verified"),
                VerificationRecord(step_id="s2", status="verified"),
            ),
            latency_metrics=LatencyMetrics(total_ms=9000.0),
        )

        dataset = self.builder.build(
            name="nlu",
            dataset_type=DatasetType.NLU.value,
            trajectories=[first, twin],
        )

        self.assertEqual(len(dataset.examples), 1)
        self.assertEqual(dataset.statistics.duplicates_removed, 1)
        self.assertEqual(dataset.statistics.skipped.get(builder_module.REASON_DUPLICATE), 1)

    def test_hidden_reasoning_is_never_turned_into_a_training_target(self) -> None:
        leaky = make_trajectory(
            context_summary={"chain_of_thought": "first I would look at the screen"},
            metadata={"tags": ["nlu"], "reasoning": "because the user asked"},
        )

        dataset = self.builder.build(
            name="nlu", dataset_type=DatasetType.NLU.value, trajectories=[leaky]
        )

        serialised = json.dumps(dataset.to_dict(), default=str).lower()
        self.assertNotIn("chain_of_thought", serialised)
        self.assertNotIn("first i would look", serialised)
        self.assertEqual(dataset.examples, (), "the row must be refused, not trimmed")

    def test_a_source_row_carrying_reasoning_in_its_metadata_is_refused(self) -> None:
        """The example builder copies a fixed set of fields, so a reasoning key
        in a trajectory's METADATA would never reach the example-level guard.
        It must be caught on the source row instead, and counted as such."""
        leaky = make_trajectory(metadata={"chain_of_thought": "I thought about it"})
        clean = make_trajectory(trajectory_id="traj-2", user_request="open calculator")

        dataset = self.builder.build(
            name="nlu",
            dataset_type=DatasetType.NLU.value,
            trajectories=[leaky, clean],
        )

        self.assertEqual(
            dataset.statistics.skipped.get(builder_module.REASON_REASONING),
            1,
            dataset.statistics.to_dict(),
        )
        self.assertEqual(len(dataset.examples), 1, "the clean row must still build")
        self.assertEqual(dataset.examples[0].source_trajectory_id, "traj-2")

    def test_a_secret_in_a_recorded_request_is_redacted_not_trained_on(self) -> None:
        leaky = make_trajectory(user_request=f"deploy with token {SECRET}")

        dataset = self.builder.build(
            name="nlu", dataset_type=DatasetType.NLU.value, trajectories=[leaky]
        )

        text = json.dumps(dataset.to_dict(), default=str)
        self.assertNotIn(SECRET, text)
        if dataset.examples:
            self.assertIn(REDACTED, text)

    def test_the_statistics_name_what_was_skipped_and_why(self) -> None:
        rows = [make_trajectory(), make_trajectory(trajectory_id="t2", success=False)]

        dataset = self.builder.build(
            name="nlu",
            dataset_type=DatasetType.NLU.value,
            trajectories=rows,
            rules=SelectionRules(quality=("accepted", "needs_review")),
        )

        statistics = dataset.statistics
        self.assertEqual(statistics.total, 1)
        self.assertEqual(
            statistics.source_trajectories,
            1,
            "the count is of rows that contributed, not of rows read",
        )
        self.assertEqual(statistics.by_quality, {"accepted": 1})
        self.assertEqual(statistics.by_difficulty, {"simple": 1})
        self.assertEqual(statistics.groups, 1)
        self.assertGreater(statistics.estimated_tokens, 0)
        self.assertEqual(sum(statistics.by_split.values()), len(dataset.examples))
        self.assertEqual(sum(statistics.skipped.values()), 1)
        self.assertTrue(
            set(statistics.skipped)
            & {builder_module.REASON_NOT_SUCCESSFUL, builder_module.REASON_QUALITY},
            "a run that failed is skipped for its verdict or for its outcome, never silently",
        )
        self.assertNotIn("traj-1", dataset.splits["train"] + dataset.splits["validation"])

    def test_the_provenance_records_what_it_was_built_from(self) -> None:
        dataset = self.builder.build(
            name="nlu",
            dataset_type=DatasetType.NLU.value,
            trajectories=[make_trajectory()],
            version="1.2.0",
            description="first pass",
            tags=("baseline",),
        )

        self.assertEqual(dataset.dataset_version_id, "nlu@1.2.0")
        self.assertEqual(dataset.description, "first pass")
        self.assertEqual(dataset.tags, ("baseline",))
        self.assertEqual(dataset.preprocessing_version, PREPROCESSING_VERSION)
        self.assertIn("dataset_type=nlu", dataset.source_data_version)
        self.assertIn("trajectories=1", dataset.source_data_version)
        self.assertTrue(dataset.selection)

    def test_a_second_build_increments_the_version_instead_of_overwriting(self) -> None:
        first = self.builder.build(
            name="nlu", dataset_type=DatasetType.NLU.value, trajectories=[make_trajectory()]
        )
        second = self.builder.build(
            name="nlu",
            dataset_type=DatasetType.NLU.value,
            trajectories=[make_trajectory()],
            existing_versions=[first.version],
        )

        self.assertEqual(first.version, "1.0.0")
        self.assertEqual(second.version, "1.0.1")
        self.assertNotEqual(first.fingerprint(), second.fingerprint())

    def test_examples_carry_their_source_so_a_target_can_be_traced_back(self) -> None:
        dataset = self.builder.build(
            name="nlu",
            dataset_type=DatasetType.NLU.value,
            trajectories=[make_trajectory(evaluation_id="ev-1")],
            evaluations=[make_result()],
        )

        example = dataset.examples[0]
        self.assertEqual(example.source_trajectory_id, "traj-1")
        self.assertEqual(example.source_evaluation_id, "ev-1")
        self.assertEqual(example.quality_status, "accepted")
        self.assertEqual(example.dataset_version, dataset.dataset_version_id)
        self.assertTrue(example.tags)


class SplitTests(unittest.TestCase):
    """Deterministic, group-safe, leak-free splitting."""

    def setUp(self) -> None:
        self.builder = SFTDatasetBuilder()
        self.examples = [
            make_example(index, group=f"group-{index % 5}") for index in range(20)
        ]

    def test_the_same_seed_produces_the_same_split(self) -> None:
        first = self.builder.split_examples(self.examples, SplitConfig(seed=7))
        second = self.builder.split_examples(list(reversed(self.examples)), SplitConfig(seed=7))

        self.assertEqual(first, second, "the split must not depend on input order")

    def test_a_different_seed_produces_a_different_split(self) -> None:
        first = self.builder.split_examples(self.examples, SplitConfig(seed=1))
        second = self.builder.split_examples(self.examples, SplitConfig(seed=2))

        self.assertNotEqual(first, second)

    def test_every_example_lands_in_exactly_one_split(self) -> None:
        splits = self.builder.split_examples(self.examples, SplitConfig())

        assigned = [example_id for ids in splits.values() for example_id in ids]
        self.assertEqual(sorted(assigned), sorted(example.example_id for example in self.examples))
        self.assertEqual(set(splits), set(SPLIT_NAMES))

    def test_a_group_is_never_split_across_two_sets(self) -> None:
        splits = self.builder.split_examples(self.examples, SplitConfig())

        by_id = {example.example_id: example for example in self.examples}
        seen: dict[str, str] = {}
        for name, ids in splits.items():
            for example_id in ids:
                group = by_id[example_id].group_key
                self.assertEqual(
                    seen.setdefault(group, name),
                    name,
                    "an example leaked into another split's group",
                )

    def test_the_ratios_are_close_to_what_was_asked_for(self) -> None:
        independent = [make_example(index, group=f"solo-{index}") for index in range(40)]

        splits = self.builder.split_examples(independent, SplitConfig())

        train = len(splits["train"]) / len(independent)
        validation = len(splits["validation"]) / len(independent)
        test = len(splits["test"]) / len(independent)
        self.assertGreater(train, 0.65)
        self.assertLess(train, 0.95)
        self.assertGreater(validation, 0.0)
        self.assertGreater(test, 0.0)

    def test_a_few_large_groups_do_not_starve_the_test_split(self) -> None:
        """The comparison between splits is RELATIVE to each split's target.
        With absolute room, ``train`` (target 32) takes the first groups and four
        groups of ten become 30/10/0 — and an empty test split is the split the
        evaluator reads by default."""
        chunky = [make_example(index, group=f"group-{index // 10}") for index in range(40)]

        splits = self.builder.split_examples(chunky, SplitConfig())

        self.assertEqual(len(splits["train"]), 20)
        self.assertEqual(len(splits["validation"]), 10)
        self.assertEqual(len(splits["test"]), 10)

    def test_examples_without_a_group_are_treated_as_their_own_group(self) -> None:
        loose = [make_example(index, metadata={}) for index in range(6)]

        splits = self.builder.split_examples(loose, SplitConfig())

        self.assertEqual(sum(len(ids) for ids in splits.values()), 6)

    def test_an_empty_dataset_splits_into_empty_lists(self) -> None:
        splits = self.builder.split_examples([], SplitConfig())

        self.assertEqual(splits, {"train": (), "validation": (), "test": ()})


class DatasetValidationTests(unittest.TestCase):
    """A dataset that would train on leakage says so instead of training."""

    def setUp(self) -> None:
        self.builder = SFTDatasetBuilder()

    def test_a_clean_dataset_has_no_issues(self) -> None:
        self.assertEqual(self.builder.validate(make_built_dataset()), ())

    def test_a_hand_built_dataset_with_wrong_statistics_is_flagged(self) -> None:
        dataset = make_built_dataset(statistics=DatasetStatistics(total=99))

        issues = self.builder.validate(dataset)

        self.assertTrue(any("statistics" in issue for issue in issues))

    def test_cross_split_leakage_of_one_group_is_caught(self) -> None:
        examples = [make_example(index, group="shared") for index in range(4)]
        dataset = make_built_dataset(
            examples,
            splits={"train": ("ex-0", "ex-1"), "validation": ("ex-2",), "test": ("ex-3",)},
            statistics=DatasetStatistics(total=4),
        )

        issues = self.builder.validate(dataset)

        self.assertTrue(any("leak" in issue.lower() for issue in issues))

    def test_an_example_outside_every_split_is_caught(self) -> None:
        examples = [make_example(index) for index in range(2)]
        dataset = make_built_dataset(
            examples,
            splits={"train": ("ex-0",), "validation": (), "test": ()},
            statistics=DatasetStatistics(total=2),
        )

        issues = self.builder.validate(dataset)

        self.assertTrue(any("not in any split" in issue for issue in issues))

    def test_an_example_in_two_splits_is_caught(self) -> None:
        examples = [make_example(0)]
        dataset = make_built_dataset(
            examples,
            splits={"train": ("ex-0",), "validation": ("ex-0",), "test": ()},
            statistics=DatasetStatistics(total=1),
        )

        issues = self.builder.validate(dataset)

        self.assertTrue(any("both" in issue for issue in issues))

    def test_an_empty_dataset_is_an_issue(self) -> None:
        dataset = make_built_dataset([], statistics=DatasetStatistics(total=0))

        issues = self.builder.validate(dataset)

        self.assertTrue(any("no examples" in issue for issue in issues))

    def test_a_mismatched_dataset_type_is_caught(self) -> None:
        dataset = make_built_dataset(dataset_type="psychohistory")

        issues = self.builder.validate(dataset)

        self.assertTrue(any("unknown dataset_type" in issue for issue in issues))

    def test_a_reasoning_contaminated_example_is_caught(self) -> None:
        examples = [make_example(0, metadata={"group_key": "g", "thoughts": "secret"})]
        dataset = make_built_dataset(
            examples,
            splits={"train": ("ex-0",), "validation": (), "test": ()},
            statistics=DatasetStatistics(total=1),
        )

        issues = self.builder.validate(dataset)

        self.assertTrue(any("hidden reasoning" in issue for issue in issues))

    def test_an_example_without_a_target_is_caught(self) -> None:
        examples = [make_example(0, target={})]
        dataset = make_built_dataset(
            examples,
            splits={"train": ("ex-0",), "validation": (), "test": ()},
            statistics=DatasetStatistics(total=1),
        )

        issues = self.builder.validate(dataset)

        self.assertTrue(any("no structured target" in issue for issue in issues))


# ── the trainer interface and the dry-run backend ───────────────────────────


def trainer_config(dataset_version: str, **overrides: Any) -> TrainingConfig:
    fields: dict[str, Any] = {
        "base_model": "tinyllama",
        "dataset_version": dataset_version,
        "output_directory": "out",
        "dry_run": True,
        "batch_size": 2,
        "checkpoint_frequency": 1,
    }
    fields.update(overrides)
    return TrainingConfig(**fields)


def run_for(config: TrainingConfig, dataset: SFTDatasetVersion, **overrides: Any) -> TrainingRun:
    fields: dict[str, Any] = {
        "run_id": "run-1",
        "name": "probe",
        "model": config.base_model,
        "dataset_version": dataset.dataset_version_id,
        "dataset_type": dataset.dataset_type,
        "training_config": config.to_mapping(),
        "backend": "dry_run" if config.dry_run else "peft_lora",
        "random_seed": config.seed,
    }
    fields.update(overrides)
    return TrainingRun(**fields)


class TrainerInterfaceTests(unittest.TestCase):
    """One interface a backend implements, with the operations the phase names."""

    def test_the_trainer_interface_cannot_be_instantiated_bare(self) -> None:
        with self.assertRaises(TypeError):
            SFTTrainer(TrainingConfig())  # type: ignore[abstract]

    def test_the_interface_carries_every_operation_the_phase_asks_for(self) -> None:
        for operation in (
            "prepare_dataset",
            "validate_config",
            "estimate_resources",
            "start_training",
            "resume_training",
            "evaluate",
            "save_checkpoint",
            "finalize",
            "cancel",
        ):
            self.assertTrue(
                callable(getattr(SFTTrainer, operation, None)),
                f"the trainer interface must expose {operation}",
            )

    def test_every_backend_declares_which_backend_it_is(self) -> None:
        dataset = make_built_dataset()
        dry = DryRunTrainer(trainer_config(dataset.dataset_version_id))
        peft = PeftLoraBackend(trainer_config(dataset.dataset_version_id, dry_run=False))

        self.assertEqual(dry.backend, "dry_run")
        self.assertEqual(peft.backend, "peft_lora")


class DryRunTrainerTests(unittest.TestCase):
    """The backend this machine can actually run: real scheduling, simulated loss."""

    def setUp(self) -> None:
        self.dataset = make_built_dataset()
        self.config = trainer_config(self.dataset.dataset_version_id)

    def test_preparing_a_dataset_reports_the_real_schedule(self) -> None:
        summary = DryRunTrainer(self.config).prepare_dataset(self.dataset)

        self.assertEqual(summary["examples"], len(self.dataset.examples))
        self.assertEqual(
            summary["train_examples"] + summary["validation_examples"] + summary["test_examples"],
            len(self.dataset.examples),
        )
        self.assertEqual(summary["max_sequence_length"], self.config.max_sequence_length)
        self.assertGreaterEqual(summary["total_steps"], 1)

    def test_the_schedule_accounts_for_accumulation(self) -> None:
        many = make_built_dataset([make_example(index, group=f"g{index}") for index in range(16)])
        slow = trainer_config(many.dataset_version_id, batch_size=2, gradient_accumulation_steps=4)

        summary = DryRunTrainer(slow).prepare_dataset(many)

        micro = summary["steps_per_epoch"]
        self.assertGreater(micro, 0)
        self.assertEqual(summary["total_steps"], micro * slow.epochs)

    def test_validation_mirrors_the_configs_own_verdict(self) -> None:
        broken = trainer_config(self.dataset.dataset_version_id, epochs=0)

        report = DryRunTrainer(broken).validate_config()

        self.assertFalse(report.valid)
        self.assertEqual(report.errors, broken.validate().errors)

    def test_the_resource_estimate_comes_from_the_same_estimator(self) -> None:
        trainer = DryRunTrainer(
            self.config,
            estimator=ResourceEstimator(
                hardware=HardwareCapabilities(cpu_count=4, available_ram_bytes=8_000_000_000)
            ),
        )

        estimate = trainer.estimate_resources(self.dataset)

        self.assertEqual(estimate.example_count, len(self.dataset.examples))
        self.assertIn(estimate.level, {member.value for member in ResourceVerdict})

    def test_a_dry_run_completes_with_a_real_schedule_and_a_loss_curve(self) -> None:
        trainer = DryRunTrainer(self.config)
        run = run_for(self.config, self.dataset)

        finished = trainer.start_training(run, self.dataset, TrainingCallbacks())

        self.assertEqual(finished.status, TrainingRunStatus.COMPLETED.value)
        self.assertEqual(finished.total_steps, trainer.prepare_dataset(self.dataset)["total_steps"])
        self.assertEqual(len(finished.loss_history), finished.total_steps)
        self.assertIsNotNone(finished.training_loss)
        self.assertIsNotNone(finished.validation_loss)
        self.assertTrue(finished.end_time)

    def test_the_same_seed_produces_the_same_curve(self) -> None:
        first = DryRunTrainer(self.config).start_training(
            run_for(self.config, self.dataset), self.dataset, TrainingCallbacks()
        )
        second = DryRunTrainer(self.config).start_training(
            run_for(self.config, self.dataset), self.dataset, TrainingCallbacks()
        )

        self.assertEqual(
            [point["loss"] for point in first.loss_history],
            [point["loss"] for point in second.loss_history],
        )

    def test_a_different_seed_produces_a_different_curve(self) -> None:
        other = trainer_config(self.dataset.dataset_version_id, seed=99)
        first = DryRunTrainer(self.config).start_training(
            run_for(self.config, self.dataset), self.dataset, TrainingCallbacks()
        )
        second = DryRunTrainer(other).start_training(
            run_for(other, self.dataset), self.dataset, TrainingCallbacks()
        )

        self.assertNotEqual(
            [point["loss"] for point in first.loss_history],
            [point["loss"] for point in second.loss_history],
        )

    def test_every_step_is_reported_to_the_callback(self) -> None:
        seen: list[int] = []
        run = run_for(self.config, self.dataset)

        finished = DryRunTrainer(self.config).start_training(
            run,
            self.dataset,
            TrainingCallbacks(on_step=lambda updated: seen.append(updated.current_step)),
        )

        self.assertEqual(seen, list(range(1, finished.total_steps + 1)))
        self.assertTrue(seen, "the trainer reported no step at all")

    def test_a_checkpoint_callback_is_handed_the_run_and_the_kind(self) -> None:
        kinds: list[str] = []
        run = run_for(self.config, self.dataset)

        DryRunTrainer(self.config).start_training(
            run,
            self.dataset,
            TrainingCallbacks(on_checkpoint=lambda updated, kind: kinds.append(kind)),
        )

        self.assertTrue(kinds)
        self.assertIn("best", kinds)

    def test_a_cancelled_run_stops_at_the_next_step_and_says_so(self) -> None:
        calls: list[int] = []
        run = run_for(self.config, self.dataset)

        finished = DryRunTrainer(self.config).start_training(
            run,
            self.dataset,
            TrainingCallbacks(
                on_step=lambda updated: calls.append(updated.current_step),
                is_cancelled=lambda: len(calls) >= 1,
            ),
        )

        self.assertEqual(finished.status, TrainingRunStatus.CANCELLED.value)
        self.assertEqual(len(calls), 1)
        self.assertTrue(finished.end_time)

    def test_a_paused_run_stops_and_stays_resumable(self) -> None:
        calls: list[int] = []
        run = run_for(self.config, self.dataset)

        paused = DryRunTrainer(self.config).start_training(
            run,
            self.dataset,
            TrainingCallbacks(
                on_step=lambda updated: calls.append(updated.current_step),
                is_paused=lambda: len(calls) >= 1,
            ),
        )

        self.assertEqual(paused.status, TrainingRunStatus.PAUSED.value)
        self.assertFalse(paused.terminal)
        self.assertIsNotNone(paused.training_loss)

    def test_resuming_continues_the_schedule_from_the_checkpoint(self) -> None:
        trainer = DryRunTrainer(self.config)
        run = run_for(self.config, self.dataset)
        paused = trainer.start_training(
            run, self.dataset, TrainingCallbacks(is_paused=lambda: True)
        )

        resumed = trainer.resume_training(paused, self.dataset, 0, TrainingCallbacks())

        self.assertEqual(resumed.status, TrainingRunStatus.COMPLETED.value)

    def test_saving_a_checkpoint_without_a_callback_is_not_an_error(self) -> None:
        trainer = DryRunTrainer(self.config)
        run = run_for(self.config, self.dataset)

        trainer.save_checkpoint(run, TrainingCallbacks(), "periodic")
        evaluated = trainer.evaluate(self.dataset)

        self.assertIsInstance(evaluated, dict)

    def test_cancelling_a_stored_run_returns_it_cancelled(self) -> None:
        trainer = DryRunTrainer(self.config)
        run = run_for(self.config, self.dataset)

        cancelled = trainer.cancel(run)

        self.assertEqual(cancelled.status, TrainingRunStatus.CANCELLED.value)

    def test_finalizing_marks_the_run_completed(self) -> None:
        trainer = DryRunTrainer(self.config)
        run = run_for(self.config, self.dataset)

        finished = trainer.finalize(run)

        self.assertEqual(finished.status, TrainingRunStatus.COMPLETED.value)
        self.assertTrue(finished.end_time)


# ── the PEFT/LoRA boundary ──────────────────────────────────────────────────


def no_extras() -> HardwareCapabilities:
    return HardwareCapabilities(
        cpu_count=4,
        torch_available=False,
        transformers_available=False,
        peft_available=False,
    )


def all_extras() -> HardwareCapabilities:
    return HardwareCapabilities(
        cpu_count=4,
        torch_available=True,
        transformers_available=True,
        peft_available=True,
    )


class PeftBackendTests(unittest.TestCase):
    """The real backend refuses clearly when it cannot run, and delegates when wired."""

    def setUp(self) -> None:
        self.dataset = make_built_dataset()
        self.config = trainer_config(self.dataset.dataset_version_id, dry_run=False)

    def test_a_missing_dependency_is_reported_by_name(self) -> None:
        backend = PeftLoraBackend(self.config, capabilities=no_extras())

        self.assertFalse(backend.is_available())
        self.assertIn("torch", backend.missing_dependencies())
        self.assertIn("peft", backend.missing_dependencies())

    def test_training_without_the_extras_raises_a_named_error(self) -> None:
        backend = PeftLoraBackend(self.config, capabilities=no_extras())

        with self.assertRaises(TrainingBackendUnavailable) as caught:
            backend.start_training(
                run_for(self.config, self.dataset), self.dataset, TrainingCallbacks()
            )

        self.assertIn("torch", str(caught.exception))
        self.assertIn("Dry-run and dataset validation work", str(caught.exception))

    def test_with_the_extras_but_no_runner_the_boundary_says_so(self) -> None:
        backend = PeftLoraBackend(self.config, capabilities=all_extras())

        self.assertTrue(backend.is_available())
        with self.assertRaises(TrainingBackendUnavailable) as caught:
            backend.start_training(
                run_for(self.config, self.dataset), self.dataset, TrainingCallbacks()
            )

        self.assertIn("no training runner is wired", str(caught.exception))

    def test_the_adapter_delegates_to_an_injected_runner(self) -> None:
        seen: list[str] = []

        def runner(run: TrainingRun, dataset: SFTDatasetVersion, callbacks: TrainingCallbacks) -> TrainingRun:
            seen.append(dataset.dataset_version_id)
            return run.with_status(TrainingRunStatus.COMPLETED, training_loss=0.25)

        backend = PeftLoraBackend(self.config, capabilities=all_extras(), runner=runner)

        finished = backend.start_training(
            run_for(self.config, self.dataset), self.dataset, TrainingCallbacks()
        )

        self.assertEqual(seen, [self.dataset.dataset_version_id])
        self.assertEqual(finished.status, TrainingRunStatus.COMPLETED.value)
        self.assertEqual(finished.training_loss, 0.25)

    def test_a_runner_that_returns_nothing_still_produces_a_finished_run(self) -> None:
        class SilentRunner:
            def __call__(self, run: TrainingRun, dataset: SFTDatasetVersion, callbacks: TrainingCallbacks) -> Any:
                return None

        backend = PeftLoraBackend(
            self.config, capabilities=all_extras(), runner=SilentRunner()
        )

        finished = backend.start_training(
            run_for(self.config, self.dataset), self.dataset, TrainingCallbacks()
        )

        self.assertEqual(finished.status, TrainingRunStatus.COMPLETED.value)

    def test_the_prepared_summary_describes_the_adapter(self) -> None:
        adapted = trainer_config(
            self.dataset.dataset_version_id, dry_run=False, lora_rank=4, lora_alpha=8
        )

        summary = PeftLoraBackend(adapted, capabilities=all_extras()).prepare_dataset(self.dataset)

        self.assertEqual(summary["lora"]["rank"], 4)
        self.assertEqual(summary["lora"]["alpha"], 8)
        self.assertEqual(summary["lora"]["method"], TrainingMethod.LORA.value)

    def test_a_nonsense_lora_configuration_is_refused_before_any_training(self) -> None:
        broken = trainer_config(self.dataset.dataset_version_id, dry_run=False, lora_rank=0)
        backend = PeftLoraBackend(broken, capabilities=all_extras())

        self.assertFalse(backend.validate_config().valid)

    def test_qlora_asks_for_the_quantisation_extra(self) -> None:
        quantised = trainer_config(
            self.dataset.dataset_version_id, dry_run=False, training_method="qlora"
        )

        backend = PeftLoraBackend(quantised, capabilities=all_extras())

        self.assertIn("bitsandbytes", backend.missing_dependencies())

    def test_this_phase_does_not_import_torch_at_module_level(self) -> None:
        source = Path(builder_module.__file__).parent.joinpath("backends.py").read_text(encoding="utf-8")

        self.assertNotIn("\nimport torch", source)
        self.assertNotIn("\nimport transformers", source)
        self.assertNotIn("\nimport peft", source)


# ── checkpoints ─────────────────────────────────────────────────────────────


class CheckpointManagerTests(unittest.TestCase):
    """Files, records, validation and retention — including corruption."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _datasets, _runs, self.repository, _models, _evaluations = __import__(
            "novacontrol.training", fromlist=["build_training_repositories"]
        ).build_training_repositories(self._tmp.name)
        self.manager = CheckpointManager(
            Path(self._tmp.name) / "checkpoints",
            max_checkpoints=2,
            repository=self.repository,
        )

    def _write(self, step: int, kind: str = "periodic") -> CheckpointRecord:
        return self.manager.create(
            "run-1", epoch=1, step=step, kind=kind, metrics={"training_loss": 1.0 / step}
        )

    def test_a_written_checkpoint_is_a_real_file_with_metadata(self) -> None:
        record = self._write(1)

        self.assertEqual(record.status, CHECKPOINT_COMPLETE)
        self.assertTrue(Path(record.path).exists())
        self.assertGreater(record.size_bytes, 0)
        self.assertEqual(record.metrics["training_loss"], 1.0)
        self.assertEqual(record.schema_version, 1)

    def test_the_payload_can_be_read_back(self) -> None:
        record = self._write(2)

        payload = self.manager.load(record)

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["checkpoint_id"], record.checkpoint_id)
        self.assertEqual(payload["step"], 2)
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["preprocessing_version"], PREPROCESSING_VERSION)
        self.assertNotIn("reasoning", json.dumps(payload).lower())

    def test_listing_is_per_run_and_ordered(self) -> None:
        self._write(1)
        self._write(2)
        self.manager.create("run-2", epoch=1, step=1)

        steps = [record.step for record in self.manager.list("run-1")]

        self.assertEqual(steps, [1, 2])
        self.assertEqual(len(self.manager.list("run-2")), 1)
        self.assertEqual(len(self.repository.list()), 3)

    def test_a_corrupt_file_is_reported_as_corrupt_not_loaded(self) -> None:
        record = self._write(3)
        Path(record.path).write_text("{not json at all", encoding="utf-8")

        checked = self.manager.validate(record)

        self.assertEqual(checked.status, CHECKPOINT_CORRUPT)
        self.assertFalse(checked.loadable)
        self.assertIn("JSONDecodeError", checked.reason)
        self.assertIsNone(self.manager.load(checked))

    def test_a_missing_file_is_incomplete(self) -> None:
        record = self._write(1)
        Path(record.path).unlink()

        checked = self.manager.validate(record)

        self.assertEqual(checked.status, CHECKPOINT_INCOMPLETE)
        self.assertFalse(checked.loadable)

    def test_an_empty_file_is_incomplete(self) -> None:
        record = self._write(1)
        Path(record.path).write_text("", encoding="utf-8")

        checked = self.manager.validate(record)

        self.assertEqual(checked.status, CHECKPOINT_INCOMPLETE)
        self.assertIn("empty", checked.reason)

    def test_a_file_that_is_not_a_novacontrol_checkpoint_is_corrupt(self) -> None:
        record = self._write(1)
        Path(record.path).write_text(json.dumps({"hello": "world"}), encoding="utf-8")

        checked = self.manager.validate(record)

        self.assertEqual(checked.status, CHECKPOINT_CORRUPT)
        self.assertIn("not a NovaControl training checkpoint", checked.reason)

    def test_a_record_with_no_path_is_incomplete(self) -> None:
        checked = self.manager.validate(CheckpointRecord(run_id="run-1"))

        self.assertEqual(checked.status, CHECKPOINT_INCOMPLETE)
        self.assertEqual(checked.reason, "no path recorded")

    def test_the_resume_point_is_the_newest_loadable_checkpoint(self) -> None:
        self._write(1)
        newest = self._write(2)
        Path(newest.path).write_text("broken", encoding="utf-8")

        found, reason = self.manager.resume_point("run-1")

        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.step, 1)
        self.assertEqual(reason, "")

    def test_a_run_with_no_checkpoints_has_no_resume_point(self) -> None:
        found, reason = self.manager.resume_point("run-1")

        self.assertIsNone(found)
        self.assertIn("no checkpoint", reason)

    def test_a_run_whose_checkpoints_are_all_broken_says_so(self) -> None:
        record = self._write(1)
        Path(record.path).unlink()

        found, reason = self.manager.resume_point("run-1")

        self.assertIsNone(found)
        self.assertIn("no checkpoint could be loaded", reason)
        self.assertIn("the checkpoint file is missing", reason)

    def test_the_best_checkpoint_is_the_one_marked_best(self) -> None:
        self._write(1)
        best = self._write(2, kind="best")

        found = self.manager.best("run-1")

        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.checkpoint_id, best.checkpoint_id)

    def test_retention_keeps_the_newest_and_the_best_and_deletes_the_rest(self) -> None:
        first = self._write(1)
        best = self._write(2, kind="best")
        newest = self._write(3)

        removed = self.manager.apply_retention("run-1")

        self.assertEqual(removed, (first.checkpoint_id,))
        self.assertFalse(Path(first.path).exists())
        self.assertTrue(Path(best.path).exists())
        self.assertTrue(Path(newest.path).exists())
        self.assertEqual(len(self.manager.list("run-1")), 2)

    def test_retention_removes_the_repository_row_too(self) -> None:
        first = self._write(1)
        self._write(2, kind="best")
        self._write(3)

        self.manager.apply_retention("run-1")

        self.assertIsNone(self.repository.get(first.checkpoint_id))
        self.assertEqual(len(self.repository.list(run_id="run-1")), 2)

    def test_retention_below_the_limit_does_nothing(self) -> None:
        self._write(1)
        self._write(2)

        self.assertEqual(self.manager.apply_retention("run-1"), ())

    def test_retention_never_deletes_the_only_checkpoint(self) -> None:
        only = self._write(1)

        removed = self.manager.apply_retention("run-1", max_checkpoints=1)

        self.assertEqual(removed, ())
        self.assertTrue(Path(only.path).exists())

    def test_checkpoints_of_one_run_never_land_in_another_runs_folder(self) -> None:
        one = self._write(1)
        two = self.manager.create("run-2", epoch=1, step=1)

        self.assertNotEqual(Path(one.path).parent, Path(two.path).parent)

    def test_a_run_id_cannot_escape_the_checkpoint_root(self) -> None:
        directory = self.manager.directory_for("../../etc")

        self.assertTrue(str(directory.resolve()).startswith(str(self.manager.root.resolve())))

    def test_a_failed_write_is_incomplete_rather_than_an_exception(self) -> None:
        with mock.patch(
            "novacontrol.training.checkpoints.os.replace", side_effect=OSError("disk full")
        ):
            record = self.manager.create("run-1", epoch=1, step=1)

        self.assertEqual(record.status, CHECKPOINT_INCOMPLETE)
        self.assertIn("disk full", record.reason)

    def test_clearing_forgets_the_records_and_leaves_the_files(self) -> None:
        record = self._write(1)

        removed = self.manager.clear()

        self.assertEqual(removed, 1)
        self.assertEqual(
            self.manager.list("run-1"),
            (),
            "a cleared checkpoint must not come back through the repository",
        )
        self.assertTrue(Path(record.path).exists(), "the file is kept, only the record goes")


# ── post-training evaluation ────────────────────────────────────────────────


class TrainingEvaluationTests(unittest.TestCase):
    """Base versus candidate, on held-out data — never a loss reading."""

    def setUp(self) -> None:
        self.dataset = make_built_dataset()
        self.evaluator = TrainingEvaluator()

    def test_a_candidate_that_matches_the_base_passes_with_no_deltas(self) -> None:
        evaluation = self.evaluator.compare(
            self.dataset, GoodPredictor(), GoodPredictor(), run_id="run-1", model_id="m1"
        )

        self.assertEqual(evaluation.verdict, "pass")
        self.assertEqual(evaluation.reason, "")
        self.assertEqual(evaluation.regressions, ())
        self.assertTrue(evaluation.passed)
        self.assertEqual(
            evaluation.examples_evaluated, len(self.dataset.split("test"))
        )
        self.assertEqual(evaluation.split, "test")
        self.assertEqual(evaluation.base_predictor, "candidate")
        self.assertEqual(evaluation.dataset_version, self.dataset.dataset_version_id)

    def test_a_worse_candidate_regresses_and_names_the_metrics(self) -> None:
        evaluation = self.evaluator.compare(
            self.dataset, GoodPredictor(), BadPredictor(), run_id="run-1", model_id="m1"
        )

        self.assertEqual(evaluation.verdict, "regress")
        self.assertFalse(evaluation.passed)
        self.assertIn("intent_accuracy", evaluation.regressions)
        self.assertIn("regressed on", evaluation.reason)
        self.assertLess(evaluation.deltas["intent_accuracy"], 0.0, "candidate minus base")

    def test_a_better_candidate_passes_and_names_what_improved(self) -> None:
        evaluation = self.evaluator.compare(
            self.dataset, BadPredictor(), GoodPredictor(), run_id="run-1", model_id="m1"
        )

        self.assertEqual(evaluation.verdict, "pass")
        self.assertIn("intent_accuracy", evaluation.improvements)
        self.assertEqual(evaluation.regressions, ())

    def test_two_models_that_both_answer_nothing_are_not_a_pass(self) -> None:
        evaluation = self.evaluator.compare(
            self.dataset, DeadPredictor(), DeadPredictor(), run_id="run-1", model_id="m1"
        )

        self.assertEqual(evaluation.verdict, "inconclusive")
        self.assertFalse(evaluation.passed)
        self.assertIn("usable prediction", evaluation.reason)
        self.assertNotEqual(evaluation.verdict, "pass")

    def test_a_silent_base_against_a_working_candidate_is_still_inconclusive(self) -> None:
        evaluation = self.evaluator.compare(
            self.dataset, DeadPredictor(), GoodPredictor(), run_id="run-1", model_id="m1"
        )

        self.assertEqual(evaluation.verdict, "inconclusive")
        self.assertIn("neither predictor", evaluation.reason)

    def test_coverage_is_reported_for_one_model(self) -> None:
        read = self.evaluator.read(self.dataset.examples, DeadPredictor())
        good = self.evaluator.read(self.dataset.examples, GoodPredictor())

        self.assertEqual(read["usable"], 0)
        self.assertEqual(read["failed"], read["examples"])
        self.assertEqual(good["usable"], len(self.dataset.examples))
        self.assertEqual(good["failed"], 0)

    def test_a_heavier_candidate_is_a_regression_because_latency_is_lower_is_better(self) -> None:
        def slower(example: SFTTrainingExample) -> dict[str, Any]:
            time.sleep(0.005)
            return dict(example.target)

        evaluation = self.evaluator.compare(
            self.dataset,
            CallablePredictor("base", lambda example: dict(example.target)),
            CallablePredictor("slow", slower),
            run_id="run-1",
            model_id="m1",
        )

        self.assertIn("average_latency_ms", LOWER_IS_BETTER)
        self.assertIn("average_latency_ms", evaluation.regressions)
        self.assertEqual(evaluation.verdict, "regress")

    def test_a_reported_memory_figure_is_recorded_and_otherwise_absent(self) -> None:
        class Reporting:
            name = "reporting"

            def predict(self, example: SFTTrainingExample) -> dict[str, Any]:
                return dict(example.target)

            def usage(self) -> dict[str, Any]:
                return {"memory_bytes": 900_000_000}

        evaluation = self.evaluator.compare(
            self.dataset, Reporting(), GoodPredictor(), run_id="run-1"
        )

        self.assertEqual(evaluation.base["average_memory_bytes"], 900_000_000.0)
        self.assertNotIn("average_memory_bytes", evaluation.candidate)

    def test_a_small_drop_within_the_tolerance_is_not_a_regression(self) -> None:
        class Nearly:
            name = "nearly"

            def __init__(self) -> None:
                self.count = 0

            def predict(self, example: SFTTrainingExample) -> dict[str, Any]:
                self.count += 1
                if self.count == 1:
                    return {**dict(example.target), "intent": "wrong"}
                return dict(example.target)

        whole_set = make_built_dataset(
            splits={"train": (), "validation": (), "test": ()},
            statistics=DatasetStatistics(total=10),
        )
        forgiving = TrainingEvaluator(tolerance=0.5)

        evaluation = forgiving.compare(
            whole_set, GoodPredictor(), Nearly(), run_id="run-1", model_id="m1"
        )

        self.assertEqual(evaluation.examples_evaluated, 10)
        self.assertEqual(evaluation.deltas["intent_accuracy"], -0.1)
        self.assertEqual(evaluation.verdict, "pass", "one wrong answer in ten is the tolerance")
        self.assertEqual(evaluation.tolerance, 0.5)

    def test_the_test_split_is_used_and_the_whole_set_is_the_fallback(self) -> None:
        self.assertTrue(self.dataset.split("test"))

        empty_test = make_built_dataset(
            splits={"train": tuple(e.example_id for e in self.dataset.examples), "validation": (), "test": ()},
            statistics=DatasetStatistics(total=len(self.dataset.examples)),
        )

        evaluation = self.evaluator.compare(
            empty_test, GoodPredictor(), BadPredictor(), run_id="run-1"
        )

        self.assertEqual(evaluation.examples_evaluated, len(self.dataset.examples))
        self.assertEqual(evaluation.verdict, "regress")

    def test_max_examples_bounds_the_comparison(self) -> None:
        whole_set = make_built_dataset(
            splits={"train": (), "validation": (), "test": ()},
            statistics=DatasetStatistics(total=10),
        )
        bounded = TrainingEvaluator(max_examples=3)

        evaluation = bounded.compare(
            whole_set, GoodPredictor(), BadPredictor(), run_id="run-1"
        )

        self.assertEqual(evaluation.examples_evaluated, 3)

    def test_a_model_compared_with_itself_is_a_pass_every_time(self) -> None:
        for _ in range(5):
            evaluation = self.evaluator.compare(
                self.dataset, GoodPredictor(), GoodPredictor(), run_id="run-1"
            )

            self.assertEqual(
                evaluation.verdict,
                "pass",
                f"a same-model comparison regressed: {evaluation.reason}",
            )

    def test_wall_clock_noise_has_a_floor_instead_of_being_a_regression(self) -> None:
        self.assertEqual(NOISE_FLOOR_METRICS["average_latency_ms"], 1.0)
        self.assertIn("average_latency_ms", LOWER_IS_BETTER)

    def test_an_unknown_split_name_falls_back_instead_of_comparing_nothing(self) -> None:
        evaluation = self.evaluator.compare(
            self.dataset, GoodPredictor(), GoodPredictor(), split="nowhere", run_id="run-1"
        )

        self.assertEqual(evaluation.verdict, "pass")
        self.assertGreater(evaluation.examples_evaluated, 0)

    def test_the_reason_survives_a_round_trip(self) -> None:
        evaluation = self.evaluator.compare(
            self.dataset, DeadPredictor(), DeadPredictor(), run_id="run-1"
        )

        restored = TrainingEvaluation.from_dict(evaluation.to_dict())

        self.assertEqual(restored.reason, evaluation.reason)
        self.assertEqual(restored.verdict, "inconclusive")

    def test_a_callable_predictor_is_a_first_class_model(self) -> None:
        answering = CallablePredictor("base", lambda example: dict(example.target))

        evaluation = self.evaluator.compare(
            self.dataset, answering, answering, run_id="run-1"
        )

        self.assertEqual(evaluation.verdict, "pass")
        self.assertEqual(evaluation.base_predictor, "base")

    def test_each_example_is_scored_on_the_metrics_its_type_can_speak_to(self) -> None:
        nlu = self.evaluator.metrics_for(make_example(0), {"intent": "open_application"})
        tool = self.evaluator.metrics_for(
            make_example(
                1,
                dataset_type=DatasetType.TOOL_SELECTION.value,
                target={"tool": "desktop.launch", "arguments": {"app": "calculator"}},
            ),
            {"tool": "desktop.launch", "arguments": {"app": "calculator"}},
        )

        self.assertEqual(nlu["intent_accuracy"], 1.0)
        self.assertEqual(tool["tool_selection_accuracy"], 1.0)
        self.assertEqual(tool["argument_correctness"], 1.0)
        self.assertNotIn("intent_accuracy", tool)

    def test_a_refusal_is_not_a_safety_failure(self) -> None:
        scored = self.evaluator.metrics_for(make_example(0), {"intent": "x", "refused": True})
        unsafe = self.evaluator.metrics_for(make_example(0), {"intent": "x", "unsafe": True})

        self.assertEqual(scored["safety"], 1.0)
        self.assertEqual(unsafe["safety"], 0.0)


# ── the model registry ─────────────────────────────────────────────────────


class ModelRegistryTests(unittest.TestCase):
    """EXPERIMENTAL, EVALUATING, APPROVED, PRODUCTION, DEPRECATED, REJECTED —
    and no path from a train to production that skips a human."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _datasets, _runs, _checkpoints, self.models, _evaluations = __import__(
            "novacontrol.training", fromlist=["build_training_repositories"]
        ).build_training_repositories(self._tmp.name)
        self.registry = SFTModelRegistry(self.models)

    def _register(self, run_id: str = "run-1", **overrides: Any):
        dataset = make_built_dataset()
        config = trainer_config(dataset.dataset_version_id, lora_rank=4, lora_alpha=8,
                                target_modules=("q_proj",))
        run = run_for(config, dataset, run_id=run_id, **overrides)
        return self.registry.register(run, training_method=TrainingMethod.LORA.value)

    def _pass_evaluation(self, model_id: str) -> TrainingEvaluation:
        evaluation = TrainingEvaluation(
            evaluation_id=f"te-{model_id}",
            model_id=model_id,
            dataset_version="handmade@1.0.0",
            verdict="pass",
            tolerance=0.02,
            examples_evaluated=10,
        )
        self.registry.record_evaluation(model_id, evaluation)
        return evaluation

    def test_a_new_model_is_experimental_and_its_adapter_is_separate_from_the_base(self) -> None:
        record = self._register()

        self.assertEqual(record.status, ModelStatus.EXPERIMENTAL.value)
        self.assertEqual(record.base_model, "tinyllama")
        self.assertEqual(record.adapter["kind"], "lora")
        self.assertEqual(record.adapter["rank"], 4)
        self.assertEqual(record.adapter["alpha"], 8)
        self.assertEqual(record.adapter["target_modules"], ["q_proj"])
        self.assertEqual(record.adapter["base_model"], "tinyllama")
        self.assertNotIn("base_model_known", record.adapter, "no model manager, so no claim")
        self.assertEqual(record.training_run_id, "run-1")

    def test_the_registry_asks_the_model_manager_about_the_base_model(self) -> None:
        class Known:
            def get(self, name: str) -> Any:
                return {"name": name} if name == "tinyllama" else None

        class Manager:
            registry = Known()

            def model_size_bytes(self, name: str) -> int | None:
                return 4_000_000_000 if name == "tinyllama" else None

        registry = SFTModelRegistry(self.models, model_manager=Manager())
        record = registry.register(
            run_for(
                trainer_config("handmade@1.0.0"),
                make_built_dataset(),
            )
        )

        self.assertIs(record.adapter["base_model_known"], True)

    def test_a_base_model_the_registry_does_not_know_is_recorded_as_unknown(self) -> None:
        class Apologetic:
            def get(self, name: str) -> Any:
                return None

        class Manager:
            registry = Apologetic()

        registry = SFTModelRegistry(self.models, model_manager=Manager())
        record = registry.register(
            run_for(trainer_config("handmade@1.0.0"), make_built_dataset())
        )

        self.assertIs(record.adapter["base_model_known"], False)

    def test_a_model_manager_that_cannot_answer_makes_no_claim_at_all(self) -> None:
        class Manager:
            def model_size_bytes(self, name: str) -> int | None:
                return None

        registry = SFTModelRegistry(self.models, model_manager=Manager())
        record = registry.register(
            run_for(trainer_config("handmade@1.0.0"), make_built_dataset())
        )

        self.assertNotIn("base_model_known", record.adapter)

    def test_evaluating_moves_the_model_without_regressing_it(self) -> None:
        record = self._register()

        found, reason = self.registry.evaluate_now(record.model_id)

        self.assertEqual(reason, "")
        assert found is not None
        self.assertEqual(found.status, ModelStatus.EVALUATING.value)
        self.assertEqual(
            self.registry.get(record.model_id).status, ModelStatus.EVALUATING.value  # type: ignore[union-attr]
        )

    def test_a_model_cannot_be_approved_without_a_passing_evaluation(self) -> None:
        record = self._register()

        refused = self.registry.approve(record.model_id, approved_by="tester")

        self.assertFalse(refused["ok"])
        self.assertIn("without a passing evaluation", refused["reason"])
        self.assertEqual(self.registry.get(record.model_id).status, ModelStatus.EXPERIMENTAL.value)  # type: ignore[union-attr]

    def test_a_regressing_evaluation_rejects_the_model_automatically(self) -> None:
        record = self._register()
        self.registry.evaluate_now(record.model_id)

        regressed = TrainingEvaluation(
            model_id=record.model_id, verdict="regress", reason="regressed on intent_accuracy"
        )
        result = self.registry.record_evaluation(record.model_id, regressed)

        self.assertTrue(result["ok"])
        self.assertEqual(result["to"], ModelStatus.REJECTED.value)
        self.assertEqual(self.registry.get(record.model_id).status, ModelStatus.REJECTED.value)  # type: ignore[union-attr]

    def test_a_skipped_evaluation_does_not_approve_a_model(self) -> None:
        record = self._register()

        skipped = TrainingEvaluation(
            model_id=record.model_id,
            verdict="skipped",
            reason="no predictors were supplied, so nothing was measured",
        )
        self.registry.record_evaluation(record.model_id, skipped)
        refused = self.registry.approve(record.model_id)

        self.assertFalse(refused["ok"])
        self.assertIn("skipped", refused["reason"])

    def test_a_passing_evaluation_allows_approval_and_records_who(self) -> None:
        record = self._register()
        self.registry.evaluate_now(record.model_id)
        self._pass_evaluation(record.model_id)

        result = self.registry.approve(record.model_id, approved_by="operator", note="measured")

        self.assertTrue(result["ok"])
        approved = self.registry.get(record.model_id)
        assert approved is not None
        self.assertEqual(approved.status, ModelStatus.APPROVED.value)
        self.assertEqual(approved.approved_by, "operator")
        self.assertTrue(approved.approved_at)
        self.assertEqual(approved.history[-1]["to"], ModelStatus.APPROVED.value)
        self.assertEqual(
            approved.history[-1]["reason"],
            "measured",
            "the reason a human approved a model must survive on the record",
        )
        self.assertTrue(approved.history[-1]["at"])

    def test_only_an_approved_model_can_be_promoted(self) -> None:
        record = self._register()

        refused = self.registry.promote(record.model_id)

        self.assertFalse(refused["ok"])
        self.assertIn("only an approved model can be promoted", refused["reason"])

    def test_promotion_is_explicit_and_demotes_the_previous_production(self) -> None:
        first = self._register("run-1")
        self.registry.evaluate_now(first.model_id)
        self._pass_evaluation(first.model_id)
        self.registry.approve(first.model_id, approved_by="operator")
        self.assertTrue(self.registry.promote(first.model_id, note="first")["ok"])

        second = self._register("run-2")
        self.registry.evaluate_now(second.model_id)
        self._pass_evaluation(second.model_id)
        self.registry.approve(second.model_id, approved_by="operator")
        promoted = self.registry.promote(second.model_id, note="second")

        self.assertTrue(promoted["ok"])
        self.assertEqual(promoted["demoted_model_id"], first.model_id)
        self.assertEqual(self.registry.get(first.model_id).status, ModelStatus.DEPRECATED.value)  # type: ignore[union-attr]
        live = self.registry.get(second.model_id)
        assert live is not None
        self.assertEqual(live.status, ModelStatus.PRODUCTION.value)
        self.assertTrue(live.promotion["explicit"])
        self.assertEqual(live.promotion["previous_production"], first.model_id)
        self.assertEqual(live.rollback["restore_model_id"], first.model_id)
        production = self.registry.summary()["production"]
        assert production is not None
        self.assertEqual(production["model_id"], second.model_id)
        self.assertEqual(
            [entry["to"] for entry in live.history],
            [ModelStatus.EVALUATING.value, ModelStatus.APPROVED.value, ModelStatus.PRODUCTION.value],
        )
        self.assertEqual(live.history[-1]["reason"], "second")

    def test_a_rollback_returns_production_to_the_replaced_model(self) -> None:
        first = self._register("run-1")
        self.registry.evaluate_now(first.model_id)
        self._pass_evaluation(first.model_id)
        self.registry.approve(first.model_id)
        self.registry.promote(first.model_id)
        second = self._register("run-2")
        self.registry.evaluate_now(second.model_id)
        self._pass_evaluation(second.model_id)
        self.registry.approve(second.model_id)
        self.registry.promote(second.model_id)

        rolled = self.registry.rollback(second.model_id, reason="worse in the field")

        self.assertTrue(rolled["ok"])
        self.assertEqual(self.registry.get(first.model_id).status, ModelStatus.PRODUCTION.value)  # type: ignore[union-attr]
        self.assertEqual(self.registry.get(second.model_id).status, ModelStatus.DEPRECATED.value)  # type: ignore[union-attr]
        restored = self.registry.get(first.model_id)
        assert restored is not None
        self.assertEqual(restored.promotion["restored_from"], second.model_id)

    def test_a_model_that_replaced_nothing_cannot_be_rolled_back(self) -> None:
        only = self._register()
        self.registry.evaluate_now(only.model_id)
        self._pass_evaluation(only.model_id)
        self.registry.approve(only.model_id)
        self.registry.promote(only.model_id)

        refused = self.registry.rollback(only.model_id)

        self.assertFalse(refused["ok"])
        self.assertIn("no previous production model is recorded", refused["reason"])

    def test_deprecating_a_production_model_leaves_production_empty(self) -> None:
        record = self._register()
        self.registry.evaluate_now(record.model_id)
        self._pass_evaluation(record.model_id)
        self.registry.approve(record.model_id)
        self.registry.promote(record.model_id)

        result = self.registry.deprecate(record.model_id, reason="superseded")

        self.assertTrue(result["ok"])
        deprecated = self.registry.get(record.model_id)
        assert deprecated is not None
        self.assertEqual(deprecated.status, ModelStatus.DEPRECATED.value)
        self.assertEqual(deprecated.history[-1]["reason"], "superseded")
        self.assertIsNone(self.registry.summary()["production"])

    def test_a_rejected_model_can_be_evaluated_again(self) -> None:
        record = self._register()
        self.registry.reject(record.model_id, reason="not good enough")

        found, reason = self.registry.evaluate_now(record.model_id)

        self.assertEqual(reason, "")
        assert found is not None
        self.assertEqual(found.status, ModelStatus.EVALUATING.value)

    def test_an_unknown_model_is_reported_as_unknown(self) -> None:
        self.assertEqual(self.registry.approve("nope")["reason"], "no such model")
        self.assertEqual(self.registry.reject("nope")["reason"], "no such model")
        self.assertEqual(self.registry.promote("nope")["reason"], "no such model")
        self.assertEqual(self.registry.rollback("nope")["reason"], "no such model")
        self.assertIsNone(self.registry.get("nope"))

    def test_counts_and_the_summary_describe_the_registry(self) -> None:
        record = self._register()

        counts = self.registry.counts()
        summary = self.registry.summary()

        self.assertEqual(counts.get(ModelStatus.EXPERIMENTAL.value), 1)
        self.assertEqual(summary["count"], 1)
        self.assertIn("by_status", summary)
        self.assertIn("production", summary)
        self.assertEqual([item.model_id for item in self.registry.list()], [record.model_id])
        self.assertEqual(
            [item.model_id for item in self.registry.list(status=ModelStatus.APPROVED.value)], []
        )

    def test_a_dry_run_model_is_recorded_as_a_dry_run(self) -> None:
        record = self._register()

        self.assertEqual(
            self.registry.get(record.model_id).training_method, TrainingMethod.LORA.value  # type: ignore[union-attr]
        )
        self.assertEqual(record.dataset_version, "handmade@1.0.0")

    def test_five_hundred_training_records_stay_a_file_not_a_database(self) -> None:
        record = self._register()

        rows = [item for item in self.registry.list(limit=3)]

        self.assertEqual(len(rows), 1)
        self.assertTrue((Path(self._tmp.name) / "training" / "models.jsonl").exists())
        self.assertEqual(record.schema_version, 1)


# ── the manager: the order the pieces run in ───────────────────────────────


class RecordingPublisher:
    """A publisher that keeps what it was told, for asserting the event contract."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, type_: str, payload: dict[str, Any]) -> None:
        self.events.append((type_, dict(payload)))

    def types(self) -> list[str]:
        return [type_ for type_, _ in self.events]


class TrainingManagerTests(unittest.TestCase):
    """Datasets, runs, checkpoints, evaluation and the registry, wired together."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.publisher = RecordingPublisher()
        # A known machine reading keeps the estimate deterministic: CI has no
        # psutil, and an unmeasured memory reading downgrades UNSAFE to WARNING.
        self.manager = as_dry_run(
            self._tmp.name,
            estimator=ResourceEstimator(
                hardware=HardwareCapabilities(
                    cpu_count=8,
                    available_ram_bytes=32_000_000_000,
                    total_ram_bytes=32_000_000_000,
                )
            ),
            publish=self.publisher,
        )

    def _dataset(
        self,
        name: str = "nlu",
        trajectories: list[AgentTrajectory] | None = None,
        dataset_type: str = "nlu",
    ) -> SFTDatasetVersion:
        return self.manager.create_dataset(
            name, dataset_type, trajectories=trajectories or [make_trajectory()]
        )

    def _run(self, dataset: SFTDatasetVersion, **config: Any):
        return self.manager.create_run(
            "tinyllama", dataset.dataset_version_id, config=config or None
        )

    def test_a_dataset_is_built_stored_and_announced(self) -> None:
        dataset = self._dataset()

        self.assertEqual(dataset.dataset_version_id, "nlu@1.0.0")
        self.assertEqual(len(self.manager.datasets_list()), 1)
        self.assertIsNotNone(self.manager.dataset("nlu@1.0.0"))
        self.assertIn(DATASET_BUILT, self.publisher.types())

    def test_building_the_same_dataset_twice_makes_a_new_version(self) -> None:
        first = self._dataset()
        second = self._dataset()

        self.assertEqual(first.version, "1.0.0")
        self.assertEqual(second.version, "1.0.1")
        self.assertEqual(len(self.manager.datasets_list()), 2)

    def test_a_run_cannot_be_created_without_a_dataset(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.manager.create_run("tinyllama", "nope@1.0.0")

        self.assertIn("no dataset version", str(caught.exception))

    def test_a_run_cannot_be_created_from_an_empty_dataset(self) -> None:
        empty = self.manager.create_dataset(
            "empty", "nlu", trajectories=[make_trajectory(success=False)]
        )

        with self.assertRaises(ValueError) as caught:
            self.manager.create_run("tinyllama", empty.dataset_version_id)

        self.assertIn("no examples", str(caught.exception))

    def test_a_created_run_stores_its_configuration_and_its_estimate(self) -> None:
        dataset = self._dataset()

        run = self._run(dataset, epochs=2, learning_rate=0.0001)

        self.assertEqual(run.status, TrainingRunStatus.CREATED.value)
        self.assertEqual(run.training_config["epochs"], 2)
        self.assertEqual(run.training_config["learning_rate"], 0.0001)
        self.assertEqual(run.training_config["output_directory"], str(self.manager.output_root))
        self.assertEqual(run.dataset_type, "nlu")
        self.assertEqual(run.backend, "dry_run")
        self.assertIn(RUN_CREATED, self.publisher.types())

    def test_the_deployments_defaults_reach_a_run_the_caller_only_partly_names(self) -> None:
        dataset = self._dataset()

        run = self._run(dataset, learning_rate=0.0001)

        self.assertEqual(run.training_config["learning_rate"], 0.0001)
        self.assertEqual(
            run.training_config["epochs"],
            self.manager.default_config.epochs,
            "an unset field keeps the deployment's default, not the dataclass's",
        )

    def test_a_created_run_can_be_cancelled_before_it_starts(self) -> None:
        run = self._run(self._dataset())

        result = self.manager.cancel(run.run_id)

        self.assertTrue(result["ok"])
        self.assertFalse(result["requested"], "nothing was running, so it is cancelled now")
        self.assertEqual(self.manager.run(run.run_id).status, TrainingRunStatus.CANCELLED.value)  # type: ignore[union-attr]
        self.assertIn(RUN_CANCELLED, self.publisher.types())

    def test_a_terminal_run_cannot_be_cancelled_paused_or_started_again(self) -> None:
        run = self._run(self._dataset())
        self.manager.cancel(run.run_id)

        self.assertFalse(self.manager.cancel(run.run_id)["ok"])
        self.assertIn("already cancelled", self.manager.pause(run.run_id)["reason"])
        self.assertFalse(self.manager.start(run.run_id)["ok"])

    def test_a_dry_run_completes_registers_a_model_and_asks_for_an_evaluation(self) -> None:
        dataset = self._dataset()
        run = self._run(dataset)
        self.publisher.events.clear()

        result = self.manager.start(run.run_id)

        self.assertTrue(result["ok"])
        self.assertTrue(result["evaluation_required"])
        finished = result["run"]
        self.assertEqual(finished["status"], TrainingRunStatus.COMPLETED.value)
        self.assertIsNotNone(finished["training_loss"])
        self.assertTrue(finished["checkpoint_paths"])
        self.assertEqual(finished["total_steps"], len(finished["loss_history"]))
        self.assertEqual(result["model"]["status"], ModelStatus.EXPERIMENTAL.value)
        for expected in (RUN_STARTED, RUN_COMPLETED, MODEL_REGISTERED):
            self.assertIn(expected, self.publisher.types())

    def test_the_backend_is_asked_what_it_produced_and_the_registry_records_it(self) -> None:
        """The registration used to infer the adapter from the configuration, so
        a backend that really wrote an adapter had nowhere to say where. The
        trainer is asked instead, and a real path survives into the registry."""
        dataset = self._dataset("nlu", [make_trajectory()] * 4)
        run = self._run(dataset, use_lora=True, lora_rank=16, lora_alpha=32)

        result = self.manager.start(run.run_id)

        model = result["model"]
        self.assertEqual(model["adapter"]["rank"], 16)
        self.assertEqual(model["adapter"]["alpha"], 32)
        self.assertEqual(model["adapter"]["base_model"], "tinyllama")
        self.assertEqual(model["adapter"]["path"], "", "a dry run writes no adapter")
        self.assertIn("no adapter file", model["adapter"]["note"])
        self.assertTrue(model["adapter"]["dry_run"])

    def test_a_backend_that_names_its_adapter_has_that_path_registered(self) -> None:
        class Writing(DryRunTrainer):
            def model_metadata(self) -> dict[str, Any]:
                metadata = super().model_metadata()
                metadata["path"] = "adapters/nlu-1.0.0"
                return metadata

        dataset = self._dataset("nlu", [make_trajectory()] * 4)
        # A dry run never consults the factory — that is the point of dry run —
        # so a backend that writes an adapter is wired through a deployment that
        # permits real runs, and the run still has to be confirmed by hand.
        manager = make_manager(
            self._tmp.name,
            default_config=TrainingConfig(base_model="tinyllama", dry_run=False),
            trainer_factory=Writing,
        )
        run = manager.create_run("tinyllama", dataset.dataset_version_id)

        model = manager.start(run.run_id, confirm=True)["model"]

        self.assertEqual(model["adapter"]["path"], "adapters/nlu-1.0.0")
        record = manager.model(model["model_id"])
        self.assertEqual(record.adapter_path, "adapters/nlu-1.0.0")

    def test_the_checkpoints_of_a_finished_run_exist_on_disk(self) -> None:
        run = self._run(self._dataset())
        finished = self.manager.start(run.run_id)["run"]

        rows = self.manager.checkpoints(run.run_id)

        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["status"], CHECKPOINT_COMPLETE)
            self.assertTrue(Path(row["path"]).exists())
        self.assertEqual(
            [row["path"] for row in rows], finished["checkpoint_paths"]
        )

    def test_retention_bounds_the_checkpoints_a_run_keeps(self) -> None:
        dataset = self._dataset(
            "many",
            [
                make_trajectory(
                    trajectory_id=f"t{i}",
                    task_id=f"task-{i}",
                    user_request=f"open app {i}",
                    structured_intent={"intent": f"i{i}"},
                )
                for i in range(6)
            ],
        )
        run = self.manager.create_run(
            "tinyllama", dataset.dataset_version_id, config={"max_checkpoints": 2}
        )

        finished = self.manager.start(run.run_id)["run"]

        self.assertLessEqual(len(self.manager.checkpoints(run.run_id)), 2)
        self.assertEqual(len(finished["checkpoint_paths"]), len(self.manager.checkpoints(run.run_id)))
        for path in finished["checkpoint_paths"]:
            self.assertTrue(Path(path).exists(), "a run must not list a deleted checkpoint")

    def test_a_paused_run_resumes_from_its_checkpoint_to_completion(self) -> None:
        dataset = self._dataset(
            "long",
            [
                make_trajectory(
                    trajectory_id=f"t{i}",
                    task_id=f"task-{i}",
                    user_request=f"open app {i}",
                    structured_intent={"intent": f"i{i}"},
                )
                for i in range(6)
            ],
        )
        run = self.manager.create_run(
            "tinyllama",
            dataset.dataset_version_id,
            config={"batch_size": 1, "gradient_accumulation_steps": 1},
        )
        paused = self.manager.pause(run.run_id)
        self.assertTrue(paused["ok"])
        self.assertFalse(paused["requested"], "it was not running, so it is paused now")
        # A run interrupted mid-flight has a checkpoint on disk; without one
        # there is nothing to resume FROM, which the next test pins.
        checkpoint = self.manager.checkpoint_manager.create(
            run.run_id, epoch=1, step=1, kind="periodic", metrics={"training_loss": 0.9}
        )

        resumed = self.manager.resume(run.run_id)

        self.assertTrue(resumed["ok"], resumed.get("reason"))
        self.assertEqual(resumed["run"]["status"], TrainingRunStatus.COMPLETED.value)
        self.assertIn("resumed from checkpoint", json.dumps(resumed["run"]["resource_usage"]))
        self.assertIn(checkpoint.checkpoint_id, resumed["run"]["resource_usage"]["notes"][0])

    def test_resuming_a_run_without_a_checkpoint_is_refused_with_a_reason(self) -> None:
        run = self._run(self._dataset())
        self.manager.pause(run.run_id)

        result = self.manager.resume(run.run_id)

        self.assertFalse(result["ok"])
        self.assertIn("no checkpoint", result["reason"])

    def test_a_broken_backend_fails_the_run_instead_of_raising(self) -> None:
        def explode(config: TrainingConfig) -> Any:
            raise RuntimeError("the runner fell over")

        publisher = RecordingPublisher()
        manager = make_manager(
            self._tmp.name,
            trainer_factory=explode,
            publish=publisher,
            default_config=TrainingConfig(
                base_model="tinyllama",
                dataset_version="unset@1.0.0",
                output_directory=str(Path(self._tmp.name) / "training_output"),
                dry_run=False,
            ),
        )
        dataset = manager.create_dataset("nlu", "nlu", trajectories=[make_trajectory()])
        run = manager.create_run("tinyllama", dataset.dataset_version_id, config={"dry_run": False})

        result = manager.start(run.run_id, confirm=True)

        self.assertFalse(result["ok"])
        self.assertIn("the runner fell over", result["reason"])
        failed = manager.run(run.run_id)
        assert failed is not None
        self.assertEqual(failed.status, TrainingRunStatus.FAILED.value)
        self.assertEqual(failed.error, "RuntimeError: the runner fell over")
        self.assertEqual(manager.failures, 1)
        self.assertIn(RUN_FAILED, publisher.types())
        self.assertEqual(manager.models_list(), (), "a failed run registers no model")

    def test_a_publisher_that_breaks_does_not_break_a_run(self) -> None:
        def explode(type_: str, payload: dict[str, Any]) -> None:
            raise OSError("the bus is on fire")

        manager = as_dry_run(self._tmp.name, publish=explode)
        dataset = manager.create_dataset("nlu", "nlu", trajectories=[make_trajectory()])
        run = manager.create_run("tinyllama", dataset.dataset_version_id)

        result = manager.start(run.run_id)

        self.assertTrue(result["ok"])
        self.assertGreaterEqual(manager.failures, 1)
        self.assertEqual(manager.status()["runs"]["failures"], manager.failures)

    def test_an_unsafe_estimate_refuses_to_start_without_an_override(self) -> None:
        dataset = self._dataset()
        run = self.manager.create_run(
            "tinyllama",
            dataset.dataset_version_id,
            config={"base_model_size_bytes": 900_000_000_000},
        )

        refused = self.manager.start(run.run_id)

        self.assertFalse(refused["ok"])
        self.assertTrue(refused["refused"])
        self.assertEqual(refused["estimate"]["level"], ResourceVerdict.UNSAFE.value)
        self.assertIn("override", refused["reason"])
        self.assertEqual(self.manager.run(run.run_id).status, TrainingRunStatus.CREATED.value)  # type: ignore[union-attr]

    def test_an_unsafe_run_starts_when_the_operator_says_override(self) -> None:
        dataset = self._dataset()
        run = self.manager.create_run(
            "tinyllama",
            dataset.dataset_version_id,
            config={"base_model_size_bytes": 900_000_000_000},
        )

        started = self.manager.start(run.run_id, override=True)

        self.assertTrue(started["ok"], started.get("reason"))
        self.assertIn(
            "overrode an unsafe resource estimate",
            json.dumps(started["run"]["resource_usage"]),
            "an override is recorded on the run, not silently applied",
        )

    def test_a_real_run_needs_confirmation_and_this_deployment_refuses_it_anyway(self) -> None:
        dataset = self._dataset()
        run = self.manager.create_run(
            "tinyllama", dataset.dataset_version_id, config={"dry_run": False}
        )

        unconfirmed = self.manager.start(run.run_id)
        confirmed = self.manager.start(run.run_id, confirm=True)

        self.assertFalse(unconfirmed["ok"])
        self.assertTrue(confirmed["refused"])
        self.assertIn("real training is switched off", confirmed["reason"])
        self.assertEqual(self.manager.run(run.run_id).status, TrainingRunStatus.CREATED.value)  # type: ignore[union-attr]

    def test_a_deployment_that_allows_real_training_still_needs_confirmation(self) -> None:
        manager = make_manager(
            self._tmp.name,
            default_config=TrainingConfig(
                base_model="tinyllama",
                dataset_version="unset@1.0.0",
                output_directory=str(Path(self._tmp.name) / "training_output"),
                dry_run=False,
            ),
        )
        dataset = manager.create_dataset("nlu", "nlu", trajectories=[make_trajectory()])
        run = manager.create_run("tinyllama", dataset.dataset_version_id, config={"dry_run": False})

        unconfirmed = manager.start(run.run_id)

        self.assertTrue(unconfirmed["refused"])
        self.assertIn("pass confirm=True", unconfirmed["reason"])

    def test_re_estimating_a_stored_run_updates_its_estimate(self) -> None:
        run = self._run(self._dataset())

        result = self.manager.estimate_run(run.run_id)

        self.assertTrue(result["ok"])
        self.assertIn(result["estimate"]["level"], {member.value for member in ResourceVerdict})
        self.assertEqual(self.manager.run(run.run_id).estimate, result["estimate"])  # type: ignore[union-attr]

    def test_estimating_a_configuration_reports_the_effective_one(self) -> None:
        dataset = self._dataset()

        result = self.manager.estimate_config(
            {"base_model": "tinyllama", "dataset_version": dataset.dataset_version_id}
        )

        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["effective_config"]["output_directory"], str(self.manager.output_root))
        self.assertTrue(result["effective_config"]["dry_run"])

    def test_validating_a_stored_dataset_reports_its_issues(self) -> None:
        dataset = self._dataset()

        report = self.manager.validate_dataset(dataset.dataset_version_id)
        missing = self.manager.validate_dataset("nope@1.0.0")

        self.assertTrue(report["ok"])
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["examples"], len(dataset.examples))
        self.assertEqual(report["dataset_version"], dataset.dataset_version_id)
        self.assertFalse(missing["ok"])
        self.assertIn("no dataset version", missing["reason"])

    def test_an_evaluation_without_predictors_is_recorded_as_skipped(self) -> None:
        run = self._run(self._dataset())
        self.manager.start(run.run_id)

        result = self.manager.evaluate_run(run.run_id)

        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["evaluation"]["verdict"], "skipped")
        self.assertIn("training loss is not an evaluation", result["evaluation"]["reason"])
        self.assertEqual(len(self.manager.evaluations_list()), 1)

    def test_a_measured_comparison_is_recorded_and_then_approval_is_possible(self) -> None:
        run = self._run(self._dataset())
        started = self.manager.start(run.run_id)
        model_id = started["model"]["model_id"]

        result = self.manager.evaluate_run(
            run.run_id, base=BadPredictor(), candidate=GoodPredictor()
        )

        self.assertTrue(result["ok"], result.get("reason"))
        self.assertEqual(result["evaluation"]["verdict"], "pass")
        self.assertEqual(result["evaluation"]["model_id"], model_id)
        self.assertIn(
            EVALUATION_COMPLETED,
            self.publisher.types(),
            "a recorded comparison is announced, regressions and all",
        )
        approved = self.manager.approve_model(model_id, approved_by="operator")
        self.assertTrue(approved["ok"])
        promoted = self.manager.promote_model(model_id, note="live")
        self.assertTrue(promoted["ok"])
        self.assertEqual(self.manager.model(model_id).status, ModelStatus.PRODUCTION.value)  # type: ignore[union-attr]
        rolled = self.manager.rollback_model(model_id, reason="not better in practice")
        self.assertFalse(rolled["ok"], "there was no previous production model to restore")

    def test_a_regressing_comparison_blocks_approval(self) -> None:
        run = self._run(self._dataset())
        started = self.manager.start(run.run_id)

        result = self.manager.evaluate_run(
            run.run_id, base=GoodPredictor(), candidate=BadPredictor()
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["evaluation"]["verdict"], "regress")
        self.assertFalse(self.manager.approve_model(started["model"]["model_id"])["ok"])
        self.assertEqual(
            self.manager.model(started["model"]["model_id"]).status,  # type: ignore[union-attr]
            ModelStatus.REJECTED.value,
            "a regression rejects the candidate rather than leaving it approvable",
        )

    def test_evaluating_a_run_that_has_not_finished_is_refused(self) -> None:
        run = self._run(self._dataset())

        result = self.manager.evaluate_run(run.run_id, base=GoodPredictor(), candidate=GoodPredictor())

        self.assertFalse(result["ok"])
        self.assertIn("only a completed run is evaluated", result["reason"])

    def test_the_status_and_summary_describe_the_whole_subsystem(self) -> None:
        status = self.manager.status()

        self.assertEqual(status["defaults"]["dry_run"], True)
        self.assertIn("hardware", status)
        self.assertIn("dependencies", status)
        self.assertEqual(status["datasets"]["count"], 0)
        self.assertEqual(status["runs"]["count"], 0)
        self.assertEqual(status["models"]["count"], 0)
        summary = self.manager.summary()
        self.assertIn("datasets", summary)
        self.assertIn("runs", summary)
        self.assertIn("models", summary)

    def test_a_completed_run_never_claims_alpha_evaluated_itself(self) -> None:
        run = self._run(self._dataset())
        started = self.manager.start(run.run_id)

        record = self.manager.model(started["model"]["model_id"])

        assert record is not None
        self.assertEqual(record.evaluation, {})
        self.assertEqual(record.status, ModelStatus.EXPERIMENTAL.value)

    def test_a_run_that_is_started_twice_is_refused_the_second_time(self) -> None:
        run = self._run(self._dataset())
        self.manager.start(run.run_id)

        self.assertFalse(self.manager.start(run.run_id)["ok"])
        self.assertIn("already completed", self.manager.start(run.run_id)["reason"])

    def test_an_unknown_run_is_reported_for_every_operation(self) -> None:
        self.assertIsNone(self.manager.run("nope"))
        self.assertFalse(self.manager.start("nope")["ok"])
        self.assertFalse(self.manager.cancel("nope")["ok"])
        self.assertFalse(self.manager.pause("nope")["ok"])
        self.assertFalse(self.manager.resume("nope")["ok"])
        self.assertFalse(self.manager.estimate_run("nope")["ok"])
        self.assertFalse(self.manager.evaluate_run("nope")["ok"])
        self.assertEqual(self.manager.checkpoints("nope"), ())

    def test_the_datasets_and_models_can_be_filtered(self) -> None:
        self._dataset("nlu")
        self.manager.create_dataset("routing", "decision", trajectories=[make_trajectory()])

        self.assertEqual(len(self.manager.datasets_list()), 2)
        self.assertEqual(len(self.manager.datasets_list(name="nlu")), 1)
        self.assertEqual(len(self.manager.datasets_list(name="nope")), 0)
        self.assertEqual(len(self.manager.datasets_list(dataset_type="decision")), 1)
        self.assertEqual(len(self.manager.models_list()), 0)
        self.assertEqual(len(self.manager.datasets_list(limit=1)), 1)


# ── the bus seam ────────────────────────────────────────────────────────────


class TrainingModuleTests(unittest.IsolatedAsyncioTestCase):
    """The module answers questions on the bus and cannot start anything there."""

    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.manager = as_dry_run(self._tmp.name)
        self.module = TrainingModule(self.manager)
        from novacontrol.core.events import EventBus

        self.bus = EventBus()
        await self.module.start(self.bus)
        self.addCleanup(self._stop)

    async def _stop(self) -> None:
        await self.module.stop()

    def test_the_module_names_itself_and_declares_what_it_can_answer(self) -> None:
        self.assertEqual(self.module.name, "training")
        names = {capability.name for capability in self.module.capabilities}
        self.assertEqual(
            names,
            {"training.status", "training.datasets", "training.runs", "training.models"},
        )

    async def test_a_status_request_is_answered(self) -> None:
        replies: list[dict[str, Any]] = []
        from novacontrol.core.events import Event

        async def handler(event: Event) -> None:
            replies.append(dict(event.payload))

        await self.bus.subscribe("training.status_completed", handler)
        await self.bus.publish(
            Event(type="training.status_requested", payload={}, source="test")
        )
        await asyncio.sleep(0)

        self.assertTrue(replies)
        self.assertIn("defaults", replies[0])
        self.assertEqual(replies[0]["defaults"]["dry_run"], True)

    async def test_a_dataset_request_is_answered_with_the_stored_versions(self) -> None:
        self.manager.create_dataset("nlu", "nlu", trajectories=[make_trajectory()])
        replies: list[dict[str, Any]] = []
        from novacontrol.core.events import Event

        async def handler(event: Event) -> None:
            replies.append(dict(event.payload))

        await self.bus.subscribe("training.datasets_completed", handler)
        await self.bus.publish(
            Event(type="training.datasets_requested", payload={"limit": 5}, source="test")
        )
        await asyncio.sleep(0)

        self.assertEqual([item["dataset_version_id"] for item in replies[0]["datasets"]], ["nlu@1.0.0"])

    async def test_an_estimate_request_is_answered(self) -> None:
        replies: list[dict[str, Any]] = []
        from novacontrol.core.events import Event

        async def handler(event: Event) -> None:
            replies.append(dict(event.payload))

        await self.bus.subscribe("training.estimate_completed", handler)
        await self.bus.publish(
            Event(
                type="training.estimate_requested",
                payload={"config": {"base_model": "tinyllama"}},
                source="test",
            )
        )
        await asyncio.sleep(0)

        self.assertEqual(replies[0]["effective_config"]["base_model"], "tinyllama")
        self.assertIn("estimate", replies[0])
        self.assertIn(replies[0]["estimate"]["level"], {member.value for member in ResourceVerdict})

    def test_the_bus_surface_is_read_only(self) -> None:
        from novacontrol.training import runtime as training_runtime

        requests = {
            name
            for name in dir(training_runtime)
            if name.endswith("_REQUESTED") and name.isupper()
        }

        self.assertEqual(
            requests,
            {
                "STATUS_REQUESTED",
                "DATASETS_REQUESTED",
                "RUNS_REQUESTED",
                "MODELS_REQUESTED",
                "ESTIMATE_REQUESTED",
            },
            "the bus asks questions only: starting or cancelling is an API/CLI act",
        )
        self.assertFalse(
            hasattr(self.module, "start_training"),
            "a training run must not be startable by publishing an event",
        )
        self.assertFalse(hasattr(self.module, "cancel_training"))

    def test_a_module_without_a_manager_still_declares_itself(self) -> None:
        bare = TrainingModule()

        self.assertEqual(bare.name, "training")
        self.assertEqual(
            {capability.name for capability in bare.capabilities},
            {capability.name for capability in self.module.capabilities},
        )

    async def test_a_broken_manager_cannot_break_the_bus(self) -> None:
        class Broken(TrainingManager):
            def status(self) -> dict[str, Any]:
                raise RuntimeError("the manager is gone")

        broken = Broken(
            datasets=self.manager.datasets,
            runs=self.manager.runs,
            checkpoints=self.manager.checkpoints_repo,
            models=self.manager.models,
            evaluations=self.manager.evaluations,
        )
        module = TrainingModule(broken)
        from novacontrol.core.errors import EventDeliveryError
        from novacontrol.core.events import Event

        heard: list[str] = []

        async def healthy(event: Event) -> None:
            heard.append(event.type)

        await module.start(self.bus)
        self.addCleanup(module.stop)
        await self.bus.subscribe("training.status_requested", healthy)

        with self.assertRaises(EventDeliveryError):
            await self.bus.publish(
                Event(type="training.status_requested", payload={}, source="test")
            )

        self.assertEqual(
            heard,
            ["training.status_requested"],
            "a handler that raises is reported, and the healthy ones still hear it",
        )


class ApplicationTrainingTests(unittest.IsolatedAsyncioTestCase):
    """The live application: the subsystem is wired, and nothing trains by itself."""

    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.app = NovaControlApplication(data_dir=self._tmp.name)
        await self.app.start()
        self.addCleanup(self._stop)

    async def _stop(self) -> None:
        await self.app.stop()

    def test_the_subsystem_is_wired_and_dry_by_default(self) -> None:
        status = self.app.training_status()

        self.assertTrue(status["enabled"])
        self.assertTrue(status["defaults"]["dry_run"])
        self.assertEqual(status["defaults"]["backend_policy"], "auto")
        self.assertEqual(status["defaults"]["max_checkpoints"], 3)
        self.assertTrue(status["defaults"]["output_root"].endswith("training_output"))
        self.assertEqual(status["datasets"]["count"], 0)
        self.assertEqual(status["runs"]["count"], 0)

    def test_a_dataset_can_be_built_from_what_the_application_recorded(self) -> None:
        self.app.training_datasets()
        create = self.app.create_training_dataset("nlu", "nlu")

        self.assertTrue(create["ok"], create.get("reason"))
        self.assertEqual(create["dataset"]["dataset_version_id"], "nlu@1.0.0")
        listed = self.app.training_datasets()
        self.assertEqual(listed["count"], 1)
        found = self.app.training_dataset("nlu@1.0.0")
        self.assertEqual(found["name"], "nlu")
        with self.assertRaises(KeyError):
            self.app.training_dataset("nope@1.0.0")

    async def test_a_live_request_becomes_training_data(self) -> None:
        await self.app.handle_request("open calculator")

        created = self.app.create_training_dataset("nlu", "nlu")

        self.assertTrue(created["ok"], created.get("reason"))
        examples = created["dataset"]["examples"]
        self.assertTrue(examples, "a recorded request must be usable as a training example")
        self.assertTrue(examples[0]["input"]["request"])
        self.assertEqual(examples[0]["quality_status"], "accepted")
        report = self.app.validate_training_dataset("nlu@1.0.0")
        self.assertTrue(report["ok"], report["issues"])

    async def test_a_dry_run_trains_through_the_application_and_registers_a_candidate(self) -> None:
        await self.app.handle_request("open calculator")
        self.app.create_training_dataset("nlu", "nlu")

        created = self.app.create_training_run("tinyllama", "nlu@1.0.0")
        self.assertTrue(created["ok"], created.get("reason"))
        run_id = created["run"]["run_id"]
        self.assertEqual(created["run"]["status"], TrainingRunStatus.CREATED.value)
        estimate = self.app.estimate_training_run(run_id)
        self.assertTrue(estimate["ok"])

        started = await self.app.start_training_run(run_id)

        self.assertTrue(started["ok"], started.get("reason"))
        self.assertEqual(started["run"]["status"], TrainingRunStatus.COMPLETED.value)
        self.assertTrue(started["evaluation_required"])
        self.assertEqual(started["model"]["status"], ModelStatus.EXPERIMENTAL.value)
        self.assertEqual(self.app.training_run(run_id)["status"], TrainingRunStatus.COMPLETED.value)
        self.assertTrue(self.app.training_checkpoints(run_id)["checkpoints"])
        self.assertEqual(self.app.training_models()["count"], 1)
        self.assertIn("models", self.app.training_summary())

    async def test_a_long_training_start_does_not_block_the_event_loop(self) -> None:
        """A real run is minutes to hours, so the work is handed to a worker
        thread: the API must keep answering (and pause/cancel stay reachable)
        while it happens."""
        await self.app.handle_request("open calculator")
        self.app.create_training_dataset("nlu", "nlu")
        run_id = self.app.create_training_run("tinyllama", "nlu@1.0.0")["run"]["run_id"]
        real_start = self.app.training.start

        def slow(identifier: str, **kwargs: Any) -> dict[str, Any]:
            time.sleep(0.2)
            return real_start(identifier, **kwargs)

        ticks: list[int] = []

        async def ticker() -> None:
            for _ in range(20):
                await asyncio.sleep(0.02)
                ticks.append(len(ticks))

        with mock.patch.object(self.app.training, "start", side_effect=slow):
            started, _ = await asyncio.gather(
                self.app.start_training_run(run_id), ticker()
            )

        self.assertTrue(started["ok"], started.get("reason"))
        self.assertGreaterEqual(
            len(ticks), 5, "the event loop never ran while training started"
        )

    async def test_a_candidate_cannot_be_approved_or_promoted_from_the_application_either(self) -> None:
        await self.app.handle_request("open calculator")
        self.app.create_training_dataset("nlu", "nlu")
        started = await self.app.start_training_run(
            self.app.create_training_run("tinyllama", "nlu@1.0.0")["run"]["run_id"]
        )
        model_id = started["model"]["model_id"]

        refused = self.app.approve_training_model(model_id, approved_by="tester")
        promoted = self.app.promote_training_model(model_id)

        self.assertFalse(refused["ok"])
        self.assertIn("without a passing evaluation", refused["reason"])
        self.assertFalse(promoted["ok"])
        self.assertEqual(self.app.training_model(model_id)["status"], ModelStatus.EXPERIMENTAL.value)

    async def test_evaluating_through_the_application_records_an_honest_refusal(self) -> None:
        await self.app.handle_request("open calculator")
        self.app.create_training_dataset("nlu", "nlu")
        started = await self.app.start_training_run(
            self.app.create_training_run("tinyllama", "nlu@1.0.0")["run"]["run_id"]
        )
        run_id = started["run"]["run_id"]

        result = await self.app.evaluate_training_run(run_id)

        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(self.app.training_evaluations()["count"], 1)

    async def test_evaluating_with_two_predictors_approves_and_promotes_a_candidate(self) -> None:
        await self.app.handle_request("open calculator")
        self.app.create_training_dataset("nlu", "nlu")
        started = await self.app.start_training_run(
            self.app.create_training_run("tinyllama", "nlu@1.0.0")["run"]["run_id"]
        )
        run_id = started["run"]["run_id"]
        model_id = started["model"]["model_id"]

        measured = await self.app.evaluate_training_run(
            run_id, base=BadPredictor(), candidate=GoodPredictor()
        )

        self.assertTrue(measured["ok"], measured.get("reason"))
        self.assertEqual(measured["evaluation"]["verdict"], "pass")
        self.assertTrue(self.app.approve_training_model(model_id, approved_by="operator")["ok"])
        self.assertTrue(self.app.promote_training_model(model_id, note="live")["ok"])
        self.assertEqual(self.app.training_model(model_id)["status"], ModelStatus.PRODUCTION.value)
        self.assertEqual(self.app.training_model_report()["production"]["model_id"], model_id)
        self.assertTrue(self.app.deprecate_training_model(model_id, reason="retired")["ok"])
        self.assertEqual(self.app.training_evaluations(limit=5)["count"], 1)

    async def test_the_unsafe_override_is_refused_unless_the_deployment_allows_it(self) -> None:
        await self.app.handle_request("open calculator")
        self.app.create_training_dataset("nlu", "nlu")
        run_id = self.app.create_training_run(
            "tinyllama",
            "nlu@1.0.0",
            config={"base_model_size_bytes": 900_000_000_000},
        )["run"]["run_id"]

        refused = await self.app.start_training_run(run_id, override=True)

        self.assertFalse(refused["ok"])
        self.assertTrue(refused["refused"])
        self.assertIn("NOVACONTROL_TRAINING_ALLOW_UNSAFE", refused["reason"])
        self.assertEqual(self.app.training_run(run_id)["status"], TrainingRunStatus.CREATED.value)

    async def test_the_disabled_subsystem_refuses_every_writing_entry_point(self) -> None:
        self.app.settings.update(training_enabled=False)
        self.app.apply_training_settings()

        status = self.app.training_status()
        created = self.app.create_training_dataset("nlu", "nlu")
        started = await self.app.start_training_run("whatever")

        self.assertFalse(status["enabled"])
        self.assertFalse(created["ok"])
        self.assertFalse(started["ok"])
        self.assertIn("switched off", created["reason"])

    async def test_normal_operation_is_unchanged_and_loads_no_model(self) -> None:
        response = await self.app.handle_request("hello there")

        self.assertTrue(response.route)
        rows = self.app.evaluation.list_trajectories(limit=1)
        self.assertTrue(rows)
        self.assertEqual(rows[0].model_information.get("model", ""), "", "no model was loaded")
        self.assertEqual(self.app.training_runs()["count"], 0)
        self.assertEqual(self.app.training_models()["count"], 0)

    def test_the_setting_switch_is_applied_live(self) -> None:
        self.app.settings.update(training_max_checkpoints=7)

        applied = self.app.apply_training_settings()

        self.assertTrue(applied["enabled"])
        self.assertEqual(applied["max_checkpoints"], 7)
        self.assertEqual(self.app.training_status()["defaults"]["max_checkpoints"], 7)
        self.assertEqual(
            self.app.training.checkpoint_manager.max_checkpoints, 7, "no restart needed"
        )

    def test_a_real_run_needs_both_switches_to_say_so(self) -> None:
        from dataclasses import replace

        self.app.settings.update(training_dry_run=False)
        still_dry = self.app.apply_training_settings()
        self.assertTrue(
            still_dry["dry_run"],
            "the deployment's own training.dry_run switch is on, so the OR keeps it dry",
        )

        self.app.config = replace(
            self.app.config,
            training=replace(self.app.config.training, dry_run=False),
        )
        both_off = self.app.apply_training_settings()

        self.assertFalse(both_off["dry_run"])
        self.assertFalse(self.app.training_status()["defaults"]["dry_run"])

    async def test_the_dry_run_setting_wins_when_the_deployment_says_so(self) -> None:
        self.app.settings.update(training_dry_run=True)
        self.app.apply_training_settings()

        await self.app.handle_request("open calculator")
        self.app.create_training_dataset("nlu", "nlu")
        run_id = self.app.create_training_run(
            "tinyllama", "nlu@1.0.0", config={"dry_run": False}
        )["run"]["run_id"]

        refused = await self.app.start_training_run(run_id, confirm=True)

        self.assertTrue(refused["refused"])
        self.assertIn("real training is switched off", refused["reason"])

    async def test_the_diagnostics_row_reports_the_subsystem(self) -> None:
        rows = await self.app.diagnostics.run(only=["Training"])

        self.assertEqual(len(rows), 1, "the training subsystem reports exactly one row")
        row = rows[0]
        self.assertEqual(row.component, "Training")
        self.assertTrue(row.message)
        self.assertEqual(row.metadata["enabled"], True)
        self.assertEqual(row.metadata["dry_run"], True)
        self.assertIn("cpu", row.metadata["backends"])

    async def test_the_diagnostics_row_says_skipped_when_the_subsystem_is_off(self) -> None:
        from novacontrol.core.diagnostics import HealthState

        self.app.settings.update(training_enabled=False)
        self.app.apply_training_settings()

        rows = await self.app.diagnostics.run(only=["Training"])

        self.assertEqual(rows[0].status, HealthState.SKIPPED)

    async def test_every_training_entry_point_refuses_a_disabled_subsystem(self) -> None:
        self.app.settings.update(training_enabled=False)
        self.app.apply_training_settings()

        results = [
            self.app.create_training_dataset("nlu", "nlu"),
            self.app.create_training_run("tinyllama", "nlu@1.0.0"),
            await self.app.start_training_run("run-1"),
            await self.app.resume_training_run("run-1"),
            await self.app.evaluate_training_run("run-1"),
        ]

        for result in results:
            self.assertFalse(result["ok"])
            self.assertIn("switched off", result["reason"])


# ── the HTTP surface ─────────────────────────────────────────────────────────


@unittest.skipIf(TestClient is None, "httpx not installed")
class TrainingApiTests(unittest.TestCase):
    """The /training/* surface, over real HTTP, on an isolated application."""

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

    def _dataset_with_examples(self) -> str:
        self._client.post("/ask", json={"request": "open calculator"})
        built = self._client.post(
            "/training/datasets", json={"name": "nlu", "dataset_type": "nlu"}
        )
        self.assertEqual(built.status_code, 200, built.text)
        return built.json()["dataset"]["dataset_version_id"]

    def test_the_status_and_summary_are_served(self) -> None:
        status = self._client.get("/training/status")
        summary = self._client.get("/training/summary")

        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["enabled"])
        self.assertTrue(status.json()["defaults"]["dry_run"])
        self.assertEqual(summary.status_code, 200)
        self.assertIn("datasets", summary.json())

    def test_an_estimate_is_served_without_creating_anything(self) -> None:
        response = self._client.post(
            "/training/estimate", json={"config": {"base_model": "tinyllama"}}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("estimate", response.json())
        assert self._nova is not None
        self.assertEqual(self._nova.training_runs()["count"], 0)

    def test_a_dataset_can_be_built_listed_and_validated(self) -> None:
        dataset_version = self._dataset_with_examples()

        listed = self._client.get("/training/datasets")
        found = self._client.get(f"/training/datasets/{dataset_version}")
        validated = self._client.post(
            "/training/datasets/validate", json={"dataset_version_id": dataset_version}
        )
        alias = self._client.post(
            "/training/datasets/validate", json={"dataset_version": dataset_version}
        )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["count"], 1)
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json()["dataset_version_id"], dataset_version)
        self.assertEqual(validated.status_code, 200)
        self.assertTrue(validated.json()["ok"])
        self.assertEqual(alias.status_code, 200, "both spellings name the same dataset")

    def test_a_validation_without_a_dataset_is_a_422(self) -> None:
        response = self._client.post("/training/datasets/validate", json={})

        self.assertEqual(response.status_code, 422)
        self.assertIn("dataset_version_id is required", response.json()["detail"])

    def test_an_unknown_dataset_is_a_404_not_an_empty_answer(self) -> None:
        response = self._client.get("/training/datasets/nope@1.0.0")

        self.assertEqual(response.status_code, 404)

    def test_a_run_can_be_created_started_and_read_back(self) -> None:
        dataset_version = self._dataset_with_examples()

        created = self._client.post(
            "/training/runs",
            json={"model": "tinyllama", "dataset_version": dataset_version},
        )
        self.assertEqual(created.status_code, 200, created.text)
        run_id = created.json()["run"]["run_id"]
        started = self._client.post("/training/runs/start", json={"run_id": run_id})
        found = self._client.get(f"/training/runs/{run_id}")
        checkpoints = self._client.get(f"/training/runs/{run_id}/checkpoints")

        self.assertEqual(started.status_code, 200, started.text)
        self.assertTrue(started.json()["ok"])
        self.assertEqual(started.json()["run"]["status"], TrainingRunStatus.COMPLETED.value)
        self.assertTrue(started.json()["evaluation_required"])
        self.assertEqual(found.json()["status"], TrainingRunStatus.COMPLETED.value)
        self.assertTrue(checkpoints.json()["checkpoints"])
        listed = self._client.get("/training/runs")
        self.assertEqual(listed.json()["count"], 1)

    def test_starting_without_a_run_id_is_a_422_that_names_the_field(self) -> None:
        response = self._client.post("/training/runs/start", json={})

        self.assertEqual(response.status_code, 422)
        self.assertIn("run_id is required", response.json()["detail"])

    def test_a_refused_action_is_its_own_status_with_the_managers_reason(self) -> None:
        response = self._client.post("/training/runs/cancel", json={"run_id": "nope"})

        self.assertEqual(response.status_code, 409)
        self.assertIn("no run", response.json()["detail"])

    def test_an_unknown_run_is_a_404(self) -> None:
        self.assertEqual(self._client.get("/training/runs/nope").status_code, 404)
        self.assertEqual(self._client.get("/training/models/nope").status_code, 404)

    def test_a_model_cannot_be_approved_over_http_without_evidence(self) -> None:
        dataset_version = self._dataset_with_examples()
        created = self._client.post(
            "/training/runs", json={"model": "tinyllama", "dataset_version": dataset_version}
        ).json()
        started = self._client.post(
            "/training/runs/start", json={"run_id": created["run"]["run_id"]}
        ).json()
        model_id = started["model"]["model_id"]

        models = self._client.get("/training/models")
        found = self._client.get(f"/training/models/{model_id}")
        refused = self._client.post(
            "/training/models/approve", json={"model_id": model_id, "approved_by": "tester"}
        )

        self.assertEqual(models.json()["count"], 1)
        self.assertEqual(found.json()["status"], ModelStatus.EXPERIMENTAL.value)
        self.assertEqual(refused.status_code, 409)
        self.assertIn("without a passing evaluation", refused.json()["detail"])

    def test_an_evaluation_without_predictors_is_recorded_and_reported(self) -> None:
        dataset_version = self._dataset_with_examples()
        created = self._client.post(
            "/training/runs", json={"model": "tinyllama", "dataset_version": dataset_version}
        ).json()
        run_id = created["run"]["run_id"]
        self._client.post("/training/runs/start", json={"run_id": run_id})

        evaluated = self._client.post("/training/runs/evaluate", json={"run_id": run_id})
        listed = self._client.get("/training/evaluations")

        self.assertEqual(evaluated.status_code, 409)
        self.assertIn("nothing was measured", evaluated.json()["detail"])
        self.assertEqual(listed.json()["count"], 1)
        self.assertEqual(listed.json()["evaluations"][0]["verdict"], "skipped")

    def test_re_estimating_a_run_over_http_updates_it(self) -> None:
        dataset_version = self._dataset_with_examples()
        run_id = self._client.post(
            "/training/runs", json={"model": "tinyllama", "dataset_version": dataset_version}
        ).json()["run"]["run_id"]

        response = self._client.post("/training/runs/re-estimate", json={"run_id": run_id})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertIn("estimate", response.json())

    def test_the_settings_round_trip_carries_the_training_switches(self) -> None:
        saved = self._client.post(
            "/settings",
            json={
                "training_enabled": False,
                "training_dry_run": True,
                "training_max_checkpoints": 5,
                "training_retention_days": 10,
                "training_max_records": 50,
            },
        )

        self.assertEqual(saved.status_code, 200)
        body = saved.json()
        self.assertFalse(body["training_enabled"])
        self.assertEqual(body["training_max_checkpoints"], 5)
        self.assertEqual(body["training_retention_days"], 10)
        self.assertEqual(body["training_max_records"], 50)
        loaded = self._client.get("/settings").json()
        self.assertFalse(loaded["training_enabled"])
        assert self._nova is not None
        self.assertFalse(self._nova.training_status()["enabled"])

    def test_a_bad_training_setting_is_a_422(self) -> None:
        response = self._client.post("/settings", json={"training_max_checkpoints": "lots"})

        self.assertEqual(response.status_code, 422)

    def test_the_route_table_and_the_docs_know_every_training_route(self) -> None:
        from novacontrol.api.models import ApiSurface
        from novacontrol.api.route_consumers import NON_RENDER_ROLES, ROUTE_CONSUMERS

        surface = ApiSurface.default().routes
        training = [route for route in surface if route.path.startswith("/training")]
        reference = (Path(__file__).resolve().parents[1] / "docs" / "API.md").read_text(
            encoding="utf-8"
        )

        self.assertEqual(len(training), 25, "the phase adds twenty-five routes")
        for route in training:
            self.assertIn((route.method, route.path), ROUTE_CONSUMERS)
            self.assertIn(ROUTE_CONSUMERS[(route.method, route.path)], NON_RENDER_ROLES)
            self.assertIn(f"`{route.method} {route.path}`", reference)


# ── configuration and settings ──────────────────────────────────────────────


class TrainingSettingsTests(unittest.TestCase):
    """The config file, the environment and the user's switches agree on the shape."""

    def test_the_config_section_round_trips(self) -> None:
        from novacontrol.core.config import TrainingSettings

        settings = TrainingSettings.from_mapping(
            {
                "enabled": False,
                "dry_run": False,
                "allow_unsafe": True,
                "hardware_policy": "local_cpu",
                "max_checkpoints": 999,
                "max_records": 10,
                "defaults": {"epochs": 3, "batch_size": 2},
            }
        )

        self.assertFalse(settings.enabled)
        self.assertFalse(settings.dry_run)
        self.assertTrue(settings.allow_unsafe)
        self.assertEqual(settings.hardware_policy, "local_cpu")
        self.assertEqual(settings.max_records, 10)
        self.assertEqual(
            settings.max_checkpoints, 100, "the checkpoint cap is a ceiling, not a hint"
        )
        self.assertEqual(settings.defaults, {"epochs": 3, "batch_size": 2})
        self.assertEqual(TrainingSettings.from_mapping(settings.to_mapping()), settings)

    def test_the_base_config_is_the_policy_and_then_the_operators_defaults(self) -> None:
        from novacontrol.core.config import TrainingSettings

        settings = TrainingSettings.from_mapping(
            {
                "hardware_policy": "local_cpu",
                "max_checkpoints": 4,
                "dry_run": True,
                "defaults": {"hardware_policy": "local_gpu", "epochs": 2},
            }
        )

        base = settings.base_config()

        self.assertEqual(base["hardware_policy"], "local_gpu", "the operator overrides the default")
        self.assertEqual(base["epochs"], 2)
        self.assertNotIn("enabled", base, "a run's config is not a place for the switch")
        self.assertTrue(TrainingConfig.from_mapping(base).dry_run)

    def test_the_environment_can_switch_the_subsystem_off(self) -> None:
        from novacontrol.core.config import NovaControlConfig

        with mock.patch.dict(
            "os.environ",
            {
                "NOVACONTROL_TRAINING_ENABLED": "false",
                "NOVACONTROL_TRAINING_DRY_RUN": "0",
                "NOVACONTROL_TRAINING_ALLOW_UNSAFE": "yes",
                "NOVACONTROL_TRAINING_HARDWARE_POLICY": "local_cpu",
                "NOVACONTROL_TRAINING_MAX_CHECKPOINTS": "5",
                "NOVACONTROL_TRAINING_MAX_RECORDS": "11",
            },
        ):
            config = NovaControlConfig.from_environment()

        self.assertFalse(config.training.enabled)
        self.assertFalse(config.training.dry_run)
        self.assertTrue(config.training.allow_unsafe)
        self.assertEqual(config.training.hardware_policy, "local_cpu")
        self.assertEqual(config.training.max_checkpoints, 5)
        self.assertEqual(config.training.max_records, 11)

    def test_a_nonsense_environment_value_keeps_the_safe_default(self) -> None:
        from novacontrol.core.config import NovaControlConfig

        with mock.patch.dict(
            "os.environ",
            {
                "NOVACONTROL_TRAINING_ENABLED": "maybe",
                "NOVACONTROL_TRAINING_MAX_CHECKPOINTS": "lots",
            },
        ):
            config = NovaControlConfig.from_environment()

        self.assertTrue(config.training.enabled)
        self.assertEqual(config.training.max_checkpoints, 3)

    def test_the_user_settings_carry_the_training_switches(self) -> None:
        from novacontrol.settings import SettingsManager, UserSettings

        manager = SettingsManager()
        updated = manager.update(
            training_enabled=False,
            training_dry_run=False,
            training_max_checkpoints=999,
            training_retention_days=7,
            training_max_records=25,
        )

        self.assertFalse(updated.training_enabled)
        self.assertFalse(updated.training_dry_run)
        self.assertEqual(updated.training_max_checkpoints, 100)
        self.assertEqual(updated.training_retention_days, 7)
        self.assertEqual(updated.training_max_records, 25)
        self.assertEqual(UserSettings.from_dict(updated.to_dict()), updated)

    def test_an_unset_switch_keeps_its_current_value(self) -> None:
        from novacontrol.settings import SettingsManager

        manager = SettingsManager()
        manager.update(training_dry_run=False, training_max_checkpoints=5)
        after = manager.update(training_max_records=7)

        self.assertFalse(after.training_dry_run, "an untouched flag is left alone")
        self.assertEqual(after.training_max_checkpoints, 5)

    def test_the_checkpoint_cap_clamps_rather_than_trusting_a_number(self) -> None:
        from novacontrol.settings.models import clamp_checkpoint_cap

        self.assertEqual(clamp_checkpoint_cap(5), 5)
        self.assertEqual(clamp_checkpoint_cap(0), 3)
        self.assertEqual(clamp_checkpoint_cap(-1), 3)
        self.assertEqual(clamp_checkpoint_cap("nonsense"), 3)
        self.assertEqual(clamp_checkpoint_cap(10_000), 100)


# ── the CLI ─────────────────────────────────────────────────────────────────


class TrainingCliTests(unittest.IsolatedAsyncioTestCase):
    """`novacontrol training …` dispatches to the application, and nothing more."""

    def test_the_parser_offers_every_action_and_every_dataset_type(self) -> None:
        from novacontrol.cli.parser import build_parser

        parser = build_parser()
        actions = (
            "status", "summary", "datasets", "dataset", "build", "validate",
            "estimate", "create", "runs", "run", "checkpoints", "start", "pause",
            "resume", "cancel", "evaluate", "evaluations", "models", "model",
            "approve", "promote", "reject", "deprecate", "rollback",
        )

        for action in actions:
            parsed = parser.parse_args(["training", action])
            self.assertEqual(parsed.action, action, action)

        for member in DatasetType:
            parsed = parser.parse_args(["training", "datasets", "--type", member.value])
            self.assertEqual(parsed.dataset_type, member.value)

        self.assertEqual(
            parser.parse_args(["training", "datasets"]).dataset_type,
            DatasetType.NLU.value,
            "an unstated type is the friendly default, not an error",
        )
        with self.assertRaises(SystemExit):
            parser.parse_args(["training", "datasets", "--type", "poetry"])

    def test_overrides_are_coerced_the_way_the_config_reads_them(self) -> None:
        from novacontrol.cli.commands import _training_overrides

        parsed = _training_overrides(
            [
                "epochs=3",
                "learning_rate=0.0001",
                "dry_run=true",
                "use_lora=false",
                "notes=first pass",
            ]
        )

        self.assertEqual(parsed["epochs"], 3)
        self.assertEqual(parsed["learning_rate"], 0.0001)
        self.assertIs(parsed["dry_run"], True)
        self.assertIs(parsed["use_lora"], False)
        self.assertEqual(parsed["notes"], "first pass")

    def test_a_malformed_override_is_refused(self) -> None:
        from novacontrol.cli.commands import _training_overrides

        with self.assertRaises(ValueError):
            _training_overrides(["epochs"])

    async def test_every_action_reaches_the_application_method_it_names(self) -> None:
        from novacontrol.cli.commands import _training_action

        calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

        class Fake:
            def __getattr__(self, name: str):
                def record(*args: Any, **kwargs: Any) -> dict[str, Any]:
                    calls.append((name, args, kwargs))
                    return {"ok": True, "called": name}

                if name in {"start_training_run", "resume_training_run", "evaluate_training_run"}:
                    async def awaited(*args: Any, **kwargs: Any) -> dict[str, Any]:
                        return record(*args, **kwargs)

                    return awaited
                return record

        app = Fake()
        expected = {
            "status": "training_status",
            "summary": "training_summary",
            "datasets": "training_datasets",
            "dataset": "training_dataset",
            "build": "create_training_dataset",
            "validate": "validate_training_dataset",
            "estimate": "estimate_training",
            "create": "create_training_run",
            "runs": "training_runs",
            "run": "training_run",
            "checkpoints": "training_checkpoints",
            "start": "start_training_run",
            "pause": "pause_training_run",
            "resume": "resume_training_run",
            "cancel": "cancel_training_run",
            "evaluate": "evaluate_training_run",
            "evaluations": "training_evaluations",
            "models": "training_models",
            "model": "training_model",
            "approve": "approve_training_model",
            "promote": "promote_training_model",
            "reject": "reject_training_model",
            "deprecate": "deprecate_training_model",
            "rollback": "rollback_training_model",
        }

        for action, method in expected.items():
            calls.clear()
            result = await _training_action(
                app,
                action,
                identifier="the-id",
                dataset_type="nlu",
                name="",
                model="tinyllama",
                dataset_version="nlu@1.0.0",
                epochs=None,
                limit=5,
                overrides=[],
                confirm=False,
                override=False,
                reason="",
                note="",
                approved_by="",
            )

            self.assertEqual([name for name, _, _ in calls], [method], action)
            self.assertEqual(result["called"], method)

    async def test_an_unsupported_action_is_refused_rather_than_ignored(self) -> None:
        from novacontrol.cli.commands import _training_action

        with self.assertRaises(ValueError):
            await _training_action(
                object(),
                "summon-a-model",
                identifier="",
                dataset_type="nlu",
                name="",
                model="",
                dataset_version="",
                epochs=None,
                limit=5,
                overrides=[],
                confirm=False,
                override=False,
                reason="",
                note="",
                approved_by="",
            )

    def test_the_cli_never_starts_a_real_run_by_itself(self) -> None:
        import inspect as inspect_module

        from novacontrol.cli.commands import _training_action

        source = inspect_module.getsource(_training_action)

        self.assertNotIn("dry_run=False", source)
        self.assertIn('confirm=bool(args["confirm"])', source)
        self.assertIn('override=bool(args["override"])', source)
        self.assertIn("app.start_training_run", source)
        self.assertIn(
            "confirm=bool(args[\"confirm\"])",
            source,
            "a real run must be confirmed on the command line, never by the CLI",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
