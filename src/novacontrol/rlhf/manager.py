"""The RLHF/RLAIF manager: Phase 18's orchestration, built on Phase 16's.

This class subclasses :class:`~novacontrol.training.manager.TrainingManager` for
the same reason Phase 17 did: the hard parts of a training run already exist and
are inherited unchanged — the run store, the estimate-then-confirm gate, the
refusal to start an unsafe run, the checkpoint callbacks and retention, resume
and cancel, the model registry, and the rule that approval and promotion are
explicit operations. What an RL run adds is local:

  * a different DATASET (reward-labelled rows with source and integrity),
  * a different CONFIGURATION (``RLTrainingConfig``: mode, policy algorithm,
    reward provider, evaluator, rollouts),
  * a different BACKEND (mode-specific trainers built on the policy-optimizer
    plug),
  * FEEDBACK and RATINGS as first-class stored rows, with quality screening,
    provenance and disagreement detection,
  * a four-model EVALUATION whose verdict comes from measured behaviour,
  * the pipeline PLAN a dry run shows before anything starts.

Three seams reach into the parent — ``_dataset_for``, ``_estimate_for`` and
``_trainer_for`` — which is what keeps RL from becoming a second training stack.

The rails hold here too: nothing starts by accident, a real run needs the
dependencies AND a wired runner AND permission, dry runs are the development
path, and no model is loaded to answer a question.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from novacontrol.evaluation.evaluator import EvaluationResult
from novacontrol.evaluation.models import AgentTrajectory, now_iso
from novacontrol.evaluation.storage import InMemoryRecordStore
from novacontrol.rlhf.backends import (
    POLICY_OPTIMIZERS,
    RLRunner,
    RLTrainer,
    provider_for_config,
    rl_trainer_for,
)
from novacontrol.rlhf.config import RLTrainingConfig
from novacontrol.rlhf.datasets import (
    RewardDatasetBuilder,
    RewardDatasetRequest,
    RewardDatasetRules,
)
from novacontrol.rlhf.evaluation import RLModelEvaluator, regression_report
from novacontrol.rlhf.evaluators import (
    DEFAULT_CRITERIA,
    EvaluationRequest,
    Evaluator,
    EvaluatorUnavailable,
    evaluator_for,
)
from novacontrol.rlhf.feedback import FeedbackQualityFilter
from novacontrol.rlhf.integrity import FeedbackDisagreementDetector
from novacontrol.rlhf.models import (
    MODES,
    FeedbackStatus,
    HumanFeedback,
    HumanFeedbackType,
    RewardDatasetVersion,
    RewardExample,
    RLMode,
    Rollout,
)
from novacontrol.rlhf.pipeline import RLPipeline
from novacontrol.rlhf.resources import RLResourceEstimator
from novacontrol.rlhf.storage import (
    DisagreementRepository,
    FeedbackRepository,
    RatingRepository,
    RewardDatasetRepository,
)
from novacontrol.training.backends import SFTTrainer
from novacontrol.training.checkpoints import CheckpointManager
from novacontrol.training.config import TrainingConfig
from novacontrol.training.evaluation import ModelPredictor, TrainingEvaluator
from novacontrol.training.manager import (
    RUN_CREATED,
    Publisher,
    TrainingManager,
)
from novacontrol.training.models import (
    ResourceEstimate,
    TrainingRun,
    TrainingRunStatus,
)
from novacontrol.training.registry import SFTModelRegistry
from novacontrol.training.resources import ResourceEstimator
from novacontrol.training.storage import (
    CheckpointRepository,
    DatasetVersionRepository,
    TrainingEvaluationRepository,
    TrainingModelRepository,
    TrainingRunRepository,
)

_logger = logging.getLogger(__name__)

#: Phase 18's own events. The run lifecycle events stay Phase 16's: an RL run IS
#: a training run, and a second vocabulary for the same lifecycle would be a
#: second thing to keep in sync.
RLHF_FEEDBACK_RECEIVED = "rlhf.feedback_received"
RLHF_FEEDBACK_DECIDED = "rlhf.feedback_decided"
RLHF_RATING_CREATED = "rlhf.rating_created"
RLHF_DISAGREEMENT_FOUND = "rlhf.disagreement_detected"
RLHF_DATASET_BUILT = "rlhf.dataset_built"
RLHF_DRY_RUN = "rlhf.dry_run_completed"
RLHF_COMPARISON = "rlhf.comparison_completed"

#: The run ``algorithm`` value for each mode. A run stores its MODE here because
#: that is what a reader filters by; the policy algorithm lives in the config.
ALGORITHMS: tuple[str, ...] = MODES

#: A factory that builds a backend for one configuration (and the run it belongs
#: to, when there is one). Tests inject a stand-in here.
RLTrainerFactory = Callable[[RLTrainingConfig, TrainingRun | None], RLTrainer]


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _empty_dataset_repository() -> DatasetVersionRepository:
    """The supervised dataset store this manager inherits for evaluations only."""
    return DatasetVersionRepository(InMemoryRecordStore())


class RLHFManager(TrainingManager):
    """Records feedback, rates with evaluators, builds reward datasets, runs RL."""

    def __init__(
        self,
        *,
        datasets: RewardDatasetRepository,
        runs: TrainingRunRepository,
        checkpoints: CheckpointRepository,
        models: TrainingModelRepository,
        evaluations: TrainingEvaluationRepository,
        feedback: FeedbackRepository | None = None,
        ratings: RatingRepository | None = None,
        disagreements: DisagreementRepository | None = None,
        supervised_datasets: DatasetVersionRepository | None = None,
        source: Any = None,
        output_root: str | Path = "",
        default_config: RLTrainingConfig | None = None,
        estimator: ResourceEstimator | None = None,
        evaluator: TrainingEvaluator | None = None,
        rl_evaluator: RLModelEvaluator | None = None,
        builder: RewardDatasetBuilder | None = None,
        registry: SFTModelRegistry | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        trainer_factory: RLTrainerFactory | None = None,
        runner: RLRunner | None = None,
        pipeline: RLPipeline | None = None,
        feedback_filter: FeedbackQualityFilter | None = None,
        detector: FeedbackDisagreementDetector | None = None,
        judge: Any = None,
        max_checkpoints: int = 3,
        publish: Publisher | None = None,
    ) -> None:
        defaults = default_config if default_config is not None else RLTrainingConfig()
        super().__init__(
            # The supervised dataset repository is inherited and used ONLY for
            # evaluation datasets: a reward run's data lives in its own store, and
            # pointing the parent at it would make two managers write one file.
            datasets=supervised_datasets
            if supervised_datasets is not None
            else _empty_dataset_repository(),
            runs=runs,
            checkpoints=checkpoints,
            models=models,
            evaluations=evaluations,
            source=source,
            output_root=output_root,
            default_config=defaults.as_training_config(),
            estimator=estimator,
            evaluator=evaluator,
            registry=registry,
            checkpoint_manager=checkpoint_manager,
            max_checkpoints=max_checkpoints,
            publish=publish,
        )
        self.reward_datasets = datasets
        self.rl_defaults = defaults
        # The default builder GENERATES rewards from recorded behaviour with the
        # deployment's configured provider: a dataset built from trajectories
        # alone would otherwise have no reward to learn from, which is not what
        # "build a dataset from what happened here" means.
        self.dataset_builder = (
            builder
            if builder is not None
            else RewardDatasetBuilder(
                policy=defaults.reward_policy,
                provider=provider_for_config(
                    defaults,
                    evaluator=evaluator_for(
                        defaults.evaluator, judge=judge, policy=defaults.reward_policy
                    ),
                ),
            )
        )
        self.rl_estimator = RLResourceEstimator(estimator=self.estimator)
        self.feedback = (
            feedback if feedback is not None else FeedbackRepository(InMemoryRecordStore())
        )
        self.ratings = (
            ratings if ratings is not None else RatingRepository(InMemoryRecordStore())
        )
        self.disagreements = (
            disagreements
            if disagreements is not None
            else DisagreementRepository(InMemoryRecordStore())
        )
        policy = defaults.reward_policy
        self.feedback_filter = (
            feedback_filter
            if feedback_filter is not None
            else FeedbackQualityFilter(policy=policy)
        )
        self.detector = (
            detector if detector is not None else FeedbackDisagreementDetector(policy)
        )
        self.rl_evaluator = (
            rl_evaluator if rl_evaluator is not None else RLModelEvaluator(evaluator)
        )
        self._rl_trainer_factory = trainer_factory
        self._runner = runner
        self._judge = judge
        self.pipeline = (
            pipeline
            if pipeline is not None
            else RLPipeline(
                trainer_factory=lambda config: self._rl_trainer(config, None),
                dataset_lookup=self.reward_dataset,
            )
        )

    # ── feedback ─────────────────────────────────────────────────────────────

    def submit_feedback(
        self,
        *,
        feedback_type: str,
        trajectory_id: str = "",
        task_id: str = "",
        user_ref: str = "",
        session_ref: str = "",
        rating: float | None = None,
        selected_candidate: str = "",
        correction: str = "",
        reason_category: str = "",
        confidence: float | None = None,
        trajectory: AgentTrajectory | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Screen and store one piece of feedback. Never deletes, always explains.

        The row is redacted, judged against what is already known about the same
        target, and stored with its verdict. A rejected row is stored too: the
        rejection is information, and hiding it would make the filter
        unaccountable.
        """
        row = HumanFeedback(
            trajectory_id=_text(trajectory_id),
            task_id=_text(task_id),
            user_ref=_text(user_ref),
            session_ref=_text(session_ref),
            feedback_type=_text(feedback_type, HumanFeedbackType.ACCEPT.value),
            rating=rating,
            selected_candidate=_text(selected_candidate),
            correction=_text(correction),
            reason_category=_text(reason_category),
            confidence=confidence,
            metadata=dict(metadata or {}),
        )
        known = self.feedback.list(trajectory_id=row.trajectory_id, limit=0)
        screened, verdict = self.feedback_filter.apply(
            row, known=known, trajectory=trajectory
        )
        if screened is None:
            return {
                "ok": False,
                "stored": False,
                "reason": "the row carries neither a feedback type nor a target",
                "verdict": verdict.to_dict(),
            }
        self.feedback.save(screened)
        self._announce(
            RLHF_FEEDBACK_RECEIVED,
            {
                "feedback_id": screened.feedback_id,
                "trajectory_id": screened.trajectory_id,
                "feedback_type": screened.feedback_type,
                "status": screened.status,
            },
        )
        return {
            "ok": screened.status != FeedbackStatus.REJECTED.value,
            "stored": True,
            "feedback": screened.to_dict(),
            "verdict": verdict.to_dict(),
            "review_required": screened.status == FeedbackStatus.NEEDS_REVIEW.value,
        }

    def feedback_list(
        self,
        *,
        status: str = "",
        feedback_type: str = "",
        trajectory_id: str = "",
        pending_only: bool = False,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        return [
            row.to_dict()
            for row in self.feedback.list(
                status=status,
                feedback_type=feedback_type,
                trajectory_id=trajectory_id,
                pending_only=pending_only,
                limit=limit,
            )
        ]

    def feedback_stats(self) -> dict[str, Any]:
        counts = self.feedback.counts()
        return {
            "rows": sum(counts.values()),
            "by_status": counts,
            "pending_review": len(self.feedback.pending()),
            "types": {
                member.value: len(self.feedback.list(feedback_type=member.value, limit=0))
                for member in HumanFeedbackType
            },
        }

    def decide_feedback(
        self, feedback_id: str, decision: str, *, reviewer: str = "", reason: str = ""
    ) -> dict[str, Any]:
        """A person settles a held row: accepted stays usable, rejected does not."""
        row = self.feedback.get(feedback_id)
        if row is None:
            return {"ok": False, "reason": f"no feedback {feedback_id!r}"}
        wanted = _text(decision).lower()
        if wanted not in {"accept", "reject"}:
            return {
                "ok": False,
                "reason": "decision must be 'accept' or 'reject'",
            }
        reviewed = row.with_review(wanted, reviewer, reason)
        status = (
            FeedbackStatus.ACCEPTED.value
            if wanted == "accept"
            else FeedbackStatus.REJECTED.value
        )
        stored = HumanFeedback.from_dict({**reviewed.to_dict(), "status": status})
        self.feedback.save(stored)
        self._announce(
            RLHF_FEEDBACK_DECIDED,
            {
                "feedback_id": stored.feedback_id,
                "decision": wanted,
                "status": status,
            },
        )
        return {"ok": True, "feedback": stored.to_dict()}

    def pending_feedback(self, limit: int = 0) -> tuple[HumanFeedback, ...]:
        return self.feedback.pending(limit=limit)

    # ── ratings ──────────────────────────────────────────────────────────────

    def rate(
        self,
        subject: Mapping[str, Any] | AgentTrajectory,
        *,
        evaluator: str = "auto",
        criteria: Sequence[str] = (),
        save: bool = True,
    ) -> dict[str, Any]:
        """Ask an evaluator for a structured rating of observable facts.

        The evaluator reads only what is supplied: a task, a candidate, a
        verification summary, a recorded outcome. An evaluator that cannot
        answer — no judge wired, nothing to judge — returns ``ok: False`` with
        the reason, and no rating is stored.
        """
        request = (
            EvaluationRequest.for_trajectory(subject, criteria=criteria)
            if isinstance(subject, AgentTrajectory)
            else _request_from_mapping(subject, criteria)
        )
        resolved: Evaluator = evaluator_for(
            evaluator, judge=self._judge, policy=self.rl_defaults.reward_policy
        )
        problems = resolved.validate(request)
        if problems:
            return {"ok": False, "reason": "; ".join(problems)}
        try:
            rating = resolved.evaluate(request)
        except EvaluatorUnavailable as exc:
            return {"ok": False, "reason": str(exc), "evaluator": resolved.evaluator_id}
        if save:
            self.ratings.save(rating)
            self._announce(
                RLHF_RATING_CREATED,
                {
                    "rating_id": rating.rating_id,
                    "trajectory_id": rating.trajectory_id,
                    "evaluator": rating.evaluator_id,
                    "score": rating.score,
                },
            )
        return {
            "ok": True,
            "rating": rating.to_dict(),
            "explanation": resolved.explain(rating),
            "stored": bool(save),
        }

    def ratings_list(
        self,
        *,
        trajectory_id: str = "",
        evaluator_id: str = "",
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        return [
            rating.to_dict()
            for rating in self.ratings.list(
                trajectory_id=trajectory_id, evaluator_id=evaluator_id, limit=limit
            )
        ]

    def rating_stats(self) -> dict[str, Any]:
        rows = self.ratings.list(limit=0)
        by_evaluator: dict[str, int] = {}
        for row in rows:
            by_evaluator[row.evaluator_id or "unknown"] = (
                by_evaluator.get(row.evaluator_id or "unknown", 0) + 1
            )
        return {"rows": len(rows), "by_evaluator": by_evaluator}

    # ── disagreement ─────────────────────────────────────────────────────────

    def detect_disagreements(self, *, limit: int = 0) -> list[dict[str, Any]]:
        """Find where sources contradict each other, and store the finding.

        Nothing is resolved here. Each report names both readings, the gap and a
        recommendation; a person or a deterministic check settles it later.
        """
        subjects: list[str] = []
        for row in self.feedback.list(limit=0):
            if row.trajectory_id and row.trajectory_id not in subjects:
                subjects.append(row.trajectory_id)
        for rating_row in self.ratings.list(limit=0):
            if rating_row.trajectory_id and rating_row.trajectory_id not in subjects:
                subjects.append(rating_row.trajectory_id)
        found: list[dict[str, Any]] = []
        for subject_id in subjects[: limit or len(subjects)]:
            human = tuple(self.feedback.list(trajectory_id=subject_id, limit=0))
            ai = tuple(self.ratings.list(trajectory_id=subject_id, limit=0))
            alternate = ai[1] if len(ai) > 1 else None
            reports = self.detector.detect(
                subject_id,
                human=human,
                ai=ai[0] if ai else None,
                ai_alt=alternate,
            )
            for report in reports:
                self.disagreements.save(report)
                found.append(report.to_dict())
            if reports:
                self._announce(
                    RLHF_DISAGREEMENT_FOUND,
                    {
                        "subject_id": subject_id,
                        "kinds": sorted({report.kind for report in reports}),
                    },
                )
        return found

    def disagreements_list(
        self, *, kind: str = "", subject_id: str = "", limit: int = 0
    ) -> list[dict[str, Any]]:
        return [
            row.to_dict()
            for row in self.disagreements.list(
                kind=kind, subject_id=subject_id, limit=limit
            )
        ]

    def disagreement_stats(self) -> dict[str, Any]:
        counts = self.disagreements.counts()
        return {"rows": sum(counts.values()), "by_kind": counts}

    # ── datasets ─────────────────────────────────────────────────────────────

    def build_dataset(
        self,
        name: str,
        *,
        mode: str = "",
        version: str = "",
        description: str = "",
        rules: RewardDatasetRules | None = None,
        split: Any = None,
        trajectories: Sequence[AgentTrajectory] = (),
        evaluations: Sequence[EvaluationResult] = (),
        feedback: Sequence[HumanFeedback] = (),
        ratings: Sequence[Any] = (),
        rewards: Sequence[Any] = (),
        rollouts: Sequence[Rollout] = (),
        examples: Sequence[RewardExample] = (),
        source_datasets: Sequence[str] = (),
        tags: Sequence[str] = (),
    ) -> RewardDatasetVersion:
        """Build and store one immutable reward dataset version.

        Feedback and ratings default to everything stored, because that is what
        a person building a dataset from this installation means. A mode-specific
        build adds the matching requirement: an RLHF dataset must contain what
        people said, an RLAIF dataset what evaluators said.
        """
        chosen_mode = _text(mode, self.rl_defaults.mode)
        default_rules = RewardDatasetRules(
            mode=chosen_mode if chosen_mode in MODES else "mixed",
            require_human=chosen_mode == RLMode.RLHF.value,
            require_ai=chosen_mode == RLMode.RLAIF.value,
        )
        effective = rules if rules is not None else default_rules
        if rules is not None:
            effective = replace(
                rules,
                require_human=rules.require_human or chosen_mode == RLMode.RLHF.value,
                require_ai=rules.require_ai or chosen_mode == RLMode.RLAIF.value,
            )
        request = RewardDatasetRequest.of(
            trajectories=trajectories,
            evaluations=evaluations,
            feedback=tuple(feedback) if feedback else tuple(self.feedback.list(limit=0)),
            ratings=tuple(ratings) if ratings else tuple(self.ratings.list(limit=0)),
            rewards=rewards,
            rollouts=rollouts,
            examples=examples,
            source_datasets=source_datasets,
        )
        dataset = self.dataset_builder.build(
            name,
            request,
            version=version,
            description=description,
            rules=effective,
            split=split,
            tags=tags,
            existing_versions=[
                item.version for item in self.reward_datasets.versions(name)
            ],
        )
        self.reward_datasets.save(dataset)
        self._announce(
            RLHF_DATASET_BUILT,
            {
                "dataset_version": dataset.dataset_version_id,
                "mode": dataset.mode,
                "examples": len(dataset),
                "accepted": len(dataset.accepted_examples()),
            },
        )
        return dataset

    def reward_dataset(self, dataset_version_id: str) -> RewardDatasetVersion | None:
        return self.reward_datasets.get(dataset_version_id)

    def reward_datasets_list(
        self, *, mode: str = "", name: str = "", limit: int = 0
    ) -> tuple[RewardDatasetVersion, ...]:
        return self.reward_datasets.list(mode=mode, name=name, limit=limit)

    def validate_dataset(self, dataset_version_id: str) -> dict[str, Any]:
        dataset = self.reward_dataset(dataset_version_id)
        if dataset is None:
            return {"ok": False, "reason": f"no reward dataset version {dataset_version_id!r}"}
        issues = self.dataset_builder.validate(dataset)
        audit_issues = self.dataset_builder.validate_reasoning_free(dataset)
        return {
            "ok": not issues and not audit_issues,
            "dataset_version": dataset.dataset_version_id,
            "issues": [*issues, *audit_issues],
            "examples": len(dataset),
            "accepted": len(dataset.accepted_examples()),
            "trusted": len(dataset.trusted_examples()),
            "splits": {name: len(ids) for name, ids in dataset.splits.items() if ids},
            "sources": dict(dataset.sources),
        }

    # ── configuration ────────────────────────────────────────────────────────

    def resolve_rl_config(
        self,
        config: RLTrainingConfig | TrainingConfig | Mapping[str, Any] | None = None,
        *,
        mode: str = "",
        algorithm: str = "",
    ) -> RLTrainingConfig:
        """This deployment's RL defaults, overridden by what the caller named."""
        defaults = dict(self.rl_defaults.to_mapping())
        if isinstance(config, RLTrainingConfig):
            merged = {**defaults, **dict(config.to_mapping())}
        elif isinstance(config, TrainingConfig):
            merged = {
                **defaults,
                **dict(RLTrainingConfig.from_training_config(config).to_mapping()),
            }
        else:
            merged = dict(defaults)
            for key, value in dict(config or {}).items():
                if value is not None:
                    merged[str(key)] = value
        if _text(mode):
            merged["mode"] = _text(mode)
        if _text(algorithm):
            merged["algorithm"] = _text(algorithm)
        return RLTrainingConfig.from_mapping(merged).with_defaults(
            output_directory=str(self.output_root)
        )

    def algorithms(self) -> dict[str, Any]:
        """The policy optimizers and modes as documentation, plus readiness."""
        capabilities = self.rl_estimator.capabilities()
        return {
            "modes": {
                RLMode.RLHF.value: (
                    "reinforcement learning from human feedback: the dataset must "
                    "contain what people said"
                ),
                RLMode.RLAIF.value: (
                    "reinforcement learning from AI feedback: the dataset must "
                    "contain evaluator ratings"
                ),
            },
            "policy_optimizers": {
                name: dict(description)
                for name, description in POLICY_OPTIMIZERS.items()
            },
            "implemented": [
                name for name, item in POLICY_OPTIMIZERS.items() if item["implemented"]
            ],
            "dry_run_supported": True,
            "cuda_required": False,
            "missing_dependencies": list(capabilities.missing_dependencies()),
            "training_dependencies_ready": capabilities.training_dependencies_ready,
            "note": (
                "a dry run walks the pipeline without training and works with no RL "
                "dependencies installed; a real run needs them, a wired policy "
                "optimizer runner, this deployment's permission and an explicit "
                "confirmation"
            ),
        }

    # ── runs ─────────────────────────────────────────────────────────────────

    def create_run(
        self,
        model: str,
        dataset_version: str,
        *,
        config: RLTrainingConfig | TrainingConfig | Mapping[str, Any] | None = None,
        name: str = "",
    ) -> TrainingRun:
        """Validate an RL configuration, audit the rewards, and store a CREATED run."""
        dataset = self.reward_dataset(dataset_version)
        if dataset is None:
            raise ValueError(
                f"no reward dataset version {dataset_version!r}: build it first"
            )
        if not dataset.examples:
            raise ValueError(f"reward dataset {dataset_version!r} has no examples")
        cfg = self.resolve_rl_config(config)
        cfg = replace(
            cfg,
            base_model=_text(model) or cfg.base_model,
            reward_dataset_version=dataset_version,
            max_checkpoints=max(1, cfg.max_checkpoints),
        )
        validation = cfg.validate()
        if not validation.valid:
            raise ValueError(
                "invalid RL training configuration: " + "; ".join(validation.errors)
            )
        trainer = self._rl_trainer(cfg, None)
        audit = trainer.audit_rewards(dataset)
        if audit.problems:
            raise ValueError(
                f"reward dataset {dataset_version!r} cannot train a {cfg.mode} run: "
                + "; ".join(audit.problems)
            )
        estimate = self.rl_estimator.estimate(cfg, dataset=dataset)
        run = TrainingRun(
            name=(
                name
                or f"{cfg.base_model or 'model'}+{cfg.mode}/{cfg.algorithm}@{dataset.version}"
            ),
            model=cfg.base_model,
            dataset_version=dataset_version,
            dataset_type=dataset.mode,
            training_config=self._stored_config(cfg, dataset_version),
            estimate=estimate.to_dict(),
            backend="dry_run" if cfg.dry_run else estimate.backend,
            algorithm=cfg.mode,
            rl_metrics={
                "mode": cfg.mode,
                "algorithm": cfg.algorithm,
                "reward_provider": cfg.reward_provider,
                "evaluator": cfg.evaluator,
                "reward_audit": audit.to_dict(),
                "rollouts": cfg.rollout_count,
                "gamma": cfg.gamma,
                # A record is honest about itself: a dry run trains nothing, and an
                # optimizer that does not learn (the mock) produces measurements
                # rather than a policy even when the run is not dry.
                "simulated": bool(
                    cfg.dry_run
                    or not POLICY_OPTIMIZERS.get(cfg.algorithm, {}).get("learns", False)
                ),
            },
            random_seed=cfg.seed,
        )
        self.runs.save(run)
        self._announce(
            RUN_CREATED,
            {
                "run_id": run.run_id,
                "dataset_version": dataset_version,
                "algorithm": run.algorithm,
                "dry_run": cfg.dry_run,
            },
        )
        return run

    def rl_runs(
        self, *, mode: str = "", status: str = "", limit: int = 0
    ) -> tuple[TrainingRun, ...]:
        """The runs this manager owns, filtered by mode when asked."""
        wanted = _text(mode).lower()
        found = [
            run
            for run in self.runs_list(status=status, dataset_version="", limit=0)
            if run.algorithm in ALGORITHMS and (not wanted or run.algorithm == wanted)
        ]
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)

    def estimate_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        cfg = self.resolve_rl_config(config)
        validation = cfg.validate()
        dataset = (
            self.reward_dataset(cfg.reward_dataset_version)
            if cfg.reward_dataset_version
            else None
        )
        estimate = self.rl_estimator.estimate(cfg, dataset=dataset)
        payload: dict[str, Any] = {
            "valid": validation.valid,
            "errors": list(validation.errors),
            "warnings": list(validation.warnings),
            "mode": cfg.mode,
            "algorithm": cfg.algorithm,
            "policy_optimizer": dict(POLICY_OPTIMIZERS.get(cfg.algorithm, {})),
            "needs_reference_model": cfg.needs_reference_model,
            "effective_config": cfg.to_mapping(),
            "estimate": estimate.to_dict(),
        }
        if dataset is not None:
            payload["dataset"] = {
                "dataset_version": dataset.dataset_version_id,
                "mode": dataset.mode,
                "examples": len(dataset),
                "accepted": len(dataset.accepted_examples()),
                "sources": dict(dataset.sources),
            }
        return payload

    def estimate_run(self, run_id: str) -> dict[str, Any]:
        run = self.runs.get(run_id)
        if run is None:
            return {"ok": False, "reason": f"no run {run_id!r}"}
        dataset = self._dataset_for(run)
        estimate = self._estimate_for(
            TrainingConfig.from_mapping(run.training_config), dataset, run
        )
        self.runs.save(replace(run, estimate=estimate.to_dict(), updated_at=now_iso()))
        return {
            "ok": True,
            "run_id": run_id,
            "mode": run.algorithm,
            "estimate": estimate.to_dict(),
        }

    def pipeline_plan(
        self, config: Mapping[str, Any] | None = None, dataset_version: str = ""
    ) -> dict[str, Any]:
        cfg = self.resolve_rl_config(config)
        if dataset_version:
            cfg = replace(cfg, reward_dataset_version=dataset_version)
        dataset = (
            self.reward_dataset(cfg.reward_dataset_version)
            if cfg.reward_dataset_version
            else None
        )
        plan = self.pipeline.plan(cfg, dataset=dataset)
        return {"ok": plan.ok, "plan": plan.to_dict()}

    def dry_run(
        self,
        model: str = "",
        dataset_version: str = "",
        *,
        config: RLTrainingConfig | TrainingConfig | Mapping[str, Any] | None = None,
        mode: str = "",
        algorithm: str = "",
    ) -> dict[str, Any]:
        """Validate, price, plan and simulate a run — and start nothing.

        This is what an operator or a CI job uses to answer "would this work
        here?" without an RL dependency and without touching a model. It records
        ``started: False`` so nothing that reads it can mistake it for a run.
        """
        cfg = self.resolve_rl_config(config, mode=mode, algorithm=algorithm)
        cfg = replace(
            cfg,
            base_model=_text(model) or cfg.base_model,
            reward_dataset_version=dataset_version or cfg.reward_dataset_version,
            dry_run=True,
        )
        dataset = (
            self.reward_dataset(cfg.reward_dataset_version)
            if cfg.reward_dataset_version
            else None
        )
        payload = self.pipeline.dry_run(cfg, dataset=dataset)
        if dataset_version and dataset is None:
            payload["ok"] = False
            payload["errors"] = [
                *payload["errors"],
                f"no reward dataset version {dataset_version!r}",
            ]
        self._announce(
            RLHF_DRY_RUN,
            {
                "dataset_version": cfg.reward_dataset_version,
                "mode": cfg.mode,
                "algorithm": cfg.algorithm,
                "ok": payload["ok"],
            },
        )
        return payload

    # ── evaluation ───────────────────────────────────────────────────────────

    def compare_models(
        self,
        dataset_version: str,
        *,
        base: ModelPredictor,
        candidate: ModelPredictor,
        sft: ModelPredictor | None = None,
        preference: ModelPredictor | None = None,
        split: str = "test",
        tolerance: float | None = None,
        run_id: str = "",
    ) -> dict[str, Any]:
        """Base, SFT and preference models against the RL candidate, measured."""
        dataset = self.datasets.get(dataset_version)
        if dataset is None:
            return {"ok": False, "reason": f"no evaluation dataset version {dataset_version!r}"}
        run = self.runs.get(run_id) if run_id else None
        evaluation = self.rl_evaluator.compare(
            dataset,
            base=base,
            candidate=candidate,
            sft=sft,
            preference=preference,
            split=split,
            tolerance=tolerance,
            run_id=run_id,
            reward_metrics=dict(run.rl_metrics) if run is not None else {},
        )
        self.evaluations.save(evaluation)
        self._announce(
            RLHF_COMPARISON,
            {
                "run_id": run_id,
                "dataset_version": dataset_version,
                "verdict": evaluation.verdict,
            },
        )
        return {
            "ok": True,
            "evaluation": evaluation.to_dict(),
            "report": regression_report(evaluation),
            "verdict": evaluation.verdict,
            "regressions": list(evaluation.regressions),
        }

    def evaluate_run(
        self,
        run_id: str,
        *,
        base: ModelPredictor | None = None,
        candidate: ModelPredictor | None = None,
        sft: ModelPredictor | None = None,
        preference: ModelPredictor | None = None,
        dataset_version: str = "",
        split: str = "test",
        tolerance: float | None = None,
    ) -> dict[str, Any]:
        """Evaluate a stored run against every baseline that was supplied."""
        if base is None or candidate is None:
            return {
                "ok": False,
                "reason": (
                    "an evaluation needs at least a base model and a candidate "
                    "model to measure behaviour; neither was supplied"
                ),
            }
        run = self.runs.get(run_id)
        if run is None:
            return {"ok": False, "reason": f"no run {run_id!r}"}
        if run.status != TrainingRunStatus.COMPLETED.value:
            return {
                "ok": False,
                "reason": f"run {run_id!r} is {run.status}, so it was not evaluated",
            }
        chosen = _text(dataset_version)
        if not chosen:
            supervised = self.datasets.list(limit=1)
            if not supervised:
                return {
                    "ok": False,
                    "reason": (
                        "no evaluation dataset is available: supply the supervised "
                        "dataset version the comparison runs on"
                    ),
                }
            chosen = supervised[0].dataset_version_id
        return self.compare_models(
            chosen,
            base=base,
            candidate=candidate,
            sft=sft,
            preference=preference,
            split=split,
            tolerance=tolerance,
            run_id=run_id,
        )

    def rl_evaluations_list(self, *, limit: int = 0) -> list[dict[str, Any]]:
        return [row.to_dict() for row in self.evaluations.list(limit=limit)]

    # ── status ───────────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        payload = super().status()
        runs = self.rl_runs()
        payload["datasets"] = {
            "count": len(self.reward_datasets.list(limit=0)),
            "names": sorted({item.name for item in self.reward_datasets.list(limit=0)}),
        }
        payload["runs"]["by_mode"] = {
            name: sum(1 for run in runs if run.algorithm == name) for name in ALGORITHMS
        }
        payload["feedback"] = self.feedback_stats()
        payload["ratings"] = self.rating_stats()
        payload["disagreements"] = self.disagreement_stats()
        payload["modes"] = list(ALGORITHMS)
        payload["defaults"]["mode"] = self.rl_defaults.mode
        payload["defaults"]["algorithm"] = self.rl_defaults.algorithm
        return payload

    def summary(self) -> dict[str, Any]:
        payload = super().summary()
        runs = self.rl_runs()
        payload["datasets"] = [
            item.dataset_version_id for item in self.reward_datasets.list()
        ]
        payload["runs"] = [
            {
                "run_id": run.run_id,
                "model": run.model,
                "dataset_version": run.dataset_version,
                "mode": run.algorithm,
                "status": run.status,
                "backend": run.backend,
                "training_loss": run.training_loss,
                "rl_metrics": dict(run.rl_metrics),
            }
            for run in runs[:20]
        ]
        payload["feedback"] = self.feedback_stats()
        payload["ratings"] = self.rating_stats()
        payload["disagreements"] = self.disagreement_stats()
        return payload

    # ── the three seams ──────────────────────────────────────────────────────

    def _dataset_for(self, run: TrainingRun) -> Any:
        dataset = self.reward_datasets.get(run.dataset_version)
        return dataset if dataset is not None else self.datasets.get(run.dataset_version)

    def _estimate_for(
        self,
        cfg: TrainingConfig,
        dataset: Any,
        run: TrainingRun | None = None,
    ) -> ResourceEstimate:
        rl_config = (
            self._config_from_run(run)
            if run is not None
            else RLTrainingConfig.from_training_config(cfg)
        )
        return self.rl_estimator.estimate(rl_config, dataset=dataset)

    def _trainer_for(
        self, cfg: TrainingConfig, run: TrainingRun | None = None
    ) -> SFTTrainer:
        rl_config = (
            self._config_from_run(run)
            if run is not None
            else RLTrainingConfig.from_training_config(cfg)
        )
        return self._rl_trainer(rl_config, run)

    def _rl_trainer(
        self, cfg: RLTrainingConfig, run: TrainingRun | None
    ) -> RLTrainer:
        if self._rl_trainer_factory is not None:
            return self._rl_trainer_factory(cfg, run)
        evaluator = evaluator_for(
            cfg.evaluator, judge=self._judge, policy=cfg.reward_policy
        )
        return rl_trainer_for(
            cfg,
            estimator=self.estimator,
            runner=self._runner,
            evaluator=evaluator,
        )

    def _config_from_run(self, run: TrainingRun) -> RLTrainingConfig:
        return RLTrainingConfig.from_mapping(run.training_config).with_defaults(
            output_directory=str(self.output_root)
        )

    @staticmethod
    def _stored_config(
        cfg: RLTrainingConfig, dataset_version: str
    ) -> dict[str, Any]:
        """The configuration a run row carries, readable by BOTH phases.

        The parent's lifecycle reads a run's configuration back as a
        :class:`TrainingConfig`, so ``dataset_version`` is added as an alias for
        the reward field and ``dataset_type`` is left out (the run record already
        carries the mode, and the supervised validator only knows its own
        families). The RL fields ride along untouched, which is how the mode,
        algorithm, provider and rollouts survive a restart.
        """
        stored = dict(cfg.to_mapping())
        stored.pop("dataset_type", None)
        stored["dataset_version"] = dataset_version
        return stored

    # ── helpers ──────────────────────────────────────────────────────────────

    def held_examples(self, dataset_version: str) -> tuple[RewardExample, ...]:
        """Rows a dataset held back from its splits, so a person can settle them."""
        dataset = self.reward_dataset(dataset_version)
        if dataset is None:
            return ()
        return tuple(
            row
            for row in dataset.examples
            if row.status
            in {FeedbackStatus.NEEDS_REVIEW.value, FeedbackStatus.REJECTED.value}
        )

    @staticmethod
    def _output_directory_report(path: str) -> dict[str, Any]:
        """Whether a run could write where it was told to — checked, not created."""
        raw = _text(path)
        if not raw:
            return {
                "path": "",
                "ok": False,
                "reason": "no output directory is configured",
                "exists": False,
                "writable": False,
            }
        target = Path(raw)
        exists = target.exists()
        if exists and not target.is_dir():
            return {
                "path": str(target),
                "ok": False,
                "reason": "a file already exists at this path",
                "exists": True,
                "writable": False,
            }
        probe = target if exists else target.parent
        writable = probe.exists() and os.access(probe, os.W_OK)
        return {
            "path": str(target),
            "ok": bool(writable),
            "reason": "" if writable else f"{probe} is not writable",
            "exists": exists,
            "writable": bool(writable),
            "checked": str(probe),
        }


def _request_from_mapping(
    subject: Mapping[str, Any], criteria: Sequence[str]
) -> EvaluationRequest:
    """Read an ad-hoc evaluation request from a mapping (API/CLI convenience)."""

    def part(key: str) -> dict[str, Any]:
        value = subject.get(key)
        if isinstance(value, Mapping):
            return {str(name): item for name, item in value.items()}
        return {}

    constraints = subject.get("constraints")
    return EvaluationRequest(
        task_id=_text(subject.get("task_id")),
        trajectory_id=_text(subject.get("trajectory_id")),
        task=part("task"),
        context=part("context"),
        candidate=part("candidate"),
        constraints=tuple(str(item) for item in constraints)
        if isinstance(constraints, (list, tuple))
        else (),
        verification=part("verification"),
        outcome=part("outcome"),
        criteria=tuple(criteria) if criteria else DEFAULT_CRITERIA,
    )


__all__ = [
    "ALGORITHMS",
    "RLHF_COMPARISON",
    "RLHF_DATASET_BUILT",
    "RLHF_DISAGREEMENT_FOUND",
    "RLHF_DRY_RUN",
    "RLHF_FEEDBACK_DECIDED",
    "RLHF_FEEDBACK_RECEIVED",
    "RLHF_RATING_CREATED",
    "RLHFManager",
    "RLTrainerFactory",
]
