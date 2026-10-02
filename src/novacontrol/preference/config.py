"""Preference-training configuration, validated in one place.

DPO and ORPO differ in exactly one way this configuration cares about: DPO
optimises a policy against a frozen REFERENCE model and needs that model in
memory and on disk, while ORPO drops the reference entirely and folds the
preference term into a single odds-ratio objective. That difference lives here —
in ``reference_model`` and in the estimator's component list — and nowhere else,
so the rest of the pipeline (dataset preparation, checkpoints, evaluation, the
registry) is shared between the two algorithms rather than duplicated per
algorithm.

Every shared rule is re-used rather than re-stated: :meth:`validate` asks
:class:`~novacontrol.training.config.TrainingConfig` for the errors and warnings
that apply to any training run (epochs, batch size, learning rate, precision, the
LoRA settings, the hardware policy) and then adds the preference-specific ones.
One vocabulary for "epochs must be between 1 and 100", whichever objective is
being trained.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from novacontrol.preference.models import PreferenceAlgorithm, PreferenceDatasetType
from novacontrol.training.config import (
    MAX_CHECKPOINTS,
    MAX_EPOCHS,
    PRECISIONS,
    TrainingConfig,
    TrainingConfigValidation,
)
from novacontrol.training.models import HardwarePolicy, TrainingMethod

#: DPO's beta is the strength of the KL penalty that keeps the policy near the
#: reference; ORPO's objective has no reference, and the same field is the weight
#: of its odds-ratio term. Both are "how much the preference term counts", and
#: both live in [0.01, 1.0] in practice; above that the model stops learning the
#: task, so it is an error rather than a shrug.
MAX_BETA = 1.0
MIN_BETA = 0.01


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _whole(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _real(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _flag(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _target_modules(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


@dataclass(frozen=True, slots=True)
class PreferenceTrainingConfig:
    """One preference-optimization run's parameters.

    ``dry_run`` defaults to True, exactly as it does for supervised fine-tuning:
    nothing starts training because a configuration exists. ``algorithm`` picks
    the objective (``dpo``/``orpo``); the trainer is chosen from it and nothing
    else in the pipeline branches on it.
    """

    base_model: str = ""
    reference_model: str = ""
    preference_dataset_version: str = ""
    dataset_type: str = ""
    algorithm: str = PreferenceAlgorithm.DPO.value
    output_directory: str = ""
    epochs: int = 1
    batch_size: int = 1
    gradient_accumulation_steps: int = 8
    learning_rate: float = 5e-5
    beta: float = 0.1
    warmup_ratio: float = 0.03
    max_sequence_length: int = 1024
    evaluation_frequency: int = 0
    checkpoint_frequency: int = 0
    seed: int = 42
    precision: str = "fp32"
    gradient_checkpointing: bool = True
    max_checkpoints: int = 3
    resume_from_checkpoint: str = ""
    use_lora: bool = True
    training_method: str = TrainingMethod.LORA.value
    lora_rank: int = 8
    lora_alpha: int = 16
    lora_dropout: float = 0.05
    target_modules: tuple[str, ...] = ()
    hardware_policy: str = HardwarePolicy.AUTO.value
    dry_run: bool = True
    base_model_parameters: int = 0
    base_model_size_bytes: int = 0
    notes: str = ""

    # -- reading ---------------------------------------------------------------

    @property
    def effective_method(self) -> str:
        """The adaptation method that will actually run, reconciled with LoRA."""
        if not self.use_lora:
            return TrainingMethod.FULL.value
        return self.training_method

    @property
    def needs_reference_model(self) -> bool:
        """Whether this objective keeps a frozen reference model in memory.

        DPO does; ORPO does not. This is the one place the difference is written
        down, and the resource estimator reads it instead of branching on the
        algorithm itself.
        """
        return self.algorithm == PreferenceAlgorithm.DPO.value

    def validate(self) -> TrainingConfigValidation:
        """Every reason this configuration should not start, and every caveat.

        The shared rules come from Phase 16's validator (via
        :meth:`as_training_config`), so there is one definition of a usable
        learning rate or LoRA rank across both training phases.
        """
        shared = self.as_training_config().validate()
        errors = list(shared.errors)
        warnings = list(shared.warnings)

        algorithms = {member.value for member in PreferenceAlgorithm}
        if self.algorithm not in algorithms:
            errors.append(
                f"algorithm must be one of {', '.join(sorted(algorithms))} "
                f"(got {self.algorithm!r})"
            )
        if not self.preference_dataset_version:
            errors.append("preference_dataset_version is required (name@version)")
        if self.dataset_type:
            types = {member.value for member in PreferenceDatasetType}
            if self.dataset_type not in types:
                errors.append(
                    f"unknown dataset_type {self.dataset_type!r}: expected one of "
                    + ", ".join(sorted(types))
                )
        if not MIN_BETA <= self.beta <= MAX_BETA:
            errors.append(
                f"beta must be between {MIN_BETA} and {MAX_BETA} (got {self.beta})"
            )
        if self.reference_model and not self.needs_reference_model:
            warnings.append(
                f"algorithm={self.algorithm} needs no reference model; "
                "reference_model is ignored (the preference term is folded into "
                "the model's own odds ratio)"
            )
        if self.needs_reference_model and not self.reference_model:
            warnings.append(
                "no reference_model is set, so the base model is used as the "
                "frozen reference: the policy will be pulled back toward the "
                "model it started from"
            )
        if not self.dry_run:
            warnings.append(
                f"dry_run is off: a real {self.algorithm.upper()} run needs the "
                "optional training dependencies installed, this installation to "
                "permit it, and an explicit confirmation"
            )
        return TrainingConfigValidation(errors=tuple(errors), warnings=tuple(warnings))

    # -- reuse ------------------------------------------------------------------

    def as_training_config(self) -> TrainingConfig:
        """The same run, in the Phase 16 configuration the shared machinery reads.

        The checkpoint manager, the resource estimator, the backends and the run
        record all take a :class:`TrainingConfig`; this projection is how a
        preference run reuses them instead of growing a parallel copy of each.
        ``dataset_type`` is deliberately not carried across — the shared
        validator only knows the supervised families, and the preference family
        is validated above.
        """
        return TrainingConfig(
            base_model=self.base_model,
            dataset_version=self.preference_dataset_version,
            dataset_type="",
            output_directory=self.output_directory,
            epochs=self.epochs,
            batch_size=self.batch_size,
            gradient_accumulation_steps=self.gradient_accumulation_steps,
            learning_rate=self.learning_rate,
            warmup_ratio=self.warmup_ratio,
            max_sequence_length=self.max_sequence_length,
            evaluation_frequency=self.evaluation_frequency,
            checkpoint_frequency=self.checkpoint_frequency,
            seed=self.seed,
            precision=self.precision,
            gradient_checkpointing=self.gradient_checkpointing,
            max_checkpoints=self.max_checkpoints,
            resume_from_checkpoint=self.resume_from_checkpoint,
            use_lora=self.use_lora,
            lora_rank=self.lora_rank,
            lora_alpha=self.lora_alpha,
            lora_dropout=self.lora_dropout,
            target_modules=self.target_modules,
            hardware_policy=self.hardware_policy,
            dry_run=self.dry_run,
            training_method=self.training_method,
            base_model_parameters=self.base_model_parameters,
            base_model_size_bytes=self.base_model_size_bytes,
            notes=self.notes,
        )

    @classmethod
    def from_training_config(
        cls, config: TrainingConfig, **updates: Any
    ) -> PreferenceTrainingConfig:
        """Read a Phase 16 configuration into this one (the fields they share)."""
        return cls(
            base_model=config.base_model,
            preference_dataset_version=config.dataset_version,
            output_directory=config.output_directory,
            epochs=config.epochs,
            batch_size=config.batch_size,
            gradient_accumulation_steps=config.gradient_accumulation_steps,
            learning_rate=config.learning_rate,
            warmup_ratio=config.warmup_ratio,
            max_sequence_length=config.max_sequence_length,
            evaluation_frequency=config.evaluation_frequency,
            checkpoint_frequency=config.checkpoint_frequency,
            seed=config.seed,
            precision=config.precision,
            gradient_checkpointing=config.gradient_checkpointing,
            max_checkpoints=config.max_checkpoints,
            resume_from_checkpoint=config.resume_from_checkpoint,
            use_lora=config.use_lora,
            training_method=config.training_method,
            lora_rank=config.lora_rank,
            lora_alpha=config.lora_alpha,
            lora_dropout=config.lora_dropout,
            target_modules=config.target_modules,
            hardware_policy=config.hardware_policy,
            dry_run=config.dry_run,
            base_model_parameters=config.base_model_parameters,
            base_model_size_bytes=config.base_model_size_bytes,
            notes=config.notes,
            **updates,
        )

    # -- writing ----------------------------------------------------------------

    def to_mapping(self) -> dict[str, Any]:
        return {
            "base_model": self.base_model,
            "reference_model": self.reference_model,
            "preference_dataset_version": self.preference_dataset_version,
            "dataset_type": self.dataset_type,
            "algorithm": self.algorithm,
            "output_directory": self.output_directory,
            "epochs": self.epochs,
            "batch_size": self.batch_size,
            "gradient_accumulation_steps": self.gradient_accumulation_steps,
            "learning_rate": self.learning_rate,
            "beta": self.beta,
            "warmup_ratio": self.warmup_ratio,
            "max_sequence_length": self.max_sequence_length,
            "evaluation_frequency": self.evaluation_frequency,
            "checkpoint_frequency": self.checkpoint_frequency,
            "seed": self.seed,
            "precision": self.precision,
            "gradient_checkpointing": self.gradient_checkpointing,
            "max_checkpoints": self.max_checkpoints,
            "resume_from_checkpoint": self.resume_from_checkpoint,
            "use_lora": self.use_lora,
            "training_method": self.training_method,
            "lora_rank": self.lora_rank,
            "lora_alpha": self.lora_alpha,
            "lora_dropout": self.lora_dropout,
            "target_modules": list(self.target_modules),
            "hardware_policy": self.hardware_policy,
            "dry_run": self.dry_run,
            "base_model_parameters": self.base_model_parameters,
            "base_model_size_bytes": self.base_model_size_bytes,
            "notes": self.notes,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> PreferenceTrainingConfig:
        """Read a configuration, keeping defaults for anything unusable.

        Parsing is not validation, exactly as in Phase 16: a value that cannot be
        read at all keeps its default so the error message names the field rather
        than reporting a silent zero.
        """
        defaults = cls()
        if not isinstance(data, Mapping):
            return defaults
        return cls(
            base_model=_text(data.get("base_model")),
            reference_model=_text(data.get("reference_model")),
            preference_dataset_version=_text(
                data.get("preference_dataset_version"), _text(data.get("dataset_version"))
            ),
            dataset_type=_text(data.get("dataset_type")),
            algorithm=_text(data.get("algorithm"), defaults.algorithm),
            output_directory=_text(data.get("output_directory")),
            epochs=_whole(data.get("epochs"), defaults.epochs),
            batch_size=_whole(data.get("batch_size"), defaults.batch_size),
            gradient_accumulation_steps=_whole(
                data.get("gradient_accumulation_steps"),
                defaults.gradient_accumulation_steps,
            ),
            learning_rate=_real(data.get("learning_rate"), defaults.learning_rate),
            beta=_real(data.get("beta"), defaults.beta),
            warmup_ratio=_real(data.get("warmup_ratio"), defaults.warmup_ratio),
            max_sequence_length=_whole(
                data.get("max_sequence_length"), defaults.max_sequence_length
            ),
            evaluation_frequency=_whole(
                data.get("evaluation_frequency"), defaults.evaluation_frequency
            ),
            checkpoint_frequency=_whole(
                data.get("checkpoint_frequency"), defaults.checkpoint_frequency
            ),
            seed=_whole(data.get("seed"), defaults.seed),
            precision=_text(data.get("precision"), defaults.precision),
            gradient_checkpointing=_flag(
                data.get("gradient_checkpointing"), defaults.gradient_checkpointing
            ),
            max_checkpoints=_whole(data.get("max_checkpoints"), defaults.max_checkpoints),
            resume_from_checkpoint=_text(data.get("resume_from_checkpoint")),
            use_lora=_flag(data.get("use_lora"), defaults.use_lora),
            training_method=_text(data.get("training_method"), defaults.training_method),
            lora_rank=_whole(data.get("lora_rank"), defaults.lora_rank),
            lora_alpha=_whole(data.get("lora_alpha"), defaults.lora_alpha),
            lora_dropout=_real(data.get("lora_dropout"), defaults.lora_dropout),
            target_modules=_target_modules(data.get("target_modules")),
            hardware_policy=_text(data.get("hardware_policy"), defaults.hardware_policy),
            dry_run=_flag(data.get("dry_run"), defaults.dry_run),
            base_model_parameters=_whole(
                data.get("base_model_parameters"), defaults.base_model_parameters
            ),
            base_model_size_bytes=_whole(
                data.get("base_model_size_bytes"), defaults.base_model_size_bytes
            ),
            notes=_text(data.get("notes")),
        )

    def with_defaults(self, *, output_directory: str = "") -> PreferenceTrainingConfig:
        """The same config with the manager's defaults filled in for blank fields."""
        updates: dict[str, Any] = {}
        if not self.output_directory and output_directory:
            updates["output_directory"] = output_directory
        return replace(self, **updates) if updates else self

    def fingerprint(self) -> str:
        """A stable identity for a configuration, for a run that must say what ran."""
        payload = json.dumps(self.to_mapping(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def resolve_sequence(value: Any) -> tuple[str, ...]:
    """Read a sequence of strings from a mapping value (API convenience)."""
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, Sequence):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


__all__ = [
    "MAX_BETA",
    "MAX_CHECKPOINTS",
    "MAX_EPOCHS",
    "MIN_BETA",
    "PRECISIONS",
    "PreferenceTrainingConfig",
    "resolve_sequence",
]
