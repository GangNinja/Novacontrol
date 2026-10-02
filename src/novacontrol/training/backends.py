"""The trainer interface, a dry-run backend, and an isolated PEFT/LoRA adapter.

NovaControl must not depend on a specific ML training library. Nothing in this
package imports ``torch``, ``transformers`` or ``peft`` at module level: a real
backend asks for them lazily, inside the method that needs them, and reports
exactly what is missing instead of failing an import somewhere far away. The
default backend is the DRY-RUN trainer, which needs nothing installed and is
what this machine (16 GB, Intel iGPU + NPU, no CUDA) can actually exercise.

The dry-run trainer is honest about what it is: it computes the step schedule
from the dataset's real size, walks it, produces a *simulated* loss curve, and
writes real checkpoint files with real metadata. It never claims a trained
model, and the post-training evaluation treats a dry-run run as un-evaluated
unless a predictor is explicitly supplied.

The PEFT/LoRA adapter is the boundary a real run plugs into: it probes for the
optional extras without importing them, validates the LoRA configuration, and
executes a runner the operator injects. The concrete Transformers/PEFT training
loop is deliberately NOT shipped in Phase 16 (it would be untestable here and
untested code in a training path is worse than a clear boundary); the adapter is
where it lands, and the missing-dependency message says how to get it.
"""

from __future__ import annotations

import abc
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from novacontrol.evaluation.models import now_iso
from novacontrol.training.config import TrainingConfig, TrainingConfigValidation
from novacontrol.training.hardware import HardwareCapabilities, detect_hardware
from novacontrol.training.models import (
    MAX_LOSS_POINTS,
    SFTDatasetVersion,
    TrainingRun,
    TrainingRunStatus,
)
from novacontrol.training.resources import ResourceEstimator

#: A hook a real backend can call to report progress. Every callback is
#: optional; a trainer must run with none of them (a test, a CLI dry run).
ProgressCallback = Callable[[TrainingRun], None]
CheckpointCallback = Callable[[TrainingRun, str], None]
StopCallback = Callable[[], bool]


class TrainingBackendUnavailable(RuntimeError):
    """A real backend was asked to run without its optional dependencies."""


@dataclass(slots=True)
class TrainingCallbacks:
    """How a trainer talks to its orchestrator without owning it.

    The manager passes these in: the trainer reports a step, asks whether a
    cancellation was requested, and requests a checkpoint; it never writes to
    the run store itself. That split is what makes the trainer testable with a
    handful of closures — and what keeps one writer per run.
    """

    on_step: ProgressCallback | None = None
    on_checkpoint: CheckpointCallback | None = None
    is_cancelled: StopCallback | None = None
    is_paused: StopCallback | None = None
    #: Called once per step, before the loss is computed. The hook a test uses
    #: to make a step fail, or to cancel mid-run through the manager.
    step_hook: Callable[[int], None] | None = None

    def cancelled(self) -> bool:
        return bool(self.is_cancelled and self.is_cancelled())

    def paused(self) -> bool:
        return bool(self.is_paused and self.is_paused())

    def step(self, run: TrainingRun, *, index: int) -> None:
        if self.step_hook is not None:
            self.step_hook(index)
        if self.on_step is not None:
            self.on_step(run)

    def checkpoint(self, run: TrainingRun, kind: str) -> None:
        if self.on_checkpoint is not None:
            self.on_checkpoint(run, kind)


class SFTTrainer(abc.ABC):
    """The operations a supervised-fine-tuning backend provides.

    Deliberately small and library-free: the orchestrator drives the loop
    (``start_training`` is expected to RUN to completion or to a terminal
    status and return the final run), so a backend cannot smuggle a second
    scheduler, a second store or a second lifecycle into NovaControl.
    """

    #: A stable name a stored run carries ("dry_run", "peft_lora").
    backend = "abstract"

    #: Where this backend wrote its adapter, if it wrote one. A dry run leaves
    #: it empty; a real backend sets it while it trains so the registry can
    #: point at the artefact instead of describing the settings it *would* use.
    adapter_path: str = ""

    def __init__(
        self,
        config: TrainingConfig,
        *,
        estimator: ResourceEstimator | None = None,
    ) -> None:
        self.config = config
        self.estimator = estimator if estimator is not None else ResourceEstimator()

    @property
    def name(self) -> str:
        return self.backend

    # -- checks ----------------------------------------------------------------

    def validate_config(self) -> TrainingConfigValidation:
        return self.config.validate()

    def model_metadata(self) -> dict[str, Any]:
        """What this backend produced, in the shape the model registry stores.

        Derived from the configuration rather than invented: a dry run trains
        nothing, so it reports the LoRA settings it would have used and an empty
        ``path``. A real backend overrides this (or just sets ``adapter_path``)
        to name what it actually wrote, which is the difference between a
        registry entry a deployment can load and one that only looks complete.
        """
        return {
            "kind": self.config.effective_method,
            "rank": self.config.lora_rank,
            "alpha": self.config.lora_alpha,
            "dropout": self.config.lora_dropout,
            "target_modules": list(self.config.target_modules),
            "base_model": self.config.base_model,
            "path": self.adapter_path,
            "dry_run": self.config.dry_run,
        }

    def prepare_dataset(self, dataset: SFTDatasetVersion) -> dict[str, Any]:
        """What this backend will actually feed a model, as a summary mapping."""
        train_rows = len(dataset.split("train")) or len(dataset)
        micro_batches = math.ceil(train_rows / max(1, self.config.batch_size))
        steps_per_epoch = max(
            1, math.ceil(micro_batches / max(1, self.config.gradient_accumulation_steps))
        )
        return {
            "dataset_version": dataset.dataset_version_id,
            "dataset_type": dataset.dataset_type,
            "examples": len(dataset),
            "train_examples": train_rows,
            "validation_examples": len(dataset.split("validation")),
            "test_examples": len(dataset.split("test")),
            "steps_per_epoch": steps_per_epoch,
            "total_steps": steps_per_epoch * max(1, self.config.epochs),
            "max_sequence_length": self.config.max_sequence_length,
        }

    def estimate_resources(self, dataset: SFTDatasetVersion | None = None) -> Any:
        return self.estimator.estimate(self.config, dataset=dataset)

    def evaluate(self, dataset: SFTDatasetVersion) -> dict[str, Any]:
        """Backend-local checks (not the model comparison, which is separate)."""
        return {}

    def save_checkpoint(self, run: TrainingRun, callbacks: TrainingCallbacks, kind: str) -> None:
        callbacks.checkpoint(run, kind)

    def finalize(self, run: TrainingRun) -> TrainingRun:
        finish = now_iso()
        updates: dict[str, Any] = {"end_time": finish}
        if run.validation_loss is None and run.training_loss is not None:
            updates["validation_loss"] = run.training_loss
        return run.with_status(TrainingRunStatus.COMPLETED, **updates)

    def cancel(self, run: TrainingRun) -> TrainingRun:
        return run.with_status(TrainingRunStatus.CANCELLED, end_time=now_iso())

    # -- the loop --------------------------------------------------------------

    @abc.abstractmethod
    def start_training(
        self,
        run: TrainingRun,
        dataset: SFTDatasetVersion,
        callbacks: TrainingCallbacks,
    ) -> TrainingRun:
        """Run to completion (or cancellation/pause) and return the final run."""

    def resume_training(
        self,
        run: TrainingRun,
        dataset: SFTDatasetVersion,
        checkpoint_step: int,
        callbacks: TrainingCallbacks,
    ) -> TrainingRun:
        """Continue a run from a checkpoint step. Backends may re-run from zero."""
        return self.start_training(run, dataset, callbacks)


# ── the dry-run backend ─────────────────────────────────────────────────────


class DryRunTrainer(SFTTrainer):
    """Walks a real step schedule and produces a SIMULATED loss curve.

    Nothing is trained and no model is touched. What it does do is exercise the
    whole orchestration path — statuses, step accounting, checkpoints, pause and
    cancel, resume from a checkpoint — which is exactly what this phase's tests
    need and what an operator with no training hardware can still validate.
    """

    backend = "dry_run"

    def model_metadata(self) -> dict[str, Any]:
        metadata = super().model_metadata()
        metadata["note"] = "a dry run trains nothing: no adapter file was written"
        return metadata

    def __init__(
        self,
        config: TrainingConfig,
        *,
        estimator: ResourceEstimator | None = None,
        capabilities: HardwareCapabilities | None = None,
    ) -> None:
        super().__init__(config, estimator=estimator)
        self.capabilities = (
            capabilities if capabilities is not None else detect_hardware()
        )

    # -- simulation ------------------------------------------------------------

    def _loss(self, step: int, total: int, seed: int, *, validation: bool = False) -> float:
        """A deterministic, documentedly fake curve: high, falling, jittered."""
        progress = min(1.0, max(0.0, step / max(1, total)))
        base = 1.5 / (1.0 + 3.0 * progress)
        jitter = 0.02 * math.sin(seed + step * 0.7)
        value = base + jitter
        if validation:
            value = value * 1.08 + 0.01
        return round(max(0.0, value), 6)

    def _plan(self, dataset: SFTDatasetVersion) -> tuple[int, int]:
        summary = self.prepare_dataset(dataset)
        return int(summary["steps_per_epoch"]), int(summary["total_steps"])

    def start_training(
        self,
        run: TrainingRun,
        dataset: SFTDatasetVersion,
        callbacks: TrainingCallbacks,
    ) -> TrainingRun:
        return self._walk(run, dataset, callbacks, first_step=0)

    def resume_training(
        self,
        run: TrainingRun,
        dataset: SFTDatasetVersion,
        checkpoint_step: int,
        callbacks: TrainingCallbacks,
    ) -> TrainingRun:
        return self._walk(run, dataset, callbacks, first_step=max(0, int(checkpoint_step)))

    def _walk(
        self,
        run: TrainingRun,
        dataset: SFTDatasetVersion,
        callbacks: TrainingCallbacks,
        *,
        first_step: int,
    ) -> TrainingRun:
        config = self.config
        steps_per_epoch, total_steps = self._plan(dataset)
        seed = int(config.seed)
        epoch = max(0, first_step // max(1, steps_per_epoch))
        step = max(0, first_step)
        history = list(run.loss_history)
        current = run.with_status(
            TrainingRunStatus.RUNNING,
            start_time=run.start_time or now_iso(),
            total_steps=total_steps,
            current_epoch=epoch,
            current_step=step,
        )
        while epoch < config.epochs:
            epoch += 1
            for _ in range(steps_per_epoch):
                if callbacks.cancelled():
                    return current.with_status(
                        TrainingRunStatus.CANCELLED, end_time=now_iso()
                    )
                step += 1
                loss = self._loss(step, total_steps, seed)
                history.append({"epoch": epoch, "step": step, "loss": loss})
                current = current.with_status(
                    TrainingRunStatus.RUNNING,
                    current_epoch=epoch,
                    current_step=step,
                    training_loss=loss,
                    loss_history=tuple(history[-MAX_LOSS_POINTS:]),
                )
                callbacks.step(current, index=step)
                if config.checkpoint_frequency and step % config.checkpoint_frequency == 0:
                    callbacks.checkpoint(current, "periodic")
                if config.evaluation_frequency and step % config.evaluation_frequency == 0:
                    validation = self._loss(step, total_steps, seed, validation=True)
                    current = current.with_status(
                        TrainingRunStatus.RUNNING, validation_loss=validation
                    )
                    callbacks.step(current, index=step)
            validation_loss = self._loss(
                min(step, total_steps), total_steps, seed, validation=True
            )
            current = current.with_status(
                TrainingRunStatus.RUNNING, validation_loss=validation_loss
            )
            if not config.checkpoint_frequency:
                callbacks.checkpoint(current, "periodic")
            if callbacks.paused():
                return current.with_status(TrainingRunStatus.PAUSED, end_time=now_iso())
            if callbacks.cancelled():
                return current.with_status(TrainingRunStatus.CANCELLED, end_time=now_iso())
        callbacks.checkpoint(current, "best")
        return self.finalize(current)


# ── the PEFT/LoRA adapter ───────────────────────────────────────────────────


#: A runner receives the run, the dataset and the callbacks, and returns the
#: final run. An operator (or a later phase) wires the concrete Transformers /
#: PEFT loop behind this callable; tests use it to exercise the adapter path.
PeftRunner = Callable[[TrainingRun, SFTDatasetVersion, TrainingCallbacks], TrainingRun]


class PeftLoraBackend(SFTTrainer):
    """The isolated PEFT/LoRA boundary.

    It probes for ``torch``/``transformers``/``peft`` without importing them,
    validates the LoRA configuration, and refuses to run when the extras are
    missing — with the missing package names in the message. When a ``runner``
    is injected the backend executes it; otherwise it reports that the concrete
    loop is not part of this phase's build.
    """

    backend = "peft_lora"

    def __init__(
        self,
        config: TrainingConfig,
        *,
        estimator: ResourceEstimator | None = None,
        capabilities: HardwareCapabilities | None = None,
        runner: PeftRunner | None = None,
    ) -> None:
        super().__init__(config, estimator=estimator)
        self.capabilities = (
            capabilities if capabilities is not None else detect_hardware()
        )
        self.runner = runner

    def model_metadata(self) -> dict[str, Any]:
        """The adapter this run produced, including where it was written.

        ``adapter_path`` is whatever the injected runner recorded. When it is
        empty the output directory is named anyway, so an operator can look
        where the run was told to write rather than at a blank field.
        """
        metadata = super().model_metadata()
        metadata["output_directory"] = str(self.config.output_directory)
        return metadata

    def missing_dependencies(self) -> tuple[str, ...]:
        quantised = self.config.effective_method == "qlora"
        return self.capabilities.missing_dependencies(quantised=quantised)

    def is_available(self) -> bool:
        return not self.missing_dependencies()

    def prepare_dataset(self, dataset: SFTDatasetVersion) -> dict[str, Any]:
        summary = super().prepare_dataset(dataset)
        summary["lora"] = {
            "rank": self.config.lora_rank,
            "alpha": self.config.lora_alpha,
            "dropout": self.config.lora_dropout,
            "target_modules": list(self.config.target_modules),
            "method": self.config.effective_method,
        }
        return summary

    def start_training(
        self,
        run: TrainingRun,
        dataset: SFTDatasetVersion,
        callbacks: TrainingCallbacks,
    ) -> TrainingRun:
        missing = self.missing_dependencies()
        if missing:
            raise TrainingBackendUnavailable(
                "a real LoRA/PEFT run needs the optional training dependencies; missing: "
                + ", ".join(missing)
                + ". Dry-run and dataset validation work without them."
            )
        if self.runner is None:
            raise TrainingBackendUnavailable(
                "the PEFT/LoRA adapter is installed but no training runner is wired: "
                "Phase 16 ships the adapter boundary, and a runner is how the concrete "
                "Transformers/PEFT loop attaches. Provide PeftLoraBackend(runner=...)."
            )
        prepared = self.prepare_dataset(dataset)
        current = run.with_status(
            TrainingRunStatus.RUNNING,
            start_time=run.start_time or now_iso(),
            total_steps=int(prepared["total_steps"]),
        )
        result = self.runner(current, dataset, callbacks)
        return result if isinstance(result, TrainingRun) else self.finalize(current)


__all__ = [
    "DryRunTrainer",
    "PeftLoraBackend",
    "PeftRunner",
    "SFTTrainer",
    "TrainingBackendUnavailable",
    "TrainingCallbacks",
]
