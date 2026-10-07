"""Phase 20's configuration: every threshold named once, and checked honestly.

The shape follows Phase 18's :class:`~novacontrol.rlhf.config.RLTrainingConfig`
on purpose: a frozen record with defaults, ``validate()`` that returns errors and
warnings rather than raising, a mapping round-trip so a run can be stored and read
back, and a ``fingerprint()`` so two runs can be told apart by their settings.

Three things are deliberately NOT configurable into danger:

  * an algorithm that is not implemented is an ERROR, not a warning — a run that
    asks for PPO gets refused rather than silently downgraded to the mock;
  * ``dry_run`` is ON by default, and turning it off is a WARNING that says what
    a real run needs, because the default machine has no CUDA and 16 GB of RAM;
  * every bounded loop has a ceiling and the validator refuses values above it,
    so no configuration can construct an episode that cannot terminate.

Nothing in this module starts anything. A configuration is a description.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from novacontrol.agentic.models import (
    CREDIT_METHODS,
    EXPLORATION_STRATEGIES,
    MAX_CURRICULUM_LEVEL,
    MAX_EPISODE_STEPS_CEILING,
    MAX_PLANNING_HORIZON_CEILING,
    MAX_RETRIES_CEILING,
    MIN_CURRICULUM_LEVEL,
    REWARD_DIMENSIONS,
    CreditMethod,
    ExplorationStrategy,
    as_flag,
    as_int,
    as_mapping,
    as_real,
    as_text,
    as_texts,
)
from novacontrol.training.config import TrainingConfig

AGENTIC_CONFIG_VERSION = "phase20.1"


class AgenticAlgorithm(StrEnum):
    """The policy optimizers this phase knows about.

    ``mock_agentic_policy`` is the only one implemented: a deterministic walk
    over episode rewards and credit that produces a :class:`PolicyUpdate` and
    changes no weights. PPO, GRPO and the actor-critic/policy-gradient family are
    in the vocabulary so a configuration can NAME what it intends and be priced,
    validated and refused honestly — never silently substituted.
    """

    MOCK = "mock_agentic_policy"
    PPO = "ppo"
    GRPO = "grpo"
    ACTOR_CRITIC = "actor_critic"
    POLICY_GRADIENT = "policy_gradient"


AGENTIC_ALGORITHMS: tuple[str, ...] = tuple(member.value for member in AgenticAlgorithm)
IMPLEMENTED_AGENTIC_ALGORITHMS: tuple[str, ...] = (AgenticAlgorithm.MOCK.value,)
PLANNED_AGENTIC_ALGORITHMS: tuple[str, ...] = (
    AgenticAlgorithm.PPO.value,
    AgenticAlgorithm.GRPO.value,
    AgenticAlgorithm.ACTOR_CRITIC.value,
    AgenticAlgorithm.POLICY_GRADIENT.value,
)

#: Ceilings the validator enforces, named once.
MAX_EPISODE_STEPS = MAX_EPISODE_STEPS_CEILING
MAX_PLANNING_HORIZON = MAX_PLANNING_HORIZON_CEILING
MAX_RETRIES = MAX_RETRIES_CEILING
MAX_EPISODES = 4096
MAX_EPISODES_PER_EVALUATION = 2048


@dataclass(frozen=True, slots=True)
class ExplorationConfig:
    """How far exploration may go, and what makes it stop."""

    strategy: str = ExplorationStrategy.GREEDY.value
    epsilon: float = 0.1
    temperature: float = 1.0
    #: A hard cap on exploration actions in one run. Reaching it ends
    #: exploration, not the episode.
    max_exploration_actions: int = 64
    #: The largest share of steps that may be exploration (0..1).
    max_exploration_rate: float = 0.25
    #: How many exploration actions may fail before exploration is suspended.
    max_failed_exploration: int = 8
    #: How many safety interventions stop exploration outright.
    max_safety_interventions: int = 1
    #: A cost in reward units charged to exploration actions, so the budget is
    #: visible in the numbers rather than only in a counter.
    cost_per_action: float = 0.02
    #: A ceiling on the reward exploration may be charged, so a long run cannot
    #: accumulate an unbounded exploratory debt.
    max_cost: float = 2.0
    #: Whether exploration is allowed at all. Off means every decision is greedy.
    enabled: bool = False
    #: Never explore into a destructive or external-side-effect action. This is
    #: not configurable to True: exploration by doing harm is not a feature.
    allow_unsafe: bool = False
    seed: int = 0

    def to_mapping(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "epsilon": self.epsilon,
            "temperature": self.temperature,
            "max_exploration_actions": self.max_exploration_actions,
            "max_exploration_rate": self.max_exploration_rate,
            "max_failed_exploration": self.max_failed_exploration,
            "max_safety_interventions": self.max_safety_interventions,
            "cost_per_action": self.cost_per_action,
            "max_cost": self.max_cost,
            "enabled": self.enabled,
            "allow_unsafe": self.allow_unsafe,
            "seed": self.seed,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> ExplorationConfig:
        rows = as_mapping(data)
        strategy = as_text(rows.get("strategy"), ExplorationStrategy.GREEDY.value)
        if strategy not in EXPLORATION_STRATEGIES:
            strategy = ExplorationStrategy.GREEDY.value
        return cls(
            strategy=strategy,
            epsilon=as_real(rows.get("epsilon"), 0.1) or 0.0,
            temperature=as_real(rows.get("temperature"), 1.0) or 1.0,
            max_exploration_actions=max(0, as_int(rows.get("max_exploration_actions"), 64)),
            max_exploration_rate=as_real(rows.get("max_exploration_rate"), 0.25) or 0.0,
            max_failed_exploration=max(0, as_int(rows.get("max_failed_exploration"), 8)),
            max_safety_interventions=max(0, as_int(rows.get("max_safety_interventions"), 1)),
            cost_per_action=as_real(rows.get("cost_per_action"), 0.02) or 0.0,
            max_cost=as_real(rows.get("max_cost"), 2.0) or 0.0,
            enabled=bool(as_flag(rows.get("enabled"), False)),
            # A stored row that claims unsafe exploration is ON is read as OFF:
            # the permission is not something a file may grant.
            allow_unsafe=False,
            seed=as_int(rows.get("seed")),
        )


@dataclass(frozen=True, slots=True)
class CurriculumConfig:
    """When the curriculum advances, and when it steps back."""

    enabled: bool = True
    start_level: int = MIN_CURRICULUM_LEVEL
    max_level: int = MAX_CURRICULUM_LEVEL
    #: Episodes that must be recorded at a level before it can be judged.
    min_episodes_per_level: int = 8
    #: Success rate needed to advance.
    min_success_rate: float = 0.8
    #: Failure rate that sends the curriculum BACK a level (regression guard).
    max_failure_rate: float = 0.4
    #: Whether a level already passed may be re-entered when the current one is
    #: failing. On by default: the point of a curriculum is to keep the agent
    #: inside its competence, not to march forward regardless.
    allow_regression: bool = True
    #: How many consecutive failing evaluations trigger a regression.
    regression_patience: int = 2
    version: str = "phase20.1"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "start_level": self.start_level,
            "max_level": self.max_level,
            "min_episodes_per_level": self.min_episodes_per_level,
            "min_success_rate": self.min_success_rate,
            "max_failure_rate": self.max_failure_rate,
            "allow_regression": self.allow_regression,
            "regression_patience": self.regression_patience,
            "version": self.version,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> CurriculumConfig:
        rows = as_mapping(data)
        return cls(
            enabled=bool(as_flag(rows.get("enabled"), True)),
            start_level=max(
                MIN_CURRICULUM_LEVEL, as_int(rows.get("start_level"), MIN_CURRICULUM_LEVEL)
            ),
            max_level=min(
                MAX_CURRICULUM_LEVEL, as_int(rows.get("max_level"), MAX_CURRICULUM_LEVEL)
            ),
            min_episodes_per_level=max(1, as_int(rows.get("min_episodes_per_level"), 8)),
            min_success_rate=as_real(rows.get("min_success_rate"), 0.8) or 0.0,
            max_failure_rate=as_real(rows.get("max_failure_rate"), 0.4) or 0.0,
            allow_regression=bool(as_flag(rows.get("allow_regression"), True)),
            regression_patience=max(1, as_int(rows.get("regression_patience"), 2)),
            version=as_text(rows.get("version"), "phase20.1"),
        )


@dataclass(frozen=True, slots=True)
class AgenticRewardWeights:
    """The weight of every reward dimension, in ONE table.

    Safety has its own weight, and there is no configuration in which safety is
    folded into efficiency: the two are reported separately everywhere, so a
    policy that trades one for the other is visible rather than averaged away.
    """

    # Dimension weights.
    task_success: float = 1.0
    verification: float = 0.5
    safety: float = 2.0
    efficiency: float = 0.2
    latency: float = 0.1
    resource_usage: float = 0.1
    tool_correctness: float = 0.3
    planning_efficiency: float = 0.2
    recovery_quality: float = 0.3
    # Step signals.
    correct_tool_reward: float = 0.15
    valid_action_reward: float = 0.05
    subtask_reward: float = 0.2
    verification_passed_reward: float = 0.3
    information_reward: float = 0.1
    efficient_action_reward: float = 0.05
    # Penalties.
    unnecessary_action_penalty: float = 0.1
    failed_action_penalty: float = 0.3
    incorrect_tool_penalty: float = 0.25
    invalid_arguments_penalty: float = 0.4
    repeated_failure_penalty: float = 0.5
    latency_penalty: float = 0.2
    resource_penalty: float = 0.2
    unsafe_penalty: float = 5.0
    unnecessary_recovery_penalty: float = 0.4
    # Terminal reward.
    terminal_success: float = 1.0
    terminal_failure: float = -1.0
    terminal_verification: float = 0.5
    # Thresholds the reward layer measures against.
    latency_budget_ms: float = 2000.0
    resource_budget: float = 1.0
    #: A policy must not be able to farm step rewards into a success.
    max_step_reward_share: float = 0.5

    def weight_for(self, dimension: str) -> float:
        return float(getattr(self, dimension, 0.0)) if dimension in REWARD_DIMENSIONS else 0.0

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_success": self.task_success,
            "verification": self.verification,
            "safety": self.safety,
            "efficiency": self.efficiency,
            "latency": self.latency,
            "resource_usage": self.resource_usage,
            "tool_correctness": self.tool_correctness,
            "planning_efficiency": self.planning_efficiency,
            "recovery_quality": self.recovery_quality,
            "correct_tool_reward": self.correct_tool_reward,
            "valid_action_reward": self.valid_action_reward,
            "subtask_reward": self.subtask_reward,
            "verification_passed_reward": self.verification_passed_reward,
            "information_reward": self.information_reward,
            "efficient_action_reward": self.efficient_action_reward,
            "unnecessary_action_penalty": self.unnecessary_action_penalty,
            "failed_action_penalty": self.failed_action_penalty,
            "incorrect_tool_penalty": self.incorrect_tool_penalty,
            "invalid_arguments_penalty": self.invalid_arguments_penalty,
            "repeated_failure_penalty": self.repeated_failure_penalty,
            "latency_penalty": self.latency_penalty,
            "resource_penalty": self.resource_penalty,
            "unsafe_penalty": self.unsafe_penalty,
            "unnecessary_recovery_penalty": self.unnecessary_recovery_penalty,
            "terminal_success": self.terminal_success,
            "terminal_failure": self.terminal_failure,
            "terminal_verification": self.terminal_verification,
            "latency_budget_ms": self.latency_budget_ms,
            "resource_budget": self.resource_budget,
            "max_step_reward_share": self.max_step_reward_share,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> AgenticRewardWeights:
        rows = as_mapping(data)
        base = cls()
        values: dict[str, float] = {}
        for name in base.to_mapping():
            values[name] = as_real(rows.get(name), float(getattr(base, name))) or 0.0
        return cls(**values)

    def fingerprint(self) -> str:
        canonical = json.dumps(self.to_mapping(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class PromotionThresholds:
    """What a candidate policy must clear before a person may promote it.

    Every threshold is a number a caller sets. There is deliberately no
    hard-coded universal "better": a candidate is compared against ITS baseline
    on the metrics this installation cares about, and the decision is recorded
    with the numbers that produced it.
    """

    min_task_success: float = 0.8
    min_verification_success: float = 0.8
    min_safety_rate: float = 1.0
    max_latency_ms: float = 5000.0
    max_resource_units: float = 1.0
    max_success_regression: float = 0.02
    max_safety_regression: float = 0.0
    max_latency_regression: float = 0.25
    min_sample_size: int = 8
    #: A promotion always needs a person's explicit approval, even when every
    #: numeric gate passes. This field exists so a report can state it; setting
    #: it False still requires an approval because the gate ignores it.
    require_approval: bool = True

    def to_mapping(self) -> dict[str, Any]:
        return {
            "min_task_success": self.min_task_success,
            "min_verification_success": self.min_verification_success,
            "min_safety_rate": self.min_safety_rate,
            "max_latency_ms": self.max_latency_ms,
            "max_resource_units": self.max_resource_units,
            "max_success_regression": self.max_success_regression,
            "max_safety_regression": self.max_safety_regression,
            "max_latency_regression": self.max_latency_regression,
            "min_sample_size": self.min_sample_size,
            "require_approval": self.require_approval,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> PromotionThresholds:
        rows = as_mapping(data)
        base = cls()
        values: dict[str, Any] = {}
        for name in base.to_mapping():
            default = getattr(base, name)
            if isinstance(default, bool):
                values[name] = bool(as_flag(rows.get(name), default))
            elif isinstance(default, int):
                values[name] = as_int(rows.get(name), default)
            else:
                values[name] = as_real(rows.get(name), float(default)) or 0.0
        return cls(**values)


@dataclass(frozen=True, slots=True)
class AgenticConfigValidation:
    """The verdict of :meth:`AgenticRLConfig.validate`."""

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
class AgenticRLConfig:
    """One agentic-RL run's settings, and the loops' explicit bounds."""

    policy_id: str = "agentic-candidate"
    base_policy: str = "rule_based"
    environment: str = "tool_selection"
    environments: tuple[str, ...] = ()
    algorithm: str = AgenticAlgorithm.MOCK.value
    dry_run: bool = True
    output_directory: str = "training_output/agentic"
    seed: int = 0
    gamma: float = 0.99
    # -- bounded loops ---------------------------------------------------------
    max_episode_steps: int = 16
    max_planning_horizon: int = 8
    max_retries: int = 2
    timeout_seconds: float = 60.0
    resource_budget: float = 1.0
    # -- schedules -------------------------------------------------------------
    episodes: int = 16
    episodes_per_evaluation: int = 8
    evaluation_frequency: int = 8
    checkpoint_frequency: int = 8
    max_checkpoints: int = 3
    resume_from_checkpoint: str = ""
    # -- the Phase 16 projection reads these -----------------------------------
    epochs: int = 1
    batch_size: int = 4
    gradient_accumulation_steps: int = 1
    learning_rate: float = 2e-4
    max_sequence_length: int = 512
    precision: str = "fp32"
    hardware_policy: str = "auto"
    # -- sub-configurations ----------------------------------------------------
    exploration: ExplorationConfig = field(default_factory=ExplorationConfig)
    curriculum: CurriculumConfig = field(default_factory=CurriculumConfig)
    reward: AgenticRewardWeights = field(default_factory=AgenticRewardWeights)
    promotion: PromotionThresholds = field(default_factory=PromotionThresholds)
    credit_method: str = CreditMethod.DISCOUNTED_RETURN.value
    # -- provenance ------------------------------------------------------------
    reward_version: str = "phase15.1"
    verifier_versions: Mapping[str, str] = field(default_factory=dict)
    curriculum_version: str = "phase20.1"
    notes: tuple[str, ...] = ()

    # -- derived ---------------------------------------------------------------

    @property
    def environment_list(self) -> tuple[str, ...]:
        """Every environment this run uses, the single one included."""
        if self.environments:
            return tuple(self.environments)
        return (self.environment,) if self.environment else ()

    @property
    def is_simulated(self) -> bool:
        """Whether the configured algorithm is one this build actually runs."""
        return self.algorithm in IMPLEMENTED_AGENTIC_ALGORITHMS

    @property
    def explores(self) -> bool:
        return self.exploration.enabled and self.exploration.strategy != ExplorationStrategy.GREEDY.value

    # -- validation ------------------------------------------------------------

    def validate(self) -> AgenticConfigValidation:
        errors: list[str] = []
        warnings: list[str] = []
        if not self.policy_id.strip():
            errors.append("a policy id is required so the run can be tracked")
        if self.algorithm not in AGENTIC_ALGORITHMS:
            errors.append(
                f"unknown algorithm {self.algorithm!r}: expected "
                + ", ".join(AGENTIC_ALGORITHMS)
            )
        elif self.algorithm in PLANNED_AGENTIC_ALGORITHMS:
            errors.append(
                f"algorithm={self.algorithm!r} is not implemented in this phase: the "
                "optimizer interface is ready for it, but the only optimizer that "
                f"runs today is {AgenticAlgorithm.MOCK.value!r}"
            )
        if not self.environment_list:
            errors.append("at least one environment must be named")
        if not 1 <= self.max_episode_steps <= MAX_EPISODE_STEPS:
            errors.append(
                f"max_episode_steps must be between 1 and {MAX_EPISODE_STEPS}; "
                "an episode without a step ceiling cannot terminate"
            )
        if not 1 <= self.max_planning_horizon <= MAX_PLANNING_HORIZON:
            errors.append(
                f"max_planning_horizon must be between 1 and {MAX_PLANNING_HORIZON}"
            )
        if not 0 <= self.max_retries <= MAX_RETRIES:
            errors.append(f"max_retries must be between 0 and {MAX_RETRIES}")
        if not 0.0 <= self.gamma <= 1.0:
            errors.append("gamma must be between 0 and 1")
        if self.timeout_seconds <= 0:
            errors.append("timeout_seconds must be positive; every episode needs a clock")
        if self.resource_budget <= 0:
            errors.append("resource_budget must be positive")
        if not 1 <= self.episodes <= MAX_EPISODES:
            errors.append(f"episodes must be between 1 and {MAX_EPISODES}")
        if not 1 <= self.episodes_per_evaluation <= MAX_EPISODES_PER_EVALUATION:
            errors.append(
                "episodes_per_evaluation must be between 1 and "
                f"{MAX_EPISODES_PER_EVALUATION}"
            )
        if self.learning_rate <= 0:
            errors.append("learning_rate must be positive")
        if self.batch_size < 1:
            errors.append("batch_size must be at least 1")
        if self.gradient_accumulation_steps < 1:
            errors.append("gradient_accumulation_steps must be at least 1")
        if self.credit_method not in CREDIT_METHODS:
            errors.append(
                f"unknown credit method {self.credit_method!r}: expected "
                + ", ".join(CREDIT_METHODS)
            )
        if not 0.0 <= self.exploration.epsilon <= 1.0:
            errors.append("exploration epsilon must be between 0 and 1")
        if self.exploration.temperature <= 0:
            errors.append("exploration temperature must be positive")
        if not 0.0 <= self.exploration.max_exploration_rate <= 1.0:
            errors.append("exploration max_exploration_rate must be between 0 and 1")
        if self.exploration.allow_unsafe:
            errors.append(
                "exploration.allow_unsafe is not a supported setting: exploring by "
                "performing unsafe actions is not a feature of this phase"
            )
        level = self.curriculum
        if not MIN_CURRICULUM_LEVEL <= level.start_level <= level.max_level <= MAX_CURRICULUM_LEVEL:
            errors.append(
                "curriculum levels must satisfy "
                f"{MIN_CURRICULUM_LEVEL} <= start_level <= max_level <= {MAX_CURRICULUM_LEVEL}"
            )
        if not 0.0 <= level.min_success_rate <= 1.0:
            errors.append("curriculum min_success_rate must be between 0 and 1")
        if not 0.0 <= level.max_failure_rate <= 1.0:
            errors.append("curriculum max_failure_rate must be between 0 and 1")
        promotion = self.promotion
        if promotion.min_sample_size < 1:
            errors.append("promotion min_sample_size must be at least 1")
        for name in (
            "min_task_success",
            "min_verification_success",
            "min_safety_rate",
        ):
            value = float(getattr(promotion, name))
            if not 0.0 <= value <= 1.0:
                errors.append(f"promotion {name} must be between 0 and 1")
        if promotion.max_latency_ms <= 0:
            errors.append("promotion max_latency_ms must be positive")
        if self.reward.max_step_reward_share >= 1.0:
            errors.append(
                "reward max_step_reward_share must stay below 1 so step rewards "
                "cannot out-weigh the outcome"
            )
        # Warnings: things a person should read, not things that break a run.
        if not self.dry_run:
            warnings.append(
                "dry_run is off: a real agentic run needs the optional training "
                "dependencies, a wired optimizer that actually learns, and this "
                "installation's permission. Everything else in this phase — the "
                "environments, rollouts, rewards, credit assignment, curriculum and "
                "evaluation — runs without them."
            )
        if self.is_simulated and not self.dry_run:
            warnings.append(
                f"algorithm={self.algorithm!r} is simulated: it produces "
                "measurements and a policy update record, not a trained policy"
            )
        if self.explores:
            warnings.append(
                "exploration is enabled: it stays inside the action mask, never "
                "explores into an action the permission layer refuses, and stops "
                "when its budget is spent or a safety intervention happens"
            )
        if self.curriculum.enabled and self.curriculum.min_success_rate >= 1.0:
            warnings.append(
                "curriculum min_success_rate is 1.0: the curriculum will not advance "
                "until every episode at a level succeeds"
            )
        return AgenticConfigValidation(errors=tuple(errors), warnings=tuple(warnings))

    # -- serialisation ---------------------------------------------------------

    def to_mapping(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "base_policy": self.base_policy,
            "environment": self.environment,
            "environments": list(self.environments),
            "algorithm": self.algorithm,
            "dry_run": self.dry_run,
            "output_directory": self.output_directory,
            "seed": self.seed,
            "gamma": self.gamma,
            "max_episode_steps": self.max_episode_steps,
            "max_planning_horizon": self.max_planning_horizon,
            "max_retries": self.max_retries,
            "timeout_seconds": self.timeout_seconds,
            "resource_budget": self.resource_budget,
            "episodes": self.episodes,
            "episodes_per_evaluation": self.episodes_per_evaluation,
            "evaluation_frequency": self.evaluation_frequency,
            "checkpoint_frequency": self.checkpoint_frequency,
            "max_checkpoints": self.max_checkpoints,
            "resume_from_checkpoint": self.resume_from_checkpoint,
            "epochs": self.epochs,
            "batch_size": self.batch_size,
            "gradient_accumulation_steps": self.gradient_accumulation_steps,
            "learning_rate": self.learning_rate,
            "max_sequence_length": self.max_sequence_length,
            "precision": self.precision,
            "hardware_policy": self.hardware_policy,
            "exploration": self.exploration.to_mapping(),
            "curriculum": self.curriculum.to_mapping(),
            "reward": self.reward.to_mapping(),
            "promotion": self.promotion.to_mapping(),
            "credit_method": self.credit_method,
            "reward_version": self.reward_version,
            "verifier_versions": dict(self.verifier_versions),
            "curriculum_version": self.curriculum_version,
            "notes": list(self.notes),
            "schema_version": AGENTIC_CONFIG_VERSION,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> AgenticRLConfig:
        rows = as_mapping(data)
        base = cls()
        algorithm = as_text(rows.get("algorithm"), base.algorithm)
        credit = as_text(rows.get("credit_method"), base.credit_method)
        if credit not in CREDIT_METHODS:
            credit = base.credit_method
        return cls(
            policy_id=as_text(rows.get("policy_id"), base.policy_id),
            base_policy=as_text(rows.get("base_policy"), base.base_policy),
            environment=as_text(rows.get("environment"), base.environment),
            environments=as_texts(rows.get("environments")),
            algorithm=algorithm,
            dry_run=bool(as_flag(rows.get("dry_run"), base.dry_run)),
            output_directory=as_text(rows.get("output_directory"), base.output_directory),
            seed=as_int(rows.get("seed"), base.seed),
            gamma=as_real(rows.get("gamma"), base.gamma) or 0.0,
            max_episode_steps=max(1, as_int(rows.get("max_episode_steps"), base.max_episode_steps)),
            max_planning_horizon=max(
                1, as_int(rows.get("max_planning_horizon"), base.max_planning_horizon)
            ),
            max_retries=max(0, as_int(rows.get("max_retries"), base.max_retries)),
            timeout_seconds=as_real(rows.get("timeout_seconds"), base.timeout_seconds) or 0.0,
            resource_budget=as_real(rows.get("resource_budget"), base.resource_budget) or 0.0,
            episodes=max(1, as_int(rows.get("episodes"), base.episodes)),
            episodes_per_evaluation=max(
                1, as_int(rows.get("episodes_per_evaluation"), base.episodes_per_evaluation)
            ),
            evaluation_frequency=max(
                0, as_int(rows.get("evaluation_frequency"), base.evaluation_frequency)
            ),
            checkpoint_frequency=max(
                0, as_int(rows.get("checkpoint_frequency"), base.checkpoint_frequency)
            ),
            max_checkpoints=max(1, as_int(rows.get("max_checkpoints"), base.max_checkpoints)),
            resume_from_checkpoint=as_text(rows.get("resume_from_checkpoint")),
            epochs=max(1, as_int(rows.get("epochs"), base.epochs)),
            batch_size=max(1, as_int(rows.get("batch_size"), base.batch_size)),
            gradient_accumulation_steps=max(
                1, as_int(rows.get("gradient_accumulation_steps"), base.gradient_accumulation_steps)
            ),
            learning_rate=as_real(rows.get("learning_rate"), base.learning_rate) or 0.0,
            max_sequence_length=max(
                1, as_int(rows.get("max_sequence_length"), base.max_sequence_length)
            ),
            precision=as_text(rows.get("precision"), base.precision),
            hardware_policy=as_text(rows.get("hardware_policy"), base.hardware_policy),
            exploration=ExplorationConfig.from_mapping(rows.get("exploration")),
            curriculum=CurriculumConfig.from_mapping(rows.get("curriculum")),
            reward=AgenticRewardWeights.from_mapping(rows.get("reward")),
            promotion=PromotionThresholds.from_mapping(rows.get("promotion")),
            credit_method=credit,
            reward_version=as_text(rows.get("reward_version"), base.reward_version),
            verifier_versions={
                str(key): str(value)
                for key, value in as_mapping(rows.get("verifier_versions")).items()
            },
            curriculum_version=as_text(
                rows.get("curriculum_version"), base.curriculum_version
            ),
            notes=as_texts(rows.get("notes")),
        )

    def with_defaults(self, **updates: Any) -> AgenticRLConfig:
        """A copy with named fields replaced (the caller always wins)."""
        return replace(self, **updates)

    def fingerprint(self) -> str:
        canonical = json.dumps(self.to_mapping(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def output_path(self) -> Path:
        return Path(self.output_directory)

    def as_training_config(self) -> TrainingConfig:
        """The same run in the Phase 16 shape the shared machinery reads.

        Reused rather than reimplemented: the resource estimator, the checkpoint
        model and the run record all speak :class:`TrainingConfig`, so this is
        the one projection that lets an agentic run use them unchanged.
        """
        return TrainingConfig(
            base_model="",
            dataset_type="agentic_episodes",
            dataset_version=f"agentic:{self.environment}",
            output_directory=self.output_directory,
            epochs=self.epochs,
            batch_size=self.batch_size,
            gradient_accumulation_steps=self.gradient_accumulation_steps,
            learning_rate=self.learning_rate,
            max_sequence_length=self.max_sequence_length,
            evaluation_frequency=self.evaluation_frequency,
            checkpoint_frequency=self.checkpoint_frequency,
            seed=self.seed,
            precision=self.precision,
            max_checkpoints=self.max_checkpoints,
            resume_from_checkpoint=self.resume_from_checkpoint,
            use_lora=False,
            hardware_policy=self.hardware_policy,
            dry_run=self.dry_run,
            training_method="dry_run",
            notes="; ".join(self.notes),
        )

    def estimator_extras(self) -> dict[str, int]:
        """The components an agentic run adds to a resource estimate."""
        per_episode = max(1, self.max_episode_steps)
        episodes = max(1, self.episodes)
        return {
            "episode_buffer": int(
                episodes * per_episode * max(1, self.max_sequence_length) * 4
            ),
            "curriculum_state": 4096,
            "policy_registry": 65536,
        }

    def estimate_reasons(self) -> tuple[str, ...]:
        return (
            f"{max(1, self.episodes)} episode(s) × {max(1, self.max_episode_steps)} step(s) "
            f"× {max(1, self.max_sequence_length)} token(s) of retained observation at 4 bytes each",
            "the curriculum state and the policy registry are counted as fixed small components",
            f"algorithm={self.algorithm!r} ({'simulated' if self.is_simulated else 'not implemented'})",
        )


def resolve_environment(value: Any) -> tuple[str, ...]:
    """Read an environment setting that may be one name or a list of them."""
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    return as_texts(value)


def sequences_equal(left: Sequence[Any], right: Sequence[Any]) -> bool:
    return tuple(left) == tuple(right)


__all__ = [
    "AGENTIC_ALGORITHMS",
    "AGENTIC_CONFIG_VERSION",
    "IMPLEMENTED_AGENTIC_ALGORITHMS",
    "MAX_EPISODES",
    "MAX_EPISODES_PER_EVALUATION",
    "MAX_EPISODE_STEPS",
    "MAX_PLANNING_HORIZON",
    "MAX_RETRIES",
    "PLANNED_AGENTIC_ALGORITHMS",
    "AgenticAlgorithm",
    "AgenticConfigValidation",
    "AgenticRLConfig",
    "AgenticRewardWeights",
    "CurriculumConfig",
    "ExplorationConfig",
    "PromotionThresholds",
    "resolve_environment",
]
