"""PI-15 contracts for isolated experimental RL training.

These records live beside the PI-13 environments and PI-14 reward evidence in
the same isolated research SQLite database.  They intentionally carry no
portfolio, order, wallet, autonomy or operator-permission handles.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from novacontrol.novamarket.rl.models import RL_OBSERVATION_SCHEMA_VERSION

TRAINING_PHASE = "PI-15"
TRAINING_CONTRACT_VERSION = "pi15-rl-training-v1"
TRAINING_CONFIG_SCHEMA_VERSION = "pi15-training-config-v1"
POLICY_SCHEMA_VERSION = "pi15-policy-artifact-v1"
CHECKPOINT_SCHEMA_VERSION = "pi15-training-checkpoint-v1"
TRAINING_RUN_SCHEMA_VERSION = "pi15-training-run-v1"

MAX_CONFIGS = 200
MAX_RUNS = 500
MAX_CHECKPOINTS = 2_000
MAX_TRIALS = 500
MAX_POLICIES = 500
MAX_EVALUATIONS = 1_000
MAX_REPLAY_CAPACITY = 50_000
MAX_MAX_STEPS = 200_000
MAX_EPISODES = 1_000
MAX_EPISODE_LENGTH = 5_000
MAX_HIDDEN_LAYERS = 3
MAX_HIDDEN_WIDTH = 512
MAX_TRAINING_SECONDS = 3_600
MAX_MEMORY_MB = 16_384
MAX_OPTIMIZATION_TRIALS = 20
MAX_COMPARED_POLICIES = 8
SUPPORTED_ALGORITHMS = ("dqn",)
SUPPORTED_ACTIVATIONS = ("relu", "tanh")
SUPPORTED_OPTIMIZERS = ("adam", "sgd")
SPLIT_NAMES = ("train", "validation", "test")


class TrainingError(ValueError):
    """A caller supplied a training request outside the PI-15 contract."""


class TrainingValidationError(TrainingError):
    """A training configuration or artifact failed validation."""


class TrainingStateError(RuntimeError):
    """The training lifecycle does not permit the requested operation."""


class TrainingResourceError(RuntimeError):
    """A bounded resource limit would be exceeded."""


class TrainingStoreError(RuntimeError):
    """The isolated training store cannot complete the request."""


class TrainingRunStatus(StrEnum):
    CREATED = "created"
    VALIDATING = "validating"
    PREPARING = "preparing"
    RUNNING = "running"
    PAUSED = "paused"
    VALIDATING_POLICY = "validating_policy"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            TrainingRunStatus.COMPLETED,
            TrainingRunStatus.FAILED,
            TrainingRunStatus.CANCELLED,
        }


class PolicyStatus(StrEnum):
    CREATED = "created"
    TRAINING = "training"
    TRAINED = "trained"
    VALIDATION_PENDING = "validation_pending"
    VALIDATED = "validated"
    TESTED = "tested"
    REJECTED = "rejected"
    ARCHIVED = "archived"


ALLOWED_POLICY_TRANSITIONS: Mapping[str, frozenset[str]] = {
    PolicyStatus.CREATED.value: frozenset(
        {PolicyStatus.TRAINING.value, PolicyStatus.TRAINED.value, PolicyStatus.REJECTED.value}
    ),
    PolicyStatus.TRAINING.value: frozenset(
        {PolicyStatus.TRAINED.value, PolicyStatus.REJECTED.value}
    ),
    PolicyStatus.TRAINED.value: frozenset(
        {
            PolicyStatus.VALIDATION_PENDING.value,
            PolicyStatus.VALIDATED.value,
            PolicyStatus.TESTED.value,
            PolicyStatus.REJECTED.value,
            PolicyStatus.ARCHIVED.value,
        }
    ),
    PolicyStatus.VALIDATION_PENDING.value: frozenset(
        {PolicyStatus.VALIDATED.value, PolicyStatus.REJECTED.value}
    ),
    PolicyStatus.VALIDATED.value: frozenset(
        {PolicyStatus.TESTED.value, PolicyStatus.REJECTED.value, PolicyStatus.ARCHIVED.value}
    ),
    PolicyStatus.TESTED.value: frozenset(
        {PolicyStatus.REJECTED.value, PolicyStatus.ARCHIVED.value}
    ),
    PolicyStatus.REJECTED.value: frozenset({PolicyStatus.ARCHIVED.value}),
    PolicyStatus.ARCHIVED.value: frozenset(),
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def canonical_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise TrainingValidationError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TrainingValidationError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise TrainingValidationError(f"{name} must be a finite number")
    return number


def _whole(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise TrainingValidationError(f"{name} must be an integer")
    try:
        number = int(str(value))
    except (TypeError, ValueError) as exc:
        raise TrainingValidationError(f"{name} must be an integer") from exc
    if str(value).strip() not in {str(number), f"{float(number):.0f}"}:
        # Accept integer strings and integral JSON numbers only.
        try:
            if float(value) != number:
                raise TrainingValidationError(f"{name} must be an integer")
        except (TypeError, ValueError) as exc:
            raise TrainingValidationError(f"{name} must be an integer") from exc
    return number


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TrainingValidationError(f"{name} must be an object")
    return {str(key): item for key, item in value.items()}


def _time(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        at = value
    else:
        try:
            at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise TrainingValidationError(f"{name} must be an ISO timestamp") from exc
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return at.astimezone(UTC)


def _split_payload(value: Mapping[str, Any]) -> dict[str, str]:
    known = {"label", "start", "end"}
    unknown = sorted(set(value) - known)
    if unknown:
        raise TrainingValidationError("unknown split field(s): " + ", ".join(unknown))
    return {
        "label": _text(value.get("label")),
        "start": _time(value.get("start"), "split start").isoformat(),
        "end": _time(value.get("end"), "split end").isoformat(),
    }


@dataclass(frozen=True, slots=True)
class TrainingConfig:
    """One immutable, versioned experimental training configuration."""

    name: str
    algorithm: str = "dqn"
    feature_set_version: str = RL_OBSERVATION_SCHEMA_VERSION
    environment: Mapping[str, Any] = field(default_factory=dict)
    reward_config: Mapping[str, Any] = field(default_factory=dict)
    splits: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    max_episodes: int = 5
    max_steps: int = 500
    episode_length: int = 63
    batch_size: int = 32
    learning_rate: float = 0.001
    gamma: float = 0.99
    epsilon_start: float = 1.0
    epsilon_end: float = 0.05
    epsilon_decay: float = 0.995
    hidden_layers: tuple[int, ...] = (64, 32)
    activation: str = "relu"
    optimizer: str = "adam"
    target_update_interval: int = 25
    replay_capacity: int = 5_000
    checkpoint_interval: int = 100
    validation_interval: int = 2
    validation_max_transitions: int = 80
    test_max_transitions: int = 120
    max_wall_seconds: int = 600
    max_memory_mb: int = 1_024
    seed: int = 42
    description: str = ""

    def __post_init__(self) -> None:
        name = _text(self.name)
        if not name:
            raise TrainingValidationError("training configuration name is required")
        algorithm = _text(self.algorithm, "dqn").lower()
        if algorithm not in SUPPORTED_ALGORITHMS:
            raise TrainingValidationError(
                "algorithm must be one of " + ", ".join(SUPPORTED_ALGORITHMS)
            )
        feature_set_version = _text(self.feature_set_version)
        if feature_set_version != RL_OBSERVATION_SCHEMA_VERSION:
            raise TrainingValidationError(
                "feature_set_version must match the PI-13 observation schema "
                f"{RL_OBSERVATION_SCHEMA_VERSION!r}"
            )
        environment = _mapping(self.environment, "environment")
        if not environment:
            raise TrainingValidationError("environment configuration is required")
        from novacontrol.novamarket.rl.models import RLEnvironmentConfig

        RLEnvironmentConfig.from_mapping(environment)
        reward_config = _mapping(self.reward_config, "reward_config")
        from novacontrol.novamarket.rewards.models import RewardConfig

        RewardConfig.from_mapping(reward_config)
        splits = _mapping(self.splits, "splits")
        missing = [label for label in SPLIT_NAMES if label not in splits]
        unknown = [label for label in splits if label not in SPLIT_NAMES]
        if missing:
            raise TrainingValidationError(
                "training requires train, validation and test splits; missing: "
                + ", ".join(missing)
            )
        if unknown:
            raise TrainingValidationError("unknown split(s): " + ", ".join(unknown))
        normalized_splits = {
            label: _split_payload(_mapping(splits[label], f"split {label}"))
            for label in SPLIT_NAMES
        }
        windows = []
        for label in SPLIT_NAMES:
            row = normalized_splits[label]
            start = _time(row["start"], f"{label} start")
            end = _time(row["end"], f"{label} end")
            if start >= end:
                raise TrainingValidationError(
                    f"{label} split start must precede its end by at least one bar timestamp"
                )
            windows.append((label, start, end))
        for previous, current in zip(windows, windows[1:], strict=False):
            if previous[2] >= current[1]:
                raise TrainingValidationError(
                    "training splits must be chronological and non-overlapping: "
                    f"{previous[0]} ends at {previous[2].isoformat()}, "
                    f"{current[0]} starts at {current[1].isoformat()}"
                )
        environment_start = _time(environment.get("start"), "environment start")
        environment_end = _time(environment.get("end"), "environment end")
        for label, start, end in windows:
            if start < environment_start or end > environment_end:
                raise TrainingValidationError(
                    f"{label} split must lie inside the environment window"
                )
        max_episodes = _whole(self.max_episodes, "max_episodes")
        max_steps = _whole(self.max_steps, "max_steps")
        episode_length = _whole(self.episode_length, "episode_length")
        batch_size = _whole(self.batch_size, "batch_size")
        replay_capacity = _whole(self.replay_capacity, "replay_capacity")
        target_update = _whole(self.target_update_interval, "target_update_interval")
        checkpoint_interval = _whole(self.checkpoint_interval, "checkpoint_interval")
        validation_interval = _whole(self.validation_interval, "validation_interval")
        validation_max = _whole(self.validation_max_transitions, "validation_max_transitions")
        test_max = _whole(self.test_max_transitions, "test_max_transitions")
        max_wall = _whole(self.max_wall_seconds, "max_wall_seconds")
        max_memory = _whole(self.max_memory_mb, "max_memory_mb")
        seed = _whole(self.seed, "seed")
        if not 1 <= max_episodes <= MAX_EPISODES:
            raise TrainingValidationError(f"max_episodes must be within [1, {MAX_EPISODES}]")
        if not 1 <= max_steps <= MAX_MAX_STEPS:
            raise TrainingValidationError(f"max_steps must be within [1, {MAX_MAX_STEPS}]")
        if not 2 <= episode_length <= MAX_EPISODE_LENGTH:
            raise TrainingValidationError(
                f"episode_length must be within [2, {MAX_EPISODE_LENGTH}]"
            )
        if not 1 <= batch_size <= 512:
            raise TrainingValidationError("batch_size must be within [1, 512]")
        if not 1 <= replay_capacity <= MAX_REPLAY_CAPACITY:
            raise TrainingValidationError(
                f"replay_capacity must be within [1, {MAX_REPLAY_CAPACITY}]"
            )
        if target_update < 1 or checkpoint_interval < 1 or validation_interval < 1:
            raise TrainingValidationError(
                "target_update_interval, checkpoint_interval and validation_interval "
                "must be positive"
            )
        if not 1 <= validation_max <= 2_000 or not 1 <= test_max <= 2_000:
            raise TrainingValidationError(
                "validation_max_transitions and test_max_transitions must be within [1, 2000]"
            )
        if not 1 <= max_wall <= MAX_TRAINING_SECONDS:
            raise TrainingValidationError(
                f"max_wall_seconds must be within [1, {MAX_TRAINING_SECONDS}]"
            )
        if not 1 <= max_memory <= MAX_MEMORY_MB:
            raise TrainingValidationError(f"max_memory_mb must be within [1, {MAX_MEMORY_MB}]")
        learning_rate = _finite(self.learning_rate, "learning_rate")
        gamma = _finite(self.gamma, "gamma")
        epsilon_start = _finite(self.epsilon_start, "epsilon_start")
        epsilon_end = _finite(self.epsilon_end, "epsilon_end")
        epsilon_decay = _finite(self.epsilon_decay, "epsilon_decay")
        if not 1e-6 <= learning_rate <= 0.5:
            raise TrainingValidationError("learning_rate must be within [1e-6, 0.5]")
        if not 0.0 <= gamma < 1.0:
            raise TrainingValidationError("gamma must be within [0, 1)")
        if not 0.0 <= epsilon_end <= epsilon_start <= 1.0:
            raise TrainingValidationError(
                "epsilon must satisfy 0 <= epsilon_end <= epsilon_start <= 1"
            )
        if not 0.0 < epsilon_decay <= 1.0:
            raise TrainingValidationError("epsilon_decay must be within (0, 1]")
        hidden_layers = tuple(self.hidden_layers)
        if not 1 <= len(hidden_layers) <= MAX_HIDDEN_LAYERS:
            raise TrainingValidationError(
                f"hidden_layers must contain 1-{MAX_HIDDEN_LAYERS} layer sizes"
            )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 4 <= value <= MAX_HIDDEN_WIDTH
            for value in hidden_layers
        ):
            raise TrainingValidationError(
                f"hidden layer widths must be integers within [4, {MAX_HIDDEN_WIDTH}]"
            )
        activation = _text(self.activation, "relu").lower()
        optimizer = _text(self.optimizer, "adam").lower()
        if activation not in SUPPORTED_ACTIVATIONS:
            raise TrainingValidationError(
                "activation must be one of " + ", ".join(SUPPORTED_ACTIVATIONS)
            )
        if optimizer not in SUPPORTED_OPTIMIZERS:
            raise TrainingValidationError(
                "optimizer must be one of " + ", ".join(SUPPORTED_OPTIMIZERS)
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "algorithm", algorithm)
        object.__setattr__(self, "feature_set_version", feature_set_version)
        object.__setattr__(self, "environment", dict(sorted(environment.items())))
        object.__setattr__(self, "reward_config", dict(sorted(reward_config.items())))
        object.__setattr__(
            self,
            "splits",
            {
                label: {
                    "label": normalized_splits[label]["label"] or label,
                    "start": normalized_splits[label]["start"],
                    "end": normalized_splits[label]["end"],
                }
                for label in SPLIT_NAMES
            },
        )
        object.__setattr__(self, "max_episodes", max_episodes)
        object.__setattr__(self, "max_steps", max_steps)
        object.__setattr__(self, "episode_length", episode_length)
        object.__setattr__(self, "batch_size", batch_size)
        object.__setattr__(self, "learning_rate", learning_rate)
        object.__setattr__(self, "gamma", gamma)
        object.__setattr__(self, "epsilon_start", epsilon_start)
        object.__setattr__(self, "epsilon_end", epsilon_end)
        object.__setattr__(self, "epsilon_decay", epsilon_decay)
        object.__setattr__(self, "hidden_layers", hidden_layers)
        object.__setattr__(self, "activation", activation)
        object.__setattr__(self, "optimizer", optimizer)
        object.__setattr__(self, "target_update_interval", target_update)
        object.__setattr__(self, "replay_capacity", replay_capacity)
        object.__setattr__(self, "checkpoint_interval", checkpoint_interval)
        object.__setattr__(self, "validation_interval", validation_interval)
        object.__setattr__(self, "validation_max_transitions", validation_max)
        object.__setattr__(self, "test_max_transitions", test_max)
        object.__setattr__(self, "max_wall_seconds", max_wall)
        object.__setattr__(self, "max_memory_mb", max_memory)
        object.__setattr__(self, "seed", seed)
        object.__setattr__(self, "description", str(self.description or ""))

    @property
    def config_hash(self) -> str:
        return canonical_hash(self.payload())

    @property
    def config_id(self) -> str:
        return "rl_train_cfg_" + self.config_hash[:20]

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "algorithm": self.algorithm,
            "feature_set_version": self.feature_set_version,
            "environment": dict(self.environment),
            "reward_config": dict(self.reward_config),
            "splits": {
                label: dict(row) for label, row in self.splits.items()
            },
            "max_episodes": self.max_episodes,
            "max_steps": self.max_steps,
            "episode_length": self.episode_length,
            "batch_size": self.batch_size,
            "learning_rate": self.learning_rate,
            "gamma": self.gamma,
            "epsilon_start": self.epsilon_start,
            "epsilon_end": self.epsilon_end,
            "epsilon_decay": self.epsilon_decay,
            "hidden_layers": list(self.hidden_layers),
            "activation": self.activation,
            "optimizer": self.optimizer,
            "target_update_interval": self.target_update_interval,
            "replay_capacity": self.replay_capacity,
            "checkpoint_interval": self.checkpoint_interval,
            "validation_interval": self.validation_interval,
            "validation_max_transitions": self.validation_max_transitions,
            "test_max_transitions": self.test_max_transitions,
            "max_wall_seconds": self.max_wall_seconds,
            "max_memory_mb": self.max_memory_mb,
            "seed": self.seed,
            "description": self.description,
            "schema_version": TRAINING_CONFIG_SCHEMA_VERSION,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_id": self.config_id,
            "config_hash": self.config_hash,
            **self.payload(),
            "simulated": True,
            "experimental": True,
            "advisory_only": True,
            "production_authority": False,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> TrainingConfig:
        if not isinstance(payload, Mapping):
            raise TrainingValidationError("training configuration must be an object")
        known = set(cls.__dataclass_fields__)
        ignored = {"config_id", "config_hash", "schema_version", "simulated", "experimental",
                   "advisory_only", "production_authority"}
        unknown = sorted(set(payload) - known - ignored)
        if unknown:
            raise TrainingValidationError(
                "unknown training configuration field(s): " + ", ".join(unknown)
            )
        values = {key: payload[key] for key in known if key in payload}
        if not str(values.get("name") or "").strip():
            raise TrainingValidationError("training configuration name is required")
        hidden = values.get("hidden_layers")
        if isinstance(hidden, (list, tuple)):
            values["hidden_layers"] = tuple(hidden)
        splits = values.get("splits")
        if isinstance(splits, Mapping):
            values["splits"] = {
                str(label): dict(row) if isinstance(row, Mapping) else row
                for label, row in splits.items()
            }
        return cls(**values)


@dataclass(frozen=True, slots=True)
class TrainingRun:
    run_id: str = field(default_factory=lambda: uuid4().hex)
    config_id: str = ""
    config_hash: str = ""
    name: str = ""
    algorithm: str = "dqn"
    status: str = TrainingRunStatus.CREATED.value
    current_episode: int = 0
    total_episodes: int = 0
    current_step: int = 0
    total_steps: int = 0
    metrics: Mapping[str, Any] = field(default_factory=dict)
    resource_usage: Mapping[str, Any] = field(default_factory=dict)
    checkpoint_ids: tuple[str, ...] = ()
    policy_id: str = ""
    seed: int = 0
    training_signature: str = ""
    error: str = ""
    stop_reason: str = ""
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    started_at: str = ""
    ended_at: str = ""
    updated_at: str = field(default_factory=lambda: utcnow().isoformat())

    @property
    def terminal(self) -> bool:
        return self.status in {
            TrainingRunStatus.COMPLETED.value,
            TrainingRunStatus.FAILED.value,
            TrainingRunStatus.CANCELLED.value,
        }

    def with_status(self, status: TrainingRunStatus | str, **updates: Any) -> TrainingRun:
        value = status.value if isinstance(status, TrainingRunStatus) else str(status)
        return replace(self, status=value, updated_at=utcnow().isoformat(), **updates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "config_id": self.config_id,
            "config_hash": self.config_hash,
            "name": self.name,
            "algorithm": self.algorithm,
            "status": self.status,
            "current_episode": self.current_episode,
            "total_episodes": self.total_episodes,
            "current_step": self.current_step,
            "total_steps": self.total_steps,
            "metrics": dict(self.metrics),
            "resource_usage": dict(self.resource_usage),
            "checkpoint_ids": list(self.checkpoint_ids),
            "policy_id": self.policy_id,
            "seed": self.seed,
            "training_signature": self.training_signature,
            "error": self.error,
            "stop_reason": self.stop_reason,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "updated_at": self.updated_at,
            "terminal": self.terminal,
            "simulated": True,
            "experimental": True,
            "advisory_only": True,
            "production_authority": False,
            "schema_version": TRAINING_RUN_SCHEMA_VERSION,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TrainingRun:
        return cls(
            run_id=_text(data.get("run_id"), uuid4().hex),
            config_id=_text(data.get("config_id")),
            config_hash=_text(data.get("config_hash")),
            name=_text(data.get("name")),
            algorithm=_text(data.get("algorithm"), "dqn"),
            status=_text(data.get("status"), TrainingRunStatus.CREATED.value),
            current_episode=max(0, _whole(data.get("current_episode", 0), "current_episode")),
            total_episodes=max(0, _whole(data.get("total_episodes", 0), "total_episodes")),
            current_step=max(0, _whole(data.get("current_step", 0), "current_step")),
            total_steps=max(0, _whole(data.get("total_steps", 0), "total_steps")),
            metrics=dict(data.get("metrics") or {}),
            resource_usage=dict(data.get("resource_usage") or {}),
            checkpoint_ids=tuple(str(item) for item in data.get("checkpoint_ids") or ()),
            policy_id=_text(data.get("policy_id")),
            seed=_whole(data.get("seed", 0), "seed"),
            training_signature=_text(data.get("training_signature")),
            error=_text(data.get("error")),
            stop_reason=_text(data.get("stop_reason")),
            created_at=_text(data.get("created_at"), utcnow().isoformat()),
            started_at=_text(data.get("started_at")),
            ended_at=_text(data.get("ended_at")),
            updated_at=_text(data.get("updated_at"), utcnow().isoformat()),
        )


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    checkpoint_id: str = field(default_factory=lambda: "rl_ckpt_" + uuid4().hex)
    run_id: str = ""
    path: str = ""
    kind: str = "periodic"
    step: int = 0
    episode: int = 0
    integrity_hash: str = ""
    size_bytes: int = 0
    status: str = "complete"
    created_at: str = field(default_factory=lambda: utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "run_id": self.run_id,
            "path": self.path,
            "kind": self.kind,
            "step": self.step,
            "episode": self.episode,
            "integrity_hash": self.integrity_hash,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "created_at": self.created_at,
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CheckpointRecord:
        return cls(
            checkpoint_id=_text(data.get("checkpoint_id")),
            run_id=_text(data.get("run_id")),
            path=_text(data.get("path")),
            kind=_text(data.get("kind"), "periodic"),
            step=max(0, _whole(data.get("step", 0), "step")),
            episode=max(0, _whole(data.get("episode", 0), "episode")),
            integrity_hash=_text(data.get("integrity_hash")),
            size_bytes=max(0, _whole(data.get("size_bytes", 0), "size_bytes")),
            status=_text(data.get("status"), "complete"),
            created_at=_text(data.get("created_at"), utcnow().isoformat()),
        )


@dataclass(frozen=True, slots=True)
class PolicyArtifact:
    policy_id: str = field(default_factory=lambda: "rl_policy_" + uuid4().hex)
    run_id: str = ""
    checkpoint_id: str = ""
    name: str = ""
    algorithm: str = "dqn"
    artifact_version: str = "1"
    status: str = PolicyStatus.CREATED.value
    artifact_path: str = ""
    artifact_hash: str = ""
    config_id: str = ""
    config_hash: str = ""
    seed: int = 0
    training_metrics: Mapping[str, Any] = field(default_factory=dict)
    validation_metrics: Mapping[str, Any] = field(default_factory=dict)
    test_metrics: Mapping[str, Any] = field(default_factory=dict)
    dataset_provenance: Mapping[str, Any] = field(default_factory=dict)
    environment_version: str = ""
    reward_version: str = ""
    creation_metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: utcnow().isoformat())

    def with_status(self, status: PolicyStatus | str, **updates: Any) -> PolicyArtifact:
        value = status.value if isinstance(status, PolicyStatus) else str(status)
        return replace(self, status=value, updated_at=utcnow().isoformat(), **updates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "run_id": self.run_id,
            "checkpoint_id": self.checkpoint_id,
            "name": self.name,
            "algorithm": self.algorithm,
            "artifact_version": self.artifact_version,
            "status": self.status,
            "artifact_path": self.artifact_path,
            "artifact_hash": self.artifact_hash,
            "config_id": self.config_id,
            "config_hash": self.config_hash,
            "seed": self.seed,
            "training_metrics": dict(self.training_metrics),
            "validation_metrics": dict(self.validation_metrics),
            "test_metrics": dict(self.test_metrics),
            "dataset_provenance": dict(self.dataset_provenance),
            "environment_version": self.environment_version,
            "reward_version": self.reward_version,
            "creation_metadata": dict(self.creation_metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "experimental": True,
            "advisory_only": True,
            "production_authority": False,
            "activation_available": False,
            "schema_version": POLICY_SCHEMA_VERSION,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PolicyArtifact:
        return cls(
            policy_id=_text(data.get("policy_id"), "rl_policy_" + uuid4().hex),
            run_id=_text(data.get("run_id")),
            checkpoint_id=_text(data.get("checkpoint_id")),
            name=_text(data.get("name")),
            algorithm=_text(data.get("algorithm"), "dqn"),
            artifact_version=_text(data.get("artifact_version"), "1"),
            status=_text(data.get("status"), PolicyStatus.CREATED.value),
            artifact_path=_text(data.get("artifact_path")),
            artifact_hash=_text(data.get("artifact_hash")),
            config_id=_text(data.get("config_id")),
            config_hash=_text(data.get("config_hash")),
            seed=_whole(data.get("seed", 0), "seed"),
            training_metrics=dict(data.get("training_metrics") or {}),
            validation_metrics=dict(data.get("validation_metrics") or {}),
            test_metrics=dict(data.get("test_metrics") or {}),
            dataset_provenance=dict(data.get("dataset_provenance") or {}),
            environment_version=_text(data.get("environment_version")),
            reward_version=_text(data.get("reward_version")),
            creation_metadata=dict(data.get("creation_metadata") or {}),
            created_at=_text(data.get("created_at"), utcnow().isoformat()),
            updated_at=_text(data.get("updated_at"), utcnow().isoformat()),
        )


@dataclass(frozen=True, slots=True)
class OptimizationTrial:
    trial_id: str = field(default_factory=lambda: "rl_trial_" + uuid4().hex)
    search_id: str = ""
    status: str = "created"
    config: Mapping[str, Any] = field(default_factory=dict)
    validation_score: float | None = None
    run_id: str = ""
    error: str = ""
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "search_id": self.search_id,
            "status": self.status,
            "config": dict(self.config),
            "validation_score": self.validation_score,
            "run_id": self.run_id,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OptimizationTrial:
        return cls(
            trial_id=_text(data.get("trial_id"), "rl_trial_" + uuid4().hex),
            search_id=_text(data.get("search_id")),
            status=_text(data.get("status"), "created"),
            config=dict(data.get("config") or {}),
            validation_score=(
                None
                if data.get("validation_score") is None
                else _finite(data.get("validation_score"), "validation_score")
            ),
            run_id=_text(data.get("run_id")),
            error=_text(data.get("error")),
            created_at=_text(data.get("created_at"), utcnow().isoformat()),
            updated_at=_text(data.get("updated_at"), utcnow().isoformat()),
        )


def validation_payload(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a configuration without starting a run."""
    try:
        parsed = TrainingConfig.from_mapping(config)
    except TrainingValidationError as exc:
        return {"valid": False, "errors": [str(exc)], "warnings": []}
    warnings: list[str] = []
    if parsed.max_steps < parsed.max_episodes * 2:
        warnings.append("max_steps is small relative to max_episodes")
    if parsed.gamma > 0.999:
        warnings.append("gamma is very close to 1; value estimates may be unstable")
    if parsed.epsilon_end == parsed.epsilon_start:
        warnings.append("epsilon never decays; exploration remains constant")
    return {
        "valid": True,
        "errors": [],
        "warnings": warnings,
        "config_id": parsed.config_id,
        "config_hash": parsed.config_hash,
        "config": parsed.to_dict(),
    }


def allowed_policy_transition(current: str, target: str) -> bool:
    return target in ALLOWED_POLICY_TRANSITIONS.get(current, frozenset())


def sequence_text(values: Sequence[str]) -> str:
    return ", ".join(str(value) for value in values) if values else "none"
