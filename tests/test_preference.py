"""Phase 17: preference optimization (DPO / ORPO) — pairs, runs, evaluation, review.

The suite runs entirely on deterministic fixtures, mocks and dry-run mode: no
model is downloaded, no GPU is required, nothing trains for real and no CUDA
device is assumed. That is a requirement of the phase rather than a convenience —
a 16 GB Windows desktop with an Intel iGPU and an NPU must be able to prove the
preference subsystem works without loading Qwen3 8B, so every pair is built from
synthetic Phase 15 rows or submitted by a (fake) reviewer, every DPO/ORPO run is
the DRY-RUN backend or a mock, and the PEFT/LoRA boundary is exercised through an
injected runner.

Three policies get their own tests because they are the phase's safety spine:

  * a preference pair is never invented — both sides must be observable
    behaviour, and a pair whose orientation the record does not support is not
    produced at all,
  * a model is never approved from a preference loss — only from measured
    behaviour on held-out pairs, with the regression areas checked,
  * nothing trains on hidden chain-of-thought, and a row carrying one is refused
    before a pair exists rather than being trimmed into one.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient

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
    GoldenExample,
    LatencyMetrics,
    ToolCallRecord,
    UserFeedback,
    VerificationRecord,
)
from novacontrol.preference import (
    ALGORITHMS,
    EVIDENCE_EXPLICIT_USER_PREFERENCE,
    EVIDENCE_HUMAN_REVIEW,
    EVIDENCE_SYNTHETIC_RULE,
    EVIDENCE_TEACHER_AGREEMENT,
    EVIDENCE_VERIFIED_SUCCESS,
    PAIR_SPLIT_NAMES,
    PREFERENCE_COMPARISON,
    PREFERENCE_DATASET_BUILT,
    PREFERENCE_DRY_RUN,
    PREFERENCE_PAIR_BYTES_PER_TOKEN,
    PREFERENCE_PREPROCESSING_VERSION,
    PREFERENCE_REVIEW_DECIDED,
    PreferenceAlgorithm,
    PreferenceDatasetBuilder,
    PreferenceDatasetRepository,
    PreferenceDatasetType,
    PreferenceDatasetVersion,
    PreferenceEvaluator,
    PreferenceEvidence,
    PreferenceExample,
    PreferenceManager,
    PreferenceQualityConfig,
    PreferenceQualityFilter,
    PreferenceQualityStatus,
    PreferenceResourceEstimator,
    PreferenceReviewQueue,
    PreferenceReviewRepository,
    PreferenceRules,
    PreferenceSource,
    PreferenceStrength,
    PreferenceTrainingConfig,
    build_preference_repositories,
    check_regressions,
    next_preference_version,
    objective_for,
    preference_components,
    preference_dataset_version_id,
    preference_trainer_for,
)
from novacontrol.preference.backends import (
    DPOTrainer,
    DryRunPreferenceTrainer,
    ORPOTrainer,
)
from novacontrol.preference.review import DECISIONS
from novacontrol.preference.runtime import PreferenceModule
from novacontrol.training import (
    CHECKPOINT_COMPLETE,
    HardwareCapabilities,
    HardwarePolicy,
    ModelStatus,
    ResourceVerdict,
    SplitConfig,
    TrainingBackendUnavailable,
    TrainingCallbacks,
    TrainingConfig,
    TrainingRun,
    TrainingRunStatus,
    build_training_repositories,
)

SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123"


# ── fixtures ────────────────────────────────────────────────────────────────


def trajectory(
    trajectory_id: str = "traj-1",
    *,
    task_id: str = "task-1",
    request: str = "open calculator",
    intent: str = "open_application",
    success: bool = True,
    verified: bool = True,
    status: str = "completed",
    source: str = "test",
    metadata: Mapping[str, Any] | None = None,
    feedback: Any = None,
    tool: str = "desktop.launch",
) -> AgentTrajectory:
    """One run of Phase 15's record, in the shape the pair builder reads.

    Only the fields a preference pair can be built from are filled, and every one
    of them is OBSERVABLE: what was understood, what was decided, which tool ran
    with which arguments, whether verification passed. Nothing here is a thought.
    """
    return AgentTrajectory(
        trajectory_id=trajectory_id,
        task_id=task_id,
        user_request=request,
        source=source,
        structured_intent={"intent": intent, "confidence": 0.9, "strategy": "fast_path"},
        decision={"route": "direct_tool", "decision_type": "execute"},
        plan={
            "goal": request,
            "steps": [
                {"step_id": "s1", "action": "launch", "description": "launch"},
                {"step_id": "s2", "action": "verify", "description": "verify"},
            ],
        },
        execution_steps=(
            ExecutionStep(
                step_id="s1",
                description="launch",
                action="launch",
                status="completed" if success else "failed",
                attempts=1,
            ),
        ),
        tool_calls=(
            ToolCallRecord(
                tool=tool,
                step_id="s1",
                capability="desktop",
                arguments={"app": "calculator"},
                status="completed" if success else "failed",
                duration_ms=120.0,
            ),
        ),
        verification_results=(
            VerificationRecord(step_id="s1", status="verified" if verified else "failed"),
        ),
        status=status,
        success=success,
        final_result={"summary": "calculator opened" if success else ""},
        latency_metrics=LatencyMetrics(total_ms=250.0),
        metadata=dict(metadata or {}),
        user_feedback=feedback,
    )


def evaluation(trajectory_id: str = "traj-1", score: float = 0.9) -> EvaluationResult:
    """A Phase 15 evaluation with every dimension scored at ``score``."""
    return EvaluationResult(
        evaluation_id=f"ev-{trajectory_id}",
        trajectory_id=trajectory_id,
        task_id="task-1",
        overall_status="ok",
        task_success=score >= 0.5,
        dimensions=tuple(
            DimensionScore(dimension=dimension, score=score, status="ok")
            for dimension in EvaluationDimension
        ),
    )


def pair(
    dataset_type: str = PreferenceDatasetType.TOOL_SELECTION.value,
    *,
    prompt: Mapping[str, Any] | None = None,
    chosen: Mapping[str, Any] | None = None,
    rejected: Mapping[str, Any] | None = None,
    chosen_outcome: Mapping[str, Any] | None = None,
    rejected_outcome: Mapping[str, Any] | None = None,
    provenance: bool = True,
    confidence: float = 0.9,
    source: str = PreferenceSource.HUMAN_REVIEW.value,
    group_key: str = "",
    evidence: bool = True,
    **overrides: Any,
) -> PreferenceExample:
    """A well-formed pair: two observable behaviours, evidence and provenance."""
    builder = PreferenceDatasetBuilder()
    built = builder.human_pair(
        dataset_type,
        prompt=dict(prompt or {"request": "open calculator", "task_id": "task-1"}),
        chosen=dict(chosen or {"tool": "desktop.launch", "arguments": {"app": "calculator"}}),
        rejected=dict(rejected or {"tool": "browser.open"}),
        chosen_outcome=dict(chosen_outcome or {"success": True, "verification_records": 1}),
        rejected_outcome=dict(rejected_outcome or {"success": False}),
        confidence=confidence,
        group_key=group_key,
    )
    if not provenance:
        return PreferenceExample(
            **{**built.to_dict(), "source_trajectory_ids": [], "review": {}}
        )
    if not evidence:
        return PreferenceExample(**{**built.to_dict(), "strength": PreferenceStrength().to_dict()})
    updates = dict(overrides)
    return PreferenceExample(**{**built.to_dict(), **updates}) if updates else built


def make_manager(root: str | Path, **overrides: Any) -> PreferenceManager:
    """A preference manager wired to JSONL repositories under a temp directory.

    The run, checkpoint, model and evaluation stores are Phase 16's, deliberately:
    a preference run IS a training run, and a test that gave it a second store
    would not be testing the thing that ships.
    """
    datasets, reviews = build_preference_repositories(root)
    supervised, runs, checkpoints, models, evaluations = build_training_repositories(root)
    kwargs: dict[str, Any] = {
        "datasets": datasets,
        "reviews": reviews,
        "runs": runs,
        "checkpoints": checkpoints,
        "models": models,
        "evaluations": evaluations,
        "supervised_datasets": supervised,
        "output_root": Path(root) / "preference_output",
    }
    kwargs.update(overrides)
    return PreferenceManager(**kwargs)


def dry_run_manager(root: str | Path, **overrides: Any) -> PreferenceManager:
    """A manager whose deployment default is a DRY RUN — the safe configuration."""
    config = PreferenceTrainingConfig(
        base_model="tiny-model",
        preference_dataset_version="unset@1.0.0",
        output_directory=str(Path(root) / "preference_output"),
        dry_run=True,
    )
    return make_manager(root, default_config=config, **overrides)


def build_dataset(
    manager: PreferenceManager,
    *,
    name: str = "pairs",
    dataset_type: str = PreferenceDatasetType.TOOL_SELECTION.value,
    count: int = 12,
    version: str = "1.0.0",
    **overrides: Any,
) -> PreferenceDatasetVersion:
    """Build a dataset of distinguishable, group-safe pairs through the manager."""
    builder = PreferenceDatasetBuilder()
    pairs = [
        builder.human_pair(
            dataset_type,
            prompt={"request": f"open app {index}", "task_id": f"task-{index}"},
            chosen=dict(overrides.get("chosen") or {"tool": "desktop.launch", "arguments": {"app": "calculator"}}),
            rejected=dict(overrides.get("rejected") or {"tool": "browser.open"}),
            confidence=0.9,
            group_key=f"group-{index}",
        )
        for index in range(count)
    ]
    return manager.build_dataset(
        name,
        dataset_type,
        trajectories=(),
        pairs=pairs,
        version=version,
        queue_for_review=False,
    )


def config_for(dataset_version: str, **overrides: Any) -> PreferenceTrainingConfig:
    fields: dict[str, Any] = {
        "base_model": "tiny-model",
        "preference_dataset_version": dataset_version,
        "output_directory": "out",
        "dry_run": True,
        "batch_size": 2,
        "checkpoint_frequency": 1,
    }
    fields.update(overrides)
    return PreferenceTrainingConfig(**fields)


def run_for(
    config: PreferenceTrainingConfig, dataset: PreferenceDatasetVersion, **overrides: Any
) -> TrainingRun:
    fields: dict[str, Any] = {
        "run_id": "run-1",
        "name": "probe",
        "model": config.base_model,
        "dataset_version": dataset.dataset_version_id,
        "dataset_type": dataset.dataset_type,
        "training_config": config.to_mapping(),
        "backend": "dry_run" if config.dry_run else "dpo",
        "algorithm": config.algorithm,
        "random_seed": config.seed,
    }
    fields.update(overrides)
    return TrainingRun(**fields)


def ready() -> HardwareCapabilities:
    """A machine with the optional stack installed and plenty of memory."""
    return HardwareCapabilities(
        cpu_count=8,
        total_ram_bytes=64_000_000_000,
        available_ram_bytes=64_000_000_000,
        torch_available=True,
        transformers_available=True,
        peft_available=True,
        backends=("cpu",),
    )


def bare() -> HardwareCapabilities:
    """A machine with nothing optional installed — the CI/dependency baseline."""
    return HardwareCapabilities(
        cpu_count=4,
        total_ram_bytes=16_000_000_000,
        available_ram_bytes=8_000_000_000,
        backends=("cpu",),
    )


class ChoicePredictor:
    """A scripted model: answers the preferred side, or the other one."""

    def __init__(self, name: str, *, prefers: str = "chosen") -> None:
        self.predictor_name = name
        self.prefers = prefers

    @property
    def name(self) -> str:
        return self.predictor_name

    def predict(self, example: Any) -> Mapping[str, Any]:
        target = dict(getattr(example, "target", {}) or {})
        if self.prefers == "chosen":
            return target
        if self.prefers == "rejected":
            return {"tool": "browser.open"}
        return {}


class SilentPredictor:
    """A model that answers nothing at all — the honest zero-coverage case."""

    name = "silent"

    def predict(self, example: Any) -> Mapping[str, Any]:
        return {}


class RecordingPublisher:
    """Captures the events a manager published, in order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, type_: str, payload: Mapping[str, Any]) -> None:
        self.events.append((type_, dict(payload)))

    def types(self) -> list[str]:
        return [type_ for type_, _ in self.events]


def raw_pair(**overrides: Any) -> PreferenceExample:
    """A pair built directly, so a test can control what the filter will see.

    ``human_pair`` marks its result as already settled by a reviewer, which
    bypasses the WARN-to-review rule deliberately; tests about that rule need a
    pair that has not been through a person.
    """
    fields: dict[str, Any] = {
        "dataset_type": PreferenceDatasetType.TOOL_SELECTION.value,
        "prompt": {"request": "open calculator", "task_id": "task-1"},
        "chosen": {"tool": "desktop.launch", "arguments": {"app": "calculator"}},
        "rejected": {"tool": "browser.open"},
        "chosen_outcome": {
            "success": True,
            "verification_records": 1,
            "verification_failed": 0,
        },
        "rejected_outcome": {"success": False, "verification_records": 1},
        "preference_source": PreferenceSource.HUMAN_REVIEW.value,
        "confidence": 0.9,
        "strength": PreferenceStrength(
            confidence=0.9,
            evidence_quality=0.9,
            verification_strength=0.9,
            evidence=(
                PreferenceEvidence(
                    kind=EVIDENCE_HUMAN_REVIEW,
                    strength=0.95,
                    detail="a reviewer chose this candidate",
                    source=PreferenceSource.HUMAN_REVIEW.value,
                    verified=True,
                ),
            ),
            source=PreferenceSource.HUMAN_REVIEW.value,
        ),
        "source_trajectory_ids": ("traj-1",),
        "source_evaluation_ids": ("ev-1",),
        "quality": {"status": PreferenceQualityStatus.ACCEPTED.value, "reasons": []},
    }
    fields.update(overrides)
    return PreferenceExample(**fields)


def settled_pair(**overrides: Any) -> PreferenceExample:
    """The same pair after a reviewer has chosen a side for it."""
    return raw_pair(review={"decision": "choose_a", "reviewer": "probe", "reason": ""}, **overrides)


# ── the schema ────────────────────────────────


class PreferenceSchemaTests(unittest.TestCase):
    """The vocabulary: six families, two objectives, three verdicts, four answers."""

    def test_six_preference_families(self) -> None:
        self.assertEqual(
            [member.value for member in PreferenceDatasetType],
            ["nlu", "decision", "tool_selection", "planning", "recovery", "response"],
        )

    def test_two_objectives_and_no_reinforcement_learning_vocabulary(self) -> None:
        self.assertEqual([member.value for member in PreferenceAlgorithm], ["dpo", "orpo"])
        self.assertEqual(list(ALGORITHMS), ["dpo", "orpo"])
        names = " ".join(ALGORITHMS).lower()
        for absent in ("rlhf", "rlaif", "rlvr", "critique", "ppo", "grpo"):
            self.assertNotIn(absent, names)

    def test_quality_statuses_and_review_decisions(self) -> None:
        self.assertEqual(
            [member.value for member in PreferenceQualityStatus],
            ["accepted", "rejected", "needs_review"],
        )
        self.assertEqual(list(DECISIONS), ["choose_a", "choose_b", "tie", "reject"])

    def test_a_pair_projects_onto_the_supervised_shape_for_evaluation(self) -> None:
        example = raw_pair()

        projection = example.as_sft_example(split="test")

        self.assertEqual(projection.example_id, example.sft_example_id)
        self.assertEqual(dict(projection.target), dict(example.chosen))
        self.assertEqual(dict(projection.input), dict(example.prompt))
        self.assertEqual(projection.metadata["preference_id"], example.preference_id)

    def test_the_fingerprint_is_order_insensitive(self) -> None:
        first = raw_pair(chosen={"tool": "a"}, rejected={"tool": "b"})
        flipped = raw_pair(chosen={"tool": "b"}, rejected={"tool": "a"})

        self.assertEqual(first.fingerprint(), flipped.fingerprint())
        other = raw_pair(chosen={"tool": "a"}, rejected={"tool": "c"})
        self.assertNotEqual(first.fingerprint(), other.fingerprint())

    def test_the_group_key_keeps_a_prompt_together(self) -> None:
        grouped = raw_pair(prompt={"request": "x", "group_key": "g-7"})
        self.assertEqual(grouped.group_key, "g-7")
        by_trajectory = raw_pair(source_trajectory_ids=("b", "a"), prompt={"request": "x"})
        self.assertEqual(by_trajectory.group_key, "a|b")
        alone = raw_pair(prompt={"request": "x"}, source_trajectory_ids=())
        self.assertEqual(alone.group_key, alone.preference_id)

    def test_both_candidates_are_priced(self) -> None:
        small = raw_pair(chosen={"tool": "a"}, rejected={"tool": "b"})
        big = raw_pair(
            chosen={"tool": "a", "arguments": {"app": "x" * 400}},
            rejected={"tool": "b", "arguments": {"app": "y" * 400}},
        )

        self.assertGreater(big.estimated_tokens, small.estimated_tokens)

    def test_a_pair_round_trips_through_the_store(self) -> None:
        example = raw_pair()

        restored = PreferenceExample.from_dict(json.loads(json.dumps(example.to_dict())))

        self.assertEqual(restored.preference_id, example.preference_id)
        self.assertEqual(restored.fingerprint(), example.fingerprint())
        self.assertEqual(restored.strength.overall, example.strength.overall)
        self.assertTrue(restored.accepted or restored.quality_status == "")

    def test_a_malformed_row_is_read_defensively(self) -> None:
        restored = PreferenceExample.from_dict(
            {"preference_id": "p1", "prompt": "not-a-mapping", "confidence": "high"}
        )

        self.assertEqual(restored.prompt, {})
        self.assertEqual(restored.confidence, 0.0)
        self.assertEqual(restored.dataset_type, PreferenceDatasetType.NLU.value)

    def test_dataset_and_pair_version_strings(self) -> None:
        self.assertEqual(preference_dataset_version_id("nlu", ""), "nlu@")
        self.assertEqual(preference_dataset_version_id("nlu", "1.2.0"), "nlu@1.2.0")
        self.assertEqual(next_preference_version([]), "1.0.0")
        self.assertEqual(next_preference_version(["1.0.0", "1.0.2"]), "1.0.3")
        self.assertEqual(next_preference_version(["1.0.0"], requested="2.0.0"), "2.0.0")

    def test_a_dataset_projects_onto_the_supervised_shape(self) -> None:
        example = raw_pair()
        dataset = PreferenceDatasetVersion(
            dataset_version_id="pairs@1.0.0",
            name="pairs",
            version="1.0.0",
            dataset_type=example.dataset_type,
            examples=(example,),
            splits={"train": (example.preference_id,), "validation": (), "test": ()},
            source_data_version="probe",
        )

        projection = dataset.as_sft_dataset()

        self.assertEqual(len(projection), 1)
        self.assertEqual(dict(projection.examples[0].target), dict(example.chosen))
        self.assertEqual(projection.split("train")[0].example_id, example.sft_example_id)


# ── configuration ────────────────────────────────


class PreferenceConfigTests(unittest.TestCase):
    """A configuration that cannot start by itself, and says why it cannot."""

    def test_the_default_configuration_is_a_dry_run(self) -> None:
        self.assertTrue(PreferenceTrainingConfig().dry_run)
        self.assertEqual(PreferenceTrainingConfig().algorithm, "dpo")

    def test_a_usable_configuration_reports_no_errors(self) -> None:
        config = config_for("pairs@1.0.0")

        validation = config.validate()

        self.assertEqual(validation.errors, ())
        self.assertTrue(validation.valid)

    def test_an_unknown_algorithm_is_refused(self) -> None:
        config = config_for("pairs@1.0.0", algorithm="rlhf")

        errors = config.validate().errors

        self.assertTrue(any("algorithm must be one of" in error for error in errors))

    def test_beta_outside_the_range_is_refused(self) -> None:
        for beta in (0.0, 5.0):
            with self.subTest(beta=beta):
                errors = config_for("pairs@1.0.0", beta=beta).validate().errors
                self.assertTrue(any("beta must be between" in error for error in errors))

    def test_a_missing_dataset_version_is_refused(self) -> None:
        errors = PreferenceTrainingConfig(base_model="m").validate().errors

        self.assertTrue(any("preference_dataset_version is required" in e for e in errors))

    def test_an_unknown_preference_family_is_refused(self) -> None:
        errors = config_for("pairs@1.0.0", dataset_type="poetry").validate().errors

        self.assertTrue(any("unknown dataset_type" in error for error in errors))

    def test_dpo_keeps_a_reference_model_and_orpo_does_not(self) -> None:
        dpo = config_for("pairs@1.0.0", algorithm="dpo")
        orpo = config_for("pairs@1.0.0", algorithm="orpo")

        self.assertTrue(dpo.needs_reference_model)
        self.assertFalse(orpo.needs_reference_model)
        self.assertTrue(any("no reference_model" in w for w in dpo.validate().warnings))
        named = config_for("pairs@1.0.0", algorithm="orpo", reference_model="base")
        self.assertTrue(
            any("needs no reference model" in w for w in named.validate().warnings)
        )

    def test_a_real_run_is_a_warning_not_a_silent_permission(self) -> None:
        warnings = config_for("pairs@1.0.0", dry_run=False).validate().warnings

        self.assertTrue(any("dry_run is off" in warning for warning in warnings))

    def test_the_shared_validator_still_applies(self) -> None:
        errors = config_for("pairs@1.0.0", learning_rate=2.0).validate().errors

        self.assertTrue(any("learning_rate" in error for error in errors))

    def test_the_supervised_projection_keeps_the_shared_fields(self) -> None:
        config = config_for(
            "pairs@1.0.0", epochs=3, lora_rank=16, hardware_policy="local_cpu", precision="fp32"
        )

        shared = config.as_training_config()

        self.assertEqual(shared.dataset_version, "pairs@1.0.0")
        self.assertEqual(shared.epochs, 3)
        self.assertEqual(shared.lora_rank, 16)
        self.assertEqual(shared.hardware_policy, "local_cpu")
        self.assertEqual(config.effective_method, "lora")

    def test_a_supervised_configuration_can_seed_a_preference_one(self) -> None:
        shared = TrainingConfig(
            base_model="tiny", dataset_version="sft@1.0.0", output_directory="out", epochs=2
        )

        preference = PreferenceTrainingConfig.from_training_config(
            shared, algorithm="orpo"
        )

        self.assertEqual(preference.base_model, "tiny")
        self.assertEqual(preference.preference_dataset_version, "sft@1.0.0")
        self.assertEqual(preference.epochs, 2)
        self.assertEqual(preference.algorithm, "orpo")

    def test_from_mapping_keeps_defaults_for_unusable_values(self) -> None:
        config = PreferenceTrainingConfig.from_mapping(
            {"epochs": "many", "algorithm": None, "beta": "hot", "dry_run": False}
        )

        self.assertEqual(config.epochs, PreferenceTrainingConfig().epochs)
        self.assertEqual(config.beta, PreferenceTrainingConfig().beta)
        self.assertEqual(config.algorithm, "dpo")
        self.assertFalse(config.dry_run)

    def test_from_mapping_keeps_out_of_range_numbers_for_validation_to_report(self) -> None:
        config = PreferenceTrainingConfig.from_mapping({"epochs": 0})

        self.assertEqual(config.epochs, 0)
        self.assertTrue(config.validate().errors)

    def test_the_fingerprint_changes_with_the_objective(self) -> None:
        dpo = config_for("pairs@1.0.0", algorithm="dpo")
        orpo = config_for("pairs@1.0.0", algorithm="orpo")

        self.assertNotEqual(dpo.fingerprint(), orpo.fingerprint())
        self.assertEqual(dpo.fingerprint(), config_for("pairs@1.0.0").fingerprint())

    def test_the_objectives_document_themselves(self) -> None:
        documented = objective_for("dpo")

        self.assertTrue(documented["needs_reference_model"])
        self.assertIn("reference", documented["memory_note"].lower())
        self.assertIn("odds ratio", objective_for("orpo")["objective"].lower())
        self.assertEqual(objective_for("unknown"), {})


# ── the quality filter ────────────────────────────────


class PreferenceQualityTests(unittest.TestCase):
    """What may be learned from, held, or refused — with a reason each time."""

    def verdict(self, example: PreferenceExample, **config: Any) -> Any:
        filter_ = PreferenceQualityFilter(PreferenceQualityConfig(**config) if config else None)
        return filter_.assess(example)

    def test_a_well_evidenced_pair_is_accepted(self) -> None:
        verdict = self.verdict(raw_pair())

        self.assertEqual(verdict.status, PreferenceQualityStatus.ACCEPTED.value)
        self.assertEqual(verdict.reasons, ())
        self.assertIn("privacy", verdict.checks)

    def test_identical_candidates_are_rejected(self) -> None:
        same = {"tool": "desktop.launch"}
        verdict = self.verdict(raw_pair(chosen=same, rejected=dict(same)))

        self.assertEqual(verdict.status, PreferenceQualityStatus.REJECTED.value)
        self.assertIn("identical_candidates", verdict.reasons)

    def test_two_failures_are_rejected(self) -> None:
        verdict = self.verdict(
            raw_pair(
                chosen_outcome={"success": False},
                rejected_outcome={"success": False},
            )
        )

        self.assertEqual(verdict.status, PreferenceQualityStatus.REJECTED.value)
        self.assertIn("both_candidates_failed", verdict.reasons)

    def test_a_contradictory_orientation_is_rejected(self) -> None:
        verdict = self.verdict(
            raw_pair(
                chosen_outcome={"success": False},
                rejected_outcome={"success": True},
            )
        )

        self.assertEqual(verdict.status, PreferenceQualityStatus.REJECTED.value)
        self.assertIn("orientation_contradicts_outcome", verdict.reasons)

    def test_candidates_that_share_nothing_are_rejected(self) -> None:
        verdict = self.verdict(raw_pair(chosen={"tool": "a"}, rejected={"other": "b"}))

        self.assertEqual(verdict.status, PreferenceQualityStatus.REJECTED.value)
        self.assertIn("incomparable_candidates", verdict.reasons)

    def test_a_family_with_required_keys_refuses_candidates_without_them(self) -> None:
        verdict = self.verdict(raw_pair(chosen={"arguments": {"a": 1}}, rejected={"tools": 1}))

        self.assertEqual(verdict.status, PreferenceQualityStatus.REJECTED.value)
        self.assertIn("malformed_candidate", verdict.reasons)

    def test_missing_evidence_is_held_rather_than_guessed_about(self) -> None:
        verdict = self.verdict(raw_pair(strength=PreferenceStrength()))

        self.assertEqual(verdict.status, PreferenceQualityStatus.NEEDS_REVIEW.value)
        self.assertIn("insufficient_evidence", verdict.reasons)

    def test_low_confidence_is_held(self) -> None:
        verdict = self.verdict(raw_pair(confidence=0.05))

        self.assertEqual(verdict.status, PreferenceQualityStatus.NEEDS_REVIEW.value)
        self.assertIn("low_confidence", verdict.reasons)

    def test_missing_provenance_is_held(self) -> None:
        verdict = self.verdict(raw_pair(source_trajectory_ids=(), source_evaluation_ids=(), review={}))

        self.assertEqual(verdict.status, PreferenceQualityStatus.NEEDS_REVIEW.value)
        self.assertIn("missing_provenance", verdict.reasons)

    def test_a_teacher_or_synthetic_preference_is_held_until_a_person_agrees(self) -> None:
        synthetic = raw_pair(
            preference_source=PreferenceSource.SYNTHETIC.value,
            strength=PreferenceStrength(
                confidence=0.9,
                evidence_quality=0.4,
                evidence=(
                    PreferenceEvidence(kind=EVIDENCE_SYNTHETIC_RULE, strength=0.4, verified=False),
                ),
                source=PreferenceSource.SYNTHETIC.value,
            ),
        )

        verdict = self.verdict(synthetic)

        self.assertEqual(verdict.status, PreferenceQualityStatus.NEEDS_REVIEW.value)
        self.assertIn("unverified_source", verdict.reasons)
        self.assertIn("synthetic", verdict.issues[0].detail)

    def test_a_settled_pair_is_not_held_again(self) -> None:
        settled = settled_pair(confidence=0.05)

        verdict = self.verdict(settled)

        self.assertEqual(verdict.status, PreferenceQualityStatus.ACCEPTED.value)
        self.assertIn("low_confidence", verdict.reasons)

    def test_sensitive_text_is_removed_on_the_way_in(self) -> None:
        filter_ = PreferenceQualityFilter()
        dirty = raw_pair(
            chosen={"tool": "desktop.launch", "arguments": {"token": SECRET}},
            rejected={"tool": "browser.open"},
        )

        cleaned, verdict = filter_.apply(dirty)

        self.assertIsNone(cleaned)
        self.assertEqual(verdict.status, PreferenceQualityStatus.REJECTED.value)
        self.assertIn("sensitive_data", verdict.reasons)

    def test_residual_sensitive_text_can_hold_instead_of_reject(self) -> None:
        filter_ = PreferenceQualityFilter(
            PreferenceQualityConfig(residual_sensitive_rejects=False)
        )
        dirty = raw_pair(
            chosen={"tool": "desktop.launch", "arguments": {"token": SECRET}},
            rejected={"tool": "browser.open"},
        )

        cleaned, verdict = filter_.apply(dirty)

        self.assertIsNotNone(cleaned)
        self.assertEqual(verdict.status, PreferenceQualityStatus.NEEDS_REVIEW.value)
        self.assertEqual(cleaned.chosen["arguments"]["token"], REDACTED)  # type: ignore[index]
        self.assertNotIn(SECRET, json.dumps(cleaned.to_dict()))

    def test_the_filter_can_be_switched_off_and_says_so(self) -> None:
        verdict = self.verdict(raw_pair(confidence=0.0), enabled=False)

        self.assertEqual(verdict.status, PreferenceQualityStatus.ACCEPTED.value)
        self.assertIn("filter_disabled", verdict.reasons)

    def test_an_out_of_scope_source_is_refused(self) -> None:
        verdict = self.verdict(
            raw_pair(preference_source=PreferenceSource.SYNTHETIC.value),
            allow_sources=(PreferenceSource.HUMAN_REVIEW.value,),
        )

        self.assertEqual(verdict.status, PreferenceQualityStatus.REJECTED.value)
        self.assertIn("source_not_allowed", verdict.reasons)

    def test_a_required_verified_outcome_is_enforced(self) -> None:
        unverified = raw_pair(
            strength=PreferenceStrength(
                confidence=0.9,
                evidence_quality=0.9,
                verification_strength=0.0,
                evidence=(PreferenceEvidence(kind=EVIDENCE_HUMAN_REVIEW, verified=True),),
            )
        )

        verdict = self.verdict(unverified, require_verified_outcome=True)

        self.assertEqual(verdict.status, PreferenceQualityStatus.NEEDS_REVIEW.value)
        self.assertIn("verification_missing", verdict.reasons)

    def test_every_issue_is_structured(self) -> None:
        verdict = self.verdict(raw_pair(confidence=0.0, strength=PreferenceStrength()))

        for issue in verdict.issues:
            payload = issue.to_dict()
            self.assertTrue(payload["code"])
            self.assertIn(payload["severity"], {"info", "warn", "error"})
            self.assertTrue(payload["detail"])

    def test_the_strength_axes_stay_separate(self) -> None:
        strength = PreferenceStrength(
            confidence=1.0,
            evidence_quality=0.5,
            verification_strength=0.0,
            evidence=(PreferenceEvidence(kind=EVIDENCE_VERIFIED_SUCCESS, verified=True),),
            source=PreferenceSource.VERIFIED_OUTCOME.value,
        )

        self.assertEqual(strength.confidence, 1.0)
        self.assertEqual(strength.evidence_quality, 0.5)
        self.assertEqual(strength.verification_strength, 0.0)
        self.assertEqual(strength.overall, round(0.4 * 1.0 + 0.35 * 0.5, 6))
        self.assertTrue(strength.verified)
        self.assertFalse(strength.to_dict()["verified"] is None)

    def test_a_source_says_whether_it_rests_on_an_observation(self) -> None:
        self.assertTrue(PreferenceSource.HUMAN_REVIEW.verified)
        self.assertTrue(PreferenceSource.VERIFIED_OUTCOME.verified)
        self.assertFalse(PreferenceSource.TEACHER_MODEL.verified)
        self.assertFalse(PreferenceSource.SYNTHETIC.verified)


# ── the dataset builder ─────────────────────────────────────


class PreferenceDatasetBuilderTests(unittest.TestCase):
    """Pairs come from what was observed, or they do not come at all."""

    def test_a_stored_version_cannot_be_rewritten(self) -> None:
        repository = PreferenceDatasetRepository()
        dataset = self.build(
            trajectories=(
                trajectory("traj-1", intent="open_application"),
                trajectory("traj-2", intent="search_web", success=False, verified=False),
            )
        )
        self.assertEqual(len(dataset), 1)
        repository.save(dataset)

        repository.save(dataset)  # saving the same version again is allowed
        rewritten = replace(
            dataset,
            examples=(),
            statistics=dataset.statistics,
        )
        with self.assertRaises(ValueError):
            repository.save(rewritten)

    def setUp(self) -> None:
        self.builder = PreferenceDatasetBuilder()

    def build(self, **overrides: Any) -> PreferenceDatasetVersion:
        fields: dict[str, Any] = {
            "name": "nlu-pairs",
            "dataset_type": PreferenceDatasetType.NLU,
            "version": "1.0.0",
        }
        fields.update(overrides)
        return self.builder.build(**fields)

    @staticmethod
    def split_content(
        dataset: PreferenceDatasetVersion,
    ) -> dict[str, tuple[str, ...]]:
        """Which pairs a split holds, by content rather than by minted id."""
        fingerprints = {
            item.preference_id: item.fingerprint() for item in dataset.examples
        }
        return {
            name: tuple(
                fingerprints[preference_id]
                for preference_id in ids
                if preference_id in fingerprints
            )
            for name, ids in dataset.splits.items()
        }

    def test_a_verified_outcome_decides_between_two_candidates(self) -> None:
        winner = trajectory("traj-1", intent="open_application")
        loser = trajectory("traj-2", intent="search_web", success=False, verified=False)

        dataset = self.build(trajectories=(winner, loser))

        self.assertEqual(len(dataset), 1)
        pair = dataset.examples[0]
        self.assertEqual(pair.preference_source, PreferenceSource.VERIFIED_OUTCOME.value)
        self.assertEqual(pair.chosen["intent"], "open_application")
        self.assertEqual(pair.rejected["intent"], "search_web")
        self.assertEqual(pair.source_trajectory_ids, ("traj-1", "traj-2"))
        self.assertEqual(dict(dataset.preference_sources), {"verified_outcome": 1})
        self.assertTrue(pair.strength.verified)
        self.assertEqual(pair.chosen_outcome["success"], True)
        self.assertEqual(pair.rejected_outcome["success"], False)

    def test_one_run_is_not_a_preference(self) -> None:
        dataset = self.build(trajectories=(trajectory(),))

        self.assertEqual(len(dataset), 0)
        self.assertGreaterEqual(dataset.statistics.skipped["no_second_candidate"], 1)
        self.assertEqual(dataset.accepted_pairs(), ())

    def test_two_successes_are_not_a_preference(self) -> None:
        first = trajectory("traj-1", intent="open_application")
        second = trajectory("traj-2", intent="open_application_fast")

        dataset = self.build(trajectories=(first, second))

        self.assertEqual(len(dataset), 0)
        self.assertIn("no_evidence_the_rejected_side_is_worse", dataset.statistics.skipped)

    def test_explicit_feedback_pairs_an_accepted_candidate_with_a_rejected_one(self) -> None:
        accepted = trajectory(
            "traj-1", intent="open_application", feedback=UserFeedback(label="accepted")
        )
        rejected = trajectory(
            "traj-2", intent="search_web", success=False, feedback=UserFeedback(label="rejected")
        )

        dataset = self.build(trajectories=(accepted, rejected))

        sources = dict(dataset.preference_sources)
        self.assertEqual(sources.get("explicit_user_feedback"), 1)
        pair = next(
            item
            for item in dataset.examples
            if item.preference_source == PreferenceSource.EXPLICIT_USER_FEEDBACK.value
        )
        self.assertEqual(pair.chosen["intent"], "open_application")
        self.assertTrue(
            any(item.kind == EVIDENCE_EXPLICIT_USER_PREFERENCE for item in pair.strength.evidence)
        )

    def test_a_structured_correction_beats_what_the_run_produced(self) -> None:
        corrected = trajectory(
            "traj-1",
            intent="search_web",
            success=False,
            feedback=UserFeedback(label="corrected"),
            metadata={"corrected_output": {"intent": "open_application"}},
        )

        dataset = self.build(trajectories=(corrected,))

        self.assertEqual(len(dataset), 1)
        pair = dataset.examples[0]
        self.assertEqual(pair.chosen["intent"], "open_application")
        self.assertEqual(pair.rejected["intent"], "search_web")
        self.assertEqual(pair.preference_source, PreferenceSource.EXPLICIT_USER_FEEDBACK.value)
        self.assertIn("correction", pair.tags)

    def test_prose_in_metadata_is_never_parsed_into_a_pair(self) -> None:
        prose = trajectory(
            "traj-1",
            feedback=UserFeedback(label="corrected"),
            metadata={"corrected_output": "the user said it should have opened the calculator"},
        )

        dataset = self.build(trajectories=(prose,))

        self.assertEqual(len(dataset), 0)

    def test_evaluation_scores_decide_where_the_margin_is_real(self) -> None:
        good = trajectory("traj-1", intent="open_application", success=True, verified=True)
        worse = trajectory("traj-2", intent="open_calculator")
        evaluations = (evaluation("traj-1", 0.95), evaluation("traj-2", 0.4))

        dataset = self.build(trajectories=(good, worse), evaluations=evaluations)

        sources = dict(dataset.preference_sources)
        self.assertIn("evaluation", sources)
        self.assertEqual(dataset.statistics.source_evaluations, 2)

    def test_a_narrow_evaluation_margin_produces_nothing(self) -> None:
        first = trajectory("traj-1", intent="open_application")
        second = trajectory("traj-2", intent="open_calculator")
        evaluations = (evaluation("traj-1", 0.90), evaluation("traj-2", 0.88))

        dataset = self.build(
            trajectories=(first, second),
            evaluations=evaluations,
            rules=PreferenceRules(use_benchmark_expectations=False),
        )

        self.assertEqual(len(dataset), 0)
        self.assertIn("no_evidence_the_rejected_side_is_worse", dataset.statistics.skipped)

    def test_a_benchmark_expectation_pairs_only_where_the_run_disagreed(self) -> None:
        expected = GoldenExample(
            example_id="golden-1",
            request="open calculator",
            expected_tool="desktop.launch",
            expected_arguments={"app": "calculator"},
        )
        disagreed = trajectory("traj-1", tool="browser.open")

        dataset = self.build(
            name="tools",
            dataset_type=PreferenceDatasetType.TOOL_SELECTION,
            trajectories=(disagreed,),
            benchmarks=(expected,),
        )

        self.assertEqual(len(dataset), 1)
        pair = dataset.examples[0]
        self.assertEqual(pair.preference_source, PreferenceSource.BENCHMARK.value)
        self.assertEqual(pair.chosen["tool"], "desktop.launch")
        self.assertEqual(pair.rejected["tool"], "browser.open")
        self.assertIn("golden-1", pair.tags)

    def test_a_run_that_matched_the_expectation_has_no_pair_in_it(self) -> None:
        expected = GoldenExample(
            example_id="golden-1",
            request="open calculator",
            expected_tool="desktop.launch",
            expected_arguments={"app": "calculator"},
        )

        dataset = self.build(
            name="tools",
            dataset_type=PreferenceDatasetType.TOOL_SELECTION,
            trajectories=(trajectory("traj-1", tool="desktop.launch"),),
            benchmarks=(expected,),
        )

        self.assertEqual(len(dataset), 0)
        self.assertIn("identical_candidates", dataset.statistics.skipped)

    def test_a_row_carrying_reasoning_is_refused_before_a_pair_exists(self) -> None:
        winner = trajectory("traj-1", intent="open_application")
        leaky = trajectory(
            "traj-2",
            intent="search_web",
            success=False,
            verified=False,
            metadata={"chain_of_thought": "first I considered the calendar"},
        )

        dataset = self.build(trajectories=(winner, leaky))

        self.assertEqual(len(dataset), 0)
        self.assertEqual(dataset.statistics.skipped["hidden_reasoning"], 1)

    def test_a_hidden_reasoning_key_inside_a_candidate_is_refused_too(self) -> None:
        leaky = trajectory("traj-1", intent="open_application")
        object.__setattr__(  # noqa: SLF001 - the fixture is frozen on purpose
            leaky,
            "structured_intent",
            {"intent": "open_application", "reasoning": "because the user said so"},
        )
        loser = trajectory("traj-2", intent="search_web", success=False, verified=False)

        dataset = self.build(trajectories=(leaky, loser))

        self.assertEqual(len(dataset), 0)

    def test_an_unverified_source_can_be_required_to_stay_out(self) -> None:
        synthetic = raw_pair(
            preference_source=PreferenceSource.SYNTHETIC.value,
            confidence=0.9,
        )

        dataset = self.build(
            name="nlu-pairs",
            dataset_type=PreferenceDatasetType.NLU,
            trajectories=(),
            extra_pairs=(synthetic,),
            rules=PreferenceRules(require_verified_source=True),
        )

        self.assertEqual(len(dataset), 0)
        self.assertEqual(dataset.statistics.skipped.get("unverified_source"), 1)

    def test_teacher_and_synthetic_pairs_are_marked_as_such(self) -> None:
        teacher = PreferenceExample(
            dataset_type=PreferenceDatasetType.RESPONSE.value,
            prompt={"request": "x"},
            chosen={"summary": "the calculator is open"},
            rejected={"summary": "done"},
            chosen_outcome={"success": True},
            rejected_outcome={"success": False},
            preference_source=PreferenceSource.TEACHER_MODEL.value,
            confidence=0.6,
            strength=PreferenceStrength(
                confidence=0.6,
                evidence_quality=0.5,
                evidence=(
                    PreferenceEvidence(
                        kind=EVIDENCE_TEACHER_AGREEMENT, strength=0.5, verified=False
                    ),
                ),
                source=PreferenceSource.TEACHER_MODEL.value,
            ),
            source_evaluation_ids=("ev-teacher",),
        )

        dataset = self.build(
            name="responses",
            dataset_type=PreferenceDatasetType.RESPONSE,
            trajectories=(),
            extra_pairs=(teacher,),
        )

        self.assertEqual(len(dataset), 1)
        stored = dataset.examples[0]
        self.assertEqual(stored.preference_source, PreferenceSource.TEACHER_MODEL.value)
        self.assertEqual(stored.quality_status, PreferenceQualityStatus.NEEDS_REVIEW.value)
        self.assertEqual(len(dataset.accepted_pairs()), 0)

    def test_every_family_builds_its_own_pairs(self) -> None:
        families = {
            PreferenceDatasetType.NLU.value: (
                {"intent": "open_application"},
                {"intent": "search_web"},
            ),
            PreferenceDatasetType.DECISION.value: (
                {"route": "direct_tool", "decision_type": "execute"},
                {"route": "cloud_model", "decision_type": "delegate"},
            ),
            PreferenceDatasetType.TOOL_SELECTION.value: (
                {"tool": "desktop.launch", "arguments": {"app": "calculator"}},
                {"tool": "browser.open"},
            ),
            PreferenceDatasetType.PLANNING.value: (
                {"steps": [{"step_id": "s1", "action": "launch"}]},
                {
                    "steps": [
                        {"step_id": "s1", "action": "search"},
                        {"step_id": "s2", "action": "launch"},
                    ]
                },
            ),
            PreferenceDatasetType.RECOVERY.value: (
                {"strategy": "retry", "outcome": "recovered"},
                {"strategy": "give_up", "outcome": "failed"},
            ),
            PreferenceDatasetType.RESPONSE.value: (
                {"summary": "the calculator is open"},
                {"summary": "(no answer)"},
            ),
        }
        for family, (chosen, rejected) in families.items():
            with self.subTest(family=family):
                built = self.build(
                    name=f"{family}-pairs",
                    dataset_type=family,
                    trajectories=(),
                    extra_pairs=(
                        raw_pair(
                            dataset_type=family,
                            chosen=chosen,
                            rejected=rejected,
                            chosen_outcome={"success": True},
                            rejected_outcome={"success": False},
                        ),
                    ),
                )
                self.assertEqual(len(built), 1, family)
                self.assertEqual(built.dataset_type, family)

    def test_a_build_is_deterministic(self) -> None:
        winner = trajectory("traj-1", intent="open_application")
        loser = trajectory("traj-2", intent="search_web", success=False, verified=False)

        first = self.build(trajectories=(winner, loser))
        second = self.build(trajectories=(winner, loser))

        self.assertNotEqual(first.examples[0].preference_id, second.examples[0].preference_id)
        self.assertEqual(first.fingerprint(), second.fingerprint())
        # Ids are minted per build, so the raw split maps can never be equal; what
        # must be equal is WHICH pair landed in which split.
        self.assertEqual(self.split_content(first), self.split_content(second))

    def test_a_rebuild_with_the_same_content_has_the_same_fingerprint(self) -> None:
        winner = trajectory("traj-1", intent="open_application")
        loser = trajectory("traj-2", intent="search_web", success=False, verified=False)

        first = self.build(trajectories=(winner, loser))
        rebuilt = self.build(trajectories=(winner, loser))
        everyone = [pid for name in PAIR_SPLIT_NAMES for pid in first.splits[name]]
        moved = replace(
            first,
            splits={"train": (), "validation": (), "test": tuple(everyone)},
        )

        self.assertEqual(first.fingerprint(), rebuilt.fingerprint())
        self.assertNotEqual(first.fingerprint(), moved.fingerprint())

    def test_a_version_says_what_made_it(self) -> None:
        winner = trajectory("traj-1", intent="open_application")
        loser = trajectory("traj-2", intent="search_web", success=False, verified=False)

        dataset = self.build(trajectories=(winner, loser))

        self.assertEqual(dataset.dataset_version_id, "nlu-pairs@1.0.0")
        self.assertEqual(dataset.preprocessing_version, PREFERENCE_PREPROCESSING_VERSION)
        self.assertTrue(dataset.source_data_version)
        self.assertIn("quality", dataset.rules)
        self.assertEqual(dataset.statistics.total, len(dataset))
        self.assertEqual(self.builder.validate(dataset), ())

    def test_contradictory_orientations_are_refused_rather_than_kept(self) -> None:
        chosen = {"tool": "desktop.launch"}
        rejected = {"tool": "browser.open"}
        forwards = raw_pair(chosen=chosen, rejected=rejected)
        backwards = raw_pair(chosen=dict(rejected), rejected=dict(chosen))

        dataset = self.build(
            name="tools",
            dataset_type=PreferenceDatasetType.TOOL_SELECTION,
            trajectories=(),
            extra_pairs=(forwards, backwards),
        )

        self.assertEqual(len(dataset), 1)
        self.assertEqual(dataset.statistics.contradictory_removed, 1)

    def test_duplicates_are_counted_not_kept(self) -> None:
        first = raw_pair()
        duplicate = raw_pair(
            prompt=dict(first.prompt),
            chosen=dict(first.chosen),
            rejected=dict(first.rejected),
        )

        dataset = self.build(
            name="tools",
            dataset_type=PreferenceDatasetType.TOOL_SELECTION,
            trajectories=(),
            extra_pairs=(first, duplicate),
        )

        self.assertEqual(len(dataset), 1)
        self.assertEqual(dataset.statistics.duplicates_removed, 1)

    def test_the_pair_limit_is_honoured(self) -> None:
        pairs = tuple(
            raw_pair(
                prompt={"request": f"open {index}"},
                chosen={"tool": "desktop.launch", "arguments": {"app": str(index)}},
            )
            for index in range(10)
        )

        dataset = self.build(
            name="tools",
            dataset_type=PreferenceDatasetType.TOOL_SELECTION,
            trajectories=(),
            extra_pairs=pairs,
            rules=PreferenceRules(max_pairs=3),
        )

        self.assertEqual(len(dataset), 3)

    def test_a_dataset_needs_a_name_and_a_known_family(self) -> None:
        with self.assertRaises(ValueError):
            self.build(name="", trajectories=())
        with self.assertRaises(ValueError):
            self.build(name="with@sign", trajectories=())
        with self.assertRaises(ValueError):
            self.build(name="pairs", dataset_type="poetry", trajectories=())


# ── splits ────────────────────────────────────────────────


def pairs_dataset(
    count: int = 12, *, name: str = "tools"
) -> PreferenceDatasetVersion:
    """A built pair dataset with one group per pair: the evaluator's fixture."""
    builder = PreferenceDatasetBuilder()
    pairs = [
        builder.human_pair(
            PreferenceDatasetType.TOOL_SELECTION.value,
            prompt={"request": f"open app {index}", "task_id": f"task-{index}"},
            chosen={"tool": "desktop.launch", "arguments": {"app": str(index)}},
            rejected={"tool": "browser.open"},
            group_key=f"group-{index}",
        )
        for index in range(count)
    ]
    return builder.build(
        name=name,
        dataset_type=PreferenceDatasetType.TOOL_SELECTION,
        extra_pairs=pairs,
    )


class PreferenceSplitTests(unittest.TestCase):
    """Deterministic, group-safe, leak-free splits, over pairs."""

    def setUp(self) -> None:
        self.builder = PreferenceDatasetBuilder()

    def pairs(self, count: int = 12, *, shared_group: bool = False) -> list[PreferenceExample]:
        return [
            self.builder.human_pair(
                PreferenceDatasetType.TOOL_SELECTION.value,
                prompt={"request": f"open app {index}"},
                chosen={"tool": "desktop.launch", "arguments": {"app": str(index)}},
                rejected={"tool": "browser.open"},
                group_key="one-group" if shared_group else f"group-{index}",
            )
            for index in range(count)
        ]

    def test_every_pair_lands_in_exactly_one_split(self) -> None:
        pairs = self.pairs()

        splits = self.builder.split_pairs(pairs, SplitConfig())

        placed = [preference_id for ids in splits.values() for preference_id in ids]
        self.assertEqual(set(splits), {"train", "validation", "test"})
        self.assertEqual(sorted(placed), sorted(pair.preference_id for pair in pairs))

    def test_the_input_order_does_not_change_the_split(self) -> None:
        pairs = self.pairs()

        forwards = self.builder.split_pairs(pairs, SplitConfig(seed=7))
        backwards = self.builder.split_pairs(list(reversed(pairs)), SplitConfig(seed=7))

        self.assertEqual(forwards, backwards, "the split must not depend on input order")

    def test_a_group_is_never_split_across_two_sets(self) -> None:
        pairs = self.pairs(10, shared_group=True)

        splits = self.builder.split_pairs(pairs, SplitConfig())

        holders = [name for name, ids in splits.items() if ids]
        self.assertEqual(len(holders), 1, "one group leaked into two splits")
        self.assertEqual(len(splits[holders[0]]), 10)

    def test_the_ratios_are_close_to_what_was_asked_for(self) -> None:
        pairs = self.pairs(40)

        splits = self.builder.split_pairs(pairs, SplitConfig())

        train = len(splits["train"]) / len(pairs)
        self.assertGreater(train, 0.65)
        self.assertLess(train, 0.95)
        self.assertGreater(len(splits["validation"]), 0)
        self.assertGreater(len(splits["test"]), 0)

    def test_an_empty_dataset_splits_into_empty_lists(self) -> None:
        self.assertEqual(
            self.builder.split_pairs((), SplitConfig()),
            {"train": (), "validation": (), "test": ()},
        )

    def test_a_held_pair_is_stored_but_not_trainable(self) -> None:
        accepted = raw_pair()
        held = raw_pair(
            prompt={"request": "open browser", "task_id": "task-2"},
            chosen={"tool": "browser.open"},
            rejected={"tool": "desktop.launch"},
        ).with_quality(
            PreferenceQualityStatus.NEEDS_REVIEW.value, reasons=("held for a person",)
        )
        version = PreferenceDatasetVersion(
            dataset_version_id="tools@1.0.0",
            name="tools",
            version="1.0.0",
            dataset_type=PreferenceDatasetType.TOOL_SELECTION.value,
            examples=(accepted, held),
            splits={
                "train": (accepted.preference_id, held.preference_id),
                "validation": (),
                "test": (),
            },
        )

        self.assertEqual(len(version.split("train")), 1)
        self.assertEqual(len(version.split("train", accepted_only=False)), 2)
        self.assertEqual(len(version.accepted_pairs()), 1)
        self.assertEqual(version.split_names(), ("train",))


# ── the review queue ──────────────────────────────────────


class PreferenceReviewTests(unittest.TestCase):
    """A person's answer is evidence, and the queue only moves forward."""

    def setUp(self) -> None:
        self.queue = PreferenceReviewQueue(PreferenceReviewRepository())

    def submit(self, **overrides: Any) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "dataset_type": PreferenceDatasetType.NLU.value,
            "prompt": {"request": "open calculator"},
            "chosen": {"intent": "open_application"},
            "rejected": {"intent": "search_web"},
            "reviewer": "ana",
        }
        fields.update(overrides)
        return self.queue.submit(**fields)

    def queued(self, **overrides: Any) -> str:
        pair = raw_pair(quality={}, review={}, **overrides)
        result = self.queue.enqueue(pair)
        self.assertTrue(result["ok"], result)
        return pair.preference_id

    def test_a_submitted_pair_is_built_like_every_other(self) -> None:
        result = self.submit()

        self.assertTrue(result["ok"], result)
        pair = PreferenceExample.from_dict(result["pair"])
        self.assertEqual(pair.preference_source, PreferenceSource.HUMAN_REVIEW.value)
        self.assertEqual(pair.review["decision"], "choose_a")
        self.assertEqual(pair.review["reviewer"], "ana")
        self.assertTrue(pair.strength.evidence)
        self.assertTrue(pair.strength.verified)
        self.assertTrue(pair.accepted)

    def test_enqueueing_a_pair_marks_it_as_waiting_on_a_person(self) -> None:
        preference_id = self.queued()

        item = self.queue.item(preference_id)

        assert item is not None
        self.assertFalse(item.decided)
        self.assertEqual(item.quality["status"], PreferenceQualityStatus.NEEDS_REVIEW.value)
        self.assertEqual(item.candidate_a["tool"], "desktop.launch")
        self.assertEqual(self.queue.pending_count(), 1)
        self.assertEqual(self.queue.resolved_pairs(), ())

    def test_choosing_b_swaps_the_candidates_rather_than_forking_the_pair(self) -> None:
        preference_id = self.queued()

        result = self.queue.decide(preference_id, "choose_b", reviewer="bo")

        self.assertTrue(result["ok"], result)
        pair = PreferenceExample.from_dict(result["pair"])
        self.assertEqual(pair.chosen["tool"], "browser.open")
        self.assertEqual(pair.rejected["tool"], "desktop.launch")
        self.assertEqual(pair.review["reviewer"], "bo")
        self.assertEqual(pair.quality_status, PreferenceQualityStatus.ACCEPTED.value)
        self.assertEqual(self.queue.pending_count(), 0)
        self.assertEqual(
            [item.preference_id for item in self.queue.resolved_pairs()],
            [preference_id],
        )

    def test_a_tie_needs_a_reason_and_settles_nothing(self) -> None:
        preference_id = self.queued()

        refused = self.queue.decide(preference_id, "tie")
        accepted = self.queue.decide(preference_id, "tie", reason="both work equally well")

        self.assertFalse(refused["ok"])
        self.assertIn("needs a reason", refused["reason"])
        self.assertTrue(accepted["ok"])
        pair = PreferenceExample.from_dict(accepted["pair"])
        self.assertEqual(pair.quality_status, PreferenceQualityStatus.REJECTED.value)

    def test_a_rejection_needs_a_reason_too(self) -> None:
        preference_id = self.queued()

        refused = self.queue.decide(preference_id, "reject")

        self.assertFalse(refused["ok"])
        self.assertIn("needs a reason", refused["reason"])
        self.assertEqual(self.queue.pending_count(), 1)

    def test_an_unknown_decision_or_pair_is_refused_with_the_options(self) -> None:
        preference_id = self.queued()

        unknown = self.queue.decide(preference_id, "maybe")
        missing = self.queue.decide("nope", "choose_a")

        self.assertFalse(unknown["ok"])
        self.assertIn("choose_a", unknown["reason"])
        self.assertFalse(missing["ok"])
        self.assertIn("no queued pair", missing["reason"])

    def test_the_stats_name_who_decided_and_what_is_left(self) -> None:
        first = self.queued()
        second = self.queued()
        self.queue.decide(first, "choose_a", reviewer="bo")

        stats = self.queue.stats()

        self.assertEqual(stats["queued"], 2)
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["decided"], 1)
        self.assertEqual(stats["by_reviewer"], {"bo": 1})
        self.assertEqual(stats["decisions"], ["choose_a", "choose_b", "tie", "reject"])
        self.assertIsNotNone(self.queue.item(second))

    def test_a_pair_carrying_hidden_reasoning_is_never_queued(self) -> None:
        leaky = raw_pair(
            prompt={"request": "open calculator", "reasoning": "step by step"}
        )

        refused = self.queue.enqueue(leaky)

        self.assertFalse(refused["ok"])
        self.assertIn("hidden reasoning", refused["reason"])
        self.assertEqual(self.queue.pending_count(), 0)
        self.assertEqual(self.queue.failures, 1)

    def test_a_sensitive_value_is_refused_rather_than_stored(self) -> None:
        result = self.submit(chosen={"intent": SECRET})

        self.assertFalse(result["ok"])
        self.assertIn("rejected", result["reason"])

    def test_a_secret_is_still_redacted_when_the_policy_holds_rather_than_rejects(self) -> None:
        leaky = raw_pair(chosen={"tool": SECRET, "arguments": {}}, rejected={"tool": "b"})
        filter_ = PreferenceQualityFilter(
            PreferenceQualityConfig(residual_sensitive_rejects=False)
        )

        cleaned, verdict = filter_.apply(leaky)

        assert cleaned is not None
        payload = json.dumps(cleaned.to_dict())
        self.assertNotIn(SECRET, payload)
        self.assertIn(REDACTED, payload)
        self.assertIn("sensitive_data", [issue.code for issue in verdict.issues])


# ── the backends ────────────────────────────────────────


class PreferenceTrainerTests(unittest.TestCase):
    """One pipeline, two objectives, and a dry run that trains nothing."""

    def setUp(self) -> None:
        self.dataset = pairs_dataset()
        self.config = config_for(self.dataset.dataset_version_id)

    def test_each_algorithm_declares_its_objective(self) -> None:
        dpo = DPOTrainer(config_for("p@1.0.0", algorithm="dpo", dry_run=False))
        orpo = ORPOTrainer(config_for("p@1.0.0", algorithm="orpo", dry_run=False))

        self.assertEqual(dpo.name, "dpo")
        self.assertEqual(dpo.backend, "dpo")
        self.assertTrue(dpo.objective()["needs_reference_model"])
        self.assertFalse(dpo.objective()["simulated"], "a real backend is not simulated")
        self.assertEqual(orpo.name, "orpo")
        self.assertFalse(orpo.objective()["needs_reference_model"])
        self.assertIn("odds ratio", orpo.objective()["objective"].lower())

    def test_the_factory_picks_the_backend_the_configuration_asks_for(self) -> None:
        self.assertIsInstance(preference_trainer_for(self.config), DryRunPreferenceTrainer)
        self.assertIsInstance(
            preference_trainer_for(config_for("p@1.0.0", dry_run=False, algorithm="dpo")),
            DPOTrainer,
        )
        self.assertIsInstance(
            preference_trainer_for(config_for("p@1.0.0", dry_run=False, algorithm="orpo")),
            ORPOTrainer,
        )
        self.assertEqual(self.config.algorithm, "dpo")

    def test_preparing_a_dataset_reports_the_real_schedule(self) -> None:
        summary = DryRunPreferenceTrainer(self.config).prepare_dataset(self.dataset)

        self.assertEqual(summary["dataset_version"], self.dataset.dataset_version_id)
        self.assertEqual(summary["pairs"], len(self.dataset.accepted_pairs()))
        self.assertGreaterEqual(summary["train_pairs"], 1)
        self.assertEqual(
            summary["train_pairs"]
            + summary["validation_pairs"]
            + summary["test_pairs"],
            len(self.dataset),
        )
        self.assertGreaterEqual(summary["total_steps"], 1)
        self.assertEqual(summary["needs_reference_model"], self.config.needs_reference_model)
        self.assertEqual(summary["lora"]["rank"], self.config.lora_rank)

    def test_a_dry_run_walks_the_schedule_and_labels_every_figure_simulated(self) -> None:
        trainer = DryRunPreferenceTrainer(self.config)

        finished = trainer.start_training(
            run_for(self.config, self.dataset), self.dataset, TrainingCallbacks()
        )

        self.assertEqual(finished.status, TrainingRunStatus.COMPLETED.value)
        self.assertEqual(len(finished.loss_history), finished.total_steps)
        self.assertIsNotNone(finished.training_loss)
        self.assertTrue(finished.preference_metrics["simulated"])
        self.assertEqual(finished.preference_metrics["objective"], "dpo")
        self.assertIn("margin", finished.preference_metrics)
        self.assertIn("not a measurement", finished.preference_metrics["note"])

    def test_the_same_seed_walks_the_same_curve(self) -> None:
        first = DryRunPreferenceTrainer(self.config).start_training(
            run_for(self.config, self.dataset), self.dataset, TrainingCallbacks()
        )
        second = DryRunPreferenceTrainer(self.config).start_training(
            run_for(self.config, self.dataset), self.dataset, TrainingCallbacks()
        )

        self.assertEqual(
            [point["loss"] for point in first.loss_history],
            [point["loss"] for point in second.loss_history],
        )

    def test_a_paused_run_stops_and_a_resume_continues_from_its_step(self) -> None:
        config = config_for(self.dataset.dataset_version_id, epochs=2)
        seen: list[int] = []
        paused = DryRunPreferenceTrainer(config).start_training(
            run_for(config, self.dataset),
            self.dataset,
            TrainingCallbacks(
                on_step=lambda updated: seen.append(updated.current_step),
                is_paused=lambda: len(seen) >= 2,
            ),
        )
        resumed = DryRunPreferenceTrainer(config).resume_training(
            run_for(config, self.dataset), self.dataset, 1, TrainingCallbacks()
        )

        self.assertEqual(paused.status, TrainingRunStatus.PAUSED.value)
        self.assertEqual(resumed.status, TrainingRunStatus.COMPLETED.value)
        self.assertGreaterEqual(resumed.current_step, 1)
        self.assertTrue(resumed.loss_history)

    def test_a_cancelled_run_stops_at_the_next_step_and_says_so(self) -> None:
        seen: list[int] = []

        finished = DryRunPreferenceTrainer(self.config).start_training(
            run_for(self.config, self.dataset),
            self.dataset,
            TrainingCallbacks(
                on_step=lambda updated: seen.append(updated.current_step),
                is_cancelled=lambda: len(seen) >= 1,
            ),
        )

        self.assertEqual(finished.status, TrainingRunStatus.CANCELLED.value)
        self.assertEqual(len(seen), 1)

    def test_a_supervised_dataset_is_refused_by_name(self) -> None:
        supervised = self.dataset.as_sft_dataset()

        with self.assertRaises(TrainingBackendUnavailable) as caught:
            DryRunPreferenceTrainer(self.config).start_training(
                run_for(self.config, self.dataset), supervised, TrainingCallbacks()
            )

        self.assertIn("supervised dataset", str(caught.exception))

    def test_a_real_backend_names_its_missing_dependencies(self) -> None:
        real = DPOTrainer(
            config_for("p@1.0.0", dry_run=False), capabilities=bare()
        )

        self.assertFalse(real.is_available())
        missing = real.missing_dependencies()
        self.assertIn("torch", missing)
        self.assertIn("transformers", missing)
        with self.assertRaises(TrainingBackendUnavailable) as caught:
            real.start_training(
                run_for(self.config, self.dataset), self.dataset, TrainingCallbacks()
            )
        self.assertIn("torch", str(caught.exception))
        self.assertIn("Dry-run", str(caught.exception))

    def test_with_the_extras_but_no_runner_the_boundary_says_so(self) -> None:
        real = DPOTrainer(config_for("p@1.0.0", dry_run=False), capabilities=ready())

        self.assertTrue(real.is_available(), real.missing_dependencies())
        with self.assertRaises(TrainingBackendUnavailable) as caught:
            real.start_training(
                run_for(self.config, self.dataset), self.dataset, TrainingCallbacks()
            )

        self.assertIn("no training runner is wired", str(caught.exception))

    def test_an_injected_runner_is_used_and_its_result_is_kept(self) -> None:
        seen: list[str] = []

        def runner(run: TrainingRun, dataset: Any, callbacks: TrainingCallbacks) -> TrainingRun:
            seen.append(dataset.dataset_version_id)
            return run.with_status(TrainingRunStatus.COMPLETED, training_loss=0.125)

        real = DPOTrainer(
            config_for("p@1.0.0", dry_run=False),
            capabilities=ready(),
            runner=runner,
        )

        finished = real.start_training(
            run_for(self.config, self.dataset), self.dataset, TrainingCallbacks()
        )

        self.assertEqual(seen, [self.dataset.dataset_version_id])
        self.assertEqual(finished.status, TrainingRunStatus.COMPLETED.value)
        self.assertEqual(finished.training_loss, 0.125)
        self.assertFalse(finished.preference_metrics["simulated"])

    def test_a_runner_that_returns_nothing_still_produces_a_finished_run(self) -> None:
        class SilentRunner:
            def __call__(self, run: TrainingRun, dataset: Any, callbacks: TrainingCallbacks) -> Any:
                return None

        real = DPOTrainer(
            config_for("p@1.0.0", dry_run=False),
            capabilities=ready(),
            runner=SilentRunner(),
        )

        finished = real.start_training(
            run_for(self.config, self.dataset), self.dataset, TrainingCallbacks()
        )

        self.assertEqual(finished.status, TrainingRunStatus.COMPLETED.value)

    def test_the_backends_never_import_torch_at_module_level(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "novacontrol"
            / "preference"
            / "backends.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("\nimport torch", source)
        self.assertNotIn("\nimport transformers", source)
        self.assertNotIn("\nimport peft", source)


# ── resources ────────────────────────────────────────────


class PreferenceResourceTests(unittest.TestCase):
    """DPO's reference model is counted; ORPO's is not; CUDA is never assumed."""

    def setUp(self) -> None:
        self.estimator = PreferenceResourceEstimator().with_capabilities(ready())

    def test_dpo_counts_a_second_copy_for_the_reference_model(self) -> None:
        config = config_for(
            "p@1.0.0",
            algorithm="dpo",
            dry_run=False,
            base_model_size_bytes=4_000_000_000,
        )

        estimate = self.estimator.estimate(config)

        self.assertEqual(
            estimate.to_dict()["components"].get("reference_model"), 4_000_000_000
        )
        self.assertIn("reference", " ".join(estimate.reasons).lower())

    def test_orpo_counts_no_reference_model(self) -> None:
        config = config_for(
            "p@1.0.0",
            algorithm="orpo",
            dry_run=False,
            base_model_size_bytes=4_000_000_000,
        )

        estimate = self.estimator.estimate(config)

        self.assertNotIn("reference_model", estimate.to_dict()["components"])
        self.assertIn("keeps no reference model", " ".join(estimate.reasons))

    def test_pairs_are_priced_at_twice_their_tokens(self) -> None:
        dataset = pairs_dataset()

        estimate = self.estimator.estimate(config_for("p@1.0.0"), dataset=dataset)

        components = estimate.to_dict()["components"]
        tokens = dataset.statistics.estimated_tokens
        self.assertEqual(components["preference_pairs"], tokens * PREFERENCE_PAIR_BYTES_PER_TOKEN)
        self.assertEqual(estimate.example_count, len(dataset))

    def test_an_unknown_model_size_is_a_lower_bound_not_a_guess(self) -> None:
        config = config_for("p@1.0.0", algorithm="dpo", dry_run=False)

        estimate = self.estimator.estimate(config)

        self.assertNotIn("reference_model", estimate.to_dict()["components"])
        self.assertIn("lower bound", " ".join(estimate.reasons))

    def test_a_cpu_only_machine_is_not_an_error(self) -> None:
        cpu = HardwareCapabilities(
            cpu_count=8,
            total_ram_bytes=32_000_000_000,
            available_ram_bytes=16_000_000_000,
            backends=("cpu",),
        )
        estimator = PreferenceResourceEstimator().with_capabilities(cpu)

        config = config_for("p@1.0.0", hardware_policy="local_cpu", dry_run=False)

        estimate = estimator.estimate(config)

        self.assertEqual(estimate.policy, HardwarePolicy.LOCAL_CPU.value)
        self.assertIn(estimate.level, {member.value for member in ResourceVerdict})
        self.assertFalse(estimate.dry_run)
        self.assertIn("cpu", " ".join(str(value) for value in estimate.hardware.values()).lower())

    def test_the_summary_reports_the_machine_and_both_objectives(self) -> None:
        summary = self.estimator.summary(config_for("p@1.0.0"))

        self.assertIn("hardware", summary)
        self.assertIn("device", summary)
        self.assertTrue(summary["algorithms"]["dpo"]["needs_reference_model"])
        self.assertFalse(summary["algorithms"]["orpo"]["needs_reference_model"])
        self.assertIn("estimate", summary)

    def test_the_components_helper_reads_a_stored_estimate(self) -> None:
        estimate = self.estimator.estimate(config_for("p@1.0.0"))

        components = preference_components(estimate.to_dict())

        self.assertIn("preference_pairs", components)
        self.assertTrue(all(value >= 0 for value in components.values()))
        self.assertEqual(preference_components({}), {})


# ── the evaluator ────────────────────────────────────────


class PreferenceEvaluationTests(unittest.TestCase):
    """Base versus SFT versus candidate, on pairs the models have never seen."""

    def setUp(self) -> None:
        self.dataset = pairs_dataset()
        self.evaluator = PreferenceEvaluator()

    def test_preference_accuracy_reads_both_sides_of_every_pair(self) -> None:
        good = self.evaluator.read(self.dataset, ChoicePredictor("cand"))
        bad = self.evaluator.read(self.dataset, ChoicePredictor("base", prefers="rejected"))

        self.assertEqual(good.model, "cand")
        self.assertEqual(good.pairs, len(self.dataset.split("test")))
        self.assertEqual(good.preference_accuracy, 1.0)
        self.assertEqual(good.preference_gap, round(good.chosen_match_rate - good.rejected_match_rate, 6))
        self.assertEqual(bad.preference_accuracy, 0.0)
        self.assertGreater(good.chosen_match_rate, bad.chosen_match_rate)

    def test_a_silent_model_has_zero_coverage(self) -> None:
        reading = self.evaluator.read(self.dataset, SilentPredictor())

        self.assertEqual(reading.usable, 0)
        self.assertEqual(reading.failed, reading.pairs)
        self.assertEqual(reading.coverage, 0.0)
        self.assertEqual(reading.preference_accuracy, 0.0)

    def test_a_candidate_that_prefers_what_the_evidence_prefers_passes(self) -> None:
        comparison = self.evaluator.compare(
            self.dataset,
            ChoicePredictor("base", prefers="rejected"),
            ChoicePredictor("candidate"),
            run_id="run-1",
            model_id="m1",
            algorithm="dpo",
        )

        self.assertEqual(comparison.verdict, "pass")
        self.assertTrue(comparison.regressions.passed)
        self.assertGreater(comparison.regressions.preference_gain, 0)
        self.assertFalse(comparison.loss_consulted)
        self.assertEqual(comparison.evaluation.details["algorithm"], "dpo")
        self.assertFalse(comparison.evaluation.details["loss_consulted"])
        self.assertEqual(comparison.readings()["candidate"].preference_accuracy, 1.0)

    def test_a_candidate_that_prefers_the_rejected_side_regresses(self) -> None:
        comparison = self.evaluator.compare(
            self.dataset,
            ChoicePredictor("base"),
            ChoicePredictor("candidate", prefers="rejected"),
            run_id="run-1",
            model_id="m1",
        )

        self.assertEqual(comparison.verdict, "regress")
        self.assertTrue(comparison.regressions.blocks_approval)
        self.assertIn("preference_accuracy", comparison.evaluation.regressions)
        self.assertIn("tool_selection", comparison.regressions.areas_regressed)
        self.assertIn("preference", comparison.regressions.areas_regressed)

    def test_two_silent_models_are_inconclusive_rather_than_a_pass(self) -> None:
        comparison = self.evaluator.compare(
            self.dataset, SilentPredictor(), SilentPredictor(), run_id="run-1", model_id="m1"
        )

        self.assertEqual(comparison.verdict, "inconclusive")
        self.assertFalse(comparison.regressions.blocks_approval)
        self.assertIn("nothing to compare", comparison.evaluation.reason)

    def test_the_sft_stage_is_reported_when_a_third_model_is_supplied(self) -> None:
        comparison = self.evaluator.compare(
            self.dataset,
            ChoicePredictor("base", prefers="rejected"),
            ChoicePredictor("candidate"),
            sft=ChoicePredictor("sft"),
            run_id="run-1",
            model_id="m1",
        )

        self.assertIn("sft", comparison.readings())
        self.assertIsNotNone(comparison.base_to_sft)
        self.assertIsNotNone(comparison.sft_to_candidate)
        self.assertNotIn(
            "no SFT model was supplied", " ".join(comparison.notes)
        )

    def test_checking_regressions_names_the_blocking_areas(self) -> None:
        report = check_regressions(
            {"task_success": 0.9, "preference_accuracy": 0.5},
            {"task_success": 0.4, "preference_accuracy": 0.9},
            required=("task_success",),
        )

        self.assertEqual(report.verdict, "regress")
        self.assertTrue(report.blocks_approval)
        self.assertIn("task_success", report.blocking)
        self.assertIn("task_success", report.regressed)
        self.assertTrue(report.notes, "an improved metric next to a blocking one is noted")

    def test_a_comparison_with_nothing_to_compare_is_inconclusive(self) -> None:
        report = check_regressions({}, {})

        self.assertEqual(report.verdict, "inconclusive")
        self.assertFalse(report.blocks_approval)
        self.assertIn("not measured", report.reason)


# ── the manager ──────────────────────────────────────────


class PreferenceManagerTests(unittest.TestCase):
    """Datasets, runs, checkpoints, reviews, evaluation and the registry, wired."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.publisher = RecordingPublisher()
        self.manager = dry_run_manager(self._tmp.name, publish=self.publisher)

    def dataset(self, **overrides: Any) -> PreferenceDatasetVersion:
        return build_dataset(self.manager, **overrides)

    def started(self, dataset: PreferenceDatasetVersion, **config: Any) -> tuple[TrainingRun, dict[str, Any]]:
        run = self.manager.create_run(
            "tiny-model", dataset.dataset_version_id, config=config or None
        )
        result = self.manager.start(run.run_id)
        self.assertTrue(result["ok"], result)
        return run, result

    def test_a_dataset_is_built_stored_and_announced(self) -> None:
        dataset = self.dataset()

        self.assertEqual(dataset.dataset_version_id, "pairs@1.0.0")
        self.assertEqual(len(self.manager.preference_datasets()), 1)
        self.assertIsNotNone(self.manager.pair_dataset("pairs@1.0.0"))
        self.assertIn(PREFERENCE_DATASET_BUILT, self.publisher.types())
        report = self.manager.validate_dataset(dataset.dataset_version_id)
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual(report["accepted"], 12)

    def test_a_second_build_is_a_new_version(self) -> None:
        first = build_dataset(self.manager, version="")
        second = build_dataset(self.manager, version="")

        self.assertEqual(first.version, "1.0.0")
        self.assertEqual(second.version, "1.0.1")
        self.assertEqual(len(self.manager.preference_datasets()), 2)

    def test_a_preference_run_stores_its_algorithm_and_configuration(self) -> None:
        dataset = self.dataset()

        run = self.manager.create_run(
            "tiny-model", dataset.dataset_version_id, config={"algorithm": "orpo"}
        )

        self.assertEqual(run.algorithm, "orpo")
        self.assertEqual(run.dataset_type, PreferenceDatasetType.TOOL_SELECTION.value)
        self.assertEqual(run.training_config["algorithm"], "orpo")
        self.assertEqual(run.training_config["dataset_version"], dataset.dataset_version_id)
        self.assertEqual(run.backend, "dry_run")
        self.assertEqual(
            [item.run_id for item in self.manager.preference_runs(algorithm="orpo")],
            [run.run_id],
        )
        self.assertEqual(self.manager.preference_runs(algorithm="dpo"), ())

    def test_a_run_cannot_be_created_without_pairs(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.manager.create_run("tiny-model", "nope@1.0.0")

        self.assertIn("no preference dataset version", str(caught.exception))

    def test_a_dry_run_completes_and_registers_a_model_with_its_algorithm(self) -> None:
        dataset = self.dataset()

        run, result = self.started(dataset, algorithm="orpo")

        self.assertEqual(result["run"]["status"], TrainingRunStatus.COMPLETED.value)
        self.assertTrue(result["evaluation_required"])
        self.assertEqual(result["model"]["algorithm"], "orpo")
        self.assertTrue(result["model"]["adapter"]["dry_run"])
        checkpoints = self.manager.checkpoints(run.run_id)
        self.assertTrue(checkpoints)
        self.assertIn("best", {item["kind"] for item in checkpoints})
        for row in checkpoints:
            self.assertEqual(row["status"], CHECKPOINT_COMPLETE)
        found = self.manager.run(run.run_id)
        assert found is not None
        self.assertTrue(found.preference_metrics["simulated"])

    def test_a_preference_loss_is_not_an_evaluation(self) -> None:
        dataset = self.dataset()
        run, _ = self.started(dataset)

        skipped = self.manager.evaluate_run(run.run_id)

        self.assertFalse(skipped["ok"])
        self.assertTrue(skipped["skipped"])
        self.assertIn("preference loss is not an evaluation", skipped["evaluation"]["reason"])
        stored = self.manager.evaluations_list(limit=1)
        self.assertEqual(stored[0].verdict, "skipped")
        self.assertFalse(stored[0].details["loss_consulted"])

    def test_a_model_is_never_approved_on_its_preference_loss(self) -> None:
        dataset = self.dataset()
        run, _ = self.started(dataset)
        model_id = self.manager.models_list()[0].model_id

        refused = self.manager.approve_model(model_id)

        self.assertFalse(refused["ok"])
        self.assertIn("without a passing evaluation", refused["reason"])

    def test_a_measured_comparison_approves_and_promotes_explicitly(self) -> None:
        dataset = self.dataset()
        run, _ = self.started(dataset)
        model_id = self.manager.models_list()[0].model_id

        evaluated = self.manager.evaluate_run(
            run.run_id,
            base=ChoicePredictor("base", prefers="rejected"),
            candidate=ChoicePredictor("candidate"),
        )

        self.assertTrue(evaluated["ok"], evaluated)
        self.assertEqual(evaluated["evaluation"]["verdict"], "pass")
        self.assertFalse(evaluated["evaluation"]["details"]["loss_consulted"])
        approved = self.manager.approve_model(model_id, note="verified on held-out pairs")
        self.assertTrue(approved["ok"], approved)
        promoted = self.manager.promote_model(model_id, note="probe")
        self.assertTrue(promoted["ok"], promoted)
        self.assertEqual(self.manager.model(model_id).status, ModelStatus.PRODUCTION.value)  # type: ignore[union-attr]
        rolled = self.manager.rollback_model(model_id, reason="probe")
        self.assertFalse(rolled["ok"], "there was no previous production model to roll back to")
        self.assertIn("no previous production model", rolled["reason"])

    def test_a_regressing_candidate_is_rejected_by_the_comparison(self) -> None:
        dataset = self.dataset()
        run, _ = self.started(dataset)
        model_id = self.manager.models_list()[0].model_id

        evaluated = self.manager.evaluate_run(
            run.run_id,
            base=ChoicePredictor("base"),
            candidate=ChoicePredictor("candidate", prefers="rejected"),
        )

        self.assertEqual(evaluated["evaluation"]["verdict"], "regress")
        self.assertEqual(evaluated["model"]["status"], ModelStatus.REJECTED.value)
        self.assertEqual(self.manager.model(model_id).status, ModelStatus.REJECTED.value)  # type: ignore[union-attr]

    def test_a_comparison_can_be_run_without_a_run_and_is_stored(self) -> None:
        dataset = self.dataset()

        result = self.manager.compare_models(
            dataset.dataset_version_id,
            base=ChoicePredictor("base", prefers="rejected"),
            candidate=ChoicePredictor("candidate"),
            algorithm="dpo",
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["comparison"]["verdict"], "pass")
        self.assertEqual(len(self.manager.evaluations_list()), 1)
        self.assertIn(PREFERENCE_COMPARISON, self.publisher.types())

    def test_a_submitted_preference_becomes_a_dataset_pair(self) -> None:
        submitted = self.manager.submit_pair(
            dataset_type=PreferenceDatasetType.NLU.value,
            prompt={"request": "open calculator"},
            chosen={"intent": "open_application"},
            rejected={"intent": "search_web"},
            reviewer="ana",
        )
        self.assertTrue(submitted["ok"], submitted)

        dataset = self.manager.build_dataset(
            "nlu-pairs",
            PreferenceDatasetType.NLU.value,
            trajectories=(),
            queue_for_review=False,
        )

        self.assertEqual(len(dataset), 1)
        self.assertEqual(dataset.examples[0].preference_source, PreferenceSource.HUMAN_REVIEW.value)

    def test_held_pairs_wait_for_a_person_and_never_train(self) -> None:
        dataset = self.dataset()

        # human pairs are already settled, so nothing is queued by this build
        self.assertEqual(self.manager.review_stats()["pending"], 0)

        enqueued = self.manager.reviews.enqueue(raw_pair(quality={}, review={}))
        self.assertTrue(enqueued["ok"], enqueued)
        pending = self.manager.pending_reviews()
        self.assertEqual(len(pending), 1)

        decided = self.manager.decide_review(
            pending[0].preference_id, "choose_b", reviewer="bo"
        )
        self.assertTrue(decided["ok"], decided)
        self.assertIn(PREFERENCE_REVIEW_DECIDED, self.publisher.types())
        held = self.manager.held_pairs(dataset.dataset_version_id)
        self.assertEqual(held, ())

    def test_a_secret_in_a_submitted_pair_is_refused(self) -> None:
        refused = self.manager.submit_pair(
            dataset_type=PreferenceDatasetType.NLU.value,
            prompt={"request": "open calculator"},
            chosen={"intent": SECRET},
            rejected={"intent": "search_web"},
        )

        self.assertFalse(refused["ok"])
        self.assertIn("rejected", refused["reason"])

    def test_a_dry_run_previews_the_run_without_starting_it(self) -> None:
        dataset = self.dataset()

        preview = self.manager.dry_run("tiny-model", dataset.dataset_version_id, algorithm="orpo")

        self.assertTrue(preview["ok"], preview)
        self.assertFalse(preview["started"])
        self.assertEqual(preview["algorithm"], "orpo")
        self.assertFalse(preview["needs_reference_model"])
        self.assertEqual(preview["prepared"]["pairs"], 12)
        self.assertIn("estimate", preview)
        self.assertIn(PREFERENCE_DRY_RUN, self.publisher.types())
        self.assertEqual(self.manager.preference_runs(), ())

    def test_the_status_and_summary_cover_the_subsystem(self) -> None:
        dataset = self.dataset()
        self.started(dataset)

        status = self.manager.status()
        summary = self.manager.summary()

        self.assertEqual(status["datasets"]["count"], 1)
        self.assertEqual(status["runs"]["by_algorithm"], {"dpo": 1, "orpo": 0})
        self.assertIn("reviews", status)
        self.assertEqual(status["defaults"]["algorithm"], "dpo")
        self.assertTrue(summary["runs"])
        self.assertEqual(summary["runs"][0]["algorithm"], "dpo")
        self.assertEqual(summary["datasets"], ["pairs@1.0.0"])

    def test_an_estimate_prices_an_adhoc_configuration(self) -> None:
        payload = self.manager.estimate_config(
            {"base_model": "tiny-model", "algorithm": "dpo", "preference_dataset_version": "pairs@1.0.0"}
        )

        self.assertTrue(payload["valid"], payload["errors"])
        self.assertTrue(payload["needs_reference_model"])
        self.assertIn("estimate", payload)

    def test_everything_reads_back_from_disk(self) -> None:
        dataset = self.dataset()
        run, _ = self.started(dataset)

        restored = make_manager(self._tmp.name)

        self.assertIsNotNone(restored.pair_dataset(dataset.dataset_version_id))
        found = restored.run(run.run_id)
        assert found is not None
        self.assertEqual(found.algorithm, "dpo")
        self.assertEqual(restored.models_list()[0].algorithm, "dpo")
        self.assertTrue(restored.checkpoints(run.run_id))


# ── the runtime module ────────────────────────────────────


class PreferenceModuleTests(unittest.IsolatedAsyncioTestCase):
    """The module answers questions on the bus and starts nothing there."""

    async def asyncSetUp(self) -> None:
        from novacontrol.core.events import EventBus

        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.manager = dry_run_manager(self._tmp.name)
        self.module = PreferenceModule(self.manager)
        self.bus = EventBus()
        await self.module.start(self.bus)
        self.addCleanup(self._stop)

    async def _stop(self) -> None:
        await self.module.stop()

    async def replies(self, request: str, payload: dict[str, Any], completed: str) -> list[dict[str, Any]]:
        from novacontrol.core.events import Event

        found: list[dict[str, Any]] = []

        async def handler(event: Event) -> None:
            found.append(dict(event.payload))

        await self.bus.subscribe(completed, handler)
        await self.bus.publish(Event(type=request, payload=payload, source="test"))
        await asyncio.sleep(0)
        return found

    def test_the_module_names_itself_and_declares_what_it_answers(self) -> None:
        self.assertEqual(self.module.name, "preference")
        names = {capability.name for capability in self.module.capabilities}
        self.assertEqual(
            names,
            {
                "preference.status",
                "preference.datasets",
                "preference.reviews",
                "preference.algorithms",
            },
            "no capability starts a run or decides a review",
        )

    async def test_a_status_request_is_answered(self) -> None:
        replies = await self.replies(
            "preference.status_requested", {}, "preference.status_completed"
        )

        self.assertTrue(replies)
        self.assertIn("defaults", replies[0])
        self.assertEqual(replies[0]["defaults"]["algorithm"], "dpo")

    async def test_a_dataset_request_is_answered_with_the_stored_versions(self) -> None:
        build_dataset(self.manager)

        replies = await self.replies(
            "preference.datasets_requested",
            {"limit": 5},
            "preference.datasets_completed",
        )

        self.assertEqual(
            [item["dataset_version_id"] for item in replies[0]["datasets"]],
            ["pairs@1.0.0"],
        )

    async def test_a_review_request_is_answered_with_the_queue_and_its_stats(self) -> None:
        self.manager.reviews.enqueue(raw_pair(quality={}, review={}))

        replies = await self.replies(
            "preference.reviews_requested",
            {"pending_only": True},
            "preference.reviews_completed",
        )

        self.assertEqual(len(replies[0]["reviews"]), 1)
        self.assertEqual(replies[0]["stats"]["pending"], 1)

    async def test_an_algorithms_request_describes_both_objectives(self) -> None:
        replies = await self.replies(
            "preference.algorithms_requested", {}, "preference.algorithms_completed"
        )

        self.assertTrue(replies[0]["dry_run_supported"])
        self.assertTrue(replies[0]["algorithms"]["dpo"]["needs_reference_model"])
        self.assertFalse(replies[0]["algorithms"]["orpo"]["needs_reference_model"])

    async def test_an_estimate_request_is_answered_without_creating_a_run(self) -> None:
        replies = await self.replies(
            "preference.estimate_requested",
            {"config": {"base_model": "tiny-model", "algorithm": "orpo"}},
            "preference.estimate_completed",
        )

        self.assertTrue(replies[0]["valid"], replies[0]["errors"])
        self.assertFalse(replies[0]["needs_reference_model"])
        self.assertEqual(self.manager.preference_runs(), ())


# ── the application ──────────────────────────────────────


class ApplicationPreferenceTests(unittest.IsolatedAsyncioTestCase):
    """The live application: wired, dry by default, and nothing trains by itself."""

    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.app = NovaControlApplication(data_dir=self._tmp.name)
        await self.app.start()
        self.addCleanup(self._stop)

    async def _stop(self) -> None:
        await self.app.stop()

    def submitted_pair(self, index: int = 0) -> dict[str, Any]:
        return self.app.submit_preference_pair(
            dataset_type=PreferenceDatasetType.TOOL_SELECTION.value,
            prompt={"request": f"open app {index}", "group_key": f"group-{index}"},
            chosen={"tool": "desktop.launch", "arguments": {"app": str(index)}},
            rejected={"tool": "browser.open"},
            reviewer="ana",
            confidence=0.9,
        )

    def built_dataset(self, count: int = 4) -> str:
        for index in range(count):
            self.assertTrue(self.submitted_pair(index)["ok"])
        created = self.app.create_preference_dataset("tools", "tool_selection")
        self.assertTrue(created["ok"], created.get("reason"))
        return created["dataset"]["dataset_version_id"]

    def test_the_subsystem_is_wired_and_dry_by_default(self) -> None:
        status = self.app.preference_status()

        self.assertTrue(status["enabled"])
        self.assertTrue(status["defaults"]["dry_run"])
        self.assertEqual(status["defaults"]["algorithm"], "dpo")
        self.assertEqual(status["datasets"]["count"], 0)
        self.assertEqual(status["runs"]["count"], 0)
        self.assertIn("algorithms", self.app.preference_algorithms())

    def test_a_submitted_pair_becomes_a_dataset_and_a_run(self) -> None:
        dataset_version = self.built_dataset()

        listed = self.app.preference_datasets()
        validated = self.app.validate_preference_dataset(dataset_version)
        pair_id = self.app.preference_dataset(dataset_version)["examples"][0]["preference_id"]
        pair = self.app.preference_pair(dataset_version, pair_id)

        self.assertEqual(listed["count"], 1)
        self.assertTrue(validated["ok"], validated["issues"])
        self.assertTrue(pair["ok"])
        self.assertEqual(pair["pair"]["preference_source"], "human_review")
        with self.assertRaises(KeyError):
            self.app.preference_dataset("nope@1.0.0")

        created = self.app.create_preference_run("tiny-model", dataset_version)
        self.assertTrue(created["ok"], created.get("reason"))
        self.assertEqual(created["run"]["algorithm"], "dpo")

    async def test_a_dry_run_trains_through_the_application(self) -> None:
        dataset_version = self.built_dataset()
        created = self.app.create_preference_run("tiny-model", dataset_version)
        run_id = created["run"]["run_id"]

        started = await self.app.start_preference_run(run_id)
        checkpoints = self.app.preference_checkpoints(run_id)
        found = self.app.preference_run(run_id)

        self.assertTrue(started["ok"], started)
        self.assertEqual(started["run"]["status"], TrainingRunStatus.COMPLETED.value)
        self.assertEqual(started["model"]["algorithm"], "dpo")
        self.assertGreaterEqual(checkpoints["count"], 1)
        self.assertEqual(found["status"], TrainingRunStatus.COMPLETED.value)
        with self.assertRaises(KeyError):
            self.app.preference_run("nope")

    async def test_a_preference_loss_is_not_an_evaluation_and_the_registry_is_explicit(self) -> None:
        dataset_version = self.built_dataset()
        created = self.app.create_preference_run("tiny-model", dataset_version)
        run_id = created["run"]["run_id"]
        await self.app.start_preference_run(run_id)
        model = self.app.preference_models(algorithm="dpo")["models"][0]

        skipped = await self.app.evaluate_preference_run(run_id)
        measured = await self.app.evaluate_preference_run(
            run_id,
            base=ChoicePredictor("base", prefers="rejected"),
            candidate=ChoicePredictor("candidate"),
        )
        approved = self.app.approve_training_model(model["model_id"], note="verified")
        promoted = self.app.promote_training_model(model["model_id"], note="probe")

        self.assertFalse(skipped["ok"])
        self.assertTrue(skipped["skipped"])
        self.assertTrue(measured["ok"], measured)
        self.assertEqual(measured["evaluation"]["verdict"], "pass")
        self.assertEqual(approved["ok"], True, approved)
        self.assertTrue(promoted["ok"], promoted)
        self.assertEqual(model["algorithm"], "dpo")

    async def test_a_comparison_can_be_run_without_a_run(self) -> None:
        dataset_version = self.built_dataset()

        result = self.app.compare_preference_models(
            dataset_version,
            base=ChoicePredictor("base", prefers="rejected"),
            candidate=ChoicePredictor("candidate"),
            algorithm="orpo",
        )
        evaluations = self.app.preference_evaluations()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["comparison"]["verdict"], "pass")
        self.assertEqual(result["comparison"]["algorithm"], "orpo")
        self.assertGreaterEqual(evaluations["count"], 1)

    def test_a_dry_run_preview_starts_nothing(self) -> None:
        dataset_version = self.built_dataset()

        preview = self.app.dry_run_preference(
            "tiny-model", dataset_version, algorithm="orpo"
        )

        self.assertTrue(preview["ok"], preview)
        self.assertFalse(preview["started"])
        self.assertEqual(self.app.preference_runs()["count"], 0)

    def test_the_subsystem_can_be_switched_off_and_on(self) -> None:
        self.app.settings.update(preference_enabled=False)

        status = self.app.preference_status()
        refused = self.app.create_preference_dataset("tools", "tool_selection")
        refused_pair = self.submitted_pair()

        self.assertFalse(status["enabled"])
        self.assertFalse(refused["ok"])
        self.assertTrue(refused["refused"])
        self.assertFalse(refused_pair["ok"])
        self.assertIn("switched off", refused_pair["reason"])

        self.app.settings.update(preference_enabled=True)
        self.assertTrue(self.app.preference_status()["enabled"])

    async def test_the_diagnostics_roster_names_the_subsystem(self) -> None:
        report = await self.app.diagnostics_report()

        components = {row["component"] for row in report["components"]}
        self.assertIn("Preference optimization", components)

    def test_the_settings_round_trip_through_the_application(self) -> None:
        applied = self.app.apply_preference_settings()

        self.assertTrue(applied["enabled"])
        self.assertTrue(applied["dry_run"])
        self.assertEqual(applied["algorithm"], "dpo")
        self.assertEqual(applied["max_checkpoints"], 3)


# ── the HTTP surface ─────────────────────────────────────


class PreferenceApiTests(unittest.TestCase):
    """The /preference/* routes, over real HTTP, on an isolated application."""

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

    def _submit(self, index: int = 0) -> str:
        response = self._client.post(
            "/preference/reviews/submit",
            json={
                "dataset_type": "tool_selection",
                "prompt": {"request": f"open app {index}", "group_key": f"group-{index}"},
                "chosen": {"tool": "desktop.launch", "arguments": {"app": str(index)}},
                "rejected": {"tool": "browser.open"},
                "reviewer": "ana",
                "confidence": 0.9,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["preference_id"]

    def _dataset(self) -> str:
        for index in range(4):
            self._submit(index)
        built = self._client.post(
            "/preference/datasets",
            json={"name": "tools", "dataset_type": "tool_selection"},
        )
        self.assertEqual(built.status_code, 200, built.text)
        return built.json()["dataset"]["dataset_version_id"]

    def test_the_status_summary_and_algorithms_are_served(self) -> None:
        status = self._client.get("/preference/status")
        summary = self._client.get("/preference/summary")
        algorithms = self._client.get("/preference/algorithms")

        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["enabled"])
        self.assertEqual(status.json()["defaults"]["algorithm"], "dpo")
        self.assertEqual(summary.status_code, 200)
        self.assertIn("datasets", summary.json())
        self.assertIn("dpo", algorithms.json()["algorithms"])

    def test_an_estimate_is_served_without_creating_anything(self) -> None:
        response = self._client.post(
            "/preference/estimate",
            json={"base_model": "tiny-model", "algorithm": "dpo"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("estimate", response.json())
        self.assertTrue(response.json()["needs_reference_model"])
        assert self._nova is not None
        self.assertEqual(self._nova.preference_runs()["count"], 0)

    def test_a_dataset_is_built_listed_validated_and_read_back(self) -> None:
        dataset_version = self._dataset()

        listed = self._client.get("/preference/datasets")
        found = self._client.get(f"/preference/datasets/{dataset_version}")
        validated = self._client.post(
            "/preference/datasets/validate", json={"dataset_version_id": dataset_version}
        )
        pair_id = found.json()["examples"][0]["preference_id"]
        pair = self._client.get(
            f"/preference/datasets/{dataset_version}/pairs/{pair_id}"
        )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["count"], 1)
        self.assertEqual(validated.status_code, 200)
        self.assertTrue(validated.json()["ok"])
        self.assertEqual(pair.status_code, 200)
        self.assertTrue(pair.json()["pair"]["chosen"])

    def test_a_build_without_a_type_is_a_422_and_an_unknown_dataset_is_a_404(self) -> None:
        missing_name = self._client.post("/preference/datasets", json={"dataset_type": "nlu"})
        unknown = self._client.get("/preference/datasets/nope@1.0.0")
        no_version = self._client.post("/preference/datasets/validate", json={})

        self.assertEqual(missing_name.status_code, 422)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(no_version.status_code, 422)

    def test_a_dry_run_is_served_and_starts_nothing(self) -> None:
        dataset_version = self._dataset()

        response = self._client.post(
            "/preference/dry-run",
            json={"model": "tiny-model", "dataset_version": dataset_version, "algorithm": "orpo"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])
        self.assertFalse(response.json()["started"])
        assert self._nova is not None
        self.assertEqual(self._nova.preference_runs()["count"], 0)

    def test_a_run_can_be_created_started_and_read_back(self) -> None:
        dataset_version = self._dataset()

        created = self._client.post(
            "/preference/runs",
            json={"model": "tiny-model", "dataset_version": dataset_version},
        )
        self.assertEqual(created.status_code, 200, created.text)
        run_id = created.json()["run"]["run_id"]
        started = self._client.post("/preference/runs/start", json={"run_id": run_id})
        found = self._client.get(f"/preference/runs/{run_id}")
        checkpoints = self._client.get(f"/preference/runs/{run_id}/checkpoints")
        listed = self._client.get("/preference/runs", params={"algorithm": "dpo"})

        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["run"]["status"], TrainingRunStatus.COMPLETED.value)
        self.assertEqual(found.json()["algorithm"], "dpo")
        self.assertGreaterEqual(checkpoints.json()["count"], 1)
        self.assertEqual(listed.json()["count"], 1)

    def test_a_run_without_a_dataset_is_a_422(self) -> None:
        response = self._client.post("/preference/runs", json={"model": "tiny-model"})

        self.assertEqual(response.status_code, 422)
        self.assertIn("dataset_version is required", response.json()["detail"])

    def test_the_review_queue_is_served_and_a_decision_is_recorded(self) -> None:
        preference_id = self._submit()

        queue = self._client.get("/preference/reviews", params={"pending_only": False})
        item = self._client.get(f"/preference/reviews/{preference_id}")
        decided = self._client.post(
            "/preference/reviews/decide",
            json={"preference_id": preference_id, "decision": "choose_b", "reviewer": "bo"},
        )

        self.assertEqual(queue.status_code, 200)
        self.assertEqual(queue.json()["count"], 1)
        self.assertEqual(item.status_code, 200)
        self.assertEqual(decided.status_code, 200, decided.text)
        self.assertTrue(decided.json()["ok"])
        self.assertEqual(decided.json()["pair"]["review"]["reviewer"], "bo")

    def test_a_reasonless_tie_is_a_422_and_an_unknown_pair_is_a_404(self) -> None:
        preference_id = self._submit()

        tie = self._client.post(
            "/preference/reviews/decide",
            json={"preference_id": preference_id, "decision": "tie"},
        )
        missing = self._client.post(
            "/preference/reviews/decide",
            json={"preference_id": "nope", "decision": "choose_a"},
        )

        self.assertEqual(tie.status_code, 422)
        self.assertIn("needs a reason", tie.json()["detail"])
        self.assertEqual(missing.status_code, 404)

    def test_a_refused_submission_is_a_422_with_the_reason(self) -> None:
        refused = self._client.post(
            "/preference/reviews/submit",
            json={
                "dataset_type": "nlu",
                "prompt": {"request": "open calculator"},
                "chosen": {"intent": SECRET},
                "rejected": {"intent": "search_web"},
            },
        )
        malformed = self._client.post("/preference/reviews/submit", json={})

        self.assertEqual(refused.status_code, 422)
        self.assertIn("rejected", refused.json()["detail"])
        self.assertEqual(malformed.status_code, 422)

    def test_an_evaluation_without_predictors_is_refused_not_approved(self) -> None:
        dataset_version = self._dataset()
        created = self._client.post(
            "/preference/runs", json={"model": "tiny-model", "dataset_version": dataset_version}
        )
        run_id = created.json()["run"]["run_id"]
        self._client.post("/preference/runs/start", json={"run_id": run_id})

        response = self._client.post("/preference/runs/evaluate", json={"run_id": run_id})

        self.assertEqual(response.status_code, 409)
        self.assertIn("base predictor", response.json()["detail"])

    def test_a_comparison_without_predictors_is_inconclusive_not_a_pass(self) -> None:
        dataset_version = self._dataset()

        response = self._client.post(
            "/preference/compare",
            json={
                "dataset_version": dataset_version,
                "base": {"name": "base"},
                "candidate": {"name": "candidate"},
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["comparison"]["verdict"], "inconclusive")
        self.assertFalse(response.json()["comparison"]["loss_consulted"])

    def test_evaluations_and_models_are_served(self) -> None:
        dataset_version = self._dataset()
        created = self._client.post(
            "/preference/runs", json={"model": "tiny-model", "dataset_version": dataset_version}
        )
        run_id = created.json()["run"]["run_id"]
        self._client.post("/preference/runs/start", json={"run_id": run_id})

        models = self._client.get("/preference/models", params={"algorithm": "dpo"})
        model_id = models.json()["models"][0]["model_id"]
        found = self._client.get(f"/preference/models/{model_id}")
        unknown = self._client.get("/preference/models/nope")
        evaluations = self._client.get("/preference/evaluations")

        self.assertEqual(models.json()["count"], 1)
        self.assertEqual(found.json()["algorithm"], "dpo")
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(evaluations.status_code, 200)


# ── configuration and settings ─────────────────────────────


class PreferenceSettingsTests(unittest.TestCase):
    """The config section, the environment and the user's switches agree."""

    def test_the_config_section_round_trips(self) -> None:
        from novacontrol.core.config import PreferenceSettings

        settings = PreferenceSettings.from_mapping(
            {
                "enabled": False,
                "dry_run": False,
                "allow_unsafe": True,
                "hardware_policy": "local_cpu",
                "max_checkpoints": 999,
                "max_records": 10,
                "defaults": {"algorithm": "orpo", "beta": 0.05},
            }
        )

        self.assertFalse(settings.enabled)
        self.assertFalse(settings.dry_run)
        self.assertTrue(settings.allow_unsafe)
        self.assertEqual(settings.hardware_policy, "local_cpu")
        self.assertEqual(settings.max_records, 10)
        self.assertEqual(settings.max_checkpoints, 100, "the cap is a ceiling, not a hint")
        self.assertEqual(settings.defaults["algorithm"], "orpo")
        self.assertEqual(PreferenceSettings.from_mapping(settings.to_mapping()), settings)

    def test_the_base_config_is_the_policy_and_then_the_operators_defaults(self) -> None:
        from novacontrol.core.config import PreferenceSettings

        settings = PreferenceSettings.from_mapping(
            {
                "hardware_policy": "local_cpu",
                "max_checkpoints": 4,
                "dry_run": True,
                "defaults": {"algorithm": "orpo", "beta": 0.2},
            }
        )

        base = settings.base_config()

        self.assertEqual(base["hardware_policy"], "local_cpu")
        self.assertEqual(base["algorithm"], "orpo")
        self.assertEqual(base["beta"], 0.2)
        self.assertNotIn("enabled", base, "a run's config is not a place for the switch")

    def test_the_environment_can_switch_the_subsystem_off_and_tune_it(self) -> None:
        from novacontrol.core.config import NovaControlConfig

        with mock.patch.dict(
            "os.environ",
            {
                "NOVACONTROL_PREFERENCE_ENABLED": "false",
                "NOVACONTROL_PREFERENCE_DRY_RUN": "0",
                "NOVACONTROL_PREFERENCE_ALLOW_UNSAFE": "yes",
                "NOVACONTROL_PREFERENCE_HARDWARE_POLICY": "local_cpu",
                "NOVACONTROL_PREFERENCE_MAX_CHECKPOINTS": "5",
                "NOVACONTROL_PREFERENCE_MAX_RECORDS": "11",
            },
        ):
            config = NovaControlConfig.from_environment()

        self.assertFalse(config.preference.enabled)
        self.assertFalse(config.preference.dry_run)
        self.assertTrue(config.preference.allow_unsafe)
        self.assertEqual(config.preference.hardware_policy, "local_cpu")
        self.assertEqual(config.preference.max_checkpoints, 5)
        self.assertEqual(config.preference.max_records, 11)

    def test_a_nonsense_environment_value_keeps_the_safe_default(self) -> None:
        from novacontrol.core.config import NovaControlConfig

        with mock.patch.dict(
            "os.environ",
            {
                "NOVACONTROL_PREFERENCE_ENABLED": "maybe",
                "NOVACONTROL_PREFERENCE_MAX_CHECKPOINTS": "lots",
            },
        ):
            config = NovaControlConfig.from_environment()

        self.assertTrue(config.preference.enabled)
        self.assertEqual(config.preference.max_checkpoints, 3)

    def test_the_user_settings_carry_the_preference_switches(self) -> None:
        from novacontrol.settings import SettingsManager, UserSettings

        manager = SettingsManager()
        updated = manager.update(
            preference_enabled=False,
            preference_dry_run=False,
            preference_max_checkpoints=999,
            preference_retention_days=7,
            preference_max_records=25,
        )

        self.assertFalse(updated.preference_enabled)
        self.assertFalse(updated.preference_dry_run)
        self.assertEqual(updated.preference_max_checkpoints, 100)
        self.assertEqual(updated.preference_retention_days, 7)
        self.assertEqual(updated.preference_max_records, 25)
        self.assertEqual(UserSettings.from_dict(updated.to_dict()), updated)

    def test_an_unset_preference_switch_keeps_its_current_value(self) -> None:
        from novacontrol.settings import SettingsManager

        manager = SettingsManager()
        manager.update(preference_dry_run=False, preference_max_checkpoints=5)
        after = manager.update(preference_max_records=7)

        self.assertFalse(after.preference_dry_run, "an untouched flag is left alone")
        self.assertEqual(after.preference_max_checkpoints, 5)
        self.assertTrue(after.preference_enabled)


# ── the CLI ──────────────────────────────────────────────


class PreferenceCliTests(unittest.IsolatedAsyncioTestCase):
    """`novacontrol preference …` dispatches to the application, and nothing more."""

    def test_the_parser_offers_every_action_and_every_family(self) -> None:
        from novacontrol.cli.parser import build_parser

        parser = build_parser()
        actions = (
            "status", "summary", "algorithms", "datasets", "dataset", "build",
            "validate", "pair", "estimate", "dry-run", "create", "runs", "run",
            "checkpoints", "start", "pause", "resume", "cancel", "evaluate",
            "evaluations", "reviews", "review", "submit", "decide", "models", "model",
        )

        for action in actions:
            parsed = parser.parse_args(["preference", action])
            self.assertEqual(parsed.action, action, action)

        for member in PreferenceDatasetType:
            parsed = parser.parse_args(["preference", "datasets", "--type", member.value])
            self.assertEqual(parsed.dataset_type, member.value)

        self.assertEqual(
            parser.parse_args(["preference", "datasets"]).dataset_type,
            PreferenceDatasetType.NLU.value,
            "an unstated family is the friendly default, not an error",
        )
        with self.assertRaises(SystemExit):
            parser.parse_args(["preference", "datasets", "--type", "poetry"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["preference", "datasets", "--algorithm", "rlhf"])

    def test_the_objective_and_decision_flags_are_validated_where_they_are_typed(self) -> None:
        from novacontrol.cli.parser import build_parser

        parser = build_parser()

        parsed = parser.parse_args(
            ["preference", "create", "--algorithm", "orpo", "--beta", "0.05"]
        )
        self.assertEqual(parsed.algorithm, "orpo")
        self.assertEqual(parsed.beta, 0.05)
        self.assertEqual(
            parser.parse_args(["preference", "decide", "--decision", "choose_b"]).decision,
            "choose_b",
        )
        with self.assertRaises(SystemExit):
            parser.parse_args(["preference", "decide", "--decision", "maybe"])

    async def test_every_action_reaches_the_application_method_it_names(self) -> None:
        from novacontrol.cli.commands import _preference_action

        calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

        class Fake:
            def __getattr__(self, name: str):
                def record(*args: Any, **kwargs: Any) -> dict[str, Any]:
                    calls.append((name, args, kwargs))
                    return {"ok": True, "called": name}

                if name in {
                    "start_preference_run",
                    "resume_preference_run",
                    "evaluate_preference_run",
                }:
                    async def awaited(*args: Any, **kwargs: Any) -> dict[str, Any]:
                        return record(*args, **kwargs)

                    return awaited
                return record

        expected = {
            "status": "preference_status",
            "summary": "preference_summary",
            "algorithms": "preference_algorithms",
            "datasets": "preference_datasets",
            "dataset": "preference_dataset",
            "build": "create_preference_dataset",
            "validate": "validate_preference_dataset",
            "pair": "preference_pair",
            "estimate": "estimate_preference",
            "dry-run": "dry_run_preference",
            "create": "create_preference_run",
            "runs": "preference_runs",
            "run": "preference_run",
            "checkpoints": "preference_checkpoints",
            "start": "start_preference_run",
            "pause": "pause_preference_run",
            "resume": "resume_preference_run",
            "cancel": "cancel_preference_run",
            "evaluate": "evaluate_preference_run",
            "evaluations": "preference_evaluations",
            "reviews": "preference_reviews",
            "review": "preference_review",
            "submit": "submit_preference_pair",
            "decide": "decide_preference_review",
            "models": "preference_models",
            "model": "preference_model",
        }
        with TemporaryDirectory() as tmp:
            pairs_file = Path(tmp) / "pairs.json"
            pairs_file.write_text(
                json.dumps(
                    {
                        "prompt": {"request": "open calculator"},
                        "chosen": {"intent": "open_application"},
                        "rejected": {"intent": "search_web"},
                    }
                ),
                encoding="utf-8",
            )
            app = Fake()
            args: dict[str, Any] = {
                "identifier": "tools@1.0.0:pair-1",
                "dataset_type": "nlu",
                "algorithm": "dpo",
                "name": "nlu-pairs",
                "model": "tiny-model",
                "dataset_version": "pairs@1.0.0",
                "epochs": 2,
                "beta": 0.1,
                "limit": 5,
                "pairs": str(pairs_file),
                "decision": "choose_a",
                "reviewer": "ana",
                "overrides": ["epochs=3"],
                "confirm": False,
                "override": False,
                "reason": "because",
                "note": "probe",
            }
            for action, method in expected.items():
                calls.clear()
                result = await _preference_action(app, action, **args)
                self.assertEqual(len(calls), 1, action)
                self.assertEqual(calls[0][0], method, action)
                self.assertEqual(result["called"], method, action)

    async def test_a_decision_without_an_answer_and_a_pair_without_an_id_are_refused(self) -> None:
        from novacontrol.cli.commands import _preference_action

        with self.assertRaises(ValueError):
            await _preference_action(
                self._empty_app(),
                "decide",
                identifier="p1",
                decision="",
                limit=1,
                algorithm="",
            )
        with self.assertRaises(ValueError):
            await _preference_action(
                self._empty_app(),
                "pair",
                identifier="tools@1.0.0",
                limit=1,
                algorithm="",
            )

    @staticmethod
    def _empty_app() -> Any:
        class Empty:
            def __getattr__(self, name: str):
                return lambda *args, **kwargs: {"ok": True}

        return Empty()
