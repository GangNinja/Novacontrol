"""The training manager: the order everything runs in, and the safety rails.

One component owns the sequence — build a dataset, create a run, estimate the
cost, execute a backend, checkpoint as it goes, evaluate the result, register
the model — and every part of it is wrapped so that a failure is RECORDED on the
run rather than raised into whatever asked for it. The API, the CLI and the
EventBus module all call this class; none of them knows a training library
exists.

The rails that matter:

  * **nothing big starts by accident.** A run is created ``dry_run`` by default
    and starting one in real mode needs ``confirm=True`` in addition to the
    configuration saying so.
  * **an unsafe estimate refuses.** When the estimator says ``UNSAFE``, start
    returns a refusal naming the reasons; only an explicit ``override=True``
    proceeds, and the override is recorded in the run's history.
  * **a completed run is EXPERIMENTAL, never production.** Registration happens
    here; approval and promotion are explicit registry operations.
  * **evaluation needs predictors.** The manager will not invent a candidate
    model to compare: without two predictors the run gets an evaluation record
    marked ``skipped`` and approval stays impossible.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from novacontrol.evaluation.models import AgentTrajectory, now_iso
from novacontrol.training.backends import (
    DryRunTrainer,
    PeftLoraBackend,
    SFTTrainer,
    TrainingBackendUnavailable,
    TrainingCallbacks,
)
from novacontrol.training.checkpoints import CheckpointManager
from novacontrol.training.config import TrainingConfig
from novacontrol.training.datasets import SFTDatasetBuilder
from novacontrol.training.evaluation import ModelPredictor, TrainingEvaluator
from novacontrol.training.models import (
    DatasetType,
    ResourceEstimate,
    ResourceVerdict,
    SelectionRules,
    SFTDatasetVersion,
    SplitConfig,
    TrainingEvaluation,
    TrainingModelRecord,
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

#: Custom events this manager publishes. They describe the training layer's own
#: progress and are not part of the lifecycle vocabulary.
DATASET_BUILT = "training.dataset_built"
RUN_CREATED = "training.run_created"
RUN_STARTED = "training.run_started"
RUN_COMPLETED = "training.run_completed"
RUN_FAILED = "training.run_failed"
RUN_CANCELLED = "training.run_cancelled"
MODEL_REGISTERED = "training.model_registered"
EVALUATION_COMPLETED = "training.evaluation_completed"

Publisher = Callable[[str, Mapping[str, Any]], None]
TrainerFactory = Callable[[TrainingConfig], SFTTrainer]

#: Statuses a run can be resumed from.
_RESUMABLE = {
    TrainingRunStatus.PAUSED.value,
    TrainingRunStatus.FAILED.value,
    TrainingRunStatus.CANCELLED.value,
}

#: Statuses a run can be started from.
_STARTABLE = {TrainingRunStatus.CREATED.value, TrainingRunStatus.VALIDATING.value}


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


class TrainingManager:
    """Builds datasets, runs trainers, keeps checkpoints and the model registry."""

    def __init__(
        self,
        *,
        datasets: DatasetVersionRepository,
        runs: TrainingRunRepository,
        checkpoints: CheckpointRepository,
        models: TrainingModelRepository,
        evaluations: TrainingEvaluationRepository,
        source: Any = None,
        output_root: str | Path = "",
        default_config: TrainingConfig | None = None,
        estimator: ResourceEstimator | None = None,
        evaluator: TrainingEvaluator | None = None,
        builder: SFTDatasetBuilder | None = None,
        registry: SFTModelRegistry | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        trainer_factory: TrainerFactory | None = None,
        max_checkpoints: int = 3,
        publish: Publisher | None = None,
    ) -> None:
        self.datasets = datasets
        self.runs = runs
        self.checkpoints_repo = checkpoints
        self.models = models
        self.evaluations = evaluations
        self.source = source
        self.output_root = Path(output_root) if output_root else Path("training_output")
        self.default_config = default_config if default_config is not None else TrainingConfig()
        self.estimator = estimator if estimator is not None else ResourceEstimator()
        self.evaluator = evaluator if evaluator is not None else TrainingEvaluator()
        self.builder = builder if builder is not None else SFTDatasetBuilder()
        self.registry = registry if registry is not None else SFTModelRegistry(models)
        self.checkpoint_manager = (
            checkpoint_manager
            if checkpoint_manager is not None
            else CheckpointManager(
                self.output_root / "checkpoints",
                max_checkpoints=max_checkpoints,
                repository=checkpoints,
            )
        )
        self._trainer_factory = trainer_factory
        self._publish = publish
        self.failures = 0
        self._cancelled: set[str] = set()
        self._paused: set[str] = set()
        self._active: set[str] = set()

    # -- configuration ---------------------------------------------------------

    def set_publisher(self, publish: Publisher | None) -> None:
        self._publish = publish

    def set_source(self, source: Any) -> None:
        """Point the manager at the Phase 15 service it builds datasets from."""
        self.source = source

    def _announce(self, type_: str, payload: Mapping[str, Any]) -> None:
        if self._publish is None:
            return
        try:
            self._publish(type_, dict(payload))
        except Exception as exc:  # noqa: BLE001 - announcing is not the work
            self.failures += 1
            _logger.warning("Could not publish %s: %s: %s", type_, type(exc).__name__, exc)

    # -- datasets --------------------------------------------------------------

    def _fetch_source(self) -> tuple[tuple[AgentTrajectory, ...], tuple[Any, ...]]:
        """The Phase 15 rows to build from (empty when no source is attached)."""
        source = self.source
        if source is None:
            return ((), ())
        trajectories: Sequence[AgentTrajectory]
        evaluations: Sequence[Any]
        try:
            trajectories = source.trajectories.list()
        except Exception:  # noqa: BLE001 - a missing store means "nothing to build from"
            trajectories = ()
        try:
            evaluations = source.evaluations.list()
        except Exception:  # noqa: BLE001 - a missing store means "nothing to build from"
            evaluations = ()
        return (tuple(trajectories), tuple(evaluations))

    def create_dataset(
        self,
        name: str,
        dataset_type: DatasetType | str,
        *,
        rules: SelectionRules | None = None,
        split: SplitConfig | None = None,
        version: str = "",
        description: str = "",
        tags: Sequence[str] = (),
        trajectories: Sequence[AgentTrajectory] | None = None,
        evaluations: Sequence[Any] | None = None,
    ) -> SFTDatasetVersion:
        """Build and store one immutable dataset version."""
        rows, stored = self._fetch_source()
        dataset = self.builder.build(
            name=name,
            dataset_type=dataset_type,
            trajectories=tuple(trajectories) if trajectories is not None else rows,
            evaluations=tuple(evaluations) if evaluations is not None else stored,
            rules=rules,
            split=split,
            version=version,
            description=description,
            tags=tags,
            existing_versions=[item.version for item in self.datasets.versions(name)],
        )
        self.datasets.save(dataset)
        self._announce(
            DATASET_BUILT,
            {
                "dataset_version": dataset.dataset_version_id,
                "dataset_type": dataset.dataset_type,
                "examples": len(dataset),
            },
        )
        return dataset

    def dataset(self, dataset_version_id: str) -> SFTDatasetVersion | None:
        return self.datasets.get(dataset_version_id)

    def _dataset_for(self, run: TrainingRun) -> Any:
        """The dataset a run references, in the shape its trainer expects.

        The single seam a later phase needs to reuse the whole run lifecycle —
        start, resume, checkpoints, registration, evaluation — for a dataset of
        its own: Phase 17's preference manager overrides this one method and
        inherits the orchestration unchanged. Deliberately typed ``Any``: the
        return type is whatever the concrete manager stores, and a supervised
        dataset is only what *this* one stores.
        """
        return self.dataset(run.dataset_version)

    def _estimate_for(
        self,
        cfg: TrainingConfig,
        dataset: Any,
        run: TrainingRun | None = None,
    ) -> ResourceEstimate:
        """What one configuration would cost on the machine as it is now.

        Overridden by a phase whose configuration carries costs a supervised run
        does not (Phase 17 counts the DPO reference model). The default is the
        Phase 16 estimator, so nothing changes for supervised runs. ``run`` is the
        row whose stored configuration a subclass may need to read its own fields
        back out of; a supervised run keeps everything in ``cfg``.
        """
        return self.estimator.estimate(cfg, dataset=dataset)

    def datasets_list(
        self, *, dataset_type: str = "", name: str = "", limit: int = 0
    ) -> tuple[SFTDatasetVersion, ...]:
        return self.datasets.list(dataset_type=dataset_type, name=name, limit=limit)

    def validate_dataset(self, dataset_version_id: str) -> dict[str, Any]:
        dataset = self.dataset(dataset_version_id)
        if dataset is None:
            return {"ok": False, "reason": f"no dataset version {dataset_version_id!r}"}
        issues = self.builder.validate(dataset)
        return {
            "ok": not issues,
            "dataset_version": dataset.dataset_version_id,
            "issues": list(issues),
            "examples": len(dataset),
            "splits": {name: len(ids) for name, ids in dataset.splits.items() if ids},
        }

    # -- runs ------------------------------------------------------------------

    def create_run(
        self,
        model: str,
        dataset_version: str,
        *,
        config: TrainingConfig | Mapping[str, Any] | None = None,
        name: str = "",
    ) -> TrainingRun:
        """Validate a configuration, estimate its cost, and store a CREATED run."""
        dataset = self.dataset(dataset_version)
        if dataset is None:
            raise ValueError(f"no dataset version {dataset_version!r}: build it first")
        if not dataset.examples:
            raise ValueError(f"dataset {dataset_version!r} has no examples")
        cfg = self.resolve_config(config)
        cfg = replace(
            cfg,
            base_model=_text(model) or cfg.base_model,
            dataset_version=dataset_version,
            dataset_type=dataset.dataset_type,
            max_checkpoints=max(1, cfg.max_checkpoints),
        )
        validation = cfg.validate()
        if not validation.valid:
            raise ValueError("invalid training configuration: " + "; ".join(validation.errors))
        estimate = self._estimate_for(cfg, dataset)
        run = TrainingRun(
            name=name or f"{cfg.base_model or 'model'}@{dataset.version}",
            model=cfg.base_model,
            dataset_version=dataset_version,
            dataset_type=dataset.dataset_type,
            training_config=cfg.to_mapping(),
            estimate=estimate.to_dict(),
            backend="dry_run" if cfg.dry_run else estimate.backend,
            random_seed=cfg.seed,
        )
        self.runs.save(run)
        self._announce(RUN_CREATED, {"run_id": run.run_id, "dataset_version": dataset_version})
        return run

    def run(self, run_id: str) -> TrainingRun | None:
        return self.runs.get(run_id)

    def runs_list(
        self, *, status: str = "", model: str = "", dataset_version: str = "", limit: int = 0
    ) -> tuple[TrainingRun, ...]:
        return self.runs.list(
            status=status, model=model, dataset_version=dataset_version, limit=limit
        )

    def estimate_run(self, run_id: str) -> dict[str, Any]:
        """Re-estimate a run against the machine as it is NOW."""
        run = self.runs.get(run_id)
        if run is None:
            return {"ok": False, "reason": f"no run {run_id!r}"}
        dataset = self._dataset_for(run)
        cfg = TrainingConfig.from_mapping(run.training_config)
        estimate = self._estimate_for(cfg, dataset, run)
        self.runs.save(replace(run, estimate=estimate.to_dict(), updated_at=now_iso()))
        return {"ok": True, "run_id": run_id, "estimate": estimate.to_dict()}

    def estimate_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        """Estimate an ad-hoc configuration without creating a run."""
        cfg = self.resolve_config(config)
        validation = cfg.validate()
        dataset = self.dataset(cfg.dataset_version) if cfg.dataset_version else None
        estimate = self._estimate_for(cfg, dataset)
        return {
            "valid": validation.valid,
            "errors": list(validation.errors),
            "warnings": list(validation.warnings),
            "effective_config": cfg.to_mapping(),
            "estimate": estimate.to_dict(),
        }

    def resolve_config(
        self, config: TrainingConfig | Mapping[str, Any] | None = None
    ) -> TrainingConfig:
        """The configuration that would really run: this deployment's defaults,
        overridden by whatever the caller named.

        There is ONE default table (``default_config``, built from the config
        file and the user settings) and ONE place that merges it, so an operator
        who sets ``training.defaults: {epochs: 3}`` gets that for every run built
        by the API, the CLI or a script. Merging the caller's mapping over the
        dataclass defaults instead would silently discard the deployment's
        configuration — which is how a "learning_rate: 0.0001" in the config
        file turns into 0.0002 in a run and nobody notices.

        A value of ``None`` means "not stated" rather than "unset", so it does
        not clobber a real default; anything else in the caller's mapping wins.
        """
        if isinstance(config, TrainingConfig):
            merged: dict[str, Any] = dict(config.to_mapping())
        else:
            merged = dict(self.default_config.to_mapping())
            for key, value in dict(config or {}).items():
                if value is not None:
                    merged[str(key)] = value
        return TrainingConfig.from_mapping(merged).with_defaults(
            output_directory=str(self.output_root)
        )

    def _real_training_blocked(self, cfg: TrainingConfig) -> dict[str, Any] | None:
        """Refuse a real run when this deployment switched real training off.

        ``default_config.dry_run`` is the deployment's answer (the config
        section OR the user setting). A caller may build a config with
        ``dry_run=False`` to see what it would cost, but STARTING it here would
        break the promise the switch makes, so the floor is enforced at the
        point of action rather than trusted to every caller.
        """
        if cfg.dry_run or not self.default_config.dry_run:
            return None
        return {
            "ok": False,
            "refused": True,
            "reason": (
                "real training is switched off in this installation "
                "(training.dry_run / the training dry-run setting)"
            ),
        }

    def _trainer_for(self, cfg: TrainingConfig, run: TrainingRun | None = None) -> SFTTrainer:
        """The backend one configuration asks for.

        ``run`` is passed so a phase whose configuration is a SUPERSET of this one
        can rebuild it from the row it stored (Phase 17 reads the algorithm and
        beta back out of the run); the supervised path ignores it.
        """
        if cfg.dry_run:
            return DryRunTrainer(cfg, estimator=self.estimator)
        if self._trainer_factory is not None:
            return self._trainer_factory(cfg)
        return PeftLoraBackend(cfg, estimator=self.estimator)

    def start(
        self, run_id: str, *, override: bool = False, confirm: bool = False
    ) -> dict[str, Any]:
        """Validate, estimate, execute. Returns a structured result, never raises."""
        run = self.runs.get(run_id)
        if run is None:
            return {"ok": False, "reason": f"no run {run_id!r}"}
        if run.terminal:
            return {"ok": False, "reason": f"run {run_id!r} is already {run.status}"}
        if run.status not in _STARTABLE:
            return {
                "ok": False,
                "reason": f"a {run.status} run cannot be started; resume it instead",
            }
        dataset = self._dataset_for(run)
        if dataset is None:
            return self._fail(run, f"the dataset {run.dataset_version!r} no longer exists")
        cfg = TrainingConfig.from_mapping(run.training_config)
        blocked = self._real_training_blocked(cfg)
        if blocked is not None:
            return {**blocked, "run_id": run.run_id}
        validation = cfg.validate()
        if not validation.valid:
            return self._fail(run, "invalid configuration: " + "; ".join(validation.errors))
        estimate = self._estimate_for(cfg, dataset, run)
        run = self.runs.save(
            replace(
                run,
                estimate=estimate.to_dict(),
                backend="dry_run" if cfg.dry_run else estimate.backend,
                updated_at=now_iso(),
            )
        )
        if estimate.level == ResourceVerdict.UNSAFE.value and not override:
            return {
                "ok": False,
                "refused": True,
                "run_id": run.run_id,
                "reason": "the resource estimate is unsafe; pass override=True to proceed",
                "estimate": estimate.to_dict(),
            }
        if not cfg.dry_run and not confirm:
            return {
                "ok": False,
                "refused": True,
                "run_id": run.run_id,
                "reason": (
                    "this is a real (non-dry-run) training configuration: pass confirm=True "
                    "to start it"
                ),
                "estimate": estimate.to_dict(),
            }
        return self._execute(run, dataset, cfg, override=override)

    def resume(self, run_id: str, *, checkpoint_id: str = "") -> dict[str, Any]:
        """Continue a paused, failed or cancelled run from a loadable checkpoint."""
        run = self.runs.get(run_id)
        if run is None:
            return {"ok": False, "reason": f"no run {run_id!r}"}
        if run.status not in _RESUMABLE:
            return {
                "ok": False,
                "reason": f"a {run.status} run is not resumable",
            }
        dataset = self._dataset_for(run)
        if dataset is None:
            return self._fail(run, f"the dataset {run.dataset_version!r} no longer exists")
        if checkpoint_id:
            record = self.checkpoints_repo.get(checkpoint_id)
            if record is None or record.run_id != run_id:
                return {"ok": False, "reason": f"no checkpoint {checkpoint_id!r} for this run"}
            checked = self.checkpoint_manager.validate(record)
            if not checked.loadable:
                return {
                    "ok": False,
                    "reason": f"checkpoint {checkpoint_id!r} cannot be loaded: {checked.reason}",
                }
        else:
            found, reason = self.checkpoint_manager.resume_point(run_id)
            if found is None:
                return {"ok": False, "reason": reason}
            checked = found
        cfg = replace(
            TrainingConfig.from_mapping(run.training_config),
            resume_from_checkpoint=checked.checkpoint_id,
        )
        blocked = self._real_training_blocked(cfg)
        if blocked is not None:
            return {**blocked, "run_id": run.run_id}
        return self._execute(run, dataset, cfg, resume_step=checked.step)

    def _execute(
        self,
        run: TrainingRun,
        dataset: SFTDatasetVersion,
        cfg: TrainingConfig,
        *,
        override: bool = False,
        resume_step: int | None = None,
    ) -> dict[str, Any]:
        try:
            trainer = self._trainer_for(cfg, run)
        except TrainingBackendUnavailable as exc:
            # An unavailable backend is an explained condition, not a failure:
            # the diagnostics row already says the extras are missing, so it is
            # reported without inflating the failure count.
            return self._fail(run, str(exc))
        except Exception as exc:  # noqa: BLE001 - a backend that will not build is news, not a crash
            # Choosing a backend is part of starting a run, and a real backend
            # that cannot even be constructed (broken import, no runner wired)
            # has to fail the RUN — otherwise the exception escapes the API/CLI
            # and the caller loses the run it just made.
            self.failures += 1
            return self._fail(run, f"{type(exc).__name__}: {exc}")
        validation = trainer.validate_config()
        if not validation.valid:
            return self._fail(run, "invalid configuration: " + "; ".join(validation.errors))
        self._cancelled.discard(run.run_id)
        self._paused.discard(run.run_id)
        self._active.add(run.run_id)
        started = run.with_status(TrainingRunStatus.PREPARING, backend=trainer.backend)
        self.runs.save(started)
        notes: list[str] = []
        if override:
            notes.append("the operator overrode an unsafe resource estimate")
        if cfg.resume_from_checkpoint:
            notes.append(f"resumed from checkpoint {cfg.resume_from_checkpoint}")

        def on_step(updated: TrainingRun) -> None:
            self.runs.save(updated)

        def on_checkpoint(updated: TrainingRun, kind: str) -> None:
            record = self.checkpoint_manager.create(
                updated.run_id,
                epoch=updated.current_epoch,
                step=updated.current_step,
                kind=kind,
                metrics={
                    "training_loss": updated.training_loss,
                    "validation_loss": updated.validation_loss,
                },
            )
            stored = self.runs.get(updated.run_id) or updated
            best = stored.best_checkpoint_id
            if kind == "best" and record.loadable:
                best = record.checkpoint_id
            self.checkpoint_manager.apply_retention(
                updated.run_id, max_checkpoints=cfg.max_checkpoints
            )
            # The surviving set is the truth: retention may have just deleted a
            # file, and a run that listed a deleted path would be lying.
            paths = tuple(
                dict.fromkeys(
                    item.path
                    for item in self.checkpoint_manager.list(updated.run_id)
                    if item.path
                )
            )
            self.runs.save(
                replace(
                    updated,
                    checkpoint_paths=paths,
                    best_checkpoint_id=best,
                    updated_at=now_iso(),
                )
            )

        callbacks = TrainingCallbacks(
            on_step=on_step,
            on_checkpoint=on_checkpoint,
            is_cancelled=lambda: run.run_id in self._cancelled,
            is_paused=lambda: run.run_id in self._paused,
        )
        self._announce(RUN_STARTED, {"run_id": run.run_id, "backend": trainer.backend})
        try:
            if resume_step is not None:
                current = trainer.resume_training(started, dataset, resume_step, callbacks)
            else:
                current = trainer.start_training(started, dataset, callbacks)
        except TrainingBackendUnavailable as exc:
            return self._fail(started, str(exc))
        except Exception as exc:  # noqa: BLE001 - a training failure is data, not a crash
            self.failures += 1
            _logger.warning(
                "Training run %s raised: %s: %s", run.run_id, type(exc).__name__, exc
            )
            return self._fail(started, f"{type(exc).__name__}: {exc}")
        finally:
            self._active.discard(run.run_id)
            self._cancelled.discard(run.run_id)
            self._paused.discard(run.run_id)
        if notes:
            current = replace(
                current,
                resource_usage={**dict(current.resource_usage), "notes": notes},
            )
        # The checkpoint callbacks persisted their own updates (paths, best id)
        # while the trainer returned an in-memory run that never saw them: merge
        # the persisted fields rather than overwriting them with the stale copy.
        persisted = self.runs.get(run.run_id)
        if persisted is not None:
            current = replace(
                current,
                checkpoint_paths=persisted.checkpoint_paths,
                best_checkpoint_id=persisted.best_checkpoint_id,
            )
        self.runs.save(current)
        if current.status == TrainingRunStatus.COMPLETED.value:
            # The backend is asked what it produced rather than the registry
            # inferring it, so a run that wrote a real adapter records its path
            # and a dry run says plainly that it wrote nothing.
            model = self.registry.register(
                current,
                adapter=trainer.model_metadata(),
                resource_requirements=current.estimate,
                notes="; ".join(notes),
            )
            self._announce(
                RUN_COMPLETED,
                {
                    "run_id": current.run_id,
                    "model_id": model.model_id,
                    "training_loss": current.training_loss,
                },
            )
            self._announce(
                MODEL_REGISTERED,
                {"model_id": model.model_id, "run_id": current.run_id},
            )
            return {
                "ok": True,
                "run": current.to_dict(),
                "model": model.to_dict(),
                "evaluation_required": True,
            }
        if current.status == TrainingRunStatus.CANCELLED.value:
            self._announce(RUN_CANCELLED, {"run_id": current.run_id})
        return {"ok": True, "run": current.to_dict(), "cancelled": current.status == "cancelled"}

    def _fail(self, run: TrainingRun, error: str) -> dict[str, Any]:
        failed = run.with_status(TrainingRunStatus.FAILED, error=error, end_time=now_iso())
        self.runs.save(failed)
        self._announce(RUN_FAILED, {"run_id": run.run_id, "error": error})
        return {"ok": False, "run_id": run.run_id, "reason": error, "run": failed.to_dict()}

    def cancel(self, run_id: str) -> dict[str, Any]:
        """Request cancellation; a run not in this process is cancelled directly."""
        run = self.runs.get(run_id)
        if run is None:
            return {"ok": False, "reason": f"no run {run_id!r}"}
        if run.terminal:
            return {"ok": False, "reason": f"run {run_id!r} is already {run.status}"}
        if run_id in self._active:
            self._cancelled.add(run_id)
            return {"ok": True, "run_id": run_id, "requested": True}
        cancelled = run.with_status(TrainingRunStatus.CANCELLED, end_time=now_iso())
        self.runs.save(cancelled)
        self._announce(RUN_CANCELLED, {"run_id": run_id})
        return {"ok": True, "run_id": run_id, "requested": False, "run": cancelled.to_dict()}

    def pause(self, run_id: str) -> dict[str, Any]:
        run = self.runs.get(run_id)
        if run is None:
            return {"ok": False, "reason": f"no run {run_id!r}"}
        if run.terminal:
            return {"ok": False, "reason": f"run {run_id!r} is already {run.status}"}
        if run_id in self._active:
            self._paused.add(run_id)
            return {"ok": True, "run_id": run_id, "requested": True}
        paused = run.with_status(TrainingRunStatus.PAUSED)
        self.runs.save(paused)
        return {"ok": True, "run_id": run_id, "requested": False, "run": paused.to_dict()}

    # -- checkpoints and models ------------------------------------------------

    def checkpoints(self, run_id: str) -> tuple[dict[str, Any], ...]:
        return tuple(record.to_dict() for record in self.checkpoint_manager.list(run_id))

    def models_list(
        self, *, status: str = "", base_model: str = "", limit: int = 0
    ) -> tuple[TrainingModelRecord, ...]:
        return self.registry.list(status=status, base_model=base_model, limit=limit)

    def model(self, model_id: str) -> TrainingModelRecord | None:
        return self.registry.get(model_id)

    def approve_model(
        self, model_id: str, *, approved_by: str = "", note: str = ""
    ) -> dict[str, Any]:
        return self.registry.approve(model_id, approved_by=approved_by, note=note)

    def promote_model(self, model_id: str, *, note: str = "") -> dict[str, Any]:
        return self.registry.promote(model_id, note=note)

    def reject_model(self, model_id: str, *, reason: str = "") -> dict[str, Any]:
        return self.registry.reject(model_id, reason=reason)

    def deprecate_model(self, model_id: str, *, reason: str = "") -> dict[str, Any]:
        return self.registry.deprecate(model_id, reason=reason)

    def rollback_model(self, model_id: str, *, reason: str = "") -> dict[str, Any]:
        return self.registry.rollback(model_id, reason=reason)

    # -- evaluation ------------------------------------------------------------

    def evaluate_run(
        self,
        run_id: str,
        *,
        base: ModelPredictor | None = None,
        candidate: ModelPredictor | None = None,
        split: str = "test",
        tolerance: float | None = None,
    ) -> dict[str, Any]:
        """Compare the base model against the candidate on the held-out split.

        Without two predictors there is nothing honest to measure, so the run is
        marked unevaluated and approval stays impossible; the manager never
        fabricates a candidate from the training loss.
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
        dataset = self.dataset(run.dataset_version)
        if dataset is None:
            return {"ok": False, "reason": f"the dataset {run.dataset_version!r} is missing"}
        if base is None or candidate is None:
            skipped = TrainingEvaluation(
                evaluation_id=f"te-{run_id}",
                run_id=run_id,
                model_id=model.model_id,
                dataset_version=run.dataset_version,
                split=split,
                verdict="skipped",
                reason=(
                    "no base and candidate predictors were supplied, so nothing "
                    "was measured — the training loss is not an evaluation"
                ),
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
        evaluation = self.evaluator.compare(
            dataset,
            base,
            candidate,
            split=split,
            run_id=run_id,
            model_id=record.model_id,
            tolerance=tolerance,
        )
        self.evaluations.save(evaluation)
        outcome = self.registry.record_evaluation(record.model_id, evaluation)
        self._announce(
            EVALUATION_COMPLETED,
            {
                "run_id": run_id,
                "model_id": record.model_id,
                "verdict": evaluation.verdict,
                "regressions": list(evaluation.regressions),
            },
        )
        return {
            "ok": bool(outcome.get("ok")),
            "evaluation": evaluation.to_dict(),
            "model": outcome.get("model", {}),
            "reason": outcome.get("reason", ""),
        }

    def evaluations_list(self, *, limit: int = 0) -> tuple[TrainingEvaluation, ...]:
        return self.evaluations.list(limit=limit)

    # -- reporting -------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        runs = self.runs.list()
        models = self.models.list()
        datasets = self.datasets.list(limit=0)
        estimator = self.estimator.summary()
        return {
            "datasets": {
                "count": len(datasets),
                "names": sorted({dataset.name for dataset in datasets}),
            },
            "runs": {
                "count": len(runs),
                "by_status": self._counts(run.status for run in runs),
                "active": sorted(self._active),
                "failures": self.failures,
            },
            "models": {
                "count": len(models),
                "by_status": self.registry.counts(),
                "production": next(
                    (model.model_id for model in models if model.status == "production"), ""
                ),
            },
            "hardware": estimator["hardware"],
            "dependencies": estimator["dependencies"],
            "device": estimator["device"],
            "defaults": {
                "dry_run": self.default_config.dry_run,
                "backend_policy": self.default_config.hardware_policy,
                "output_root": str(self.output_root),
                "max_checkpoints": self.checkpoint_manager.max_checkpoints,
            },
        }

    @staticmethod
    def _counts(values: Any) -> dict[str, int]:
        counts: dict[str, int] = {}
        for value in values:
            name = str(value)
            counts[name] = counts.get(name, 0) + 1
        return counts

    def summary(self) -> dict[str, Any]:
        runs = self.runs.list()
        return {
            "datasets": [dataset.dataset_version_id for dataset in self.datasets.list()],
            "runs": [
                {
                    "run_id": run.run_id,
                    "model": run.model,
                    "dataset_version": run.dataset_version,
                    "status": run.status,
                    "backend": run.backend,
                    "training_loss": run.training_loss,
                    "validation_loss": run.validation_loss,
                }
                for run in runs[:20]
            ],
            "models": [
                {
                    "model_id": model.model_id,
                    "base_model": model.base_model,
                    "status": model.status,
                    "dataset_version": model.dataset_version,
                    "training_run_id": model.training_run_id,
                }
                for model in self.models.list(limit=20)
            ],
            "status": self.status(),
        }


__all__ = [
    "DATASET_BUILT",
    "EVALUATION_COMPLETED",
    "MODEL_REGISTERED",
    "RUN_CANCELLED",
    "RUN_COMPLETED",
    "RUN_CREATED",
    "RUN_FAILED",
    "RUN_STARTED",
    "TrainerFactory",
    "TrainingManager",
]
