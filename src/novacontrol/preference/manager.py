"""The preference manager: Phase 17's orchestration, built on Phase 16's.

This class is a subclass of :class:`~novacontrol.training.manager.TrainingManager`
on purpose, and that is the phase's main architectural decision. Everything a
training run needs — the run store, the estimate-then-confirm gate, the unsafe
estimate refusal, the checkpoint callbacks and retention, pause/cancel/resume, the
model registry, the "evaluation needs predictors" rule — is inherited unchanged.
What a preference run adds is small and local:

  * a different DATASET (pairs, not examples) in its own repository,
  * a different CONFIGURATION (``PreferenceTrainingConfig``, with ``algorithm`` and
    ``beta``) and therefore a different estimator, which counts the reference model
    a DPO run keeps and an ORPO run does not,
  * a different BACKEND, chosen from the algorithm,
  * a three-model EVALUATION (base vs SFT vs preference-optimized) whose verdict
    still comes from measured behaviour, never from the preference loss,
  * a human REVIEW queue, because some pairs can only be settled by a person.

Three seams make that possible, and they are the only places this class reaches
into its parent: ``_dataset_for`` (which dataset a run references),
``_estimate_for`` (what it would cost) and ``_trainer_for`` (which objective runs).
Overriding three methods is what keeps DPO and ORPO from becoming a second
training stack.

The rails are inherited, so they hold here too: nothing starts by accident, a real
run needs the deployment to permit it AND an explicit confirmation, an unsafe
estimate refuses, a completed run is registered EXPERIMENTAL, and approval and
promotion stay explicit registry operations.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from novacontrol.evaluation.datasets import GoldenDataset, GoldenExample
from novacontrol.evaluation.evaluator import EvaluationResult
from novacontrol.evaluation.models import AgentTrajectory, now_iso
from novacontrol.evaluation.storage import InMemoryRecordStore
from novacontrol.preference.backends import (
    OBJECTIVES,
    PreferenceRunner,
    PreferenceTrainer,
    objective_for,
    preference_trainer_for,
)
from novacontrol.preference.config import PreferenceTrainingConfig
from novacontrol.preference.datasets import PreferenceDatasetBuilder
from novacontrol.preference.evaluation import PreferenceEvaluator
from novacontrol.preference.models import (
    PreferenceAlgorithm,
    PreferenceDatasetType,
    PreferenceDatasetVersion,
    PreferenceExample,
    PreferenceQualityStatus,
    PreferenceRules,
)
from novacontrol.preference.quality import PreferenceQualityConfig
from novacontrol.preference.resources import PreferenceResourceEstimator
from novacontrol.preference.review import PreferenceReviewQueue
from novacontrol.preference.storage import (
    PreferenceDatasetRepository,
    PreferenceReviewRepository,
)
from novacontrol.training.backends import SFTTrainer
from novacontrol.training.checkpoints import CheckpointManager
from novacontrol.training.config import TrainingConfig
from novacontrol.training.evaluation import ModelPredictor, TrainingEvaluator
from novacontrol.training.manager import (
    EVALUATION_COMPLETED,
    RUN_CREATED,
    Publisher,
    TrainingManager,
)
from novacontrol.training.models import (
    ResourceEstimate,
    SplitConfig,
    TrainingEvaluation,
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

#: Preference-specific events. The run lifecycle events are Phase 16's, reused
#: unchanged — a preference run IS a training run, and a second vocabulary for the
#: same lifecycle would be a second thing to keep in sync.
PREFERENCE_DATASET_BUILT = "preference.dataset_built"
PREFERENCE_REVIEW_QUEUED = "preference.review_queued"
PREFERENCE_REVIEW_DECIDED = "preference.review_decided"
PREFERENCE_DRY_RUN = "preference.dry_run_completed"
PREFERENCE_COMPARISON = "preference.comparison_completed"

#: The objectives that make a run a preference run.
ALGORITHMS: tuple[str, ...] = tuple(member.value for member in PreferenceAlgorithm)

#: A factory that builds a backend for one configuration (and the run it belongs
#: to, when there is one). Tests inject a stand-in here.
PreferenceTrainerFactory = Callable[
    [PreferenceTrainingConfig, TrainingRun | None], PreferenceTrainer
]


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _empty_dataset_repository() -> DatasetVersionRepository:
    """The supervised dataset store this manager inherits but never uses."""
    return DatasetVersionRepository(InMemoryRecordStore())


class PreferenceManager(TrainingManager):
    """Builds preference datasets, runs DPO/ORPO, keeps checkpoints and reviews."""

    def __init__(
        self,
        *,
        datasets: PreferenceDatasetRepository,
        runs: TrainingRunRepository,
        checkpoints: CheckpointRepository,
        models: TrainingModelRepository,
        evaluations: TrainingEvaluationRepository,
        reviews: PreferenceReviewRepository | None = None,
        supervised_datasets: DatasetVersionRepository | None = None,
        source: Any = None,
        output_root: str | Path = "",
        default_config: PreferenceTrainingConfig | None = None,
        estimator: ResourceEstimator | None = None,
        evaluator: TrainingEvaluator | None = None,
        preference_evaluator: PreferenceEvaluator | None = None,
        builder: PreferenceDatasetBuilder | None = None,
        registry: SFTModelRegistry | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        review_queue: PreferenceReviewQueue | None = None,
        trainer_factory: PreferenceTrainerFactory | None = None,
        runner: PreferenceRunner | None = None,
        max_checkpoints: int = 3,
        publish: Publisher | None = None,
    ) -> None:
        defaults = default_config if default_config is not None else PreferenceTrainingConfig()
        super().__init__(
            # The supervised dataset repository is inherited and stays empty: a
            # preference run's dataset lives in the pair repository below, and
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
        self.pair_datasets = datasets
        self.preference_defaults = defaults
        self.pair_builder = builder if builder is not None else PreferenceDatasetBuilder()
        self.preference_estimator = PreferenceResourceEstimator(
            estimator=self.estimator, capabilities=None
        )
        self.preference_evaluator = (
            preference_evaluator if preference_evaluator is not None else PreferenceEvaluator()
        )
        self.reviews = (
            review_queue
            if review_queue is not None
            else PreferenceReviewQueue(
                reviews
                if reviews is not None
                else PreferenceReviewRepository(InMemoryRecordStore()),
                builder=self.pair_builder,
            )
        )
        self._preference_trainer_factory = trainer_factory
        self._preference_runner = runner

    # ── datasets ─────────────────────────────────────────────────────────────

    def build_dataset(
        self,
        name: str,
        dataset_type: PreferenceDatasetType | str,
        *,
        rules: PreferenceRules | None = None,
        quality: PreferenceQualityConfig | None = None,
        split: SplitConfig | None = None,
        version: str = "",
        description: str = "",
        tags: Sequence[str] = (),
        trajectories: Sequence[AgentTrajectory] | None = None,
        evaluations: Sequence[EvaluationResult] | None = None,
        benchmarks: Sequence[GoldenExample] | GoldenDataset | None = None,
        pairs: Sequence[PreferenceExample] = (),
        reviewed: Sequence[PreferenceExample] = (),
        source_datasets: Sequence[str] = (),
        queue_for_review: bool = True,
    ) -> PreferenceDatasetVersion:
        """Build and store one immutable preference dataset version.

        The Phase 15 rows come from the attached source unless the caller passes
        its own, exactly as Phase 16 does it. ``reviewed`` defaults to the pairs a
        person has actually settled: a review that has been decided is the
        strongest evidence there is, and a review that has NOT been decided must
        never be pulled in — which is why the queue's undecided rows are excluded
        rather than filtered later.
        """
        rows, stored = self._fetch_source()
        settled = tuple(reviewed) if reviewed else self.reviews.resolved_pairs()
        fixtures = benchmarks if benchmarks is not None else self._fetch_benchmarks()
        dataset = self.pair_builder.build(
            name=name,
            dataset_type=dataset_type,
            trajectories=tuple(trajectories) if trajectories is not None else rows,
            evaluations=tuple(evaluations) if evaluations is not None else stored,
            benchmarks=fixtures,
            extra_pairs=tuple(pairs),
            reviewed=settled,
            rules=rules,
            quality=quality,
            split=split,
            version=version,
            description=description,
            tags=tags,
            existing_versions=[
                item.version for item in self.pair_datasets.versions(name)
            ],
            source_datasets=source_datasets,
        )
        self.pair_datasets.save(dataset)
        self._announce(
            PREFERENCE_DATASET_BUILT,
            {
                "dataset_version": dataset.dataset_version_id,
                "dataset_type": dataset.dataset_type,
                "pairs": len(dataset),
                "accepted": len(dataset.accepted_pairs()),
                "sources": dict(dataset.preference_sources),
            },
        )
        if queue_for_review:
            self.queue_held(dataset)
        return dataset

    def pair_dataset(self, dataset_version_id: str) -> PreferenceDatasetVersion | None:
        return self.pair_datasets.get(dataset_version_id)

    def preference_datasets(
        self, *, dataset_type: str = "", name: str = "", limit: int = 0
    ) -> tuple[PreferenceDatasetVersion, ...]:
        return self.pair_datasets.list(dataset_type=dataset_type, name=name, limit=limit)

    def _fetch_benchmarks(self) -> Sequence[GoldenExample]:
        """The Phase 15 golden dataset's expectations, when the source has one.

        Read, never re-created: the benchmark source is the project's own fixture
        list, and a build that copied it would be comparing against a snapshot
        nobody updates. A source without one simply contributes no benchmark
        pairs, which is why this cannot fail a build.
        """
        source = self.source
        if source is None:
            return ()
        try:
            dataset = source.golden_dataset()
        except Exception:  # noqa: BLE001 - a missing fixture list is not a failure
            return ()
        examples = getattr(dataset, "examples", ())
        return tuple(item for item in examples if isinstance(item, GoldenExample))

    def validate_dataset(self, dataset_version_id: str) -> dict[str, Any]:
        """Every reason this version must not be trained on, plus what it holds.

        The queue count is reported with the verdict rather than hidden: a dataset
        whose pairs are still waiting for a person is not broken, and it must not
        be trained on either.
        """
        dataset = self.pair_dataset(dataset_version_id)
        if dataset is None:
            return {"ok": False, "reason": f"no preference dataset version {dataset_version_id!r}"}
        issues = self.pair_builder.validate(dataset)
        pending = len(
            [
                pair
                for pair in dataset.examples
                if pair.needs_review and not pair.reviewed
            ]
        )
        return {
            "ok": not issues,
            "dataset_version": dataset.dataset_version_id,
            "dataset_type": dataset.dataset_type,
            "issues": list(issues),
            "pairs": len(dataset),
            "accepted": len(dataset.accepted_pairs()),
            "review_required": pending,
            "splits": {name: len(ids) for name, ids in dataset.splits.items() if ids},
            "preference_sources": dict(dataset.preference_sources),
            "statistics": dataset.statistics.to_dict(),
            "source_data_version": dataset.source_data_version,
            "preprocessing_version": dataset.preprocessing_version,
        }

    def inspect_pair(self, dataset_version_id: str, preference_id: str) -> dict[str, Any]:
        """One pair, past its dataset-wide verdict: what it is and where it came from."""
        dataset = self.pair_dataset(dataset_version_id)
        if dataset is None:
            return {"ok": False, "reason": f"no preference dataset version {dataset_version_id!r}"}
        pair = dataset.example(preference_id)
        if pair is None:
            return {
                "ok": False,
                "reason": f"no pair {preference_id!r} in {dataset_version_id!r}",
            }
        split = next(
            (name for name in dataset.splits if preference_id in dataset.splits[name]), ""
        )
        return {
            "ok": True,
            "dataset_version": dataset.dataset_version_id,
            "dataset_type": dataset.dataset_type,
            "split": split,
            "pair": pair.to_dict(),
        }

    def queue_held(self, dataset: PreferenceDatasetVersion) -> tuple[str, ...]:
        """Put every held pair in front of a reviewer, once."""
        queued: list[str] = []
        for pair in dataset.examples:
            if not pair.needs_review or pair.reviewed:
                continue
            existing = self.reviews.repository.get(pair.preference_id)
            if existing is not None and existing.reviewed:
                continue
            result = self.reviews.enqueue(pair)
            if result.get("ok"):
                queued.append(pair.preference_id)
        if queued:
            self._announce(
                PREFERENCE_REVIEW_QUEUED,
                {"dataset_version": dataset.dataset_version_id, "pairs": len(queued)},
            )
        return tuple(queued)

    # ── human review ─────────────────────────────────────────────────────────

    def submit_pair(
        self,
        *,
        dataset_type: PreferenceDatasetType | str,
        prompt: Mapping[str, Any],
        chosen: Mapping[str, Any],
        rejected: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
        chosen_outcome: Mapping[str, Any] | None = None,
        rejected_outcome: Mapping[str, Any] | None = None,
        reviewer: str = "",
        reason: str = "",
        confidence: float = 1.0,
        group_key: str = "",
        tags: Sequence[str] = (),
        enqueue: bool = False,
    ) -> dict[str, Any]:
        """Record a preference a person is asserting (the strongest source there is)."""
        wanted = (
            dataset_type.value
            if isinstance(dataset_type, PreferenceDatasetType)
            else str(dataset_type)
        )
        return self.reviews.submit(
            dataset_type=wanted,
            prompt=prompt,
            chosen=chosen,
            rejected=rejected,
            context=context,
            chosen_outcome=chosen_outcome,
            rejected_outcome=rejected_outcome,
            reviewer=reviewer,
            reason=reason,
            confidence=confidence,
            group_key=group_key,
            tags=tags,
            enqueue=enqueue,
        )

    def reviews_list(
        self, *, pending_only: bool = True, limit: int = 0, dataset_version: str = ""
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            item.to_dict()
            for item in self.reviews.queue(
                pending_only=pending_only, limit=limit, dataset_version=dataset_version
            )
        )

    def review(self, preference_id: str) -> dict[str, Any] | None:
        item = self.reviews.item(preference_id)
        return item.to_dict() if item is not None else None

    def decide_review(
        self,
        preference_id: str,
        decision: str,
        *,
        reviewer: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        """Settle a queued pair. Choosing B swaps the pair rather than forking it."""
        result = self.reviews.decide(
            preference_id, decision, reviewer=reviewer, reason=reason
        )
        if result.get("ok"):
            self._announce(
                PREFERENCE_REVIEW_DECIDED,
                {
                    "preference_id": preference_id,
                    "decision": result.get("decision", decision),
                    "reviewer": reviewer,
                },
            )
        return result

    def review_stats(self) -> dict[str, Any]:
        return self.reviews.stats()

    # ── configuration ────────────────────────────────────────────────────────

    def resolve_preference_config(
        self,
        config: PreferenceTrainingConfig | TrainingConfig | Mapping[str, Any] | None = None,
        *,
        algorithm: str = "",
    ) -> PreferenceTrainingConfig:
        """This deployment's preference defaults, overridden by what the caller named.

        A ``TrainingConfig`` (or its mapping) is accepted as well: the two
        configurations share most of their fields, and an operator who has tuned a
        Phase 16 run should be able to carry that tuning into a preference run
        rather than retyping it. A value of ``None`` means "not stated" and does
        not clobber a real default.
        """
        defaults = dict(self.preference_defaults.to_mapping())
        if isinstance(config, PreferenceTrainingConfig):
            merged = {**defaults, **dict(config.to_mapping())}
        elif isinstance(config, TrainingConfig):
            merged = {
                **defaults,
                **dict(PreferenceTrainingConfig.from_training_config(config).to_mapping()),
            }
        else:
            merged = dict(defaults)
            for key, value in dict(config or {}).items():
                if value is not None:
                    merged[str(key)] = value
        if _text(algorithm):
            merged["algorithm"] = _text(algorithm)
        return PreferenceTrainingConfig.from_mapping(merged).with_defaults(
            output_directory=str(self.output_root)
        )

    def algorithms(self) -> dict[str, Any]:
        """The two objectives as documentation, plus what this machine can do."""
        capabilities = self.preference_estimator.capabilities()
        return {
            "algorithms": {name: dict(description) for name, description in OBJECTIVES.items()},
            "dry_run_supported": True,
            "missing_dependencies": list(capabilities.missing_dependencies()),
            "training_dependencies_ready": capabilities.training_dependencies_ready,
            "note": (
                "a dry run walks the schedule without training and works with no "
                "training dependencies installed; a real run needs them, the "
                "deployment's permission and an explicit confirmation"
            ),
        }

    # ── runs ─────────────────────────────────────────────────────────────────

    def create_run(
        self,
        model: str,
        dataset_version: str,
        *,
        config: PreferenceTrainingConfig | TrainingConfig | Mapping[str, Any] | None = None,
        name: str = "",
    ) -> TrainingRun:
        """Validate a preference configuration, price it, and store a CREATED run."""
        dataset = self.pair_dataset(dataset_version)
        if dataset is None:
            raise ValueError(
                f"no preference dataset version {dataset_version!r}: build it first"
            )
        if not dataset.examples:
            raise ValueError(f"preference dataset {dataset_version!r} has no pairs")
        cfg = self.resolve_preference_config(config)
        cfg = replace(
            cfg,
            base_model=_text(model) or cfg.base_model,
            preference_dataset_version=dataset_version,
            dataset_type=cfg.dataset_type or dataset.dataset_type,
            max_checkpoints=max(1, cfg.max_checkpoints),
        )
        validation = cfg.validate()
        if not validation.valid:
            raise ValueError(
                "invalid preference training configuration: " + "; ".join(validation.errors)
            )
        estimate = self.preference_estimator.estimate(cfg, dataset=dataset)
        run = TrainingRun(
            name=name or f"{cfg.base_model or 'model'}+{cfg.algorithm}@{dataset.version}",
            model=cfg.base_model,
            dataset_version=dataset_version,
            dataset_type=dataset.dataset_type,
            training_config=self._stored_config(cfg, dataset_version),
            estimate=estimate.to_dict(),
            backend="dry_run" if cfg.dry_run else estimate.backend,
            algorithm=cfg.algorithm,
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

    def preference_runs(
        self, *, algorithm: str = "", status: str = "", limit: int = 0
    ) -> tuple[TrainingRun, ...]:
        """The runs this manager owns, filtered by objective when asked."""
        wanted = _text(algorithm).lower()
        found = [
            run
            for run in self.runs_list(status=status, dataset_version="", limit=0)
            if run.algorithm in ALGORITHMS and (not wanted or run.algorithm == wanted)
        ]
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)

    def estimate_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        """Price an ad-hoc preference configuration without creating a run."""
        cfg = self.resolve_preference_config(config)
        validation = cfg.validate()
        dataset = (
            self.pair_dataset(cfg.preference_dataset_version)
            if cfg.preference_dataset_version
            else None
        )
        estimate = self.preference_estimator.estimate(cfg, dataset=dataset)
        payload = {
            "valid": validation.valid,
            "errors": list(validation.errors),
            "warnings": list(validation.warnings),
            "algorithm": cfg.algorithm,
            "objective": objective_for(cfg.algorithm),
            "needs_reference_model": cfg.needs_reference_model,
            "effective_config": cfg.to_mapping(),
            "estimate": estimate.to_dict(),
        }
        if dataset is not None:
            payload["dataset"] = self._dataset_summary(dataset)
        return payload

    def estimate_run(self, run_id: str) -> dict[str, Any]:
        """Re-price a stored run against the machine as it is now."""
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
            "algorithm": run.algorithm,
            "estimate": estimate.to_dict(),
        }

    def dry_run(
        self,
        model: str = "",
        dataset_version: str = "",
        *,
        config: PreferenceTrainingConfig | TrainingConfig | Mapping[str, Any] | None = None,
        algorithm: str = "",
    ) -> dict[str, Any]:
        """Validate, price and describe a run — and start nothing.

        This is the operation an operator or a CI job uses to answer "would this
        work here?" without a training dependency installed and without touching a
        model. It reports the intended objective, the dataset statistics, the
        resource verdict, the backend and whether the output directory is usable,
        and it records ``started: False`` so nothing that reads the answer can
        mistake it for a run.
        """
        dataset = self.pair_dataset(dataset_version) if dataset_version else None
        cfg = self.resolve_preference_config(config, algorithm=algorithm)
        cfg = replace(
            cfg,
            base_model=_text(model) or cfg.base_model,
            preference_dataset_version=dataset_version or cfg.preference_dataset_version,
            dataset_type=cfg.dataset_type or (dataset.dataset_type if dataset else ""),
            dry_run=True,
        )
        validation = cfg.validate()
        issues = self.pair_builder.validate(dataset) if dataset is not None else ()
        trainer = self._preference_trainer(cfg, None)
        prepared = trainer.prepare_dataset(dataset) if dataset is not None else {}
        estimate = self.preference_estimator.estimate(cfg, dataset=dataset)
        output = self._output_directory_report(cfg.output_directory)
        payload: dict[str, Any] = {
            "ok": not validation.errors and not issues and output["ok"],
            "dry_run": True,
            "started": False,
            "algorithm": cfg.algorithm,
            "objective": objective_for(cfg.algorithm),
            "needs_reference_model": cfg.needs_reference_model,
            "config": cfg.to_mapping(),
            "errors": list(validation.errors),
            "warnings": list(validation.warnings),
            "dataset_issues": list(issues),
            "dataset": self._dataset_summary(dataset) if dataset is not None else {},
            "prepared": prepared,
            "estimate": estimate.to_dict(),
            "output_directory": output,
            "backend": {
                "name": trainer.name,
                "backend": trainer.backend,
                "available": trainer.is_available(),
                "missing_dependencies": list(trainer.missing_dependencies()),
                "lora": bool(cfg.use_lora),
                "training_method": cfg.effective_method,
            },
            "note": (
                "a dry run validates the dataset, the configuration, the backend and "
                "the output directory, and prices the run; it never starts training"
            ),
        }
        if dataset_version and dataset is None:
            payload["ok"] = False
            payload["errors"] = [
                *payload["errors"],
                f"no preference dataset version {dataset_version!r}",
            ]
        self._announce(
            PREFERENCE_DRY_RUN,
            {
                "dataset_version": cfg.preference_dataset_version,
                "algorithm": cfg.algorithm,
                "ok": payload["ok"],
                "verdict": estimate.level,
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
        split: str = "test",
        tolerance: float | None = None,
        run_id: str = "",
        algorithm: str = "",
    ) -> dict[str, Any]:
        """Base vs SFT vs preference-optimized on the same held-out pairs.

        The comparison is stored, so the verdict it produced is the one a later
        approval checks — a model cannot be approved on a comparison nobody can
        read back.
        """
        dataset = self.pair_dataset(dataset_version)
        if dataset is None:
            return {"ok": False, "reason": f"no preference dataset version {dataset_version!r}"}
        comparison = self.preference_evaluator.compare(
            dataset,
            base,
            candidate,
            sft=sft,
            split=split,
            run_id=run_id,
            model_id="",
            tolerance=tolerance,
            algorithm=algorithm,
        )
        self.evaluations.save(comparison.evaluation)
        self._announce(
            PREFERENCE_COMPARISON,
            {
                "dataset_version": dataset_version,
                "verdict": comparison.verdict,
                "algorithm": algorithm,
                "areas_regressed": list(comparison.regressions.areas_regressed),
            },
        )
        return {
            "ok": comparison.verdict != "regress",
            "dataset_version": dataset_version,
            "comparison": comparison.to_dict(),
        }

    def evaluate_run(
        self,
        run_id: str,
        *,
        base: ModelPredictor | None = None,
        candidate: ModelPredictor | None = None,
        sft: ModelPredictor | None = None,
        split: str = "test",
        tolerance: float | None = None,
    ) -> dict[str, Any]:
        """Evaluate a completed preference run against the model it came from.

        Inherits Phase 16's rule and states it again: without two predictors there
        is nothing honest to measure. The run is recorded as ``skipped`` and
        approval stays impossible, because the preference loss is not an
        evaluation — it is not even consulted here.
        """
        run = self.runs.get(run_id)
        if run is None:
            return {"ok": False, "reason": f"no run {run_id!r}"}
        if run.status != TrainingRunStatus.COMPLETED.value:
            return {
                "ok": False,
                "reason": f"run {run_id!r} is {run.status}: only a completed run is evaluated",
            }
        model = next(
            (item for item in self.models.list() if item.training_run_id == run_id), None
        )
        if model is None:
            return {"ok": False, "reason": f"no model is registered for run {run_id!r}"}
        dataset = self.pair_dataset(run.dataset_version)
        if dataset is None:
            return {
                "ok": False,
                "reason": f"the preference dataset {run.dataset_version!r} is missing",
            }
        if base is None or candidate is None:
            skipped = TrainingEvaluation(
                evaluation_id=f"pe-{run_id}",
                run_id=run_id,
                model_id=model.model_id,
                dataset_version=run.dataset_version,
                split=split,
                verdict="skipped",
                reason=(
                    "no base and candidate predictors were supplied, so nothing was "
                    "measured — a preference loss is not an evaluation"
                ),
                details={"algorithm": run.algorithm, "loss_consulted": False},
            )
            self.evaluations.save(skipped)
            return {
                "ok": False,
                "skipped": True,
                "reason": (
                    "an evaluation needs a base predictor and a candidate predictor; "
                    "none was supplied, so nothing was measured"
                ),
                "evaluation": skipped.to_dict(),
            }
        record, reason = self.registry.evaluate_now(model.model_id)
        if record is None:
            return {"ok": False, "reason": reason}
        comparison = self.preference_evaluator.compare(
            dataset,
            base,
            candidate,
            sft=sft,
            split=split,
            run_id=run_id,
            model_id=record.model_id,
            tolerance=tolerance,
            algorithm=run.algorithm,
        )
        evaluation = comparison.evaluation
        self.evaluations.save(evaluation)
        outcome = self.registry.record_evaluation(record.model_id, evaluation)
        self.runs.save(
            replace(
                run,
                evaluation_metrics=dict(evaluation.deltas),
                updated_at=now_iso(),
            )
        )
        self._announce(
            EVALUATION_COMPLETED,
            {
                "run_id": run_id,
                "model_id": record.model_id,
                "verdict": evaluation.verdict,
                "regressions": list(evaluation.regressions),
                "algorithm": run.algorithm,
            },
        )
        return {
            "ok": bool(outcome.get("ok")),
            "evaluation": evaluation.to_dict(),
            "comparison": comparison.to_dict(),
            "model": outcome.get("model", {}),
            "reason": outcome.get("reason", ""),
        }

    # ── reporting ────────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        payload = super().status()
        runs = self.preference_runs()
        payload["datasets"] = {
            "count": len(self.pair_datasets.list(limit=0)),
            "names": sorted({item.name for item in self.pair_datasets.list(limit=0)}),
        }
        payload["runs"]["by_algorithm"] = {
            name: sum(1 for run in runs if run.algorithm == name) for name in ALGORITHMS
        }
        payload["reviews"] = self.reviews.stats()
        payload["algorithms"] = {
            name: {"needs_reference_model": OBJECTIVES[name]["needs_reference_model"]}
            for name in ALGORITHMS
        }
        payload["defaults"]["algorithm"] = self.preference_defaults.algorithm
        payload["defaults"]["beta"] = self.preference_defaults.beta
        return payload

    def summary(self) -> dict[str, Any]:
        payload = super().summary()
        runs = self.preference_runs()
        payload["datasets"] = [item.dataset_version_id for item in self.pair_datasets.list()]
        payload["runs"] = [
            {
                "run_id": run.run_id,
                "model": run.model,
                "dataset_version": run.dataset_version,
                "algorithm": run.algorithm,
                "status": run.status,
                "backend": run.backend,
                "training_loss": run.training_loss,
                "preference_metrics": dict(run.preference_metrics),
            }
            for run in runs[:20]
        ]
        payload["reviews"] = self.reviews.stats()
        return payload

    # ── the three seams ──────────────────────────────────────────────────────

    def _dataset_for(self, run: TrainingRun) -> Any:
        """A preference run references a pair dataset; the parent's store is a fallback."""
        dataset = self.pair_datasets.get(run.dataset_version)
        return dataset if dataset is not None else self.datasets.get(run.dataset_version)

    def _estimate_for(
        self,
        cfg: TrainingConfig,
        dataset: Any,
        run: TrainingRun | None = None,
    ) -> ResourceEstimate:
        """The pair-aware estimate: the reference model DPO keeps is counted here."""
        preference = (
            self._config_from_run(run)
            if run is not None
            else PreferenceTrainingConfig.from_training_config(cfg)
        )
        return self.preference_estimator.estimate(preference, dataset=dataset)

    def _trainer_for(
        self, cfg: TrainingConfig, run: TrainingRun | None = None
    ) -> SFTTrainer:
        """The backend the algorithm asks for (dry run, DPO or ORPO)."""
        preference = (
            self._config_from_run(run)
            if run is not None
            else PreferenceTrainingConfig.from_training_config(cfg)
        )
        return self._preference_trainer(preference, run)

    def _preference_trainer(
        self, cfg: PreferenceTrainingConfig, run: TrainingRun | None
    ) -> PreferenceTrainer:
        if self._preference_trainer_factory is not None:
            return self._preference_trainer_factory(cfg, run)
        return preference_trainer_for(
            cfg,
            estimator=self.estimator,
            runner=self._preference_runner,
        )

    def _config_from_run(self, run: TrainingRun) -> PreferenceTrainingConfig:
        """The preference configuration a stored run carries."""
        return PreferenceTrainingConfig.from_mapping(run.training_config).with_defaults(
            output_directory=str(self.output_root)
        )

    @staticmethod
    def _stored_config(
        cfg: PreferenceTrainingConfig, dataset_version: str
    ) -> dict[str, Any]:
        """The configuration a run row carries, readable by BOTH phases.

        The parent's lifecycle reads a run's configuration back as a
        :class:`TrainingConfig` (to check dry-run, resume and validation before it
        starts anything), so the stored mapping has to be a valid view of that
        shape as well: ``dataset_version`` is added as an alias for the preference
        field, and ``dataset_type`` is left out because the run record already
        carries the pair family and the supervised validator only knows the
        supervised ones. The preference fields ride along untouched, which is how
        the algorithm, beta and reference model survive a restart.
        """
        stored = dict(cfg.to_mapping())
        stored.pop("dataset_type", None)
        stored["dataset_version"] = dataset_version
        return stored

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _dataset_summary(dataset: PreferenceDatasetVersion | None) -> dict[str, Any]:
        if dataset is None:
            return {}
        return {
            "dataset_version": dataset.dataset_version_id,
            "dataset_type": dataset.dataset_type,
            "pairs": len(dataset),
            "accepted": len(dataset.accepted_pairs()),
            "splits": {name: len(ids) for name, ids in dataset.splits.items() if ids},
            "preference_sources": dict(dataset.preference_sources),
            "statistics": dataset.statistics.to_dict(),
            "source_datasets": list(dataset.source_datasets),
            "preprocessing_version": dataset.preprocessing_version,
            "source_data_version": dataset.source_data_version,
        }

    @staticmethod
    def _output_directory_report(path: str) -> dict[str, Any]:
        """Whether a run could write where it was told to — checked, not created.

        A dry run does not create the directory: reporting that a path is usable
        and then leaving the filesystem as it was is the honest sequence, and it is
        what makes the check safe to run against a directory a reviewer only meant
        to inspect.
        """
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

    def pending_reviews(self, limit: int = 0) -> tuple[PreferenceExample, ...]:
        """The pairs waiting on a person, as the builder would consume them."""
        return self.reviews.repository.pending(limit=limit)

    def held_pairs(self, dataset_version: str) -> tuple[PreferenceExample, ...]:
        dataset = self.pair_dataset(dataset_version)
        if dataset is None:
            return ()
        return tuple(
            pair
            for pair in dataset.examples
            if pair.quality_status
            in {PreferenceQualityStatus.NEEDS_REVIEW.value, PreferenceQualityStatus.REJECTED.value}
        )


__all__ = [
    "ALGORITHMS",
    "PREFERENCE_COMPARISON",
    "PREFERENCE_DATASET_BUILT",
    "PREFERENCE_DRY_RUN",
    "PREFERENCE_REVIEW_DECIDED",
    "PREFERENCE_REVIEW_QUEUED",
    "PreferenceManager",
    "PreferenceTrainerFactory",
]
