"""Training configuration: every parameter the trainer obeys, in one place.

Nothing under a training backend hard-codes an epoch count, a learning rate or
a LoRA rank. A :class:`TrainingConfig` carries them, :meth:`TrainingConfig.validate`
returns the errors and warnings a configuration has, and a stored training run
keeps the exact mapping it ran under — so a checkpoint can be explained long
after the defaults moved.

Defaults are the safe ones for the machine this project targets: one epoch,
LoRA on, ``dry_run`` on. A real training run has to be asked for explicitly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from novacontrol.training.models import DatasetType, HardwarePolicy, TrainingMethod

#: Bounds a configuration is read against. A value outside these is an error,
#: not a clamp: silently changing what someone asked for is worse than saying no.
MAX_EPOCHS = 100
MAX_BATCH_SIZE = 4096
MAX_GRADIENT_ACCUMULATION = 4096
MAX_SEQUENCE_LENGTH = 131_072
MAX_CHECKPOINTS = 100
MAX_LORA_RANK = 1024
MAX_LORA_ALPHA = 8192

PRECISIONS: tuple[str, ...] = ("fp32", "fp16", "bf16", "int8", "int4")


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
        parts = [part.strip() for part in value.split(",")]
        return tuple(part for part in parts if part)
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


@dataclass(frozen=True, slots=True)
class TrainingConfigValidation:
    """What is wrong (and what is merely worth saying) about a configuration."""

    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class TrainingConfig:
    """One training run's parameters.

    ``base_model_size_bytes``/``base_model_parameters`` are optional readings:
    zero means "not measured", and the resource estimator will say so rather
    than invent a size. Everything else has a usable default.
    """

    base_model: str = ""
    dataset_version: str = ""
    dataset_type: str = ""
    output_directory: str = ""
    epochs: int = 1
    batch_size: int = 1
    gradient_accumulation_steps: int = 8
    learning_rate: float = 2e-4
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
    lora_rank: int = 8
    lora_alpha: int = 16
    lora_dropout: float = 0.05
    target_modules: tuple[str, ...] = ()
    hardware_policy: str = HardwarePolicy.AUTO.value
    dry_run: bool = True
    training_method: str = TrainingMethod.LORA.value
    base_model_parameters: int = 0
    base_model_size_bytes: int = 0
    notes: str = ""

    # -- reading ---------------------------------------------------------------

    @property
    def effective_method(self) -> str:
        """The method that will actually run, reconciled with ``use_lora``."""
        if not self.use_lora:
            return TrainingMethod.FULL.value
        return self.training_method

    def validate(self) -> TrainingConfigValidation:
        """Every reason this configuration should not start, and every caveat."""
        errors: list[str] = []
        warnings: list[str] = []
        if not self.base_model:
            errors.append("base_model is required")
        if not self.dataset_version:
            errors.append("dataset_version is required (name@version)")
        if not self.output_directory:
            errors.append("output_directory is required")
        if self.dataset_type:
            valid_types = {member.value for member in DatasetType}
            if self.dataset_type not in valid_types:
                errors.append(
                    f"unknown dataset_type {self.dataset_type!r}: expected one of "
                    + ", ".join(sorted(valid_types))
                )
        if not 1 <= self.epochs <= MAX_EPOCHS:
            errors.append(f"epochs must be between 1 and {MAX_EPOCHS} (got {self.epochs})")
        if not 1 <= self.batch_size <= MAX_BATCH_SIZE:
            errors.append(
                f"batch_size must be between 1 and {MAX_BATCH_SIZE} (got {self.batch_size})"
            )
        if not 1 <= self.gradient_accumulation_steps <= MAX_GRADIENT_ACCUMULATION:
            errors.append(
                "gradient_accumulation_steps must be between 1 and "
                f"{MAX_GRADIENT_ACCUMULATION} (got {self.gradient_accumulation_steps})"
            )
        if not 0.0 < self.learning_rate <= 1.0:
            errors.append(f"learning_rate must be in (0, 1] (got {self.learning_rate})")
        if not 0.0 <= self.warmup_ratio <= 1.0:
            errors.append(f"warmup_ratio must be in [0, 1] (got {self.warmup_ratio})")
        if not 64 <= self.max_sequence_length <= MAX_SEQUENCE_LENGTH:
            errors.append(
                f"max_sequence_length must be between 64 and {MAX_SEQUENCE_LENGTH} "
                f"(got {self.max_sequence_length})"
            )
        if self.evaluation_frequency < 0:
            errors.append("evaluation_frequency must be zero (end of epoch) or positive")
        if self.checkpoint_frequency < 0:
            errors.append("checkpoint_frequency must be zero (end of epoch) or positive")
        if self.precision not in PRECISIONS:
            errors.append(
                f"precision must be one of {', '.join(PRECISIONS)} (got {self.precision!r})"
            )
        if not 1 <= self.max_checkpoints <= MAX_CHECKPOINTS:
            errors.append(
                f"max_checkpoints must be between 1 and {MAX_CHECKPOINTS} "
                f"(got {self.max_checkpoints})"
            )
        policy = {member.value for member in HardwarePolicy}
        if self.hardware_policy not in policy:
            errors.append(
                f"hardware_policy must be one of {', '.join(sorted(policy))} "
                f"(got {self.hardware_policy!r})"
            )
        method = {member.value for member in TrainingMethod}
        if self.training_method not in method:
            errors.append(
                f"training_method must be one of {', '.join(sorted(method))} "
                f"(got {self.training_method!r})"
            )
        if self.training_method != TrainingMethod.FULL.value and not self.use_lora:
            errors.append(
                f"training_method {self.training_method!r} requires use_lora=true "
                "(or choose training_method=full)"
            )
        elif self.training_method == TrainingMethod.FULL.value and self.use_lora:
            warnings.append(
                "training_method=full ignores the LoRA settings; the whole model will "
                "be trained and the resource estimate will say whether that fits"
            )
        if self.use_lora:
            if not 1 <= self.lora_rank <= MAX_LORA_RANK:
                errors.append(
                    f"lora_rank must be between 1 and {MAX_LORA_RANK} (got {self.lora_rank})"
                )
            if not 1 <= self.lora_alpha <= MAX_LORA_ALPHA:
                errors.append(
                    f"lora_alpha must be between 1 and {MAX_LORA_ALPHA} (got {self.lora_alpha})"
                )
            if not 0.0 <= self.lora_dropout <= 0.9:
                errors.append(f"lora_dropout must be in [0, 0.9] (got {self.lora_dropout})")
            if any(not isinstance(name, str) or not name for name in self.target_modules):
                errors.append("target_modules must be a list of module names")
        if self.base_model_parameters < 0:
            errors.append("base_model_parameters cannot be negative")
        if self.base_model_size_bytes < 0:
            errors.append("base_model_size_bytes cannot be negative")
        if not self.dry_run:
            warnings.append(
                "dry_run is off: a real training run needs the optional training "
                "dependencies installed and an explicit confirmation"
            )
        if self.effective_method == TrainingMethod.QLORA.value and self.precision == "fp32":
            warnings.append(
                "qlora quantises the base model; precision=fp32 describes the "
                "adapter computation, and the estimate assumes a 4-bit base"
            )
        return TrainingConfigValidation(errors=tuple(errors), warnings=tuple(warnings))

    # -- writing ---------------------------------------------------------------

    def to_mapping(self) -> dict[str, Any]:
        return {
            "base_model": self.base_model,
            "dataset_version": self.dataset_version,
            "dataset_type": self.dataset_type,
            "output_directory": self.output_directory,
            "epochs": self.epochs,
            "batch_size": self.batch_size,
            "gradient_accumulation_steps": self.gradient_accumulation_steps,
            "learning_rate": self.learning_rate,
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
            "lora_rank": self.lora_rank,
            "lora_alpha": self.lora_alpha,
            "lora_dropout": self.lora_dropout,
            "target_modules": list(self.target_modules),
            "hardware_policy": self.hardware_policy,
            "dry_run": self.dry_run,
            "training_method": self.training_method,
            "base_model_parameters": self.base_model_parameters,
            "base_model_size_bytes": self.base_model_size_bytes,
            "notes": self.notes,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> TrainingConfig:
        """Read a configuration, keeping defaults for anything unusable."""
        defaults = cls()
        if not isinstance(data, Mapping):
            return defaults
        return cls(
            base_model=_text(data.get("base_model")),
            dataset_version=_text(data.get("dataset_version")),
            dataset_type=_text(data.get("dataset_type")),
            output_directory=_text(data.get("output_directory")),
            epochs=_whole(data.get("epochs"), defaults.epochs),
            batch_size=_whole(data.get("batch_size"), defaults.batch_size),
            gradient_accumulation_steps=_whole(
                data.get("gradient_accumulation_steps"), defaults.gradient_accumulation_steps
            ),
            learning_rate=_real(data.get("learning_rate"), defaults.learning_rate),
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
            lora_rank=_whole(data.get("lora_rank"), defaults.lora_rank),
            lora_alpha=_whole(data.get("lora_alpha"), defaults.lora_alpha),
            lora_dropout=_real(data.get("lora_dropout"), defaults.lora_dropout),
            target_modules=_target_modules(data.get("target_modules")),
            hardware_policy=_text(data.get("hardware_policy"), defaults.hardware_policy),
            dry_run=_flag(data.get("dry_run"), defaults.dry_run),
            training_method=_text(data.get("training_method"), defaults.training_method),
            base_model_parameters=_whole(
                data.get("base_model_parameters"), defaults.base_model_parameters
            ),
            base_model_size_bytes=_whole(
                data.get("base_model_size_bytes"), defaults.base_model_size_bytes
            ),
            notes=_text(data.get("notes")),
        )

    def with_defaults(self, *, output_directory: str = "") -> TrainingConfig:
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
    "MAX_BATCH_SIZE",
    "MAX_CHECKPOINTS",
    "MAX_EPOCHS",
    "MAX_GRADIENT_ACCUMULATION",
    "MAX_LORA_ALPHA",
    "MAX_LORA_RANK",
    "MAX_SEQUENCE_LENGTH",
    "PRECISIONS",
    "TrainingConfig",
    "TrainingConfigValidation",
    "resolve_sequence",
]
