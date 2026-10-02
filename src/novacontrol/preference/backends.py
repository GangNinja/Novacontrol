"""The preference trainer: one interface, two objectives, one shared pipeline.

DPO and ORPO are two objectives, not two pipelines. Everything they have in common
— dataset preparation, the step schedule, resource estimation, checkpoints, pause
and cancel, the model registry — is inherited, and the only things that differ live
below this docstring's second paragraph: the objective's description, whether it
keeps a reference model, and the callable a real training loop is injected
through.

    PreferenceTrainer            the interface (and the shared implementation)
          ├── DryRunPreferenceTrainer   walks the schedule; nothing is trained
          ├── DPOTrainer               the reference-anchored objective
          └── ORPOTrainer              the reference-free odds-ratio objective

:class:`~novacontrol.training.backends.SFTTrainer` is the parent, deliberately.
The orchestrator drives the loop (`start_training` runs to a terminal status and
returns the final run), the callbacks are Phase 16's, and `model_metadata` is the
same shape the registry already stores — so the preference phase adds an
Algorithm, not a second training architecture.

No training library is imported at module level. A real run asks for
``torch``/``transformers``/``peft`` lazily, inside the method that needs them, and
reports exactly what is missing. The concrete DPO/ORPO training loop is NOT
shipped here: it cannot be exercised on this machine, and untested code in a
training path is worse than a clear boundary — ``runner`` is that boundary.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

from novacontrol.evaluation.models import now_iso
from novacontrol.preference.config import PreferenceTrainingConfig
from novacontrol.preference.datasets import PreferenceDatasetBuilder
from novacontrol.preference.models import PreferenceAlgorithm, PreferenceDatasetVersion
from novacontrol.preference.resources import PreferenceResourceEstimator
from novacontrol.training.backends import (
    SFTTrainer,
    TrainingBackendUnavailable,
    TrainingCallbacks,
)
from novacontrol.training.hardware import HardwareCapabilities, detect_hardware
from novacontrol.training.models import (
    MAX_LOSS_POINTS,
    SFTDatasetVersion,
    TrainingRun,
    TrainingRunStatus,
)
from novacontrol.training.resources import ResourceEstimator

#: A runner receives the run, the preference dataset and the callbacks, and returns
#: the final run. This is where a concrete Transformers/PEFT loop for DPO or ORPO
#: attaches, and where a test injects a stand-in.
PreferenceRunner = Callable[
    [TrainingRun, PreferenceDatasetVersion, TrainingCallbacks], TrainingRun
]

#: The two objectives, written down once each. The wording is documentation that
#: happens to live in code: a report, a diagnostic row and a future reader all need
#: to know what "beta" multiplies and whether a reference model is in memory.
OBJECTIVES: Mapping[str, Mapping[str, Any]] = {
    PreferenceAlgorithm.DPO.value: {
        "algorithm": PreferenceAlgorithm.DPO.value,
        "name": "Direct Preference Optimization",
        "needs_reference_model": True,
        "beta_is": "the strength of the KL pull toward the frozen reference model",
        "objective": (
            "-log sigmoid(beta * ((log pi(y_w|x) - log pi_ref(y_w|x)) "
            "- (log pi(y_l|x) - log pi_ref(y_l|x))))"
        ),
        "memory_note": (
            "the reference model is a second copy of the weights (frozen), which "
            "the resource estimate counts"
        ),
    },
    PreferenceAlgorithm.ORPO.value: {
        "algorithm": PreferenceAlgorithm.ORPO.value,
        "name": "Odds Ratio Preference Optimization",
        "needs_reference_model": False,
        "beta_is": "the weight of the odds-ratio preference term added to the loss",
        "objective": (
            "log-odds ratio of the chosen over the rejected response, added to the "
            "supervised loss on the chosen response"
        ),
        "memory_note": (
            "no reference model is kept: the preference term is folded into the "
            "model's own odds ratio, so the run costs one model, not two"
        ),
    },
}


def objective_for(algorithm: str) -> dict[str, Any]:
    """The documented objective of one algorithm (empty for an unknown name)."""
    description = OBJECTIVES.get(str(algorithm))
    return dict(description) if description else {}


class PreferenceTrainer(SFTTrainer):
    """The shared implementation every preference objective inherits.

    The constructor takes the preference configuration and hands its Phase 16
    projection to the parent, so every inherited helper — the LoRA metadata, the
    checkpoint callbacks, the config re-validation — reads the same fields it
    always did.
    """

    algorithm = "abstract"
    #: A stored run carries this name. ``preference`` on the abstract class so a
    #: subclass that forgot to name itself is visible rather than silently reading
    #: as supervised fine-tuning.
    backend = "preference"

    def __init__(
        self,
        config: PreferenceTrainingConfig,
        *,
        estimator: ResourceEstimator | None = None,
        capabilities: HardwareCapabilities | None = None,
        runner: PreferenceRunner | None = None,
        builder: PreferenceDatasetBuilder | None = None,
    ) -> None:
        super().__init__(config.as_training_config(), estimator=estimator)
        self.preference_config = config
        self.capabilities = (
            capabilities if capabilities is not None else detect_hardware()
        )
        self.runner = runner
        self.preference_estimator = PreferenceResourceEstimator(
            estimator=estimator, capabilities=self.capabilities
        )
        self._builder = builder if builder is not None else PreferenceDatasetBuilder()

    @property
    def name(self) -> str:
        return self.algorithm

    # -- the objective ---------------------------------------------------------

    def objective(self) -> dict[str, Any]:
        """What this backend optimises, in one documented mapping."""
        described = objective_for(self.preference_config.algorithm)
        described["backend"] = self.algorithm
        described["beta"] = self.preference_config.beta
        described["dry_run"] = self.preference_config.dry_run
        described["uses_lora"] = self.preference_config.use_lora
        described["simulated"] = self.preference_config.dry_run
        return described

    # -- checks ----------------------------------------------------------------

    def missing_dependencies(self) -> tuple[str, ...]:
        quantised = self.preference_config.effective_method == "qlora"
        return self.capabilities.missing_dependencies(quantised=quantised)

    def is_available(self) -> bool:
        return not self.missing_dependencies()

    def validate_config(self) -> Any:
        return self.preference_config.validate()

    def prepare_dataset(
        self, dataset: SFTDatasetVersion | PreferenceDatasetVersion
    ) -> dict[str, Any]:
        """What this backend will feed a model: accepted pairs, and the steps.

        Only ACCEPTED pairs count. A pair the quality filter held for review is
        stored so a person can settle it, and it must not become training data
        while it waits.
        """
        dataset = self._pairs(dataset)
        train_pairs = len(dataset.split("train"))
        if not train_pairs:
            train_pairs = len(dataset.accepted_pairs())
        pair_count = len(dataset.accepted_pairs())
        micro_batches = math.ceil(train_pairs / max(1, self.preference_config.batch_size))
        steps_per_epoch = max(
            1,
            math.ceil(
                micro_batches
                / max(1, self.preference_config.gradient_accumulation_steps)
            ),
        )
        return {
            "dataset_version": dataset.dataset_version_id,
            "dataset_type": dataset.dataset_type,
            "algorithm": self.preference_config.algorithm,
            "pairs": pair_count,
            "train_pairs": train_pairs,
            "validation_pairs": len(dataset.split("validation")),
            "test_pairs": len(dataset.split("test")),
            "review_required": dataset.statistics.review_required,
            "beta": self.preference_config.beta,
            "needs_reference_model": self.preference_config.needs_reference_model,
            "steps_per_epoch": steps_per_epoch,
            "total_steps": steps_per_epoch * max(1, self.preference_config.epochs),
            "max_sequence_length": self.preference_config.max_sequence_length,
            "lora": {
                "rank": self.preference_config.lora_rank,
                "alpha": self.preference_config.lora_alpha,
                "dropout": self.preference_config.lora_dropout,
                "target_modules": list(self.preference_config.target_modules),
                "method": self.preference_config.effective_method,
            },
        }

    def estimate_resources(
        self, dataset: SFTDatasetVersion | PreferenceDatasetVersion | None = None
    ) -> Any:
        return self.preference_estimator.estimate(
            self.preference_config,
            dataset=self._pairs(dataset) if dataset is not None else None,
        )

    def evaluate(
        self, dataset: SFTDatasetVersion | PreferenceDatasetVersion
    ) -> dict[str, Any]:
        """Backend-local checks — not the model comparison, which is separate."""
        dataset = self._pairs(dataset)
        issues = self._builder.validate(dataset)
        return {
            "dataset_version": dataset.dataset_version_id,
            "valid": not issues,
            "issues": list(issues),
        }

    def model_metadata(self) -> dict[str, Any]:
        """The adapter this run produced, including which objective made it.

        The registry stores ``training_method`` (how the weights were adapted) and
        ``algorithm`` (which objective drove the adaptation) as separate things,
        because a LoRA adapter trained by DPO and one trained by SFT are the same
        KIND of artefact and very different models.
        """
        metadata = super().model_metadata()
        metadata["algorithm"] = self.preference_config.algorithm
        metadata["beta"] = self.preference_config.beta
        metadata["reference_model"] = self.preference_config.reference_model
        metadata["dry_run"] = self.preference_config.dry_run
        metadata["output_directory"] = str(self.preference_config.output_directory)
        return metadata

    # -- the loop --------------------------------------------------------------

    def _simulated_step(
        self, pair_index: int, total: int, seed: int, *, rejected: bool = False
    ) -> float:
        """A deterministic, documentedly fake margin: falling, jittered.

        Not a preference probability and not a loss: a dry run must not produce a
        number anybody could mistake for a measurement. The run marks it
        ``simulated`` and no evaluation reads it.
        """
        progress = min(1.0, max(0.0, pair_index / max(1, total)))
        base = 0.6 / (1.0 + 2.0 * progress)
        jitter = 0.01 * math.sin(seed + pair_index * 0.5)
        value = base + jitter
        return round(max(0.0, value * (0.4 if rejected else 1.0)), 6)

    def _walk(
        self,
        run: TrainingRun,
        dataset: PreferenceDatasetVersion,
        callbacks: TrainingCallbacks,
        *,
        first_step: int = 0,
    ) -> TrainingRun:
        """The shared simulation: a real schedule, honestly labelled as simulated."""
        config = self.preference_config
        prepared = self.prepare_dataset(dataset)
        steps_per_epoch = int(prepared["steps_per_epoch"])
        total_steps = max(1, int(prepared["total_steps"]))
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
                chosen_score = self._simulated_step(step, total_steps, seed)
                rejected_score = self._simulated_step(
                    step, total_steps, seed, rejected=True
                )
                loss = round(max(0.0, 1.0 - (chosen_score - rejected_score)), 6)
                history.append({"epoch": epoch, "step": step, "loss": loss})
                current = current.with_status(
                    TrainingRunStatus.RUNNING,
                    current_epoch=epoch,
                    current_step=step,
                    training_loss=loss,
                    loss_history=tuple(history[-MAX_LOSS_POINTS:]),
                    preference_metrics={
                        "simulated": True,
                        "objective": config.algorithm,
                        "chosen_score": chosen_score,
                        "rejected_score": rejected_score,
                        "margin": round(chosen_score - rejected_score, 6),
                        "note": (
                            "a dry run produces no preference probability; these "
                            "figures are a schedule, not a measurement"
                        ),
                    },
                )
                callbacks.step(current, index=step)
                if config.checkpoint_frequency and step % config.checkpoint_frequency == 0:
                    callbacks.checkpoint(current, "periodic")
            if not config.checkpoint_frequency:
                callbacks.checkpoint(current, "periodic")
            if callbacks.paused():
                return current.with_status(TrainingRunStatus.PAUSED, end_time=now_iso())
            if callbacks.cancelled():
                return current.with_status(TrainingRunStatus.CANCELLED, end_time=now_iso())
        callbacks.checkpoint(current, "best")
        return self.finalize(current)

    def _run(
        self,
        run: TrainingRun,
        dataset: PreferenceDatasetVersion,
        callbacks: TrainingCallbacks,
    ) -> TrainingRun:
        """Either the injected runner, or the simulation, or a clear refusal."""
        config = self.preference_config
        if config.dry_run:
            return self._walk(run, dataset, callbacks)
        missing = self.missing_dependencies()
        if missing:
            raise TrainingBackendUnavailable(
                f"a real {config.algorithm.upper()} run needs the optional training "
                "dependencies; missing: "
                + ", ".join(missing)
                + ". Dry-run, dataset validation and resource estimation work without "
                "them."
            )
        if self.runner is None:
            raise TrainingBackendUnavailable(
                f"the {config.algorithm.upper()} backend is available but no training "
                "runner is wired: Phase 17 ships the objective, the dataset, the "
                "resource estimate and the evaluation, and a runner is how the "
                f"concrete loop attaches. Provide {type(self).__name__}(runner=...)."
            )
        prepared = self.prepare_dataset(dataset)
        started = run.with_status(
            TrainingRunStatus.RUNNING,
            start_time=run.start_time or now_iso(),
            total_steps=int(prepared["total_steps"]),
            preference_metrics={"objective": config.algorithm, "simulated": False},
        )
        result = self.runner(started, dataset, callbacks)
        return result if isinstance(result, TrainingRun) else self.finalize(started)

    def start_training(
        self,
        run: TrainingRun,
        dataset: SFTDatasetVersion | PreferenceDatasetVersion,
        callbacks: TrainingCallbacks,
    ) -> TrainingRun:
        return self._run(run, self._pairs(dataset), callbacks)

    def resume_training(
        self,
        run: TrainingRun,
        dataset: SFTDatasetVersion | PreferenceDatasetVersion,
        checkpoint_step: int,
        callbacks: TrainingCallbacks,
    ) -> TrainingRun:
        pairs = self._pairs(dataset)
        if not self.preference_config.dry_run:
            return self._run(run, pairs, callbacks)
        return self._walk(run, pairs, callbacks, first_step=max(0, int(checkpoint_step)))

    def _pairs(
        self, dataset: SFTDatasetVersion | PreferenceDatasetVersion
    ) -> PreferenceDatasetVersion:
        """The preference version this backend can train on, or a clear refusal.

        The interface it inherits is Phase 16's, so a supervised dataset can reach
        it by type. It cannot be TRAINED on: a preference objective needs the
        pair, and quietly treating one side as a target would produce a
        supervised run wearing a preference algorithm's name.
        """
        if not isinstance(dataset, PreferenceDatasetVersion):
            raise TrainingBackendUnavailable(
                f"the {self.algorithm} backend trains preference PAIRS and was handed a "
                f"supervised dataset ({type(dataset).__name__}); build a preference "
                "dataset version and point the run at that"
            )
        return dataset


class DryRunPreferenceTrainer(PreferenceTrainer):
    """Walks the schedule and trains nothing — the default on this machine.

    It reports the objective the configuration *asked* for, so a dry run shows the
    intended algorithm, the beta it would use, whether a reference model would be
    needed and whether LoRA would be attached, without touching a model.
    """

    algorithm = "dry_run"
    backend = "dry_run"


class DPOTrainer(PreferenceTrainer):
    """Direct Preference Optimization: the reference-anchored objective.

    Isolated here on purpose. The rest of NovaControl knows only
    :class:`PreferenceTrainer` and ``algorithm == "dpo"``; nothing outside this
    class branches on the objective's mathematics, and the run's record keeps the
    beta it ran under.
    """

    algorithm = PreferenceAlgorithm.DPO.value
    backend = "dpo"


class ORPOTrainer(PreferenceTrainer):
    """Odds Ratio Preference Optimization: the reference-free objective.

    Shares every step of the pipeline with :class:`DPOTrainer` — only the objective
    (and therefore whether a reference model is counted by the resource estimate)
    differs, which is what keeps two algorithms from becoming two training stacks.
    """

    algorithm = PreferenceAlgorithm.ORPO.value
    backend = "orpo"


def preference_trainer_for(
    config: PreferenceTrainingConfig,
    *,
    estimator: ResourceEstimator | None = None,
    capabilities: HardwareCapabilities | None = None,
    runner: PreferenceRunner | None = None,
) -> PreferenceTrainer:
    """The backend one configuration asks for. One place decides this."""
    if config.dry_run:
        return DryRunPreferenceTrainer(
            config, estimator=estimator, capabilities=capabilities, runner=runner
        )
    if config.algorithm == PreferenceAlgorithm.ORPO.value:
        return ORPOTrainer(
            config, estimator=estimator, capabilities=capabilities, runner=runner
        )
    return DPOTrainer(config, estimator=estimator, capabilities=capabilities, runner=runner)


__all__ = [
    "OBJECTIVES",
    "DPOTrainer",
    "DryRunPreferenceTrainer",
    "ORPOTrainer",
    "PreferenceRunner",
    "PreferenceTrainer",
    "objective_for",
    "preference_trainer_for",
]
