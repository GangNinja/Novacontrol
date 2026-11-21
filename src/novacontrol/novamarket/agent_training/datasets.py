"""Chronological training-data preparation built on the PI-14 loader.

Training, validation and test datasets are loaded independently through
``PolicyEvaluationEngine.dataset``.  That loader bounds history at the split's
end, so feature/forecast warm-up never sees future bars.  The resulting
provenance is persisted with the run and every policy artifact.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from novacontrol.novamarket.rewards.evaluation import (
    EvaluationDataset,
    PolicyEvaluationEngine,
)
from novacontrol.novamarket.rewards.models import (
    EvaluationConfig,
    RewardConfig,
    SplitDefinition,
    canonical_hash,
)
from novacontrol.novamarket.rl.models import RLEnvironmentConfig

from .models import TrainingConfig, TrainingValidationError

BarLoader = Callable[[RLEnvironmentConfig], Sequence[Any]]
FeatureLoader = Callable[
    [RLEnvironmentConfig, datetime, Sequence[Any]], Mapping[str, Any] | None
]
ForecastLoader = Callable[[RLEnvironmentConfig, datetime], Mapping[str, Any] | None]


@dataclass(frozen=True, slots=True)
class TrainingDataBundle:
    config: TrainingConfig
    datasets: Mapping[str, EvaluationDataset]
    evaluation_configs: Mapping[str, EvaluationConfig]
    reward_config: RewardConfig
    provenance: Mapping[str, Any]

    @property
    def data_hash(self) -> str:
        return str(self.provenance["data_hash"])

    def environment_for(self, split: str) -> EvaluationDataset:
        return self.datasets[split]


class TrainingDataPreparer:
    """Loads only the three immutable chronological windows."""

    def __init__(
        self,
        *,
        bar_loader: BarLoader,
        feature_loader: FeatureLoader | None = None,
        forecast_loader: ForecastLoader | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = PolicyEvaluationEngine(
            bar_loader=bar_loader,
            feature_loader=feature_loader,
            forecast_loader=forecast_loader,
            clock=clock,
        )

    def prepare(self, config: TrainingConfig) -> TrainingDataBundle:
        try:
            environment = RLEnvironmentConfig.from_mapping(config.environment)
            reward_config = RewardConfig.from_mapping(config.reward_config)
        except Exception as exc:  # noqa: BLE001
            raise TrainingValidationError(f"invalid training dependency: {exc}") from exc
        split_definitions = {
            label: SplitDefinition(
                label=str(config.splits[label]["label"]),
                start=datetime.fromisoformat(str(config.splits[label]["start"])),
                end=datetime.fromisoformat(str(config.splits[label]["end"])),
            )
            for label in ("train", "validation", "test")
        }
        # Rebuild the PI-14 evaluation config per split.  Passing the same
        # definitions to every call makes the loader reject accidental overlap.
        base_payload = {
            "name": f"PI-15 data preparation: {config.name}",
            "environment": dict(config.environment),
            "policies": ("no_action",),
            "reward_config": dict(config.reward_config),
            "split": "train",
            "splits": {
                label: definition.to_dict()
                for label, definition in split_definitions.items()
            },
            "max_transitions": max(
                config.validation_max_transitions, config.test_max_transitions
            ),
            "description": "Chronological PI-15 split preparation; no policy result is used.",
        }
        try:
            common = EvaluationConfig.from_mapping(base_payload)
            datasets: dict[str, EvaluationDataset] = {}
            evaluation_configs: dict[str, EvaluationConfig] = {}
            for label in ("train", "validation", "test"):
                split_payload = {**base_payload, "split": label}
                split_config = EvaluationConfig.from_mapping(split_payload)
                datasets[label] = self._engine.dataset(split_config)
                evaluation_configs[label] = split_config
        except Exception as exc:  # noqa: BLE001
            raise TrainingValidationError(
                f"chronological training dataset preparation failed: {exc}"
            ) from exc
        if not datasets["train"].bars or not datasets["validation"].bars:
            raise TrainingValidationError(
                "training and validation splits must each contain at least two completed bars"
            )
        train_end = datasets["train"].end
        validation_start = datasets["validation"].start
        validation_end = datasets["validation"].end
        test_start = datasets["test"].start
        if not train_end < validation_start or not validation_end < test_start:
            raise TrainingValidationError(
                "prepared training splits overlap; refusing to use leaky data"
            )
        split_payload = {
            label: dataset.to_dict() for label, dataset in datasets.items()
        }
        data_payload = {
            "dataset_version": environment.dataset_version,
            "feature_set_version": config.feature_set_version,
            "environment_version": environment.environment_version,
            "reward_config_hash": reward_config.config_hash,
            "reward_version": reward_config.version,
            "environment_config_hash": environment.config_hash,
            "splits": split_payload,
            "evaluation_config_hash": common.config_hash,
            "warmup_policy": "history bounded at split end; training split precedes validation",
            "normalization": "none: observations are fixed raw projections",
        }
        provenance = {
            **data_payload,
            "data_hash": canonical_hash(data_payload),
            "combined_bars_hash": canonical_hash(
                {label: dataset.bars_hash for label, dataset in datasets.items()}
            ),
            "split_order": ["train", "validation", "test"],
            "test_used_for_training": False,
            "test_used_for_hyperparameters": False,
            "chronological_separation_verified": True,
            "simulated": True,
            "advisory_only": True,
            "production_authority": False,
            "warnings": sorted(
                {warning for dataset in datasets.values() for warning in dataset.warnings}
            ),
        }
        return TrainingDataBundle(
            config=config,
            datasets=datasets,
            evaluation_configs=evaluation_configs,
            reward_config=reward_config,
            provenance=provenance,
        )


__all__ = ["TrainingDataBundle", "TrainingDataPreparer"]
