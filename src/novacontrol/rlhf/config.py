"""Phase 18's two configurations: what a reward is worth, and what a run learns.

There is ONE place per question. :class:`RewardPolicyConfig` is where every
number that shapes a reward lives — which sources count, how much each is worth,
how the total is clipped and normalized, what counts as suspicious, and how a
human's button press becomes a number. It is versioned, so a stored dataset can
name the policy that produced it. :class:`RLTrainingConfig` is where a run's
parameters live, and it re-uses Phase 16's validator for every rule that already
exists (epochs, batch size, learning rate, LoRA, hardware policy) rather than
restating them.

Two boundaries are written down here instead of being left implicit:

``algorithm`` may name ``ppo`` or ``grpo`` — so a configuration can be priced and
planned — but a run that asks for one is refused, because this phase implements
the plug and the mock, not the algorithm. The refusal is a validation error with
the word "not implemented" in it, which is the honest answer.

``mode`` picks the feedback loop (RLHF or RLAIF). The pipeline is otherwise the
same; where they differ is which SOURCE the dataset must contain, and that is
checked against the built dataset rather than assumed from the mode alone.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from novacontrol.rlhf.models import (
    MODES,
    PLANNED_ALGORITHMS,
    POLICY_ALGORITHMS,
    RATING_MAX,
    RATING_MIN,
    REWARD_SOURCES,
    RLHF_VERSION,
    HumanFeedbackType,
    PolicyAlgorithm,
    RewardSource,
    RLMode,
)
from novacontrol.training.config import TrainingConfig, TrainingConfigValidation
from novacontrol.training.models import HardwarePolicy, TrainingMethod

#: The provider names a configuration may name for reward generation. ``auto``
#: means "whatever this dataset supports", and is resolved by the manager.
REWARD_PROVIDERS: tuple[str, ...] = (
    "auto",
    RewardSource.HUMAN.value,
    RewardSource.AI.value,
    RewardSource.VERIFIER.value,
    RewardSource.RULE.value,
    RewardSource.COMPOSITE.value,
)

#: The evaluator names a configuration may name. ``auto`` prefers a
#: deterministic evaluator whenever the task can be verified objectively, which
#: is the phase's stated preference; a human evaluator needs human feedback.
EVALUATORS: tuple[str, ...] = ("auto", "rule", "local", "external", "human", "none")


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _flag(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _whole(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _real(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _bounded(value: Any, default: float, *, low: float, high: float) -> float:
    number = _real(value, default)
    return max(low, min(high, number))


def _target_modules(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


#: Default worth of each source when several are composed. Human feedback is the
#: reference; an AI rating is real signal but not the same thing, and the rule
#: and verifier readings are precise about what they measured and silent about
#: everything else — so they count less on their own.
DEFAULT_PROVIDER_WEIGHTS: Mapping[str, float] = {
    RewardSource.HUMAN.value: 1.0,
    RewardSource.AI.value: 0.4,
    RewardSource.RULE.value: 0.6,
    RewardSource.VERIFIER.value: 0.8,
}

#: How each feedback type maps to a reward on a -1..1 scale. ``rating`` is 0 here
#: because a rating's value comes from the rating itself, not from the type.
DEFAULT_FEEDBACK_VALUES: Mapping[str, float] = {
    HumanFeedbackType.ACCEPT.value: 1.0,
    HumanFeedbackType.REJECT.value: -1.0,
    HumanFeedbackType.PREFER_A.value: 0.5,
    HumanFeedbackType.PREFER_B.value: -0.5,
    HumanFeedbackType.RATING.value: 0.0,
    HumanFeedbackType.CORRECTION.value: -0.3,
    HumanFeedbackType.REPORT_ERROR.value: -0.8,
    HumanFeedbackType.REPORT_UNSAFE.value: -1.0,
}


@dataclass(frozen=True, slots=True)
class RewardPolicyConfig:
    """Every number that decides what a reward is worth, in one place.

    The base components (task success, verification, tools, planning, safety,
    latency, resources) stay Phase 15's :class:`RewardConfig` — this policy says
    how the RL-specific sources (a person, an evaluator, a rule) enter the total,
    how the result is normalized and clipped, and at which point a reward stops
    being trustworthy.
    """

    version: str = RLHF_VERSION
    enabled: bool = True
    sources: tuple[str, ...] = REWARD_SOURCES
    provider_weights: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_PROVIDER_WEIGHTS)
    )
    feedback_values: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_FEEDBACK_VALUES)
    )
    #: Normalisation: the total scales into ``[clip_low, clip_high]`` around zero
    #: so a reward from a 5-point rating and one from a -3..3 engine are
    #: comparable as SIGNALS. The raw total is always kept beside it.
    normalize: bool = True
    clip_low: float = -1.0
    clip_high: float = 1.0
    #: Whether a safety penalty survives normalisation as its own component. It
    #: always does by default: "average behaviour was good but this run was
    #: unsafe" must remain readable, not be averaged away.
    keep_safety_separate: bool = True
    safety_floor: float = -1.0
    #: Below this confidence a signal is held rather than trusted.
    min_confidence: float = 0.25
    #: Integrity thresholds. ``suspicious_gap`` is how far two sources may read
    #: apart before their disagreement is flagged; ``max_reward_without_
    #: verification`` is the reward above which "nothing was verified" is a
    #: problem rather than a shrug.
    suspicious_gap: float = 0.5
    disagreement_tolerance: float = 0.25
    max_reward_without_verification: float = 0.9
    max_reward_when_failed: float = 0.0
    #: Response-length gaming: a high reward above this many tokens with no
    #: measured task gain is flagged, not silently accepted.
    length_gaming_tokens: int = 600
    #: Repeating the same action more than this many times while collecting
    #: reward is flagged as inflation.
    repeat_action_limit: int = 2
    #: Whether a dataset may contain rewards that failed the integrity check.
    require_integrity: bool = True
    #: Whether a suspicious reward may be taught as if it were settled.
    hold_suspicious: bool = True
    note: str = ""

    # -- reading ---------------------------------------------------------------

    def weight(self, source: str) -> float:
        name = _text(source).lower()
        if name in self.provider_weights:
            return float(self.provider_weights[name])
        return float(DEFAULT_PROVIDER_WEIGHTS.get(name, 0.0))

    def value(self, feedback_type: str) -> float:
        name = _text(feedback_type).lower()
        if name in self.feedback_values:
            return float(self.feedback_values[name])
        return float(DEFAULT_FEEDBACK_VALUES.get(name, 0.0))

    def allows(self, source: str) -> bool:
        return _text(source).lower() in {item.lower() for item in self.sources}

    def clip(self, value: float) -> float:
        low = min(self.clip_low, self.clip_high)
        high = max(self.clip_low, self.clip_high)
        return max(low, min(high, float(value)))

    def normalize_value(self, value: float, *, scale: float = 1.0) -> float:
        """The signal in ``[clip_low, clip_high]``; raw values stay beside it."""
        if not self.normalize:
            return float(value)
        base = scale if scale > 0 else 1.0
        return self.clip(float(value) / base)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "enabled": self.enabled,
            "sources": list(self.sources),
            "provider_weights": {
                name: self.weight(name) for name in REWARD_SOURCES
            },
            "feedback_values": {
                member.value: self.value(member.value) for member in HumanFeedbackType
            },
            "normalize": self.normalize,
            "clip_low": self.clip_low,
            "clip_high": self.clip_high,
            "keep_safety_separate": self.keep_safety_separate,
            "safety_floor": self.safety_floor,
            "min_confidence": self.min_confidence,
            "suspicious_gap": self.suspicious_gap,
            "disagreement_tolerance": self.disagreement_tolerance,
            "max_reward_without_verification": self.max_reward_without_verification,
            "max_reward_when_failed": self.max_reward_when_failed,
            "length_gaming_tokens": self.length_gaming_tokens,
            "repeat_action_limit": self.repeat_action_limit,
            "require_integrity": self.require_integrity,
            "hold_suspicious": self.hold_suspicious,
            "note": self.note,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> RewardPolicyConfig:
        defaults = cls()
        if not isinstance(data, Mapping):
            return defaults

        def weights(key: str, names: Sequence[str], base: Mapping[str, float]) -> dict[str, float]:
            merged = {name: float(base.get(name, 0.0)) for name in names}
            raw = data.get(key)
            if isinstance(raw, Mapping):
                for name in names:
                    value = raw.get(name)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        merged[name] = float(value)
            return merged

        sources = data.get("sources")
        chosen = (
            tuple(str(item).strip().lower() for item in sources if str(item).strip())
            if isinstance(sources, (list, tuple))
            else ()
        )
        return cls(
            version=_text(data.get("version"), defaults.version),
            enabled=_flag(data.get("enabled"), defaults.enabled),
            sources=tuple(item for item in chosen if item in REWARD_SOURCES) or defaults.sources,
            provider_weights=weights("provider_weights", REWARD_SOURCES, DEFAULT_PROVIDER_WEIGHTS),
            feedback_values=weights(
                "feedback_values",
                tuple(member.value for member in HumanFeedbackType),
                DEFAULT_FEEDBACK_VALUES,
            ),
            normalize=_flag(data.get("normalize"), defaults.normalize),
            clip_low=_bounded(data.get("clip_low"), defaults.clip_low, low=-1000.0, high=1000.0),
            clip_high=_bounded(
                data.get("clip_high"), defaults.clip_high, low=-1000.0, high=1000.0
            ),
            keep_safety_separate=_flag(
                data.get("keep_safety_separate"), defaults.keep_safety_separate
            ),
            safety_floor=_bounded(
                data.get("safety_floor"), defaults.safety_floor, low=-1000.0, high=0.0
            ),
            min_confidence=_bounded(
                data.get("min_confidence"), defaults.min_confidence, low=0.0, high=1.0
            ),
            suspicious_gap=_bounded(
                data.get("suspicious_gap"), defaults.suspicious_gap, low=0.0, high=10.0
            ),
            disagreement_tolerance=_bounded(
                data.get("disagreement_tolerance"),
                defaults.disagreement_tolerance,
                low=0.0,
                high=10.0,
            ),
            max_reward_without_verification=_bounded(
                data.get("max_reward_without_verification"),
                defaults.max_reward_without_verification,
                low=-1000.0,
                high=1000.0,
            ),
            max_reward_when_failed=_bounded(
                data.get("max_reward_when_failed"),
                defaults.max_reward_when_failed,
                low=-1000.0,
                high=1000.0,
            ),
            length_gaming_tokens=max(
                1, _whole(data.get("length_gaming_tokens"), defaults.length_gaming_tokens)
            ),
            repeat_action_limit=max(
                0, _whole(data.get("repeat_action_limit"), defaults.repeat_action_limit)
            ),
            require_integrity=_flag(data.get("require_integrity"), defaults.require_integrity),
            hold_suspicious=_flag(data.get("hold_suspicious"), defaults.hold_suspicious),
            note=_text(data.get("note")),
        )

    def fingerprint(self) -> str:
        payload = json.dumps(self.to_mapping(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class RLTrainingConfig:
    """One RLHF/RLAIF run's parameters, validated in one place.

    ``dry_run`` defaults to True and the algorithm defaults to the mock: nothing
    starts because a configuration exists, and the default configuration can be
    walked end to end on a machine with no RL dependencies at all.
    """

    mode: str = RLMode.RLHF.value
    algorithm: str = PolicyAlgorithm.MOCK.value
    base_model: str = ""
    policy_model: str = ""
    reference_model: str = ""
    reward_dataset_version: str = ""
    reward_provider: str = "auto"
    reward_config: Mapping[str, Any] = field(default_factory=dict)
    evaluator: str = "auto"
    environment: str = "mock"
    output_directory: str = ""
    epochs: int = 1
    batch_size: int = 1
    gradient_accumulation_steps: int = 8
    learning_rate: float = 1e-5
    rollout_count: int = 4
    max_steps: int = 8
    gamma: float = 1.0
    kl_coefficient: float = 0.0
    clip_range: float = 0.2
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
    def effective_policy(self) -> str:
        return self.policy_model or self.base_model

    @property
    def effective_method(self) -> str:
        if not self.use_lora:
            return TrainingMethod.FULL.value
        return self.training_method

    @property
    def needs_reference_model(self) -> bool:
        """Whether a KL term keeps a frozen policy in memory (RLHF's usual case)."""
        return self.kl_coefficient > 0.0

    @property
    def requires_human_feedback(self) -> bool:
        return self.mode == RLMode.RLHF.value

    @property
    def requires_ai_feedback(self) -> bool:
        return self.mode == RLMode.RLAIF.value

    @property
    def reward_policy(self) -> RewardPolicyConfig:
        return RewardPolicyConfig.from_mapping(self.reward_config)

    def validate(self) -> TrainingConfigValidation:
        shared = self.as_training_config().validate()
        errors = list(shared.errors)
        warnings = list(shared.warnings)

        if self.mode not in MODES:
            errors.append(
                f"mode must be one of {', '.join(MODES)} (got {self.mode!r})"
            )
        if self.algorithm not in POLICY_ALGORITHMS:
            errors.append(
                f"algorithm must be one of {', '.join(POLICY_ALGORITHMS)} "
                f"(got {self.algorithm!r})"
            )
        elif self.algorithm in PLANNED_ALGORITHMS:
            errors.append(
                f"algorithm {self.algorithm!r} is not implemented in this phase: "
                "the policy-optimizer interface is ready for it, but the only "
                "optimizer that runs today is "
                f"{PolicyAlgorithm.MOCK.value!r} (a deterministic, dependency-free "
                "walk used for dry runs and tests)"
            )
        if not self.reward_dataset_version:
            errors.append("reward_dataset_version is required (name@version)")
        if self.reward_provider not in REWARD_PROVIDERS:
            errors.append(
                f"reward_provider must be one of {', '.join(REWARD_PROVIDERS)} "
                f"(got {self.reward_provider!r})"
            )
        if self.evaluator not in EVALUATORS:
            errors.append(
                f"evaluator must be one of {', '.join(EVALUATORS)} (got {self.evaluator!r})"
            )
        if not 1 <= self.rollout_count <= 4096:
            errors.append(
                f"rollout_count must be between 1 and 4096 (got {self.rollout_count})"
            )
        if not 1 <= self.max_steps <= 4096:
            errors.append(f"max_steps must be between 1 and 4096 (got {self.max_steps})")
        if not 0.0 <= self.gamma <= 1.0:
            errors.append(f"gamma must be between 0 and 1 (got {self.gamma})")
        if not 0.0 <= self.kl_coefficient <= 10.0:
            errors.append(
                f"kl_coefficient must be between 0 and 10 (got {self.kl_coefficient})"
            )
        if not 0.0 <= self.clip_range <= 1.0:
            errors.append(f"clip_range must be between 0 and 1 (got {self.clip_range})")
        if self.algorithm == PolicyAlgorithm.MOCK.value:
            warnings.append(
                "algorithm 'mock_policy' is a deterministic simulation of a "
                "policy-optimization pass, not real reinforcement learning: a run "
                "built on it produces measurements, not a policy"
            )
        if not self.dry_run:
            warnings.append(
                "dry_run is off: a real RL run needs the optional training "
                "dependencies installed, a wired policy optimizer and this "
                "installation's explicit permission"
            )
        if self.needs_reference_model and not self.reference_model:
            warnings.append(
                "kl_coefficient is set but no reference_model is named, so the "
                "base model is used as the frozen reference"
            )
        if self.reference_model and not self.needs_reference_model:
            warnings.append(
                "reference_model is ignored: kl_coefficient is 0, so nothing "
                "pulls the policy toward it"
            )
        if self.mode == RLMode.RLAIF.value and self.evaluator == "human":
            warnings.append(
                "mode=rlaif with a human evaluator: the reward will come from "
                "people, which is RLHF's loop, not RLAIF's"
            )
        return TrainingConfigValidation(errors=tuple(errors), warnings=tuple(warnings))

    # -- reuse ------------------------------------------------------------------

    def as_training_config(self) -> TrainingConfig:
        """The same run in the Phase 16 shape the shared machinery reads."""
        return TrainingConfig(
            base_model=self.base_model,
            dataset_version=self.reward_dataset_version,
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
    ) -> RLTrainingConfig:
        return cls(
            base_model=config.base_model,
            reward_dataset_version=config.dataset_version,
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
            "mode": self.mode,
            "algorithm": self.algorithm,
            "base_model": self.base_model,
            "policy_model": self.policy_model,
            "reference_model": self.reference_model,
            "reward_dataset_version": self.reward_dataset_version,
            "reward_provider": self.reward_provider,
            "reward_config": self.reward_policy.to_mapping(),
            "evaluator": self.evaluator,
            "environment": self.environment,
            "output_directory": self.output_directory,
            "epochs": self.epochs,
            "batch_size": self.batch_size,
            "gradient_accumulation_steps": self.gradient_accumulation_steps,
            "learning_rate": self.learning_rate,
            "rollout_count": self.rollout_count,
            "max_steps": self.max_steps,
            "gamma": self.gamma,
            "kl_coefficient": self.kl_coefficient,
            "clip_range": self.clip_range,
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
    def from_mapping(cls, data: Mapping[str, Any] | None) -> RLTrainingConfig:
        defaults = cls()
        if not isinstance(data, Mapping):
            return defaults
        reward_config = data.get("reward_config")
        return cls(
            mode=_text(data.get("mode"), defaults.mode),
            algorithm=_text(data.get("algorithm"), defaults.algorithm),
            base_model=_text(data.get("base_model")),
            policy_model=_text(data.get("policy_model")),
            reference_model=_text(data.get("reference_model")),
            reward_dataset_version=_text(
                data.get("reward_dataset_version"), _text(data.get("dataset_version"))
            ),
            reward_provider=_text(data.get("reward_provider"), defaults.reward_provider),
            reward_config=dict(reward_config) if isinstance(reward_config, Mapping) else {},
            evaluator=_text(data.get("evaluator"), defaults.evaluator),
            environment=_text(data.get("environment"), defaults.environment),
            output_directory=_text(data.get("output_directory")),
            epochs=_whole(data.get("epochs"), defaults.epochs),
            batch_size=_whole(data.get("batch_size"), defaults.batch_size),
            gradient_accumulation_steps=_whole(
                data.get("gradient_accumulation_steps"),
                defaults.gradient_accumulation_steps,
            ),
            learning_rate=_real(data.get("learning_rate"), defaults.learning_rate),
            rollout_count=_whole(data.get("rollout_count"), defaults.rollout_count),
            max_steps=_whole(data.get("max_steps"), defaults.max_steps),
            gamma=_bounded(data.get("gamma"), defaults.gamma, low=0.0, high=1.0),
            kl_coefficient=_real(data.get("kl_coefficient"), defaults.kl_coefficient),
            clip_range=_bounded(
                data.get("clip_range"), defaults.clip_range, low=0.0, high=1.0
            ),
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

    def with_defaults(self, *, output_directory: str = "") -> RLTrainingConfig:
        updates: dict[str, Any] = {}
        if not self.output_directory and output_directory:
            updates["output_directory"] = output_directory
        return replace(self, **updates) if updates else self

    def fingerprint(self) -> str:
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
    "DEFAULT_FEEDBACK_VALUES",
    "DEFAULT_PROVIDER_WEIGHTS",
    "EVALUATORS",
    "RATING_MAX",
    "RATING_MIN",
    "REWARD_PROVIDERS",
    "RLTrainingConfig",
    "RewardPolicyConfig",
    "resolve_sequence",
]
