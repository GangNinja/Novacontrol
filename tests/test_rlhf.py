"""Phase 18: RLHF + RLAIF — feedback as a reward, with the hacking rails on.

The suite runs entirely on deterministic fixtures, mocks, synthetic rewards and
dry-run mode: no model is downloaded, no CUDA or NVIDIA GPU is required, nothing
trains for real, and the optional RL dependencies may be absent. That is a
requirement of the phase rather than a convenience — a 16 GB Windows desktop with
an Intel iGPU and an NPU must be able to prove the subsystem works, so every
reward is generated from recorded behaviour, every rollout happens in the mock
environment, every policy update is the mock optimizer's deterministic walk, and
the PEFT boundary is never touched.

Three policies get their own tests because they are the phase's spine:

  * a reward is never trusted blindly — a suspicious or invalid one is held or
    refused before it can become training data (RewardIntegrityChecker),
  * human and AI feedback are never treated as the same thing, and a
    disagreement between them is recorded, never silently resolved
    (FeedbackDisagreementDetector plus per-source provenance),
  * a higher reward is never treated as improvement — the verdict on a model
    comes from measured behaviour on held-out examples, and the registry's
    approval stays explicit.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient

from novacontrol.api.app import create_app
from novacontrol.application import NovaControlApplication
from novacontrol.audit import REDACTED
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
from novacontrol.evaluation.reward import (
    RewardConfig,
    RewardEngine,
    RewardPenalty,
    RewardResult,
)
from novacontrol.rlhf import (
    ALGORITHMS,
    DEFAULT_CRITERIA,
    IMPLEMENTED_ALGORITHMS,
    MODES,
    PIPELINE_STAGES,
    PLANNED_ALGORITHMS,
    POLICY_ALGORITHMS,
    RLHF_COMPARISON,
    RLHF_DATASET_BUILT,
    RLHF_DISAGREEMENT_FOUND,
    RLHF_DRY_RUN,
    RLHF_FEEDBACK_RECEIVED,
    RLHF_RATING_CREATED,
    RLHF_VERSION,
    AIRating,
    AIRatingRewardProvider,
    CompositeRewardProvider,
    CriterionScore,
    DisagreementKind,
    EvaluationRewardProvider,
    EvaluatorUnavailable,
    FeedbackDisagreementDetector,
    FeedbackQualityConfig,
    FeedbackQualityFilter,
    FeedbackStatus,
    HumanFeedback,
    HumanFeedbackType,
    HumanRewardProvider,
    IntegrityContext,
    MockEnvironment,
    MockPolicyOptimizer,
    NoEvaluator,
    PolicyOptimizerUnavailable,
    RewardDatasetBuilder,
    RewardDatasetRequest,
    RewardDatasetVersion,
    RewardExample,
    RewardIntegrityChecker,
    RewardIntegrityStatus,
    RewardPolicyConfig,
    RewardPropagator,
    RewardProviderUnavailable,
    RewardRequest,
    RewardSource,
    RLHFManager,
    RLHFModule,
    RLMode,
    RLModelEvaluator,
    RLPipeline,
    RLResourceEstimator,
    RLTrainingConfig,
    RolloutRunner,
    RolloutStatus,
    RuleBasedEvaluator,
    ScriptedPolicy,
    build_rlhf_repositories,
    clip_reward,
    default_composite,
    feedback_reward_value,
    normalize_reward,
    policy_optimizer_for,
    provider_for,
    regression_report,
    rl_trainer_for,
    single_step_rollout,
    summarise,
)
from novacontrol.rlhf.backends import POLICY_OPTIMIZERS, provider_for_config
from novacontrol.rlhf.datasets import (
    REASON_HUMAN_REQUIRED,
    REASON_MODE_MISMATCH,
    REASONING_KEYS,
)
from novacontrol.rlhf.feedback import (
    REASON_CONTRADICTORY,
    REASON_DUPLICATE,
    REASON_INVALID_RATING,
    REASON_LOW_CONFIDENCE,
    REASON_MISSING_RATING,
    REASON_MISSING_TARGET,
    REASON_TASK_UNFINISHED,
    REASON_UNKNOWN_TYPE,
    feedback_evidence,
)
from novacontrol.rlhf.integrity import (
    INTEGRITY_INCOMPLETE_VERIFICATION,
    INTEGRITY_LENGTH_GAMING,
    INTEGRITY_LOW_CONFIDENCE,
    INTEGRITY_REPEATED_ACTIONS,
    INTEGRITY_SOURCE_DISAGREEMENT,
    INTEGRITY_SOURCE_NOT_ALLOWED,
    INTEGRITY_TASK_FAILED_HIGH_REWARD,
    INTEGRITY_UNSAFE_HIGH_REWARD,
    INTEGRITY_WITHOUT_EVIDENCE,
)
from novacontrol.rlhf.runtime import (
    ALGORITHMS_COMPLETED,
    ALGORITHMS_REQUESTED,
    DATASETS_COMPLETED,
    DATASETS_REQUESTED,
    ESTIMATE_COMPLETED,
    ESTIMATE_REQUESTED,
    FEEDBACK_COMPLETED,
    FEEDBACK_REQUESTED,
    PIPELINE_COMPLETED,
    PIPELINE_REQUESTED,
    STATUS_COMPLETED,
    STATUS_REQUESTED,
)
from novacontrol.training import (
    DatasetStatistics,
    DatasetType,
    HardwareCapabilities,
    ResourceEstimator,
    ResourceVerdict,
    SFTDatasetVersion,
    SFTTrainingExample,
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
    tool: str = "desktop.launch",
    unsafe: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> AgentTrajectory:
    """One run of Phase 15's record, in the shape every Phase 18 reader expects.

    Only observable fields are filled: what was understood, what was decided,
    which tool ran with which arguments, whether verification passed, how long it
    took. Nothing here is a thought, and nothing here needs a model.
    """
    calls = [
        ToolCallRecord(
            tool=tool,
            step_id="s1",
            capability="desktop",
            arguments={"app": "calculator"},
            status="completed" if success else "failed",
            duration_ms=120.0,
        )
    ]
    if unsafe:
        calls.append(
            ToolCallRecord(
                tool="shell.run",
                step_id="s1",
                capability="shell",
                arguments={"command": "erase"},
                status="completed",
                requires_confirmation=True,
                approved=False,
                duration_ms=50.0,
            )
        )
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
        tool_calls=tuple(calls),
        verification_results=(
            VerificationRecord(step_id="s1", status="verified" if verified else "failed"),
        ),
        status=status,
        success=success,
        final_result={"summary": "calculator opened" if success else ""},
        latency_metrics=LatencyMetrics(total_ms=250.0),
        metadata=dict(metadata or {}),
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


def raw_feedback(**overrides: Any) -> HumanFeedback:
    """A feedback row built directly, so a test controls what the filter sees."""
    fields: dict[str, Any] = {
        "feedback_type": HumanFeedbackType.ACCEPT.value,
        "trajectory_id": "traj-1",
        "task_id": "task-1",
        "user_ref": "ana",
        "confidence": 0.9,
    }
    fields.update(overrides)
    return HumanFeedback(**fields)


def raw_rating(**overrides: Any) -> AIRating:
    """A structured AI rating: criterion scores, confidence, evidence, identity."""
    fields: dict[str, Any] = {
        "trajectory_id": "traj-1",
        "task_id": "task-1",
        "evaluator_id": "rule",
        "score": 0.9,
        "confidence": 0.9,
        "criteria": (
            CriterionScore(
                name="correctness",
                score=0.9,
                weight=1.0,
                reason="verification passed",
            ),
            CriterionScore(
                name="task_completion",
                score=0.9,
                weight=1.0,
                reason="the task succeeded",
            ),
        ),
        "evidence": ("verification_passed",),
    }
    fields.update(overrides)
    return AIRating(**fields)


def raw_reward(**overrides: Any) -> RewardResult:
    """A reward with provenance, as a provider would build one."""
    fields: dict[str, Any] = {
        "reward_id": "rwr-traj-1",
        "trajectory_id": "traj-1",
        "total_reward": 0.8,
        "normalized_reward": 0.26,
        "reward_source": RewardSource.RULE.value,
        "evaluator_id": "probe",
        "confidence": 0.9,
        "evidence": ("task_succeeded", "verification_passed"),
    }
    fields.update(overrides)
    return RewardResult(**fields)


def make_manager(root: str | Path, **overrides: Any) -> RLHFManager:
    """An RLHF manager wired to JSONL repositories under a temp directory.

    The run, checkpoint, model and evaluation stores are Phase 16's, deliberately:
    an RL run IS a training run, and a test that gave it a second store would not
    be testing the thing that ships.
    """
    repos = build_rlhf_repositories(root)
    supervised, runs, checkpoints, models, evaluations = build_training_repositories(root)
    kwargs: dict[str, Any] = {
        "datasets": repos.datasets,
        "feedback": repos.feedback,
        "ratings": repos.ratings,
        "disagreements": repos.disagreements,
        "supervised_datasets": supervised,
        "runs": runs,
        "checkpoints": checkpoints,
        "models": models,
        "evaluations": evaluations,
        "output_root": Path(root) / "rlhf_output",
    }
    kwargs.update(overrides)
    return RLHFManager(**kwargs)


def dry_run_manager(root: str | Path, **overrides: Any) -> RLHFManager:
    """A manager whose deployment default is the mock optimizer and a DRY RUN."""
    config = RLTrainingConfig(
        base_model="tiny-model",
        reward_dataset_version="unset@v1",
        output_directory=str(Path(root) / "rlhf_output"),
        dry_run=True,
        rollout_count=2,
        max_steps=4,
        checkpoint_frequency=1,
        max_checkpoints=2,
    )
    return make_manager(root, default_config=config, **overrides)


def config_for(dataset_version: str, **overrides: Any) -> RLTrainingConfig:
    """A dry-run RL configuration pointed at one reward dataset version."""
    fields: dict[str, Any] = {
        "base_model": "tiny-model",
        "reward_dataset_version": dataset_version,
        "output_directory": "out",
        "dry_run": True,
        "rollout_count": 2,
        "max_steps": 4,
        "checkpoint_frequency": 1,
        "max_checkpoints": 2,
    }
    fields.update(overrides)
    return RLTrainingConfig(**fields)


def build_managed_dataset(
    manager: RLHFManager,
    *,
    name: str = "label",
    mode: str = "mixed",
    count: int = 6,
    version: str = "v1",
    with_ratings: bool = False,
    **overrides: Any,
) -> RewardDatasetVersion:
    """Build a dataset of distinguishable, task-grouped rows through the manager."""
    trajectories = tuple(
        trajectory(f"traj-{index}", task_id=f"task-{index}", request=f"open app {index}")
        for index in range(count)
    )
    feedback = tuple(
        raw_feedback(trajectory_id=f"traj-{index}", task_id=f"task-{index}")
        for index in range(count)
    )
    ratings = (
        tuple(
            raw_rating(trajectory_id=f"traj-{index}", task_id=f"task-{index}")
            for index in range(count)
        )
        if with_ratings
        else ()
    )
    return manager.build_dataset(
        name,
        mode=mode,
        version=version,
        trajectories=trajectories,
        feedback=feedback,
        ratings=ratings,
        **overrides,
    )


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


class RecordingPublisher:
    """Captures the events a manager published, in order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, type_: str, payload: Mapping[str, Any]) -> None:
        self.events.append((type_, dict(payload)))

    def types(self) -> list[str]:
        return [type_ for type_, _ in self.events]


# ── the schema ──────────────────────────────────────────────────────────────


class RLHFSchemaTests(unittest.TestCase):
    """The vocabulary: two modes, three algorithms, five sources, four verdicts."""

    def test_two_modes_and_the_implemented_algorithm_split(self) -> None:
        self.assertEqual([member.value for member in RLMode], ["rlhf", "rlaif"])
        self.assertEqual(list(MODES), ["rlhf", "rlaif"])
        self.assertEqual(list(ALGORITHMS), ["rlhf", "rlaif"])
        self.assertEqual(list(POLICY_ALGORITHMS), ["mock_policy", "ppo", "grpo"])
        self.assertEqual(list(IMPLEMENTED_ALGORITHMS), ["mock_policy"])
        self.assertEqual(list(PLANNED_ALGORITHMS), ["ppo", "grpo"])

    def test_the_vocabulary_has_no_rlvr_or_critique_machinery(self) -> None:
        names = " ".join([*MODES, *ALGORITHMS, *POLICY_ALGORITHMS]).lower()

        for absent in ("rlvr", "critique", "rlcd", "game"):
            self.assertNotIn(absent, names)

    def test_five_reward_sources_and_four_integrity_verdicts(self) -> None:
        self.assertEqual(
            [member.value for member in RewardSource],
            ["human", "ai", "rule", "verifier", "composite"],
        )
        self.assertEqual(
            [member.value for member in RewardIntegrityStatus],
            ["valid", "suspicious", "invalid", "needs_review"],
        )

    def test_eight_feedback_types_and_three_statuses(self) -> None:
        self.assertEqual(
            [member.value for member in HumanFeedbackType],
            [
                "accept", "reject", "prefer_a", "prefer_b", "rating",
                "correction", "report_error", "report_unsafe",
            ],
        )
        self.assertEqual(
            [member.value for member in FeedbackStatus],
            ["accepted", "rejected", "needs_review"],
        )

    def test_the_pipeline_lists_its_ten_stages_in_order(self) -> None:
        self.assertEqual(
            list(PIPELINE_STAGES),
            [
                "data_preparation", "reward_generation", "reward_validation",
                "training_configuration", "resource_estimation",
                "rollout_simulation", "policy_optimization", "checkpointing",
                "evaluation", "registration",
            ],
        )

    def test_a_reward_result_round_trips_with_its_provenance(self) -> None:
        reward = raw_reward().with_provenance(
            source="human", evaluator_id="human", confidence=0.75
        )

        restored = RewardResult.from_dict(json.loads(json.dumps(reward.to_dict())))

        self.assertEqual(restored.total_reward, 0.8)
        self.assertEqual(restored.reward_source, "human")
        self.assertEqual(restored.evaluator_id, "human")
        self.assertEqual(restored.confidence, 0.75)
        self.assertEqual(restored.reward_version, raw_reward().reward_version)

    def test_the_reward_policy_is_versioned_and_fingerprinted(self) -> None:
        policy = RewardPolicyConfig()

        self.assertEqual(policy.version, RLHF_VERSION)
        self.assertEqual(policy.fingerprint(), RewardPolicyConfig().fingerprint())
        self.assertNotEqual(
            policy.fingerprint(), RewardPolicyConfig(safety_floor=-0.9).fingerprint()
        )

    def test_a_person_is_worth_more_than_an_evaluator_by_default(self) -> None:
        policy = RewardPolicyConfig()

        self.assertGreater(
            policy.weight(RewardSource.HUMAN.value),
            policy.weight(RewardSource.AI.value),
        )
        self.assertGreater(
            policy.weight(RewardSource.VERIFIER.value),
            policy.weight(RewardSource.AI.value),
        )

    def test_a_feedback_row_carries_no_hidden_reasoning(self) -> None:
        payload = raw_feedback().to_dict()

        for key in REASONING_KEYS:
            self.assertNotIn(key, payload)
        self.assertNotIn("reasoning", payload)

    def test_a_run_configuration_defaults_to_a_simulation(self) -> None:
        config = RLTrainingConfig()

        self.assertEqual(config.mode, "rlhf")
        self.assertEqual(config.algorithm, "mock_policy")
        self.assertTrue(config.dry_run)
        self.assertEqual(config.rollout_count, 4)
        self.assertFalse(config.needs_reference_model)
        self.assertTrue(config.requires_human_feedback)
        self.assertFalse(config.requires_ai_feedback)

    def test_planned_algorithms_are_refused_with_an_explanation(self) -> None:
        for algorithm in PLANNED_ALGORITHMS:
            validation = RLTrainingConfig(algorithm=algorithm).validate()

            self.assertFalse(validation.valid)
            self.assertTrue(
                any("not implemented" in error for error in validation.errors)
            )
        mock_errors = RLTrainingConfig(algorithm="mock_policy").validate().errors
        self.assertFalse(any("not implemented" in item for item in mock_errors))

    def test_a_configuration_without_a_dataset_is_invalid(self) -> None:
        validation = RLTrainingConfig().validate()

        self.assertFalse(validation.valid)
        self.assertTrue(any("reward_dataset_version" in item for item in validation.errors))

    def test_out_of_range_parameters_are_refused(self) -> None:
        config = RLTrainingConfig(
            reward_dataset_version="label@v1",
            gamma=1.5,
            rollout_count=0,
            clip_range=3.0,
        )

        errors = " ".join(config.validate().errors)

        self.assertIn("gamma", errors)
        self.assertIn("rollout_count", errors)
        self.assertIn("clip_range", errors)

    def test_the_configuration_projects_onto_phase_sixteen(self) -> None:
        config = config_for("label@v1", epochs=3)

        shared = config.as_training_config()

        self.assertEqual(shared.base_model, "tiny-model")
        self.assertEqual(shared.dataset_version, "label@v1")
        self.assertEqual(shared.epochs, 3)
        self.assertTrue(shared.dry_run)


# ── reward providers ────────────────────────────────────────────────────────


class RewardProviderTests(unittest.TestCase):
    """Provider, result, composition, normalisation and clipping."""

    def test_a_person_accepting_is_worth_a_positive_reward(self) -> None:
        provider = HumanRewardProvider()
        request = RewardRequest.for_trajectory(trajectory(), feedback=(raw_feedback(),))

        reward = provider.evaluate(request)

        self.assertEqual(reward.total_reward, 1.0)
        self.assertEqual(reward.reward_source, RewardSource.HUMAN.value)
        self.assertEqual(reward.evaluator_id, "human")
        self.assertIn("human_accepted", reward.evidence)
        self.assertEqual(reward.confidence, 0.9)

    def test_a_person_rejecting_is_negative_and_unsafe_is_the_floor(self) -> None:
        provider = HumanRewardProvider()

        rejected = provider.evaluate(
            RewardRequest.for_trajectory(
                trajectory(), feedback=(raw_feedback(feedback_type="reject"),)
            )
        )
        unsafe = provider.evaluate(
            RewardRequest.for_trajectory(
                trajectory(), feedback=(raw_feedback(feedback_type="report_unsafe"),)
            )
        )

        self.assertEqual(rejected.total_reward, -1.0)
        self.assertEqual(unsafe.total_reward, -1.0)
        self.assertIn("unsafe_reported", unsafe.evidence)

    def test_a_human_reward_without_feedback_is_unavailable(self) -> None:
        provider = HumanRewardProvider()
        request = RewardRequest.for_trajectory(trajectory())

        self.assertTrue(provider.validate(request))
        with self.assertRaises(RewardProviderUnavailable):
            provider.evaluate(request)

    def test_a_rule_evaluator_scores_every_default_criterion(self) -> None:
        request = RewardRequest.for_trajectory(trajectory()).evaluation_request()

        rating = RuleBasedEvaluator().evaluate(request)

        self.assertEqual([item.name for item in rating.criteria], list(DEFAULT_CRITERIA))
        self.assertGreater(rating.score, 0.5)
        self.assertEqual(rating.confidence, 0.9)
        self.assertTrue(rating.evidence)
        for key in REASONING_KEYS:
            self.assertNotIn(key, rating.to_dict())

    def test_an_ai_provider_rates_the_observable_request(self) -> None:
        provider = AIRatingRewardProvider(RuleBasedEvaluator())
        request = RewardRequest.for_trajectory(
            trajectory(), evaluation=evaluation()
        )

        reward = provider.evaluate(request)

        self.assertEqual(reward.reward_source, RewardSource.AI.value)
        self.assertIn("ai_rating", reward.component_rewards)
        self.assertTrue(
            any(name.startswith("criterion:") for name in reward.component_rewards)
        )
        self.assertGreater(reward.total_reward, 0.0)
        self.assertEqual(reward.confidence, 0.9)

    def test_supplied_ratings_are_used_as_given(self) -> None:
        provider = AIRatingRewardProvider(RuleBasedEvaluator())
        request = RewardRequest.for_trajectory(
            trajectory(), ratings=(raw_rating(score=1.0),)
        )

        reward = provider.evaluate(request)

        self.assertEqual(reward.total_reward, 1.0)
        self.assertEqual(reward.evaluator_id, "rule")

    def test_an_evaluator_that_is_switched_off_is_not_improvised(self) -> None:
        provider = AIRatingRewardProvider(NoEvaluator())
        request = RewardRequest.for_trajectory(trajectory(), evaluation=evaluation())

        self.assertFalse(provider.is_available())
        self.assertTrue(provider.validate(request))
        with self.assertRaises(EvaluatorUnavailable):
            provider.evaluate(request)

    def test_an_evaluation_provider_reuses_phase_fifteen(self) -> None:
        engine = RewardEngine(RewardConfig())
        provider = EvaluationRewardProvider(engine)
        request = RewardRequest.for_trajectory(trajectory(), evaluation=evaluation())

        reward = provider.evaluate(request)
        base = engine.score(trajectory(), evaluation())

        self.assertEqual(reward.total_reward, base.total_reward)
        self.assertEqual(reward.reward_source, RewardSource.RULE.value)
        self.assertTrue(reward.evidence)

    def test_an_evaluation_provider_needs_a_trajectory(self) -> None:
        provider = EvaluationRewardProvider()

        self.assertTrue(
            any("trajectory" in item for item in provider.validate(RewardRequest()))
        )
        with self.assertRaises(RewardProviderUnavailable):
            provider.evaluate(RewardRequest())

    def test_a_composite_keeps_safety_separate_and_caps_the_total(self) -> None:
        provider = default_composite()
        unsafe = trajectory("traj-unsafe", unsafe=True)
        request = RewardRequest.for_trajectory(
            unsafe,
            evaluation=evaluation("traj-unsafe"),
            feedback=(raw_feedback(trajectory_id="traj-unsafe"),),
        )

        reward = provider.evaluate(request)

        self.assertEqual(reward.reward_source, RewardSource.COMPOSITE.value)
        self.assertIn("safety:unsafe_action", reward.penalties)
        self.assertIn("safety_cap", reward.component_rewards)
        self.assertEqual(reward.total_reward, -1.0)
        self.assertIn("safety penalties kept separate", reward.explanation_summary)

    def test_a_composite_weights_a_person_above_an_evaluator(self) -> None:
        provider = CompositeRewardProvider(
            [HumanRewardProvider(), AIRatingRewardProvider(RuleBasedEvaluator())]
        )
        request = RewardRequest.for_trajectory(
            trajectory(),
            feedback=(raw_feedback(),),
            ratings=(raw_rating(score=0.0, criteria=()),),
        )

        reward = provider.evaluate(request)

        self.assertGreater(reward.total_reward, 0.0)
        self.assertGreater(
            reward.component_rewards["source:human"], reward.component_rewards["source:ai"]
        )

    def test_a_composite_names_the_source_it_could_not_measure(self) -> None:
        provider = default_composite()

        reward = provider.evaluate(RewardRequest.for_trajectory(trajectory()))

        self.assertIn("unavailable providers", reward.explanation_summary)
        self.assertIn("human_feedback", reward.explanation_summary)

    def test_provider_for_refuses_an_unknown_name(self) -> None:
        self.assertIsInstance(provider_for("human"), HumanRewardProvider)
        self.assertIsInstance(provider_for("ai"), AIRatingRewardProvider)
        self.assertIsInstance(provider_for("verifier"), EvaluationRewardProvider)

        with self.assertRaises(ValueError):
            provider_for("verifier_3000")

    def test_normalisation_does_not_wash_out_a_safety_penalty(self) -> None:
        policy = RewardPolicyConfig()
        safe = raw_reward(total_reward=0.6, penalties={})
        unsafe = raw_reward(
            total_reward=0.6,
            penalties={"safety:unsafe_action": 1.0},
            penalty_breakdown=(
                RewardPenalty(
                    name="unsafe_action",
                    count=1.0,
                    weight=1.0,
                    contribution=1.0,
                ),
            ),
        )

        normalised_safe = normalize_reward(safe, policy)
        normalised_unsafe = normalize_reward(unsafe, policy)

        self.assertGreater(normalised_safe.normalized_reward, 0.0)
        self.assertLessEqual(normalised_unsafe.normalized_reward, policy.safety_floor)
        self.assertNotEqual(
            normalised_safe.normalized_reward, normalised_unsafe.normalized_reward
        )
        self.assertEqual(normalised_unsafe.total_reward, 0.6)

    def test_clipping_bounds_the_total_without_losing_the_components(self) -> None:
        policy = RewardPolicyConfig()
        reward = raw_reward(total_reward=9.0, component_rewards={"probe": 9.0})

        clipped = clip_reward(reward, policy)

        self.assertEqual(clipped.total_reward, policy.clip_high)
        self.assertEqual(dict(clipped.component_rewards), {"probe": 9.0})

    def test_a_provider_describes_itself_for_a_report(self) -> None:
        described = default_composite().describe()

        self.assertEqual(described["provider"], "composite")
        self.assertEqual(described["source"], RewardSource.COMPOSITE.value)
        self.assertTrue(described["available"])


# ── reward integrity ────────────────────────────────────────────────────────


class RewardIntegrityTests(unittest.TestCase):
    """The reward-hacking audit: nothing below valid becomes training data."""

    def check(
        self, reward: RewardResult, context: IntegrityContext
    ) -> Any:
        return RewardIntegrityChecker().check(reward, context)

    def test_a_healthy_reward_is_valid(self) -> None:
        check = self.check(
            raw_reward(),
            IntegrityContext(subject_id="traj-1", task_succeeded=True, verification_total=1),
        )

        self.assertEqual(check.status, RewardIntegrityStatus.VALID.value)
        self.assertTrue(check.trusted)
        self.assertFalse(check.flagged)

    def test_failing_the_task_with_a_high_reward_is_invalid(self) -> None:
        check = self.check(
            raw_reward(total_reward=0.9),
            IntegrityContext(
                subject_id="traj-1", task_succeeded=False, verification_total=1
            ),
        )

        self.assertEqual(check.status, RewardIntegrityStatus.INVALID.value)
        self.assertIn(INTEGRITY_TASK_FAILED_HIGH_REWARD, check.codes())

    def test_an_unsafe_action_with_a_positive_reward_is_invalid(self) -> None:
        check = self.check(
            raw_reward(total_reward=0.5),
            IntegrityContext(
                subject_id="traj-1",
                task_succeeded=True,
                verification_total=1,
                unsafe_actions=1,
            ),
        )

        self.assertEqual(check.status, RewardIntegrityStatus.INVALID.value)
        self.assertIn(INTEGRITY_UNSAFE_HIGH_REWARD, check.codes())

    def test_a_high_reward_without_verification_is_suspicious(self) -> None:
        check = self.check(
            raw_reward(total_reward=0.95),
            IntegrityContext(subject_id="traj-1", task_succeeded=True, verification_total=0),
        )

        self.assertEqual(check.status, RewardIntegrityStatus.SUSPICIOUS.value)
        self.assertIn(INTEGRITY_INCOMPLETE_VERIFICATION, check.codes())

    def test_length_that_earned_nothing_is_suspicious(self) -> None:
        check = self.check(
            raw_reward(total_reward=0.4),
            IntegrityContext(
                subject_id="traj-1", task_succeeded=True, response_tokens=1000
            ),
        )

        self.assertEqual(check.status, RewardIntegrityStatus.SUSPICIOUS.value)
        self.assertIn(INTEGRITY_LENGTH_GAMING, check.codes())

    def test_repeating_an_action_to_inflate_a_count_is_suspicious(self) -> None:
        check = self.check(
            raw_reward(total_reward=0.4),
            IntegrityContext(
                subject_id="traj-1",
                task_succeeded=True,
                verification_total=1,
                repeated_actions=3,
            ),
        )

        self.assertEqual(check.status, RewardIntegrityStatus.SUSPICIOUS.value)
        self.assertIn(INTEGRITY_REPEATED_ACTIONS, check.codes())

    def test_a_reward_citing_nothing_is_held_for_review(self) -> None:
        check = self.check(
            raw_reward(total_reward=0.3, evidence=()),
            IntegrityContext(subject_id="traj-1", task_succeeded=True, verification_total=1),
        )

        self.assertEqual(check.status, RewardIntegrityStatus.NEEDS_REVIEW.value)
        self.assertIn(INTEGRITY_WITHOUT_EVIDENCE, check.codes())

    def test_a_low_confidence_reward_is_suspicious(self) -> None:
        check = self.check(
            raw_reward(total_reward=0.3, confidence=0.05),
            IntegrityContext(subject_id="traj-1", task_succeeded=True, verification_total=1),
        )

        self.assertEqual(check.status, RewardIntegrityStatus.SUSPICIOUS.value)
        self.assertIn(INTEGRITY_LOW_CONFIDENCE, check.codes())

    def test_a_source_the_policy_disallows_is_invalid(self) -> None:
        checker = RewardIntegrityChecker(RewardPolicyConfig(sources=("human",)))
        check = checker.check(
            raw_reward(reward_source="ai"),
            IntegrityContext(subject_id="traj-1", task_succeeded=True, verification_total=1),
        )

        self.assertEqual(check.status, RewardIntegrityStatus.INVALID.value)
        self.assertIn(INTEGRITY_SOURCE_NOT_ALLOWED, check.codes())

    def test_two_sources_that_disagree_are_held_for_review(self) -> None:
        check = self.check(
            raw_reward(normalized_reward=0.8),
            IntegrityContext(
                subject_id="traj-1",
                task_succeeded=True,
                verification_total=1,
                sources={"ai": -0.9},
            ),
        )

        self.assertEqual(check.status, RewardIntegrityStatus.NEEDS_REVIEW.value)
        self.assertIn(INTEGRITY_SOURCE_DISAGREEMENT, check.codes())

    def test_each_source_is_audited_with_the_others_as_cross_evidence(self) -> None:
        checks = RewardIntegrityChecker().check_sources(
            {
                "human": raw_reward(reward_source="human", normalized_reward=1.0),
                "ai": raw_reward(
                    reward_source="ai", normalized_reward=-1.0, total_reward=-1.0
                ),
            },
            context=IntegrityContext(
                subject_id="traj-1", task_succeeded=True, verification_total=1
            ),
        )

        self.assertEqual(set(checks), {"human", "ai"})
        for check in checks.values():
            self.assertEqual(check.status, RewardIntegrityStatus.NEEDS_REVIEW.value)

    def test_a_suspicious_reward_is_held_by_the_dataset_builder(self) -> None:
        reward = raw_reward(total_reward=0.95)
        integrity = RewardIntegrityChecker().check(
            reward,
            IntegrityContext(subject_id="traj-1", task_succeeded=True, verification_total=0),
        )
        dataset = RewardDatasetBuilder().build(
            "held",
            RewardDatasetRequest.of(examples=(self.example(reward, integrity),)),
        )

        self.assertEqual(len(dataset.examples), 1)
        self.assertFalse(dataset.examples[0].accepted)
        self.assertEqual(dataset.examples[0].status, FeedbackStatus.NEEDS_REVIEW.value)

    def test_an_invalid_reward_is_refused_rather_than_held(self) -> None:
        reward = raw_reward(total_reward=0.9)
        integrity = RewardIntegrityChecker().check(
            reward,
            IntegrityContext(subject_id="traj-1", task_succeeded=False, verification_total=1),
        )
        dataset = RewardDatasetBuilder().build(
            "refused",
            RewardDatasetRequest.of(examples=(self.example(reward, integrity),)),
        )

        self.assertEqual(dataset.examples[0].status, FeedbackStatus.REJECTED.value)
        self.assertIn("integrity", " ".join(dataset.examples[0].reasons))

    @staticmethod
    def example(reward: RewardResult, integrity: Any) -> RewardExample:
        return RewardExample(
            trajectory_id="traj-1",
            task_id="task-1",
            task={"request": "open calculator"},
            candidate={"summary": "calculator opened"},
            reward=reward,
            reward_source=reward.reward_source or RewardSource.RULE.value,
            integrity=integrity,
        )


# ── disagreement detection ─────────────────────────────────────────────────


class DisagreementTests(unittest.TestCase):
    """Two sources reading one subject differently are recorded, never resolved."""

    def test_a_person_and_an_evaluator_that_oppose_are_recorded(self) -> None:
        reports = FeedbackDisagreementDetector().detect(
            "traj-1",
            human=raw_feedback(feedback_type="reject"),
            ai=raw_rating(score=0.95),
        )

        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertEqual(report.kind, DisagreementKind.HUMAN_VS_AI.value)
        self.assertEqual(report.recommended, "review")
        self.assertGreater(report.gap, 0.0)
        self.assertLess(report.left_reading, 0.0)
        self.assertGreater(report.right_reading, 0.0)

    def test_a_failed_verification_against_praise_recommends_verifying(self) -> None:
        reports = FeedbackDisagreementDetector().detect(
            "traj-1",
            verifier=raw_reward(
                total_reward=-1.0,
                normalized_reward=-1.0,
                reward_source="verifier",
            ),
            ai=raw_rating(score=0.9),
        )

        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].kind, DisagreementKind.VERIFIER_VS_AI.value)
        self.assertEqual(reports[0].recommended, "verify")

    def test_two_evaluators_that_differ_widely_are_recorded(self) -> None:
        reports = FeedbackDisagreementDetector().detect(
            "traj-1",
            ai=raw_rating(score=1.0),
            ai_alt=raw_rating(score=0.0, criteria=()),
        )

        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].kind, DisagreementKind.AI_VS_AI.value)

    def test_two_sources_that_agree_produce_nothing(self) -> None:
        detector = FeedbackDisagreementDetector()

        reports = detector.detect("traj-1", human=raw_feedback(), ai=raw_rating(score=0.9))

        self.assertEqual(reports, ())
        self.assertEqual(detector.summary(reports)["recommendation"], "none")
        self.assertFalse(detector.review_required(reports))

    def test_the_summary_counts_by_kind(self) -> None:
        detector = FeedbackDisagreementDetector()
        reports = detector.detect(
            "traj-1",
            human=raw_feedback(feedback_type="reject"),
            ai=raw_rating(score=0.95),
        )

        summary = detector.summary(reports)

        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["by_kind"], {"human_vs_ai": 1})
        self.assertEqual(summary["subjects"], ["traj-1"])
        self.assertTrue(detector.review_required(reports))


# ── human feedback quality ─────────────────────────────────────────────────


class FeedbackQualityTests(unittest.TestCase):
    """Screening: usable, consistent, safe — and never silently deleted."""

    def test_a_clean_row_is_accepted(self) -> None:
        row, verdict = FeedbackQualityFilter().apply(raw_feedback())

        self.assertIsNotNone(row)
        self.assertEqual(verdict.status, FeedbackStatus.ACCEPTED.value)
        self.assertEqual(verdict.codes(), ())
        assert row is not None
        self.assertEqual(row.status, FeedbackStatus.ACCEPTED.value)

    def test_an_unknown_type_is_rejected_with_a_stated_reason(self) -> None:
        row, verdict = FeedbackQualityFilter().apply(raw_feedback(feedback_type="maybe"))

        self.assertEqual(verdict.status, FeedbackStatus.REJECTED.value)
        self.assertIn(REASON_UNKNOWN_TYPE, verdict.codes())
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.status, FeedbackStatus.REJECTED.value)

    def test_a_row_without_a_target_is_rejected(self) -> None:
        _, verdict = FeedbackQualityFilter().apply(
            raw_feedback(trajectory_id="", task_id="")
        )

        self.assertIn(REASON_MISSING_TARGET, verdict.codes())
        self.assertEqual(verdict.status, FeedbackStatus.REJECTED.value)

    def test_a_rating_outside_the_scale_is_rejected(self) -> None:
        _, verdict = FeedbackQualityFilter().apply(
            raw_feedback(feedback_type="rating", rating=99.0)
        )

        self.assertIn(REASON_INVALID_RATING, verdict.codes())
        self.assertEqual(verdict.status, FeedbackStatus.REJECTED.value)

    def test_a_rating_row_without_a_rating_is_rejected(self) -> None:
        _, verdict = FeedbackQualityFilter().apply(raw_feedback(feedback_type="rating"))

        self.assertIn(REASON_MISSING_RATING, verdict.codes())

    def test_a_verdict_that_carries_a_rating_is_refused(self) -> None:
        _, verdict = FeedbackQualityFilter().apply(raw_feedback(rating=0.5))

        self.assertIn(REASON_INVALID_RATING, verdict.codes())

    def test_the_same_button_twice_is_a_duplicate_held_for_review(self) -> None:
        _, verdict = FeedbackQualityFilter().apply(
            raw_feedback(), known=(raw_feedback(),)
        )

        self.assertIn(REASON_DUPLICATE, verdict.codes())
        self.assertEqual(verdict.status, FeedbackStatus.NEEDS_REVIEW.value)

    def test_opposite_verdicts_about_one_run_are_held_not_resolved(self) -> None:
        _, verdict = FeedbackQualityFilter().apply(
            raw_feedback(feedback_type="reject"),
            known=(raw_feedback(feedback_type="accept"),),
        )

        self.assertIn(REASON_CONTRADICTORY, verdict.codes())
        self.assertEqual(verdict.status, FeedbackStatus.NEEDS_REVIEW.value)

    def test_feedback_on_a_run_that_never_finished_is_held(self) -> None:
        unfinished = trajectory("traj-1", status="running", success=None)  # type: ignore[arg-type]

        _, verdict = FeedbackQualityFilter().apply(
            raw_feedback(), trajectory=unfinished
        )

        self.assertIn(REASON_TASK_UNFINISHED, verdict.codes())
        self.assertEqual(verdict.status, FeedbackStatus.NEEDS_REVIEW.value)

    def test_low_confidence_feedback_is_held(self) -> None:
        _, verdict = FeedbackQualityFilter().apply(raw_feedback(confidence=0.05))

        self.assertIn(REASON_LOW_CONFIDENCE, verdict.codes())
        self.assertEqual(verdict.status, FeedbackStatus.NEEDS_REVIEW.value)

    def test_a_correction_carrying_a_credential_is_redacted_before_storage(self) -> None:
        row, verdict = FeedbackQualityFilter().apply(
            raw_feedback(
                feedback_type="correction", correction=f"use {SECRET} instead"
            )
        )

        self.assertIsNotNone(row)
        assert row is not None
        self.assertNotIn(SECRET, row.correction)
        self.assertIn(REDACTED, row.correction)
        self.assertIn("redacted", " ".join(verdict.notes))

    def test_an_unsafe_report_is_held_not_rejected(self) -> None:
        _, verdict = FeedbackQualityFilter().apply(
            raw_feedback(feedback_type="report_unsafe", rating=0.5)
        )

        self.assertEqual(verdict.status, FeedbackStatus.NEEDS_REVIEW.value)
        self.assertIn("unsafe", " ".join(verdict.notes))

    def test_a_row_that_is_not_feedback_at_all_is_dropped(self) -> None:
        row, _ = FeedbackQualityFilter().apply(
            HumanFeedback(feedback_type="", trajectory_id="", task_id="")
        )

        self.assertIsNone(row)

    def test_the_filter_can_be_switched_off_for_an_installation(self) -> None:
        _, verdict = FeedbackQualityFilter(
            FeedbackQualityConfig(enabled=False)
        ).apply(raw_feedback(feedback_type="maybe", trajectory_id=""))

        self.assertEqual(verdict.status, FeedbackStatus.ACCEPTED.value)
        self.assertIn("switched off", " ".join(verdict.notes))

    def test_the_policy_maps_a_persons_answer_to_a_signal(self) -> None:
        policy = RewardPolicyConfig()

        self.assertEqual(feedback_reward_value(raw_feedback(), policy), 1.0)
        self.assertEqual(
            feedback_reward_value(raw_feedback(feedback_type="reject"), policy), -1.0
        )
        self.assertEqual(
            feedback_reward_value(
                raw_feedback(feedback_type="rating", rating=5.0), policy
            ),
            1.0,
        )
        self.assertEqual(
            feedback_reward_value(
                raw_feedback(feedback_type="rating", rating=1.0), policy
            ),
            -1.0,
        )

    def test_evidence_names_what_a_person_saw(self) -> None:
        self.assertIn("human_accepted", feedback_evidence(raw_feedback()))
        self.assertIn(
            "error_reported",
            feedback_evidence(raw_feedback(feedback_type="report_error")),
        )


# ── rollouts and environments ──────────────────────────────────────────────


class RolloutTests(unittest.TestCase):
    """The environment and rollout abstraction: deterministic, read-only, generic."""

    @staticmethod
    def successful_rollout() -> Any:
        environment = MockEnvironment(
            script=(({"success": True, "done": True}),), max_steps=3
        )
        policy = ScriptedPolicy([{"action": "finish"}])
        return RolloutRunner(max_steps=3).run(
            environment, policy, task={"task_id": "t"}, seed=7, task_id="t"
        )

    def test_an_episode_is_deterministic_for_a_fixed_seed(self) -> None:
        first = self.successful_rollout()
        second = self.successful_rollout()

        self.assertEqual(first.to_dict()["steps"], second.to_dict()["steps"])
        self.assertEqual(first.total_reward, second.total_reward)
        self.assertEqual(first.total_reward, 1.0)
        self.assertEqual(first.status, RolloutStatus.COMPLETED.value)

    def test_a_failure_costs_and_the_environment_is_read_only(self) -> None:
        environment = MockEnvironment(
            script=(({"success": False, "done": True}),), max_steps=2
        )

        rollout = RolloutRunner(max_steps=2).run(
            environment, ScriptedPolicy([{"action": "finish"}])
        )

        self.assertEqual(rollout.total_reward, -1.0)
        self.assertEqual(rollout.environment, "mock")
        self.assertTrue(rollout.metadata["read_only"])

    def test_the_mock_environment_never_invents_a_success(self) -> None:
        environment = MockEnvironment(max_steps=1)
        environment.reset(task={"task_id": "t"}, seed=1)

        step = environment.step({"action": "act"})

        self.assertIsNone(step["success"])
        self.assertTrue(step["done"])

    def test_a_long_episode_is_truncated_not_pretended(self) -> None:
        rollout = RolloutRunner(max_steps=2).run(
            MockEnvironment(max_steps=10), ScriptedPolicy([{"action": "act"}])
        )

        self.assertEqual(rollout.length, 2)
        self.assertEqual(rollout.status, RolloutStatus.TRUNCATED.value)

    def test_repeating_one_action_pays_a_penalty(self) -> None:
        rollout = RolloutRunner(max_steps=4).run(
            MockEnvironment(max_steps=4), ScriptedPolicy([{"action": "act"}] * 4)
        )

        rewards = rollout.step_rewards()

        self.assertEqual(len(rewards), 4)
        self.assertLess(rewards[-1], rewards[0])

    def test_a_recorded_run_projects_onto_a_one_step_rollout(self) -> None:
        rollout = single_step_rollout(trajectory(), reward=raw_reward())

        self.assertEqual(rollout.length, 1)
        self.assertTrue(rollout.steps[0].terminal)
        self.assertEqual(rollout.total_reward, 0.8)
        self.assertEqual(rollout.environment, "recorded_trajectory")

    def test_a_batch_of_rollouts_is_summarised(self) -> None:
        summary = summarise([self.successful_rollout(), self.successful_rollout()])

        self.assertEqual(summary.rollouts, 2)
        self.assertEqual(summary.mean_reward, 1.0)
        self.assertEqual(summary.by_environment, {"mock": 2})

    def test_a_reward_breakdown_is_written_beside_the_phase_fifteen_total(self) -> None:
        rollout = self.successful_rollout()

        carried = RewardPropagator(gamma=0.5).apply(rollout, trajectory())

        self.assertEqual(carried.reward_breakdown["terminal_reward"], 1.0)
        self.assertEqual(carried.reward_breakdown["gamma"], 0.5)
        self.assertEqual(dict(carried.final_result), {"summary": "calculator opened"})


# ── policy optimizers and trainers ────────────────────────────────────────


class BackendTests(unittest.TestCase):
    """The mock optimizer and the trainer plug: simulated, and loudly so."""

    def examples(self, count: int = 3) -> tuple[RewardExample, ...]:
        return tuple(
            RewardExample(
                trajectory_id=f"traj-{index}",
                task_id=f"task-{index}",
                reward=raw_reward(total_reward=0.2 * index - 0.2),
                reward_source=RewardSource.HUMAN.value,
                status=FeedbackStatus.ACCEPTED.value,
            )
            for index in range(count)
        )

    def test_the_mock_optimizer_says_it_is_a_simulation(self) -> None:
        update = MockPolicyOptimizer().optimize(
            self.examples(), config=RLTrainingConfig(reward_dataset_version="label@v1")
        )

        self.assertTrue(update.simulated)
        self.assertEqual(update.algorithm, "mock_policy")
        self.assertEqual(update.examples, 3)
        self.assertFalse(MockPolicyOptimizer().learns)
        self.assertIn("mock", " ".join(update.notes))

    def test_the_mock_optimizer_refuses_an_empty_set(self) -> None:
        with self.assertRaises(PolicyOptimizerUnavailable):
            MockPolicyOptimizer().optimize(
                (), config=RLTrainingConfig(reward_dataset_version="label@v1")
            )

    def test_a_planned_algorithm_is_refused_rather_than_faked(self) -> None:
        for algorithm in PLANNED_ALGORITHMS:
            with self.assertRaises(PolicyOptimizerUnavailable):
                policy_optimizer_for(algorithm)

        with self.assertRaises(PolicyOptimizerUnavailable):
            policy_optimizer_for("some_future_algorithm")

    def test_the_mock_optimizer_is_the_one_that_runs_today(self) -> None:
        optimizer = policy_optimizer_for("mock_policy")

        self.assertEqual(optimizer.name, "mock_policy")
        self.assertTrue(optimizer.describe()["simulated"])
        self.assertEqual(
            sorted(name for name, item in POLICY_OPTIMIZERS.items() if item["implemented"]),
            ["mock_policy"],
        )

    def test_a_dry_run_configuration_gets_the_dry_run_backend(self) -> None:
        trainer = rl_trainer_for(config_for("label@v1"))

        self.assertEqual(trainer.mode, "rlhf")
        self.assertEqual(trainer.backend, "dry_run")
        self.assertEqual(trainer.algorithm, "mock_policy")
        self.assertEqual(trainer.name, "rlhf:mock_policy")

    def test_each_mode_gets_its_own_trainer(self) -> None:
        rlhf = rl_trainer_for(config_for("label@v1", mode="rlhf", dry_run=False))
        rlaif = rl_trainer_for(config_for("label@v1", mode="rlaif", dry_run=False))

        self.assertEqual((rlhf.mode, rlhf.backend), ("rlhf", "rlhf"))
        self.assertEqual((rlaif.mode, rlaif.backend), ("rlaif", "rlaif"))

    def test_the_policy_is_described_never_loaded(self) -> None:
        trainer = rl_trainer_for(config_for("label@v1", kl_coefficient=0.1))

        policy = trainer.initialize_policy()

        self.assertFalse(policy["loaded"])
        self.assertTrue(policy["needs_reference_model"])
        self.assertEqual(policy["policy_model"], "tiny-model")
        self.assertIn("no model is loaded", policy["note"])

    def test_the_reward_machinery_is_described_never_built(self) -> None:
        trainer = rl_trainer_for(config_for("label@v1"))

        reward = trainer.initialize_reward()

        self.assertFalse(reward["built"])
        self.assertEqual(reward["provider"]["provider"], "composite")
        self.assertEqual(reward["evaluator"]["evaluator_id"], "rule")

    def test_an_rlhf_audit_needs_human_feedback_in_the_dataset(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = make_manager(tmp)
            human = build_managed_dataset(manager, mode="rlhf", count=3)
            trainer = rl_trainer_for(config_for(human.dataset_version_id, mode="rlhf"))

            audit = trainer.audit_rewards(human)

            self.assertTrue(audit.mode_ok, audit.problems)
            self.assertGreaterEqual(audit.human_backed, 1)
            self.assertGreaterEqual(audit.trusted, 1)

    def test_an_rlaif_audit_needs_ai_ratings_in_the_dataset(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = make_manager(tmp)
            ai = build_managed_dataset(manager, mode="rlaif", count=3, with_ratings=True)
            trainer = rl_trainer_for(config_for(ai.dataset_version_id, mode="rlaif"))

            audit = trainer.audit_rewards(ai)

            self.assertTrue(audit.mode_ok, audit.problems)
            self.assertGreaterEqual(audit.ai_backed, 1)

    def test_an_rlaif_audit_refuses_a_dataset_with_no_ratings(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = make_manager(tmp)
            human_only = build_managed_dataset(manager, mode="mixed", count=3)
            trainer = rl_trainer_for(
                config_for(human_only.dataset_version_id, mode="rlaif")
            )

            audit = trainer.audit_rewards(human_only)

            self.assertFalse(audit.mode_ok)
            self.assertTrue(any("mode=rlaif" in item for item in audit.problems))

    def test_a_real_run_without_dependencies_or_a_runner_is_refused(self) -> None:
        trainer = rl_trainer_for(
            config_for("label@v1", dry_run=False), capabilities=bare()
        )

        self.assertFalse(trainer.is_available())
        self.assertTrue(trainer.missing_dependencies())


# ── planning, simulation, resources and evaluation ──────────────────────


def supervised_dataset(count: int = 6) -> Any:
    """A minimal Phase 16 dataset, as an RL comparison runs on held-out examples."""
    examples = tuple(
        SFTTrainingExample(
            example_id=f"ex-{index}",
            dataset_type=DatasetType.NLU.value,
            input={"request": f"open app {index}"},
            context={},
            target={"intent": "open_application", "confidence": 0.9},
            metadata={"group_key": f"g{index}", "split": "test"},
            source_trajectory_id=f"traj-{index}",
            tags=("nlu",),
        )
        for index in range(count)
    )
    return SFTDatasetVersion(
        dataset_version_id="probe@1.0.0",
        name="probe",
        version="1.0.0",
        dataset_type=DatasetType.NLU.value,
        examples=examples,
        splits={"test": tuple(item.example_id for item in examples)},
        statistics=DatasetStatistics(
            total=count, by_split={"test": count}, estimated_tokens=200
        ),
        source_data_version="probe=1.0.0",
    )


class GoodPredictor:
    """A model that answers every example exactly as the dataset does."""

    name = "candidate"

    def predict(self, example: Any) -> Mapping[str, Any]:
        return dict(example.target)


class BadPredictor:
    """A model that answers confidently and wrongly."""

    name = "base"

    def predict(self, example: Any) -> Mapping[str, Any]:
        return {"intent": "chat", "confidence": 0.1}


class PipelineAndResourceTests(unittest.TestCase):
    """Planning, simulation, estimation — all before anything starts."""

    def test_a_plan_blocked_on_a_missing_dataset_names_the_stage(self) -> None:
        pipeline = RLPipeline(trainer_factory=lambda config: rl_trainer_for(config))

        plan = pipeline.plan(config_for("missing@v1"))
        payload = plan.to_dict()

        self.assertFalse(plan.ok)
        self.assertIn("data_preparation", plan.blocked())
        self.assertEqual([item["stage"] for item in payload["stages"]], list(PIPELINE_STAGES))
        self.assertEqual(payload["stages"][0]["status"], "blocked")

    def test_a_plan_with_a_built_dataset_is_ready(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = dry_run_manager(tmp)
            dataset = build_managed_dataset(manager, count=6)
            pipeline = RLPipeline(
                trainer_factory=lambda config: rl_trainer_for(config),
                dataset_lookup=manager.reward_dataset,
            )

            plan = pipeline.plan(config_for(dataset.dataset_version_id))

            self.assertTrue(plan.ok, plan.blocked())
            self.assertEqual(plan.to_dict()["stages"][0]["status"], "ready")

    def test_a_dry_run_provider_simulation_says_which_rows_it_could_measure(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = dry_run_manager(tmp)
            dataset = build_managed_dataset(manager, count=3)
            pipeline = RLPipeline(trainer_factory=lambda config: rl_trainer_for(config))
            provider = provider_for_config(config_for(dataset.dataset_version_id))

            simulated = pipeline.simulate_rewards(dataset, provider)

            self.assertTrue(simulated["simulated"])
            self.assertEqual(simulated["examples"], simulated["measured"])
            self.assertEqual(simulated["unavailable"], 0)
            self.assertEqual(simulated["recorded"], 0)
            for row in simulated["readings"]:
                self.assertEqual(row["origin"], "derived")

    def test_a_dry_run_rollout_simulation_is_deterministic_and_read_only(self) -> None:
        pipeline = RLPipeline(trainer_factory=lambda config: rl_trainer_for(config))

        simulated = pipeline.simulate_rollouts(config_for("label@v1", rollout_count=3))

        self.assertTrue(simulated["simulated"])
        self.assertEqual(simulated["rollouts"], 3)
        self.assertEqual(simulated["environments"], {"mock": 3})
        self.assertTrue(simulated["sample"]["metadata"]["read_only"])

    def test_checkpoint_settings_are_validated_before_a_run(self) -> None:
        validated = RLPipeline.validate_checkpoints(config_for("label@v1"))

        self.assertTrue(validated["ok"])
        self.assertEqual(validated["max_checkpoints"], 2)
        self.assertIn("best checkpoint kept", validated["detail"])

    def test_an_estimate_on_a_bare_machine_needs_no_cuda(self) -> None:
        estimator = RLResourceEstimator(capabilities=bare())

        estimate = estimator.estimate(config_for("label@v1"))
        payload = estimate.to_dict()
        summary = estimator.summary()

        self.assertEqual(payload["level"], ResourceVerdict.WARNING.value)
        self.assertIn("experience_buffer", payload["components"])
        self.assertIn("reward_dataset", payload["components"])
        self.assertFalse(summary["cuda_required"])
        self.assertEqual(summary["algorithms"]["implemented"], ["mock_policy"])
        self.assertTrue(summary["dependencies"]["missing"])

    def test_a_kl_term_adds_a_frozen_reference_policy(self) -> None:
        estimator = RLResourceEstimator(capabilities=ready())

        without = estimator.estimate(config_for("label@v1"))
        with_reference = estimator.estimate(
            config_for("label@v1", kl_coefficient=0.1, base_model_size_bytes=1_000_000)
        )

        self.assertNotIn("reference_model", without.to_dict()["components"])
        self.assertIn("reference_model", with_reference.to_dict()["components"])
        self.assertIn(
            "frozen reference policy", " ".join(with_reference.reasons)
        )

    def test_an_impossible_configuration_reads_as_unsafe(self) -> None:
        estimator = RLResourceEstimator(capabilities=bare())

        estimate = estimator.estimate(
            config_for(
                "label@v1",
                dry_run=False,
                base_model_size_bytes=200_000_000_000,
            )
        )

        self.assertEqual(estimate.level, ResourceVerdict.UNSAFE.value)
        self.assertTrue(estimate.reasons)

    def test_a_candidate_that_matches_the_dataset_passes_on_behaviour(self) -> None:
        outcome = RLModelEvaluator().compare(
            supervised_dataset(), base=BadPredictor(), candidate=GoodPredictor()
        )

        self.assertEqual(outcome.verdict, "pass")
        self.assertFalse(outcome.details["reward_metrics_consulted"])

    def test_a_regression_against_the_base_model_is_not_hidden(self) -> None:
        outcome = RLModelEvaluator().compare(
            supervised_dataset(), base=GoodPredictor(), candidate=BadPredictor()
        )

        self.assertEqual(outcome.verdict, "regress")
        self.assertTrue(outcome.regressions)
        report = regression_report(outcome)
        self.assertEqual(report["verdict"], "regress")
        self.assertFalse(report["reward_metrics_consulted"])
        self.assertIn("regressions", report)


# ── the manager: feedback, ratings, disagreements ────────────────────────


class ManagerFeedbackTests(unittest.TestCase):
    """Human feedback lives, is screened, is never deleted, and is decidable."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.publisher = RecordingPublisher()
        self.manager = dry_run_manager(self._tmp.name, publish=self.publisher)

    def test_a_submitted_row_is_screened_and_stored_with_its_verdict(self) -> None:
        result = self.manager.submit_feedback(
            feedback_type="accept", trajectory_id="traj-1", confidence=0.9
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["stored"])
        self.assertEqual(result["feedback"]["status"], "accepted")
        self.assertFalse(result["review_required"])
        self.assertIn(RLHF_FEEDBACK_RECEIVED, self.publisher.types())

    def test_a_duplicate_is_held_and_a_person_can_settle_it(self) -> None:
        self.manager.submit_feedback(feedback_type="accept", trajectory_id="traj-1")

        duplicate = self.manager.submit_feedback(
            feedback_type="accept", trajectory_id="traj-1"
        )
        held = self.manager.pending_feedback()
        settled = self.manager.decide_feedback(
            duplicate["feedback"]["feedback_id"], "accept", reviewer="ana"
        )

        self.assertTrue(duplicate["stored"])
        self.assertEqual(duplicate["feedback"]["status"], "needs_review")
        self.assertTrue(duplicate["review_required"])
        self.assertGreaterEqual(len(held), 1)
        self.assertTrue(settled["ok"])
        self.assertEqual(settled["feedback"]["status"], "accepted")

    def test_a_rejected_row_is_stored_rather_than_deleted(self) -> None:
        result = self.manager.submit_feedback(
            feedback_type="maybe", trajectory_id="traj-1"
        )
        listed = self.manager.feedback_list()

        self.assertTrue(result["stored"])
        self.assertFalse(result["ok"])
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["status"], "rejected")
        codes = [issue["code"] for issue in result["verdict"]["issues"]]
        self.assertIn(REASON_UNKNOWN_TYPE, codes)

    def test_a_decision_without_a_usable_answer_is_refused(self) -> None:
        refused = self.manager.decide_feedback("nope", "maybe")

        self.assertFalse(refused["ok"])
        self.assertIn("no feedback", refused["reason"])

    def test_a_rating_is_asked_of_the_rule_evaluator_and_stored(self) -> None:
        result = self.manager.rate(trajectory())

        self.assertTrue(result["ok"], result.get("reason"))
        rating = result["rating"]
        self.assertEqual(rating["evaluator_id"], "rule")
        self.assertEqual(len(rating["criteria"]), len(DEFAULT_CRITERIA))
        self.assertTrue(rating["evidence"])
        self.assertEqual(self.manager.rating_stats()["rows"], 1)
        self.assertIn(RLHF_RATING_CREATED, self.publisher.types())

    def test_a_rating_can_be_asked_for_without_storing_it(self) -> None:
        result = self.manager.rate(trajectory(), save=False)

        self.assertTrue(result["ok"])
        self.assertFalse(result["stored"])
        self.assertEqual(self.manager.rating_stats()["rows"], 0)

    def test_an_evaluator_that_is_switched_off_refuses_to_rate(self) -> None:
        result = self.manager.rate(trajectory(), evaluator="none")

        self.assertFalse(result["ok"])
        self.assertIn("evaluator", result["reason"].lower())

    def test_a_person_and_an_evaluator_that_disagree_are_recorded(self) -> None:
        self.manager.submit_feedback(
            feedback_type="reject", trajectory_id="traj-1", confidence=0.9
        )
        self.manager.rate(trajectory(), save=True)

        found = self.manager.detect_disagreements()
        listed = self.manager.disagreements_list()

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["kind"], "human_vs_ai")
        self.assertEqual(found[0]["recommended"], "review")
        self.assertEqual(len(listed), 1)
        self.assertEqual(
            self.manager.disagreement_stats()["by_kind"], {"human_vs_ai": 1}
        )
        self.assertIn(RLHF_DISAGREEMENT_FOUND, self.publisher.types())

    def test_two_sources_that_agree_are_not_a_disagreement(self) -> None:
        self.manager.submit_feedback(
            feedback_type="accept", trajectory_id="traj-1", confidence=0.9
        )
        self.manager.rate(trajectory(), save=True)

        self.assertEqual(self.manager.detect_disagreements(), [])

    def test_the_feedback_store_survives_a_rebuild(self) -> None:
        self.manager.submit_feedback(feedback_type="accept", trajectory_id="traj-1")
        rebuilt = dry_run_manager(self._tmp.name)

        self.assertEqual(len(rebuilt.feedback_list()), 1)
        self.assertEqual(rebuilt.feedback_list()[0]["trajectory_id"], "traj-1")


# ── the manager: reward datasets ───────────────────────────────────────────


class ManagerDatasetTests(unittest.TestCase):
    """Reward datasets are built from recorded behaviour, audited and split."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.publisher = RecordingPublisher()
        self.manager = dry_run_manager(self._tmp.name, publish=self.publisher)

    def test_a_dataset_keeps_both_sources_and_their_provenance(self) -> None:
        dataset = build_managed_dataset(self.manager, mode="mixed", count=6)

        self.assertEqual(dataset.name, "label")
        self.assertEqual(dataset.dataset_version_id, "label@v1")
        self.assertEqual(dataset.mode, "mixed")
        self.assertEqual(len(dataset.examples), 6)
        self.assertEqual(len(dataset.accepted_examples()), 6)
        self.assertTrue(dataset.splits["train"])
        self.assertEqual(dict(dataset.sources), {"composite": 6})
        self.assertEqual(dataset.reward_version, RLHF_VERSION)
        self.assertIn(RLHF_DATASET_BUILT, self.publisher.types())

    def test_an_rlhf_dataset_keeps_what_people_said(self) -> None:
        dataset = build_managed_dataset(self.manager, mode="rlhf", count=4)

        self.assertEqual(dataset.mode, "rlhf")
        self.assertEqual(len(dataset.accepted_examples()), 4)
        for row in dataset.accepted_examples():
            self.assertTrue(row.feedback_ids)

    def test_an_rlaif_dataset_keeps_what_evaluators_said(self) -> None:
        dataset = build_managed_dataset(
            self.manager, mode="rlaif", count=4, with_ratings=True
        )

        self.assertEqual(dataset.mode, "rlaif")
        self.assertEqual(len(dataset.accepted_examples()), 4)
        for row in dataset.accepted_examples():
            self.assertTrue(row.rating_ids)

    def test_a_mixed_dataset_keeps_both_kinds_of_row(self) -> None:
        """``mixed`` asks for no particular source, so a person-only row, an
        evaluator-only row and a silently recorded one all belong in it."""
        dataset = self.manager.build_dataset(
            "mix",
            mode="mixed",
            version="v1",
            trajectories=tuple(
                trajectory(f"mix-{index}", task_id=f"mixtask-{index}")
                for index in range(3)
            ),
            feedback=(
                raw_feedback(trajectory_id="mix-0", task_id="mixtask-0"),
            ),
            ratings=(
                raw_rating(trajectory_id="mix-1", task_id="mixtask-1"),
            ),
        )
        rows = {row.trajectory_id: row for row in dataset.accepted_examples()}

        self.assertEqual(len(rows), 3)
        self.assertEqual(rows["mix-0"].mode, "rlhf")
        self.assertEqual(rows["mix-1"].mode, "rlaif")
        self.assertEqual(rows["mix-2"].mode, "")

    def test_a_row_a_person_and_an_evaluator_both_judged_fits_either_mode(self) -> None:
        """A row carrying both signals is not a third kind of thing: an RLHF
        dataset has the person it asked for, an RLAIF one the evaluator."""
        def built(mode: str) -> dict[str, str]:
            dataset = self.manager.build_dataset(
                f"both-{mode}",
                mode=mode,
                version="v1",
                trajectories=(trajectory("both-0", task_id="bothtask-0"),),
                feedback=(
                    raw_feedback(trajectory_id="both-0", task_id="bothtask-0"),
                ),
                ratings=(
                    raw_rating(trajectory_id="both-0", task_id="bothtask-0"),
                ),
            )
            return {row.trajectory_id: row.status for row in dataset.examples}

        self.assertEqual(built("rlhf"), {"both-0": FeedbackStatus.ACCEPTED.value})
        self.assertEqual(built("rlaif"), {"both-0": FeedbackStatus.ACCEPTED.value})

    def test_an_rlhf_dataset_refuses_a_row_only_an_evaluator_endorsed(self) -> None:
        dataset = self.manager.build_dataset(
            "strict",
            mode="rlhf",
            version="v1",
            trajectories=(trajectory("ai-only", task_id="aitask"),),
            feedback=(),
            ratings=(raw_rating(trajectory_id="ai-only", task_id="aitask"),),
        )
        row = dataset.examples[0]

        self.assertEqual(row.status, FeedbackStatus.REJECTED.value)
        self.assertEqual(
            set(row.reasons), {REASON_HUMAN_REQUIRED, REASON_MODE_MISMATCH}
        )
        self.assertEqual(dataset.splits["train"], ())

    def test_a_stored_dataset_validates_and_carries_no_hidden_reasoning(self) -> None:
        dataset = build_managed_dataset(self.manager, count=5)

        validated = self.manager.validate_dataset(dataset.dataset_version_id)
        loaded = self.manager.reward_dataset(dataset.dataset_version_id)

        self.assertTrue(validated["ok"], validated["issues"])
        self.assertIsNotNone(loaded)
        for row in dataset.examples:
            for key in REASONING_KEYS:
                self.assertNotIn(key, row.candidate)

    def test_a_dataset_without_a_mode_defaults_to_the_deployment_mode(self) -> None:
        dataset = build_managed_dataset(self.manager, mode="", count=2)

        self.assertEqual(dataset.mode, "rlhf")

    def test_nothing_recorded_builds_an_empty_version_rather_than_a_guess(self) -> None:
        dataset = self.manager.build_dataset("empty", mode="rlhf")

        self.assertEqual(len(dataset.examples), 0)
        validated = self.manager.validate_dataset(dataset.dataset_version_id)

        self.assertFalse(validated["ok"])
        self.assertIn("no examples", " ".join(validated["issues"]))

    def test_a_held_row_is_listed_so_a_person_can_see_why(self) -> None:
        suspicious = raw_reward(total_reward=0.95)
        integrity = RewardIntegrityChecker().check(
            suspicious,
            IntegrityContext(subject_id="traj-1", task_succeeded=True, verification_total=0),
        )
        example = RewardExample(
            trajectory_id="traj-1",
            task_id="task-1",
            task={"request": "open calculator"},
            candidate={"summary": "calculator opened"},
            reward=suspicious,
            reward_source=RewardSource.RULE.value,
            integrity=integrity,
        )

        dataset = self.manager.build_dataset("held", examples=(example,), mode="mixed")
        held = self.manager.held_examples(dataset.dataset_version_id)

        self.assertEqual(len(held), 1)
        self.assertIn("integrity", " ".join(held[0].reasons))

    def test_a_version_is_never_overwritten(self) -> None:
        first = build_managed_dataset(self.manager, count=3, version="v1")
        second = build_managed_dataset(self.manager, count=3, version="")

        self.assertEqual(first.dataset_version_id, "label@v1")
        self.assertNotEqual(second.dataset_version_id, first.dataset_version_id)
        self.assertEqual(len(self.manager.reward_datasets_list()), 2)


# ── the manager: runs, dry run, registry ───────────────────────────────────


class ManagerRunTests(unittest.TestCase):
    """Runs are created explicitly, dry by default, refused when real."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.publisher = RecordingPublisher()
        self.manager = dry_run_manager(self._tmp.name, publish=self.publisher)

    def dataset(self, **overrides: Any) -> RewardDatasetVersion:
        return build_managed_dataset(self.manager, **overrides)

    def test_a_run_is_created_from_a_built_dataset_and_stays_created(self) -> None:
        built = self.dataset(count=4)

        run = self.manager.create_run("tiny-model", built.dataset_version_id)

        self.assertEqual(run.algorithm, "rlhf")
        self.assertEqual(run.status, TrainingRunStatus.CREATED.value)
        self.assertEqual(run.backend, "dry_run")
        self.assertEqual(run.rl_metrics["mode"], "rlhf")
        self.assertEqual(run.rl_metrics["algorithm"], "mock_policy")
        self.assertTrue(run.rl_metrics["simulated"])
        self.assertEqual(
            run.rl_metrics["reward_audit"]["accepted"], len(built.accepted_examples())
        )

    def test_a_run_cannot_be_created_from_a_missing_dataset(self) -> None:
        with self.assertRaises(ValueError):
            self.manager.create_run("tiny-model", "nope@v1")

    def test_an_rlhf_run_refuses_a_dataset_with_only_ai_feedback(self) -> None:
        built = self.manager.build_dataset(
            "ai-only",
            mode="rlaif",
            version="v1",
            trajectories=tuple(
                trajectory(f"ai-{index}", task_id=f"aitask-{index}")
                for index in range(3)
            ),
            feedback=(),
            ratings=tuple(
                raw_rating(trajectory_id=f"ai-{index}", task_id=f"aitask-{index}")
                for index in range(3)
            ),
        )

        with self.assertRaises(ValueError) as caught:
            self.manager.create_run("tiny-model", built.dataset_version_id)

        self.assertIn("human", str(caught.exception))

    def test_an_rlaif_run_refuses_a_dataset_with_only_human_feedback(self) -> None:
        built = self.dataset(mode="rlhf", count=3)

        with self.assertRaises(ValueError) as caught:
            self.manager.create_run(
                "tiny-model", built.dataset_version_id, config={"mode": "rlaif"}
            )

        self.assertIn("ai", str(caught.exception).lower())

    def test_a_dry_run_walks_the_whole_pipeline_and_starts_nothing(self) -> None:
        built = self.dataset(count=4)

        preview = self.manager.dry_run("tiny-model", built.dataset_version_id, mode="rlhf")

        self.assertTrue(preview["ok"], preview["errors"])
        self.assertFalse(preview["started"])
        self.assertEqual(preview["mode"], "rlhf")
        self.assertEqual(preview["algorithm"], "mock_policy")
        self.assertEqual(preview["reward_audit"]["accepted"], 4)
        self.assertGreaterEqual(preview["reward_simulation"]["measured"], 1)
        self.assertTrue(preview["checkpoints"]["ok"])
        self.assertEqual(preview["backend"]["backend"], "dry_run")
        self.assertTrue(preview["policy_optimizer"]["implemented"])
        self.assertTrue(preview["plan"]["ok"])
        self.assertEqual(self.manager.rl_runs(), ())
        self.assertIn(RLHF_DRY_RUN, self.publisher.types())

    def test_a_dry_run_on_a_missing_dataset_is_false_not_a_crash(self) -> None:
        preview = self.manager.dry_run("tiny-model", "missing@v1")

        self.assertFalse(preview["ok"])
        self.assertFalse(preview["started"])
        self.assertTrue(any("missing@v1" in error for error in preview["errors"]))

    def test_a_started_dry_run_completes_with_metrics_checkpoints_and_a_model(self) -> None:
        built = self.dataset(count=4)
        run = self.manager.create_run("tiny-model", built.dataset_version_id)

        started = self.manager.start(run.run_id)
        stored = self.manager.runs.get(run.run_id)
        checkpoints = self.manager.checkpoints(run.run_id)
        models = [
            item
            for item in self.manager.models_list(limit=0)
            if item.algorithm == "rlhf"
        ]
        status = self.manager.status()

        self.assertTrue(started["ok"], started)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.status, TrainingRunStatus.COMPLETED.value)
        self.assertEqual(stored.rl_metrics["mode"], "rlhf")
        self.assertEqual(stored.rl_metrics["algorithm"], "mock_policy")
        self.assertGreaterEqual(len(checkpoints), 1)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].status, "experimental")
        self.assertEqual(models[0].algorithm, "rlhf")
        self.assertEqual(status["runs"]["by_mode"], {"rlhf": 1, "rlaif": 0})
        self.assertEqual(status["datasets"]["count"], 1)

    def test_a_real_run_is_refused_without_dependencies_and_a_runner(self) -> None:
        built = self.dataset(count=3)
        run = self.manager.create_run(
            "tiny-model", built.dataset_version_id, config={"dry_run": False}
        )

        refused = self.manager.start(run.run_id)
        stored = self.manager.runs.get(run.run_id)

        self.assertFalse(refused["ok"])
        self.assertTrue(refused["reason"])
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertNotEqual(stored.status, TrainingRunStatus.COMPLETED.value)

    def test_an_unsafe_estimate_is_refused_unless_the_deployment_allows_it(self) -> None:
        # A known machine keeps the verdict deterministic: CI has no psutil, and
        # an unmeasured memory reading downgrades UNSAFE to WARNING.
        manager = dry_run_manager(
            self._tmp.name,
            estimator=ResourceEstimator(hardware=bare()),
            publish=self.publisher,
        )
        built = build_managed_dataset(manager, count=3)
        run = manager.create_run(
            "tiny-model",
            built.dataset_version_id,
            config={"base_model_size_bytes": 200_000_000_000},
        )

        refused = manager.start(run.run_id)

        self.assertFalse(refused["ok"])
        self.assertTrue(refused.get("refused"), refused)

    def test_the_registry_approval_stays_explicit(self) -> None:
        built = self.dataset(count=3)
        run = self.manager.create_run("tiny-model", built.dataset_version_id)
        self.manager.start(run.run_id)
        model = self.manager.models_list(limit=0)[0]

        refused = self.manager.approve_model(model.model_id, note="no evaluation yet")
        promoted = self.manager.promote_model(model.model_id, note="too early")

        self.assertFalse(refused["ok"])
        self.assertTrue(refused["reason"])
        self.assertFalse(promoted["ok"])

    def test_a_rejected_candidate_records_who_said_so_and_why(self) -> None:
        built = self.dataset(count=3)
        run = self.manager.create_run("tiny-model", built.dataset_version_id)
        self.manager.start(run.run_id)
        model = self.manager.models_list(limit=0)[0]

        rejected = self.manager.reject_model(model.model_id, reason="behaved worse")

        self.assertTrue(rejected["ok"], rejected)
        stored = self.manager.model(model.model_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.status, "rejected")

    def test_an_evaluation_needs_two_models_to_measure(self) -> None:
        built = self.dataset(count=3)
        run = self.manager.create_run("tiny-model", built.dataset_version_id)
        self.manager.start(run.run_id)

        refused = self.manager.evaluate_run(run.run_id)

        self.assertFalse(refused["ok"])
        self.assertIn("base", refused["reason"])

    def test_a_comparison_without_a_supervised_dataset_is_refused(self) -> None:
        refused = self.manager.compare_models(
            "absent@1.0.0", base=BadPredictor(), candidate=GoodPredictor()
        )

        self.assertFalse(refused["ok"])
        self.assertIn("no evaluation dataset", refused["reason"])

    def test_a_stored_comparison_is_measured_and_recorded(self) -> None:
        built = self.dataset(count=3)
        self.manager.datasets.save(supervised_dataset())
        run = self.manager.create_run("tiny-model", built.dataset_version_id)
        self.manager.start(run.run_id)

        compared = self.manager.compare_models(
            "probe@1.0.0",
            base=BadPredictor(),
            candidate=GoodPredictor(),
            run_id=run.run_id,
        )
        stored = self.manager.rl_evaluations_list()

        self.assertTrue(compared["ok"], compared)
        self.assertEqual(compared["verdict"], "pass")
        self.assertFalse(compared["report"]["reward_metrics_consulted"])
        self.assertEqual(len(stored), 1)
        self.assertIn(RLHF_COMPARISON, self.publisher.types())

    def test_an_estimate_prices_a_configuration_without_a_run(self) -> None:
        built = self.dataset(count=3)

        priced = self.manager.estimate_config(
            config_for(built.dataset_version_id).to_mapping()
        )

        self.assertTrue(priced["valid"], priced["errors"])
        self.assertEqual(priced["algorithm"], "mock_policy")
        self.assertEqual(priced["dataset"]["accepted"], 3)
        self.assertIn("estimate", priced)

    def test_an_invalid_configuration_is_priced_and_refused(self) -> None:
        priced = self.manager.estimate_config({"algorithm": "ppo"})

        self.assertFalse(priced["valid"])
        self.assertTrue(any("not implemented" in item for item in priced["errors"]))

    def test_the_algorithms_report_names_the_mock_and_the_planned(self) -> None:
        report = self.manager.algorithms()

        self.assertEqual(report["implemented"], ["mock_policy"])
        self.assertEqual(sorted(report["policy_optimizers"]), ["grpo", "mock_policy", "ppo"])
        self.assertFalse(report["cuda_required"])
        self.assertTrue(report["dry_run_supported"])

    def test_the_runs_are_listable_by_mode(self) -> None:
        built = self.dataset(count=3)
        self.manager.create_run("tiny-model", built.dataset_version_id)

        self.assertEqual(len(self.manager.rl_runs(mode="rlhf")), 1)
        self.assertEqual(len(self.manager.rl_runs(mode="rlaif")), 0)

    def test_a_created_run_can_be_cancelled_before_it_starts(self) -> None:
        built = self.dataset(count=3)
        run = self.manager.create_run("tiny-model", built.dataset_version_id)

        cancelled = self.manager.cancel(run.run_id)
        stored = self.manager.runs.get(run.run_id)

        self.assertTrue(cancelled["ok"], cancelled)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.status, TrainingRunStatus.CANCELLED.value)

    def test_a_completed_run_cannot_be_started_again(self) -> None:
        built = self.dataset(count=3)
        run = self.manager.create_run("tiny-model", built.dataset_version_id)
        self.manager.start(run.run_id)

        again = self.manager.start(run.run_id)

        self.assertFalse(again["ok"])
        self.assertIn("already", again["reason"])


# ── the runtime module ─────────────────────────────────────────────────────


class RLHFModuleTests(unittest.IsolatedAsyncioTestCase):
    """The module answers questions on the bus and starts nothing there."""

    async def asyncSetUp(self) -> None:
        from novacontrol.core.events import EventBus

        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.manager = dry_run_manager(self._tmp.name)
        self.module = RLHFModule(self.manager)
        self.bus = EventBus()
        await self.module.start(self.bus)
        self.addCleanup(self._stop)

    async def _stop(self) -> None:
        await self.module.stop()

    async def replies(
        self, request: str, payload: dict[str, Any], completed: str
    ) -> list[dict[str, Any]]:
        from novacontrol.core.events import Event

        found: list[dict[str, Any]] = []

        async def handler(event: Event) -> None:
            found.append(dict(event.payload))

        await self.bus.subscribe(completed, handler)
        await self.bus.publish(Event(type=request, payload=payload, source="test"))
        await asyncio.sleep(0)
        return found

    def test_the_module_names_itself_and_declares_what_it_answers(self) -> None:
        self.assertEqual(self.module.name, "rlhf")
        names = {capability.name for capability in self.module.capabilities}

        self.assertEqual(
            names,
            {
                "rlhf.status",
                "rlhf.feedback",
                "rlhf.ratings",
                "rlhf.disagreements",
                "rlhf.datasets",
                "rlhf.algorithms",
                "rlhf.estimate",
                "rlhf.pipeline",
            },
            "no capability starts a run or decides a piece of feedback",
        )

    async def test_a_status_request_is_answered(self) -> None:
        replies = await self.replies(STATUS_REQUESTED, {}, STATUS_COMPLETED)

        self.assertTrue(replies)
        self.assertIn("feedback", replies[0])
        self.assertEqual(replies[0]["modes"], ["rlhf", "rlaif"])

    async def test_a_feedback_request_is_answered_with_rows_and_stats(self) -> None:
        self.manager.submit_feedback(feedback_type="accept", trajectory_id="traj-1")

        replies = await self.replies(
            FEEDBACK_REQUESTED, {"limit": 5}, FEEDBACK_COMPLETED
        )

        self.assertEqual(len(replies[0]["feedback"]), 1)
        self.assertEqual(replies[0]["stats"]["by_status"], {"accepted": 1})

    async def test_a_datasets_request_is_answered_with_the_stored_versions(self) -> None:
        build_managed_dataset(self.manager, count=2)

        replies = await self.replies(
            DATASETS_REQUESTED, {"limit": 5}, DATASETS_COMPLETED
        )

        self.assertEqual(
            [item["dataset_version_id"] for item in replies[0]["datasets"]],
            ["label@v1"],
        )

    async def test_an_algorithms_request_describes_both_modes(self) -> None:
        replies = await self.replies(ALGORITHMS_REQUESTED, {}, ALGORITHMS_COMPLETED)

        self.assertEqual(sorted(replies[0]["modes"]), ["rlaif", "rlhf"])
        self.assertEqual(replies[0]["implemented"], ["mock_policy"])

    async def test_an_estimate_request_is_answered_without_creating_a_run(self) -> None:
        replies = await self.replies(
            ESTIMATE_REQUESTED,
            {"config": {"reward_dataset_version": "label@v1"}},
            ESTIMATE_COMPLETED,
        )

        self.assertIn("estimate", replies[0])
        self.assertEqual(self.manager.rl_runs(), ())

    async def test_a_pipeline_request_is_answered_with_ten_stages(self) -> None:
        replies = await self.replies(PIPELINE_REQUESTED, {}, PIPELINE_COMPLETED)

        self.assertEqual(
            [item["stage"] for item in replies[0]["plan"]["stages"]],
            list(PIPELINE_STAGES),
        )


# ── the application: wired, dry, nothing starts by itself ─────────────────


class ApplicationRLHFTests(unittest.IsolatedAsyncioTestCase):
    """The live application: the subsystem is wired and the rails hold."""

    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.app = NovaControlApplication(data_dir=self._tmp.name)
        await self.app.start()
        self.addCleanup(self._stop)

    async def _stop(self) -> None:
        await self.app.stop()

    def recorded(self, index: int = 0) -> None:
        self.app.evaluation.record(
            trajectory(
                f"traj-{index}", task_id=f"task-{index}", request=f"open app {index}"
            )
        )
        self.app.submit_rlhf_feedback(
            {
                "feedback_type": "accept",
                "trajectory_id": f"traj-{index}",
                "confidence": 0.9,
            }
        )

    def built_dataset(self, count: int = 3, **overrides: Any) -> str:
        for index in range(count):
            self.recorded(index)
        created = self.app.create_rlhf_dataset("probe", mode="rlhf", **overrides)
        self.assertTrue(created["ok"], created.get("reason"))
        return created["dataset"]["dataset_version_id"]

    def test_the_subsystem_is_wired_and_dry_by_default(self) -> None:
        status = self.app.rlhf_status()

        self.assertTrue(status["enabled"])
        self.assertTrue(status["defaults"]["dry_run"])
        self.assertEqual(status["defaults"]["algorithm"], "mock_policy")
        self.assertEqual(status["datasets"]["count"], 0)
        self.assertEqual(status["runs"]["count"], 0)
        self.assertIn("implemented", self.app.rlhf_algorithms())

    def test_feedback_recorded_by_a_person_becomes_a_dataset(self) -> None:
        dataset_version = self.built_dataset()

        listed = self.app.rlhf_datasets()
        validated = self.app.validate_rlhf_dataset(dataset_version)
        dataset = self.app.rlhf_dataset(dataset_version)
        held = self.app.rlhf_held(dataset_version)

        self.assertEqual(listed["count"], 1)
        self.assertTrue(validated["ok"], validated["issues"])
        self.assertEqual(len(dataset["examples"]), 3)
        self.assertEqual(dataset["statistics"]["accepted"], 3)
        self.assertEqual(held["count"], 0)
        with self.assertRaises(KeyError):
            self.app.rlhf_dataset("nope@v1")

    def test_a_run_is_created_and_waits_until_it_is_started(self) -> None:
        dataset_version = self.built_dataset()

        created = self.app.create_rlhf_run("tiny-model", dataset_version)

        self.assertTrue(created["ok"], created.get("reason"))
        self.assertEqual(created["run"]["algorithm"], "rlhf")
        self.assertEqual(created["run"]["status"], "created")
        self.assertEqual(self.app.rlhf_runs()["count"], 1)

    async def test_a_dry_run_trains_through_the_application(self) -> None:
        dataset_version = self.built_dataset()
        created = self.app.create_rlhf_run("tiny-model", dataset_version)
        run_id = created["run"]["run_id"]

        started = await self.app.start_rlhf_run(run_id)
        checkpoints = self.app.rlhf_checkpoints(run_id)
        found = self.app.rlhf_run(run_id)
        models = self.app.rlhf_models()

        self.assertTrue(started["ok"], started)
        self.assertEqual(started["run"]["status"], TrainingRunStatus.COMPLETED.value)
        self.assertGreaterEqual(checkpoints["count"], 1)
        self.assertEqual(found["status"], TrainingRunStatus.COMPLETED.value)
        self.assertEqual(found["rl_metrics"]["mode"], "rlhf")
        self.assertEqual(models["count"], 1)
        self.assertEqual(models["models"][0]["algorithm"], "rlhf")
        with self.assertRaises(KeyError):
            self.app.rlhf_run("nope")

    async def test_the_four_model_comparison_reports_a_measured_verdict(self) -> None:
        dataset_version = self.built_dataset()
        created = self.app.create_rlhf_run("tiny-model", dataset_version)
        run_id = created["run"]["run_id"]
        await self.app.start_rlhf_run(run_id)
        self.app.training.datasets.save(supervised_dataset())

        compared = await self.app.evaluate_rlhf_run(
            run_id,
            base=BadPredictor(),
            candidate=GoodPredictor(),
            dataset_version="probe@1.0.0",
        )
        stored = self.app.rlhf_evaluations()

        self.assertTrue(compared["ok"], compared)
        self.assertEqual(compared["verdict"], "pass")
        self.assertFalse(
            compared["evaluation"]["details"]["reward_metrics_consulted"]
        )
        self.assertGreaterEqual(stored["count"], 1)

    def test_a_dry_run_preview_starts_nothing_and_validates_everything(self) -> None:
        dataset_version = self.built_dataset()

        preview = self.app.dry_run_rlhf("tiny-model", dataset_version, mode="rlhf")

        self.assertTrue(preview["ok"], preview["errors"])
        self.assertFalse(preview["started"])
        self.assertEqual(preview["reward_audit"]["accepted"], 3)
        self.assertEqual(self.app.rlhf_runs()["count"], 0)

    def test_a_pipeline_plan_is_served_for_a_dataset(self) -> None:
        dataset_version = self.built_dataset()

        planned = self.app.rlhf_pipeline(
            {"base_model": "tiny-model"}, dataset_version=dataset_version
        )

        self.assertTrue(planned["ok"], planned)
        self.assertEqual(
            [item["stage"] for item in planned["plan"]["stages"]],
            list(PIPELINE_STAGES),
        )
        # A plan is a plan: it names the work, is dry by default, and starts none of it.
        self.assertEqual(planned["plan"]["blocked"], [])
        self.assertEqual(planned["plan"]["algorithm"], "mock_policy")
        self.assertEqual(self.app.rlhf_runs()["count"], 0)

    def test_a_plan_without_a_model_says_so_instead_of_guessing_one(self) -> None:
        dataset_version = self.built_dataset()

        planned = self.app.rlhf_pipeline(dataset_version=dataset_version)
        stage = next(
            item
            for item in planned["plan"]["stages"]
            if item["stage"] == "training_configuration"
        )

        self.assertFalse(planned["ok"])
        self.assertEqual(planned["plan"]["blocked"], ["training_configuration"])
        self.assertEqual(stage["status"], "blocked")
        self.assertIn("base_model is required", stage["data"]["errors"][0])

    def test_the_subsystem_can_be_switched_off_and_on(self) -> None:
        self.app.settings.update(rlhf_enabled=False)

        status = self.app.rlhf_status()
        refused = self.app.create_rlhf_dataset("probe", mode="rlhf")
        refused_feedback = self.app.submit_rlhf_feedback(
            {"feedback_type": "accept", "trajectory_id": "traj-1"}
        )

        self.assertFalse(status["enabled"])
        self.assertFalse(refused["ok"])
        self.assertTrue(refused["refused"])
        self.assertFalse(refused_feedback["ok"])
        self.assertIn("switched off", refused_feedback["reason"])

        self.app.settings.update(rlhf_enabled=True)
        self.assertTrue(self.app.rlhf_status()["enabled"])

    async def test_the_diagnostics_roster_names_the_subsystem(self) -> None:
        report = await self.app.diagnostics_report()

        components = {row["component"] for row in report["components"]}
        self.assertIn("RLHF / RLAIF", components)

    def test_the_settings_round_trip_through_the_application(self) -> None:
        applied = self.app.apply_rlhf_settings()

        self.assertTrue(applied["enabled"])
        self.assertTrue(applied["dry_run"])
        self.assertEqual(applied["algorithm"], "mock_policy")
        self.assertEqual(applied["max_checkpoints"], 3)


# ── the /rlhf/* routes, over real HTTP, on an isolated application ────────


class RLHFApiTests(unittest.TestCase):
    """The HTTP surface: real routes, same rails, nothing starts by itself."""

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

    def _record(self, index: int) -> None:
        assert self._nova is not None
        self._nova.evaluation.record(trajectory(f"traj-{index}", task_id=f"task-{index}"))
        self._nova.submit_rlhf_feedback(
            {"feedback_type": "accept", "trajectory_id": f"traj-{index}", "confidence": 0.9}
        )

    def _dataset(self, count: int = 3) -> str:
        for index in range(count):
            self._record(index)
        built = self._client.post("/rlhf/datasets", json={"name": "label", "mode": "rlhf"})
        self.assertEqual(built.status_code, 200, built.text)
        return built.json()["dataset"]["dataset_version_id"]

    def test_the_status_summary_and_algorithms_are_served(self) -> None:
        status = self._client.get("/rlhf/status")
        summary = self._client.get("/rlhf/summary")
        algorithms = self._client.get("/rlhf/algorithms")

        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["enabled"])
        self.assertTrue(status.json()["defaults"]["dry_run"])
        self.assertEqual(summary.status_code, 200)
        self.assertIn("datasets", summary.json())
        self.assertEqual(sorted(algorithms.json()["modes"]), ["rlaif", "rlhf"])
        self.assertEqual(algorithms.json()["implemented"], ["mock_policy"])

    def test_feedback_submitted_over_http_is_listed_and_decided(self) -> None:
        submitted = self._client.post(
            "/rlhf/feedback",
            json={"feedback_type": "accept", "trajectory_id": "traj-1", "confidence": 0.9},
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)
        feedback_id = submitted.json()["feedback"]["feedback_id"]
        default = self._client.get("/rlhf/feedback")
        everything = self._client.get("/rlhf/feedback", params={"pending_only": False})
        accepted = self._client.get("/rlhf/feedback", params={"status": "accepted"})
        decided = self._client.post(
            f"/rlhf/feedback/{feedback_id}/decide",
            json={"decision": "reject", "reviewer": "ana", "reason": "changed my mind"},
        )
        missing = self._client.post("/rlhf/feedback/nope/decide", json={"decision": "accept"})

        self.assertEqual(default.json()["count"], 1, "the default list is not empty")
        self.assertEqual(everything.json()["count"], 1)
        self.assertEqual(accepted.json()["count"], 1)
        self.assertEqual(decided.status_code, 200, decided.text)
        self.assertEqual(decided.json()["feedback"]["status"], "rejected")
        self.assertEqual(decided.json()["feedback"]["review"]["reviewer"], "ana")
        self.assertEqual(missing.status_code, 404)

    def test_a_subject_is_rated_by_the_rule_evaluator_and_stored(self) -> None:
        rated = self._client.post(
            "/rlhf/rate",
            json={"subject": {"task_id": "task-1", "trajectory_id": "traj-1"}},
        )
        listed = self._client.get("/rlhf/ratings")

        self.assertEqual(rated.status_code, 200, rated.text)
        self.assertTrue(rated.json()["ok"], rated.json().get("reason"))
        self.assertEqual(rated.json()["rating"]["evaluator_id"], "rule")
        self.assertEqual(listed.json()["count"], 1)

    def test_a_dataset_is_built_listed_validated_and_read_back(self) -> None:
        dataset_version = self._dataset()

        listed = self._client.get("/rlhf/datasets")
        found = self._client.get(f"/rlhf/datasets/{dataset_version}")
        validated = self._client.get(f"/rlhf/datasets/{dataset_version}/validate")
        held = self._client.get(f"/rlhf/datasets/{dataset_version}/held")
        missing = self._client.get("/rlhf/datasets/nope@v1")

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["count"], 1)
        self.assertEqual(found.status_code, 200)
        self.assertEqual(len(found.json()["examples"]), 3)
        self.assertTrue(validated.json()["ok"], validated.json()["issues"])
        self.assertEqual(held.json()["count"], 0)
        self.assertEqual(missing.status_code, 404)

    def test_a_build_without_a_name_or_a_mode_is_a_422(self) -> None:
        nameless = self._client.post("/rlhf/datasets", json={"mode": "rlhf"})
        modeless = self._client.post("/rlhf/datasets", json={"name": "label"})

        self.assertEqual(nameless.status_code, 422)
        self.assertEqual(modeless.status_code, 422)

    def test_a_dry_run_is_served_and_starts_nothing(self) -> None:
        dataset_version = self._dataset()

        preview = self._client.post(
            "/rlhf/dry-run",
            json={"model": "tiny-model", "dataset_version": dataset_version, "mode": "rlhf"},
        )

        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertTrue(preview.json()["ok"], preview.json()["errors"])
        self.assertFalse(preview.json()["started"])
        assert self._nova is not None
        self.assertEqual(self._nova.rlhf_runs()["count"], 0)

    def test_a_run_can_be_created_started_and_read_back(self) -> None:
        dataset_version = self._dataset()

        created = self._client.post(
            "/rlhf/runs", json={"model": "tiny-model", "dataset_version": dataset_version}
        )
        self.assertEqual(created.status_code, 200, created.text)
        run_id = created.json()["run"]["run_id"]
        started = self._client.post("/rlhf/runs/start", json={"run_id": run_id})
        found = self._client.get(f"/rlhf/runs/{run_id}")
        checkpoints = self._client.get(f"/rlhf/runs/{run_id}/checkpoints")
        listed = self._client.get("/rlhf/runs", params={"mode": "rlhf"})
        models = self._client.get("/rlhf/models", params={"mode": "rlhf"})
        model_id = models.json()["models"][0]["model_id"]
        model = self._client.get(f"/rlhf/models/{model_id}")

        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["run"]["status"], TrainingRunStatus.COMPLETED.value)
        self.assertEqual(found.json()["rl_metrics"]["mode"], "rlhf")
        self.assertGreaterEqual(checkpoints.json()["count"], 1)
        self.assertEqual(listed.json()["count"], 1)
        self.assertEqual(models.json()["count"], 1)
        self.assertEqual(model.status_code, 200)

    def test_a_run_without_a_dataset_is_a_422_and_an_unknown_run_is_a_404(self) -> None:
        incomplete = self._client.post("/rlhf/runs", json={"model": "tiny-model"})
        unknown = self._client.get("/rlhf/runs/nope")

        self.assertEqual(incomplete.status_code, 422)
        self.assertIn("dataset_version is required", incomplete.json()["detail"])
        self.assertEqual(unknown.status_code, 404)

    def test_an_estimate_and_a_pipeline_plan_start_nothing(self) -> None:
        dataset_version = self._dataset()

        estimate = self._client.post(
            "/rlhf/estimate", json={"config": {"reward_dataset_version": dataset_version}}
        )
        planned = self._client.post(
            "/rlhf/pipeline",
            json={"config": {"base_model": "tiny-model"}, "dataset_version": dataset_version},
        )

        self.assertEqual(estimate.status_code, 200)
        self.assertIn("estimate", estimate.json())
        self.assertEqual(planned.status_code, 200)
        self.assertEqual(planned.json()["plan"]["algorithm"], "mock_policy")
        assert self._nova is not None
        self.assertEqual(self._nova.rlhf_runs()["count"], 0)


# ── the `novacontrol rlhf …` command, dispatching to the application ──────


class RLHFCliTests(unittest.IsolatedAsyncioTestCase):
    """`novacontrol rlhf …` dispatches to the application, and nothing more."""

    def test_the_parser_offers_every_action_and_validates_the_typed_flags(self) -> None:
        from novacontrol.cli.parser import build_parser

        parser = build_parser()
        actions = (
            "status", "summary", "algorithms", "feedback", "submit", "decide",
            "rate", "ratings", "disagreements", "datasets", "dataset", "build",
            "validate", "held", "estimate", "dry-run", "pipeline", "create",
            "runs", "run", "checkpoints", "start", "pause", "resume", "cancel",
            "evaluate", "evaluations", "compare", "models", "model",
        )

        for action in actions:
            parsed = parser.parse_args(["rlhf", action])
            self.assertEqual(parsed.action, action, action)

        for member in RLMode:
            parsed = parser.parse_args(["rlhf", "runs", "--mode", member.value])
            self.assertEqual(parsed.mode, member.value)
        for member in HumanFeedbackType:
            parsed = parser.parse_args(["rlhf", "submit", "--feedback-type", member.value])
            self.assertEqual(parsed.feedback_type, member.value)
        for algorithm in POLICY_ALGORITHMS:
            parsed = parser.parse_args(["rlhf", "create", "--algorithm", algorithm])
            self.assertEqual(parsed.algorithm, algorithm)

        with self.assertRaises(SystemExit):
            parser.parse_args(["rlhf", "runs", "--mode", "rlvr"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["rlhf", "create", "--algorithm", "rlvr"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["rlhf", "gamble"])

    async def test_every_action_reaches_the_application_method_it_names(self) -> None:
        from novacontrol.cli.commands import _rlhf_action

        calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

        class Fake:
            def __getattr__(self, name: str):
                def record(*args: Any, **kwargs: Any) -> dict[str, Any]:
                    calls.append((name, args, kwargs))
                    return {"ok": True, "called": name}

                if name in {"start_rlhf_run", "resume_rlhf_run", "evaluate_rlhf_run"}:
                    async def awaited(*args: Any, **kwargs: Any) -> dict[str, Any]:
                        return record(*args, **kwargs)

                    return awaited
                return record

        expected = {
            "status": "rlhf_status",
            "summary": "rlhf_summary",
            "algorithms": "rlhf_algorithms",
            "feedback": "rlhf_feedback",
            "submit": "submit_rlhf_feedback",
            "decide": "decide_rlhf_feedback",
            "rate": "rate_rlhf_subject",
            "ratings": "rlhf_ratings",
            "disagreements": "rlhf_disagreements",
            "datasets": "rlhf_datasets",
            "dataset": "rlhf_dataset",
            "build": "create_rlhf_dataset",
            "validate": "validate_rlhf_dataset",
            "held": "rlhf_held",
            "estimate": "estimate_rlhf",
            "dry-run": "dry_run_rlhf",
            "pipeline": "rlhf_pipeline",
            "create": "create_rlhf_run",
            "runs": "rlhf_runs",
            "run": "rlhf_run",
            "checkpoints": "rlhf_checkpoints",
            "start": "start_rlhf_run",
            "pause": "pause_rlhf_run",
            "resume": "resume_rlhf_run",
            "cancel": "cancel_rlhf_run",
            "evaluate": "evaluate_rlhf_run",
            "evaluations": "rlhf_evaluations",
            "compare": "compare_rlhf_models",
            "models": "rlhf_models",
            "model": "rlhf_model",
        }
        with TemporaryDirectory() as tmp:
            payload_file = Path(tmp) / "rlhf.json"
            payload_file.write_text(
                json.dumps(
                    {
                        "task_id": "task-1",
                        "dataset_version": "label@v1",
                        "base": {"name": "bad"},
                        "candidate": {"name": "good"},
                    }
                ),
                encoding="utf-8",
            )
            app = Fake()
            args: dict[str, Any] = {
                "identifier": "label@v1",
                "mode": "rlhf",
                "algorithm": "mock_policy",
                "name": "probe",
                "model": "tiny-model",
                "dataset_version": "label@v1",
                "feedback_type": "accept",
                "trajectory": "traj-1",
                "task": "task-1",
                "rating": 4.0,
                "confidence": 0.9,
                "candidate": "a",
                "reason_category": "other",
                "evaluator": "auto",
                "criteria": "safety,correctness",
                "limit": 5,
                "file": str(payload_file),
                "decision": "accept",
                "reviewer": "ana",
                "overrides": ["rollout_count=2"],
                "confirm": False,
                "override": False,
                "detect": False,
                "reason": "because",
                "note": "probe",
            }
            for action, method in expected.items():
                calls.clear()
                result = await _rlhf_action(app, action, **args)
                self.assertEqual(len(calls), 1, action)
                self.assertEqual(calls[0][0], method, action)
                self.assertEqual(result["called"], method, action)

    async def test_an_undecided_row_and_the_file_less_actions_are_refused(self) -> None:
        from novacontrol.cli.commands import _rlhf_action

        class Empty:
            def __getattr__(self, name: str):
                return lambda *args, **kwargs: {"ok": True}

        empty = Empty()
        shared: dict[str, Any] = {
            "identifier": "f1",
            "limit": 1,
            "mode": "",
            "algorithm": "",
            "trajectory": "",
        }
        with self.assertRaises(ValueError):
            await _rlhf_action(empty, "decide", decision="", **shared)
        with self.assertRaises(ValueError):
            await _rlhf_action(
                empty, "build", decision="accept", name="probe", note="",
                overrides=[], **shared,
            )
        with self.assertRaises(ValueError):
            await _rlhf_action(empty, "rate", decision="", file="", **shared)
        with self.assertRaises(ValueError):
            await _rlhf_action(empty, "compare", decision="", file="", **shared)
        with self.assertRaises(ValueError):
            await _rlhf_action(
                empty, "submit", decision="", file="", feedback_type="", **shared
            )
