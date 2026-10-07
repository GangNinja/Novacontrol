"""The agentic RL trainer: the schedule around the loop, and an honest dry run.

Two pieces, and the boundary between them is the point, exactly as in Phase 18:

:class:`AgenticPolicyOptimizer` is the PLUG. Its interface is the only thing
NovaControl knows about how an agentic policy improves: hand it a batch of
EPISODES (with their rewards, dimensions, verification and credit) and a
configuration, get back a Phase 18 :class:`~novacontrol.rlhf.models.PolicyUpdate`
describing what changed. Today's only implementation,
:class:`MockAgenticPolicyOptimizer`, does NOT learn — it computes statistics over
the episodes and a coefficient-shaped delta so the pipeline around it can be
built, tested and dry-run. A future PPO/GRPO/actor-critic optimizer implements the
same one method, and the trainer, the registry, the evaluation and the API stay
untouched.

:class:`AgenticRLTrainer` is the SCHEDULE, and it extends Phase 16's
:class:`~novacontrol.training.backends.SFTTrainer` so the shared machinery —
run statuses, step accounting, checkpoints, pause/cancel, resume, the resource
estimate, the model registry — is reused rather than reimplemented. What it adds
is what an agentic run has and a supervised one does not: environments, the
curriculum, episodes, credit assignment and a policy evaluator.

Three refusals are deliberate and worded:

  * a real run without a wired optimizer that LEARNS is refused rather than
    silently downgraded to the mock;
  * a real run without the optional dependencies is refused by name;
  * and none of this starts by itself. :func:`run_agentic_dry_run` is the entry
    point a caller uses, and it simulates the entire pipeline — environment,
    state, policy, action, execution, verification, reward, credit assignment,
    transition, termination, evaluation, checkpoint configuration and model
    registration — while training nothing.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.agentic.config import (
    IMPLEMENTED_AGENTIC_ALGORITHMS,
    PLANNED_AGENTIC_ALGORITHMS,
    AgenticAlgorithm,
    AgenticRLConfig,
)
from novacontrol.agentic.curriculum import CurriculumManager
from novacontrol.agentic.environments import (
    ENVIRONMENT_LEVELS,
    AgenticEnvironment,
    build_environment,
)
from novacontrol.agentic.evaluation import (
    AgenticPolicyEvaluator,
    EvaluationConfig,
    EvaluationTask,
    baseline_table,
    default_tasks,
)
from novacontrol.agentic.models import (
    REWARD_DIMENSIONS,
    Episode,
    StepPenalty,
    as_mapping,
    as_text,
)
from novacontrol.agentic.policies import AgentPolicy, build_policy
from novacontrol.agentic.promotion import PolicyRegistry
from novacontrol.agentic.rewards import dimension_totals, episode_success
from novacontrol.agentic.rollout import AgenticRolloutManager, RolloutOutcome
from novacontrol.evaluation.models import now_iso
from novacontrol.rlhf.backends import PolicyOptimizerUnavailable
from novacontrol.rlhf.models import PolicyUpdate
from novacontrol.training.backends import (
    SFTTrainer,
    TrainingBackendUnavailable,
    TrainingCallbacks,
)
from novacontrol.training.hardware import HardwareCapabilities, detect_hardware
from novacontrol.training.models import (
    MAX_LOSS_POINTS,
    ResourceEstimate,
    TrainingRun,
    TrainingRunStatus,
)
from novacontrol.training.resources import ResourceEstimator

#: What each agentic optimizer is, written once. ``implemented`` is the honest
#: field: exactly one of these runs today, and it does not learn.
AGENTIC_OPTIMIZERS: Mapping[str, Mapping[str, Any]] = {
    AgenticAlgorithm.MOCK.value: {
        "algorithm": AgenticAlgorithm.MOCK.value,
        "name": "Mock agentic policy optimizer",
        "implemented": True,
        "learns": False,
        "description": (
            "a deterministic walk over episode rewards, credit and verification "
            "used for dry runs and tests: it produces a PolicyUpdate and writes "
            "measurements, but no weights change"
        ),
    },
    AgenticAlgorithm.PPO.value: {
        "algorithm": AgenticAlgorithm.PPO.value,
        "name": "Proximal Policy Optimization (agentic)",
        "implemented": False,
        "learns": True,
        "description": "planned: the interface accepts it, no implementation ships",
    },
    AgenticAlgorithm.GRPO.value: {
        "algorithm": AgenticAlgorithm.GRPO.value,
        "name": "Group Relative Policy Optimization (agentic)",
        "implemented": False,
        "learns": True,
        "description": "planned: the interface accepts it, no implementation ships",
    },
    AgenticAlgorithm.ACTOR_CRITIC.value: {
        "algorithm": AgenticAlgorithm.ACTOR_CRITIC.value,
        "name": "Actor-critic",
        "implemented": False,
        "learns": True,
        "description": "planned: the interface accepts it, no implementation ships",
    },
    AgenticAlgorithm.POLICY_GRADIENT.value: {
        "algorithm": AgenticAlgorithm.POLICY_GRADIENT.value,
        "name": "Policy gradient",
        "implemented": False,
        "learns": True,
        "description": "planned: the interface accepts it, no implementation ships",
    },
}

#: How many episodes a promotion gate's evaluation is expected to see by default.
DEFAULT_EVALUATION_TASKS = default_tasks()


def agentic_optimizer_for(algorithm: str) -> AgenticPolicyOptimizer:
    """The optimizer one algorithm asks for. One place decides this."""
    wanted = as_text(algorithm)
    if wanted == AgenticAlgorithm.MOCK.value:
        return MockAgenticPolicyOptimizer()
    if wanted in IMPLEMENTED_AGENTIC_ALGORITHMS:
        return MockAgenticPolicyOptimizer()
    if wanted in PLANNED_AGENTIC_ALGORITHMS:
        raise PolicyOptimizerUnavailable(
            f"algorithm={wanted!r} is not implemented in this phase: the agentic "
            "policy optimizer interface is ready for it, but the only optimizer that "
            f"runs today is {AgenticAlgorithm.MOCK.value!r}"
        )
    raise PolicyOptimizerUnavailable(
        f"unknown agentic algorithm {wanted!r}: expected "
        + ", ".join(sorted(AGENTIC_OPTIMIZERS))
    )


class AgenticPolicyOptimizer:
    """The plug a future agentic RL algorithm implements. One method, no library."""

    algorithm: str = AgenticAlgorithm.MOCK.value
    #: Whether this optimizer actually changes weights. The mock's is False and
    #: every record it produces says so.
    learns: bool = False

    @property
    def name(self) -> str:
        return self.algorithm

    def describe(self) -> dict[str, Any]:
        described = dict(AGENTIC_OPTIMIZERS.get(self.algorithm, {}))
        described["simulated"] = not self.learns
        return described

    def optimize(
        self,
        episodes: Sequence[Episode],
        *,
        config: AgenticRLConfig,
        epoch: int = 1,
        step: int = 1,
    ) -> PolicyUpdate:
        """One optimization pass over episodes. Subclasses implement this."""
        raise NotImplementedError


class MockAgenticPolicyOptimizer(AgenticPolicyOptimizer):
    """A deterministic stand-in that is loudly not training.

    It computes the statistics a real agentic optimizer would need — episode
    returns, terminal rewards, step counts, the credit spread, the verification
    rate, the safety total, the tool-correctness rate — and derives a
    coefficient-shaped ``policy_delta``. It never touches a model, never loads
    one, and marks the whole record ``simulated: True``.
    """

    algorithm = AgenticAlgorithm.MOCK.value
    learns = False

    def optimize(
        self,
        episodes: Sequence[Episode],
        *,
        config: AgenticRLConfig,
        epoch: int = 1,
        step: int = 1,
    ) -> PolicyUpdate:
        usable = [episode for episode in episodes if episode.terminal]
        if not usable:
            raise PolicyOptimizerUnavailable(
                "no TERMINAL episode was supplied, so there is nothing to optimize over: "
                "an unfinished episode has no outcome to learn from"
            )
        returns = [float(episode.total_reward) for episode in usable]
        rewards = [value for episode in usable for value in episode.step_rewards()]
        if not rewards:
            raise PolicyOptimizerUnavailable(
                "the supplied episodes contain no steps, so no reward can be read"
            )
        mean = math.fsum(returns) / len(returns)
        variance = math.fsum((value - mean) ** 2 for value in returns) / len(returns)
        std = float(variance**0.5)
        advantages = [value - mean for value in returns]
        verified = sum(1 for episode in usable for step_row in episode.steps if step_row.verified)
        steps_total = sum(episode.length for episode in usable)
        verification_rate = (verified / steps_total) if steps_total else 0.0
        safety_total = math.fsum(episode.safety_penalty for episode in usable)
        delta = mean * float(config.learning_rate) * max(1, len(usable)) ** 0.5
        return PolicyUpdate(
            algorithm=self.algorithm,
            steps=max(1, int(step)),
            examples=len(usable),
            epochs=max(0, int(epoch)),
            mean_reward=mean,
            reward_std=std,
            min_reward=min(returns),
            max_reward=max(returns),
            mean_advantage=0.0,
            advantage_std=std,
            policy_delta=delta,
            entropy=_spread(rewards),
            clip_fraction=round(
                sum(1 for value in advantages if abs(value) > 1.0) / len(advantages), 6
            ),
            simulated=True,
            notes=(
                "a mock agentic optimization pass: the numbers are statistics over "
                "the episode set, not a gradient step",
                f"verification pass rate over the batch: {verification_rate:.3f}",
                f"safety total over the batch: {safety_total:.3f} (reported separately "
                "from the returns)",
                f"{len(episodes) - len(usable)} episode(s) were not terminal and were "
                "left out",
            ),
        )


def _spread(values: Sequence[float]) -> float:
    """A bounded spread reading, not a policy entropy: honest by its label."""
    if not values:
        return 0.0
    shift = [value - min(values) for value in values]
    total = math.fsum(shift)
    if total <= 0:
        return 0.0
    shares = [value / total for value in shift if value > 0]
    return float(-math.fsum(share * math.log(share) for share in shares))


# ── checkpointing ────────────────────────────────────────────────────────────


def checkpoint_payload(
    config: AgenticRLConfig,
    *,
    policy_id: str = "",
    policy_version: str = "",
    model_id: str = "",
    training_run_id: str = "",
    environment_version: str = "",
    optimizer_metadata: Mapping[str, Any] | None = None,
    curriculum: CurriculumManager | None = None,
    step: int = 0,
    kind: str = "periodic",
) -> dict[str, Any]:
    """Everything a Phase 20 checkpoint must carry, with an integrity hash.

    The list is the specification's and it is stored as data, not as prose: the
    policy and model versions, the optimizer state where there is one, the
    training configuration, the reward configuration, the curriculum
    configuration and the environment version. A reader can restore the exact
    run from this record, and :func:`checkpoint_integrity_ok` can prove the
    record was not edited afterwards.
    """
    payload: dict[str, Any] = {
        "kind": kind,
        "step": int(step),
        "policy_id": policy_id or config.policy_id,
        "policy_version": policy_version or config.policy_id,
        "model_id": model_id,
        "training_run_id": training_run_id,
        "environment": config.environment,
        "environments": list(config.environment_list),
        "environment_version": environment_version or "phase20.1",
        "algorithm": config.algorithm,
        "simulated": config.is_simulated,
        "training_config": config.to_mapping(),
        "reward_config": config.reward.to_mapping(),
        "reward_version": config.reward_version,
        "curriculum_config": config.curriculum.to_mapping(),
        "curriculum_version": config.curriculum_version,
        "curriculum_state": curriculum.to_dict() if curriculum is not None else {},
        "exploration_config": config.exploration.to_mapping(),
        "promotion_thresholds": config.promotion.to_mapping(),
        "verifier_versions": dict(config.verifier_versions),
        "optimizer_state": dict(optimizer_metadata or {}),
        "fingerprint": config.fingerprint(),
        "created_at": now_iso(),
        "note": (
            "no model weights are stored: this phase's runs are simulated, and the "
            "checkpoint describes the run rather than an artefact"
        ),
    }
    payload["integrity"] = checkpoint_digest(payload)
    return payload


def checkpoint_digest(payload: Mapping[str, Any]) -> str:
    """A content hash over everything except the integrity field itself."""
    body = {key: value for key, value in as_mapping(payload).items() if key != "integrity"}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def checkpoint_integrity_ok(payload: Mapping[str, Any]) -> bool:
    """Whether a stored checkpoint still matches its own hash."""
    recorded = as_text(as_mapping(payload).get("integrity"))
    return bool(recorded) and recorded == checkpoint_digest(payload)


def rollback_target(
    current: Mapping[str, Any], previous: Mapping[str, Any]
) -> dict[str, Any]:
    """Which checkpoint to restore after a regression, and why.

    A rollback is a DECISION, not an action: this reports the target and the
    reason, and a caller (or a person) performs it.
    """
    current_version = as_text(as_mapping(current).get("policy_version"))
    previous_version = as_text(as_mapping(previous).get("policy_version"))
    return {
        "restore": previous_version or "",
        "from": current_version,
        "reason": (
            f"policy {current_version or 'unknown'} regressed; the previous "
            f"checkpoint {previous_version or 'unknown'} is the restore target"
        ),
        "current_integrity_ok": checkpoint_integrity_ok(current),
        "previous_integrity_ok": checkpoint_integrity_ok(previous),
    }


# ── resource governance ──────────────────────────────────────────────────────


class AgenticResourceEstimator:
    """Prices an agentic run on THIS machine, reusing the Phase 16 walk.

    The verdict vocabulary is Phase 16's (SAFE / WARNING / UNSAFE) and the
    refusal is the estimator's: an UNSAFE estimate does not start anything,
    because nothing in this module starts a run in the first place.
    """

    def __init__(
        self,
        estimator: ResourceEstimator | None = None,
        *,
        capabilities: HardwareCapabilities | None = None,
    ) -> None:
        base = estimator if estimator is not None else ResourceEstimator()
        if capabilities is not None:
            base = ResourceEstimator(
                hardware=capabilities,
                governor=getattr(base, "_governor", None),
                monitor=getattr(base, "_monitor", None),
                model_size_lookup=getattr(base, "_model_size_lookup", None),
            )
        self._estimator = base
        self._capabilities = capabilities

    def capabilities(self, *, probe_runtime: bool = False) -> HardwareCapabilities:
        if self._capabilities is not None and not probe_runtime:
            return self._capabilities
        return detect_hardware(probe_runtime=probe_runtime)

    def estimate(
        self, config: AgenticRLConfig, *, probe_runtime: bool = False
    ) -> ResourceEstimate:
        training = config.as_training_config()
        return self._estimator.estimate(
            training,
            example_count=max(1, config.episodes),
            estimated_tokens=max(1, config.episodes)
            * max(1, config.max_episode_steps)
            * max(1, config.max_sequence_length),
            probe_runtime=probe_runtime,
            extra_components=config.estimator_extras(),
            extra_reasons=config.estimate_reasons(),
        )

    def summary(self, config: AgenticRLConfig | None = None) -> dict[str, Any]:
        capabilities = self.capabilities()
        payload: dict[str, Any] = {
            "hardware": capabilities.to_dict(),
            "dependencies": {
                "training_ready": capabilities.training_dependencies_ready,
                "missing": list(capabilities.missing_dependencies()),
            },
            "cuda_required": False,
            "algorithms": {
                "implemented": list(IMPLEMENTED_AGENTIC_ALGORITHMS),
                "planned": list(PLANNED_AGENTIC_ALGORITHMS),
                "note": (
                    "only the mock agentic optimizer runs; the others are named so a "
                    "configuration can be priced and refused honestly"
                ),
            },
        }
        if config is not None:
            estimate = self.estimate(config)
            payload["estimate"] = estimate.to_dict()
            payload["verdict"] = estimate.level
            payload["allows_training"] = estimate.allows_training
            payload["override_required"] = estimate.override_required
        return payload


# ── the trainer ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AgenticTrainingSummary:
    """What a run did, in the numbers a report shows."""

    episodes: int = 0
    terminal_episodes: int = 0
    successes: int = 0
    failures: int = 0
    undecided: int = 0
    mean_reward: float = 0.0
    mean_steps: float = 0.0
    verification_pass_rate: float | None = None
    safety_total: float = 0.0
    curriculum_level: int = 0
    curriculum_action: str = ""
    updates: int = 0
    last_update: Mapping[str, Any] = field(default_factory=dict)
    manifest: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "episodes": self.episodes,
            "terminal_episodes": self.terminal_episodes,
            "successes": self.successes,
            "failures": self.failures,
            "undecided": self.undecided,
            "mean_reward": round(self.mean_reward, 6),
            "mean_steps": round(self.mean_steps, 6),
            "verification_pass_rate": self.verification_pass_rate,
            "safety_total": round(self.safety_total, 6),
            "curriculum_level": self.curriculum_level,
            "curriculum_action": self.curriculum_action,
            "updates": self.updates,
            "last_update": dict(self.last_update),
            "manifest": dict(self.manifest),
        }


class AgenticRLTrainer(SFTTrainer):
    """The schedule: environments, curriculum, episodes, optimization, evaluation."""

    mode = "agentic"
    backend = "agentic_rl"

    def __init__(
        self,
        config: AgenticRLConfig,
        *,
        estimator: ResourceEstimator | None = None,
        capabilities: HardwareCapabilities | None = None,
        optimizer: AgenticPolicyOptimizer | None = None,
        policy: AgentPolicy | None = None,
        manager: AgenticRolloutManager | None = None,
        curriculum: CurriculumManager | None = None,
        registry: PolicyRegistry | None = None,
        evaluator: AgenticPolicyEvaluator | None = None,
        environment_factory: Any = build_environment,
    ) -> None:
        super().__init__(config.as_training_config(), estimator=estimator)
        self.agentic_config = config
        self.capabilities = (
            capabilities if capabilities is not None else detect_hardware()
        )
        self.optimizer = (
            optimizer if optimizer is not None else agentic_optimizer_for(config.algorithm)
        )
        self.policy = policy if policy is not None else build_policy(config.base_policy)
        self.environment_factory = environment_factory
        self.curriculum = (
            curriculum
            if curriculum is not None
            else CurriculumManager(config.curriculum)
        )
        self.registry = registry if registry is not None else PolicyRegistry()
        self.resource_estimator = AgenticResourceEstimator(
            estimator=estimator, capabilities=self.capabilities
        )
        self.manager = (
            manager
            if manager is not None
            else AgenticRolloutManager.from_config(config, self.policy)
        )
        self.evaluator = (
            evaluator
            if evaluator is not None
            else AgenticPolicyEvaluator(config=EvaluationConfig(), rollout=self.manager)
        )
        self.episodes: list[Episode] = []
        self.summary = AgenticTrainingSummary()

    # -- interface --------------------------------------------------------------

    @property
    def name(self) -> str:
        return f"{self.mode}:{self.algorithm}"

    @property
    def algorithm(self) -> str:
        return self.optimizer.algorithm

    def missing_dependencies(self) -> tuple[str, ...]:
        """What a REAL agentic run would need. A simulated one needs nothing."""
        if self.agentic_config.dry_run or not self.optimizer.learns:
            return ()
        return self.capabilities.missing_dependencies()

    def is_available(self) -> bool:
        return not self.missing_dependencies()

    def validate_config(self) -> Any:
        """The configuration's own verdict, plus what the optimizer needs."""
        validation = self.agentic_config.validate()
        problems = list(validation.errors)
        if not self.optimizer.learns and self.agentic_config.dry_run:
            return _with_errors(validation, problems)
        return _with_errors(
            validation, [*problems, *self.missing_dependencies()]
        )

    def initialize_policy(self) -> dict[str, Any]:
        """What policy would run — described, never loaded."""
        return {
            "policy": self.policy.describe(),
            "policy_id": self.agentic_config.policy_id,
            "base_policy": self.agentic_config.base_policy,
            "algorithm": self.algorithm,
            "learns": self.optimizer.learns,
            "loaded": False,
            "note": (
                "no model is loaded by a dry run or by this description; the "
                "rule-based and mock policies are dependency-free, and a language-model "
                "adapter quotes a provider that was wired in explicitly"
            ),
        }

    def initialize_reward(self) -> dict[str, Any]:
        return {
            "dimensions": list(REWARD_DIMENSIONS),
            "weights": self.agentic_config.reward.to_mapping(),
            "credit_method": self.agentic_config.credit_method,
            "gamma": self.agentic_config.gamma,
            "reward_version": self.agentic_config.reward_version,
            "note": (
                "safety has its own dimension and weight and is reported separately "
                "from efficiency"
            ),
        }

    def initialize_environments(self) -> dict[str, Any]:
        environments = self.agentic_config.environment_list
        return {
            "environments": list(environments),
            "levels": {
                name: ENVIRONMENT_LEVELS.get(name, 0) for name in environments
            },
            "read_only": True,
            "deterministic": True,
            "note": (
                "every environment in this phase is in-memory and read-only; an "
                "environment that fronts something real goes through the permission "
                "layer like any other action"
            ),
        }

    def prepare_data(self, dataset: Any | None = None) -> dict[str, Any]:
        """What this run will do: the schedule, environments and curriculum."""
        config = self.agentic_config
        episodes = max(1, int(config.episodes))
        steps = max(1, math.ceil(episodes / max(1, config.batch_size)))
        return {
            "mode": self.mode,
            "algorithm": self.algorithm,
            "episodes": episodes,
            "batch_size": config.batch_size,
            "batches": steps,
            "max_episode_steps": config.max_episode_steps,
            "max_planning_horizon": config.max_planning_horizon,
            "max_retries": config.max_retries,
            "timeout_seconds": config.timeout_seconds,
            "curriculum": self.curriculum.summary(),
            "environments": self.initialize_environments(),
            "policy": self.initialize_policy(),
            "reward": self.initialize_reward(),
            "exploration": config.exploration.to_mapping(),
            "simulated": not self.optimizer.learns,
            "dry_run": config.dry_run,
            "dataset": as_text(
                dataset if isinstance(dataset, str) else "", "inline_episode_collection"
            ),
        }

    def estimate_resources(self, dataset: Any | None = None) -> ResourceEstimate:
        return self.resource_estimator.estimate(self.agentic_config)

    def evaluate(self, dataset: Any | None = None) -> dict[str, Any]:
        """Evaluate the configured policy, or the episodes already collected."""
        episodes = (
            tuple(dataset)
            if isinstance(dataset, (list, tuple)) and dataset and isinstance(dataset[0], Episode)
            else tuple(self.episodes)
        )
        if episodes:
            result = self.evaluator.summarise(self.policy, episodes)
        else:
            result = self.evaluator.evaluate(self.policy, default_tasks())
        # An evaluation belongs to a RECORD. A trainer that measured a policy
        # registers it as an EXPERIMENTAL candidate first, so the result lands on
        # the candidate instead of nowhere — still without promoting anything.
        if self.registry.get(self.agentic_config.policy_id) is None:
            self.register_candidate(notes=("registered by AgenticRLTrainer.evaluate()",))
        self.registry.record_evaluation(self.agentic_config.policy_id, result)
        return result.to_dict()

    def checkpoint(self, run: TrainingRun, callbacks: TrainingCallbacks, kind: str) -> None:
        callbacks.checkpoint(run, kind)

    def checkpoint_metadata(self, *, compact: bool = False) -> dict[str, Any]:
        """The metadata a checkpoint of this run must carry (Phase 20's list)."""
        payload = checkpoint_payload(
            self.agentic_config,
            environment_version=f"phase20.1/{self.agentic_config.environment}",
            optimizer_metadata=self.optimizer.describe(),
            curriculum=self.curriculum,
        )
        if compact:
            payload.pop("curriculum_state", None)
        return payload

    def cancel(self, run: TrainingRun) -> TrainingRun:
        return run.with_status(TrainingRunStatus.CANCELLED, end_time=now_iso())

    def model_metadata(self) -> dict[str, Any]:
        metadata = super().model_metadata()
        metadata.update(
            {
                "training_method": "dry_run",
                "agentic_mode": self.mode,
                "algorithm": self.algorithm,
                "simulated": not self.optimizer.learns,
                "policy_id": self.agentic_config.policy_id,
                "environment": self.agentic_config.environment,
                "curriculum_version": self.agentic_config.curriculum_version,
                "reward_version": self.agentic_config.reward_version,
                "credit_method": self.agentic_config.credit_method,
                "dry_run": self.agentic_config.dry_run,
                "output_directory": self.agentic_config.output_directory,
                "cuda_required": False,
            }
        )
        return metadata

    def register_candidate(
        self,
        *,
        training_run_id: str = "",
        notes: Sequence[str] = (),
    ) -> Any:
        """Register this run's policy in the registry as an EXPERIMENTAL candidate.

        Nothing is promoted here, and nothing can be: a fresh record is
        EXPERIMENTAL, whatever the run measured.
        """
        return self.registry.register_candidate(
            policy_id=self.agentic_config.policy_id,
            policy_version=f"{self.agentic_config.policy_id}@{self.agentic_config.fingerprint()}",
            model_id=self.policy.model_id,
            training_run_id=training_run_id,
            environment=self.agentic_config.environment,
            curriculum_version=self.agentic_config.curriculum_version,
            reward_version=self.agentic_config.reward_version,
            verifier_versions=dict(self.agentic_config.verifier_versions),
            learns=self.optimizer.learns,
            simulated=not self.optimizer.learns,
            notes=tuple(notes) or ("registered by AgenticRLTrainer",),
        )

    def comparisons(self, results: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The baseline table for a candidate this run produced."""
        return baseline_table(results)

    # -- the loop ---------------------------------------------------------------

    def _next_environment(self) -> tuple[str, dict[str, Any]]:
        """Which environment to collect in next, and the task to give it."""
        config = self.agentic_config
        if config.curriculum.enabled:
            name = self.curriculum.next_environment()
        else:
            names = config.environment_list
            if not names:
                raise KeyError("no environment is configured for this run")
            name = names[len(self.episodes) % len(names)]
        return name, self.task_for(name)

    def task_for(self, environment: str) -> dict[str, Any]:
        """The task mapping one environment is given (deterministic per level)."""
        if environment == "planning":
            return {"budget": min(6, max(2, self.agentic_config.max_planning_horizon // 2))}
        if environment == "recovery":
            return {"failures": 1}
        if environment == "tool_selection":
            return {"hint": "search the web for the answer", "correct_tool": "search_web"}
        if environment == "contextual":
            return {
                "expected_tool": "beta",
                "context": {"clue": "the beta path matches the goal"},
            }
        return {}

    def _walk(
        self,
        run: TrainingRun,
        callbacks: TrainingCallbacks,
        *,
        first_step: int = 0,
    ) -> TrainingRun:
        config = self.agentic_config
        prepared = self.prepare_data()
        total_steps = max(1, int(prepared["episodes"]))
        history = list(run.loss_history)
        current = run.with_status(
            TrainingRunStatus.RUNNING,
            start_time=run.start_time or now_iso(),
            total_steps=total_steps,
            current_epoch=1,
            current_step=max(0, first_step),
        )
        step = max(0, first_step)
        last_update: dict[str, Any] = {}
        curriculum_action = ""
        while step < total_steps:
            if callbacks.cancelled():
                return current.with_status(TrainingRunStatus.CANCELLED, end_time=now_iso())
            step += 1
            name, task = self._next_environment()
            environment: AgenticEnvironment = self.environment_factory(name)
            outcome: RolloutOutcome = self.manager.run_episode(
                environment, task=task, seed=config.seed + step
            )
            episode = outcome.episode
            self.episodes.append(episode)
            if config.curriculum.enabled:
                self.curriculum.record(episode)
            batch = tuple(self.episodes[-max(1, int(config.batch_size)) :])
            try:
                update = self.optimizer.optimize(batch, config=config, epoch=1, step=step)
            except PolicyOptimizerUnavailable as refusal:
                current = current.with_status(
                    TrainingRunStatus.RUNNING,
                    rl_metrics={
                        "mode": self.mode,
                        "algorithm": self.algorithm,
                        "simulated": True,
                        "episodes": len(self.episodes),
                        "note": str(refusal),
                    },
                )
                callbacks.step(current, index=step)
                continue
            last_update = update.to_dict()
            summary = self._summarise(self.episodes, curriculum_action)
            loss = round(1.0 / (1.0 + max(0.0, update.mean_reward)), 6)
            history.append(
                {
                    "epoch": 1,
                    "step": step,
                    "loss": loss,
                    "mean_reward": round(update.mean_reward, 6),
                }
            )
            if config.evaluation_frequency and step % config.evaluation_frequency == 0:
                decision = self.curriculum.evaluate() if config.curriculum.enabled else None
                curriculum_action = decision.action if decision is not None else ""
            current = current.with_status(
                TrainingRunStatus.RUNNING,
                current_step=step,
                training_loss=loss,
                loss_history=tuple(history[-MAX_LOSS_POINTS:]),
                rl_metrics={
                    "mode": self.mode,
                    "algorithm": self.algorithm,
                    "simulated": not self.optimizer.learns,
                    "episodes": summary.episodes,
                    "terminal_episodes": summary.terminal_episodes,
                    "successes": summary.successes,
                    "failures": summary.failures,
                    "undecided": summary.undecided,
                    "mean_reward": summary.mean_reward,
                    "mean_steps": summary.mean_steps,
                    "verification_pass_rate": summary.verification_pass_rate,
                    "safety_total": summary.safety_total,
                    "curriculum_level": summary.curriculum_level,
                    "curriculum_action": curriculum_action,
                    "environment": name,
                    "policy_update": update.to_dict(),
                    "note": (
                        "a dry run produces measurements over episodes, not a policy: "
                        "no weights change"
                    ),
                },
            )
            callbacks.step(current, index=step)
            if config.checkpoint_frequency and step % config.checkpoint_frequency == 0:
                callbacks.checkpoint(current, "periodic")
            if callbacks.paused():
                return current.with_status(TrainingRunStatus.PAUSED, end_time=now_iso())
            if callbacks.cancelled():
                return current.with_status(TrainingRunStatus.CANCELLED, end_time=now_iso())
        if not config.checkpoint_frequency:
            callbacks.checkpoint(current, "periodic")
        self.summary = self._summarise(self.episodes, curriculum_action)
        current = current.with_status(
            TrainingRunStatus.RUNNING,
            rl_metrics={
                **dict(current.rl_metrics),
                "policy_update": last_update,
                "summary": self.summary.to_dict(),
                "checkpoint": self.checkpoint_metadata(compact=True),
            },
        )
        callbacks.checkpoint(current, "best")
        return self.finalize(current)

    def _summarise(
        self, episodes: Sequence[Episode], curriculum_action: str = ""
    ) -> AgenticTrainingSummary:
        terminal = [row for row in episodes if row.terminal]
        successes = [row for row in episodes if episode_success(row) is True]
        failures = [row for row in episodes if episode_success(row) is False]
        undecided = [row for row in episodes if episode_success(row) is None]
        rewarded = [float(row.total_reward) for row in episodes]
        steps = [row.length for row in episodes]
        checked = [
            step
            for row in episodes
            for step in row.steps
            if as_text(step.verification.get("status")) != "skipped"
        ]
        passed = [step for step in checked if step.verified]
        return AgenticTrainingSummary(
            episodes=len(episodes),
            terminal_episodes=len(terminal),
            successes=len(successes),
            failures=len(failures),
            undecided=len(undecided),
            mean_reward=(math.fsum(rewarded) / len(rewarded)) if rewarded else 0.0,
            mean_steps=(math.fsum(steps) / len(steps)) if steps else 0.0,
            verification_pass_rate=(
                round(len(passed) / len(checked), 6) if checked else None
            ),
            safety_total=round(math.fsum(row.safety_penalty for row in episodes), 6),
            curriculum_level=self.curriculum.level,
            curriculum_action=curriculum_action,
            updates=len(episodes),
            last_update={},
            manifest={
                "dimension_totals": self._dimension_totals(episodes),
                "unnecessary_actions": sum(
                    1
                    for row in episodes
                    for step in row.steps
                    if step.reward is not None
                    and StepPenalty.UNNECESSARY_ACTION.value in step.reward.penalties
                ),
                "environments": sorted({row.environment_id for row in episodes}),
            },
        )

    @staticmethod
    def _dimension_totals(episodes: Sequence[Episode]) -> dict[str, float]:
        totals = dict.fromkeys(REWARD_DIMENSIONS, 0.0)
        for row in episodes:
            for name, value in dimension_totals(row).items():
                if name in totals:
                    totals[name] += float(value)
        return {name: round(value, 6) for name, value in totals.items()}

    def _run(
        self,
        run: TrainingRun,
        callbacks: TrainingCallbacks,
        *,
        first_step: int = 0,
    ) -> TrainingRun:
        config = self.agentic_config
        if config.dry_run:
            return self._walk(run, callbacks, first_step=first_step)
        missing = self.missing_dependencies()
        if missing:
            raise TrainingBackendUnavailable(
                "a real agentic run needs the optional training dependencies; missing: "
                + ", ".join(missing)
                + ". The environments, rollouts, rewards, credit assignment, "
                "curriculum, evaluation and dry run all work without them."
            )
        if not self.optimizer.learns:
            raise TrainingBackendUnavailable(
                f"algorithm={self.algorithm!r} is a simulated optimizer: it cannot run "
                "a real training pass. This phase ships the interface and the mock; a "
                "future phase adds the algorithm."
            )
        raise TrainingBackendUnavailable(
            "the agentic backend is available but no real training runner is wired: "
            "Phase 20 ships the pipeline, the environments, the reward architecture, "
            "the credit assignment, the curriculum, the evaluation, the promotion "
            "gates and the dry run. Wire a runner to train for real."
        )

    def start_training(
        self,
        run: TrainingRun,
        dataset: Any | None = None,
        callbacks: TrainingCallbacks | None = None,
    ) -> TrainingRun:
        return self._run(run, callbacks or TrainingCallbacks())

    def resume_training(
        self,
        run: TrainingRun,
        dataset: Any | None = None,
        checkpoint_step: int = 0,
        callbacks: TrainingCallbacks | None = None,
    ) -> TrainingRun:
        return self._run(
            run, callbacks or TrainingCallbacks(), first_step=max(0, int(checkpoint_step))
        )

    def train(
        self,
        run: TrainingRun,
        dataset: Any | None = None,
        callbacks: TrainingCallbacks | None = None,
    ) -> TrainingRun:
        return self.start_training(run, dataset, callbacks)


# ── the dry run ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DryRunReport:
    """The whole pipeline, simulated once, with every stage's result recorded."""

    stages: Mapping[str, Any] = field(default_factory=dict)
    config: Mapping[str, Any] = field(default_factory=dict)
    trained: bool = False
    model_loaded: bool = False
    notes: tuple[str, ...] = ()

    @property
    def ordered_stages(self) -> tuple[str, ...]:
        return tuple(self.stages)

    def stage(self, name: str) -> Any:
        return self.stages.get(name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stages": dict(self.stages),
            "stage_order": list(self.stages),
            "config": dict(self.config),
            "trained": self.trained,
            "model_loaded": self.model_loaded,
            "notes": list(self.notes),
        }


#: The stages the dry run walks, in order. The specification's list, verbatim.
DRY_RUN_STAGES: tuple[str, ...] = (
    "environment",
    "state",
    "policy",
    "action",
    "execution",
    "verification",
    "reward",
    "credit_assignment",
    "state_transition",
    "episode_termination",
    "evaluation",
    "checkpoint",
    "registration",
)


def run_agentic_dry_run(
    config: AgenticRLConfig | None = None,
    *,
    policy: AgentPolicy | None = None,
    registry: PolicyRegistry | None = None,
    tasks: Sequence[EvaluationTask] | None = None,
    manager: AgenticRolloutManager | None = None,
) -> DryRunReport:
    """Simulate the ENTIRE agentic RL pipeline and train nothing.

    One episode is collected for real (against a deterministic environment), and
    every stage the specification lists is exercised and recorded: the
    environment, the state, the policy, the action, its execution, the
    verification, the reward, the credit assignment, the state transition, the
    termination, an evaluation, the checkpoint configuration and the model
    registration. No weights change, no model is loaded, and nothing starts by
    itself — this is what a caller runs.
    """
    settings = config if config is not None else AgenticRLConfig()
    validation = settings.validate()
    if not validation.valid:
        raise ValueError(
            "this configuration cannot be dry-run: " + "; ".join(validation.errors)
        )
    chosen = policy if policy is not None else build_policy(settings.base_policy)
    runner = (
        manager
        if manager is not None
        else AgenticRolloutManager.from_config(settings, chosen)
    )
    trainer = AgenticRLTrainer(
        settings,
        policy=chosen,
        manager=runner,
        registry=registry if registry is not None else PolicyRegistry(),
    )
    environment_name = settings.environment_list[0]
    environment = build_environment(environment_name)
    task = trainer.task_for(environment_name)
    outcome = runner.run_episode(environment, task=task, seed=settings.seed)
    episode = outcome.episode
    evaluator = AgenticPolicyEvaluator(rollout=runner)
    evaluation = evaluator.summarise(chosen, (episode,))
    checkpoint = trainer.checkpoint_metadata()
    trainer.register_candidate(notes=("dry run",))
    trainer.registry.record_evaluation(settings.policy_id, evaluation)
    record = trainer.registry.get(settings.policy_id)
    first = episode.steps[0] if episode.steps else None
    stages: dict[str, Any] = {
        "environment": {
            **dict(environment.get_metadata()),
            "goal": environment.goal(),
            "task": dict(task),
        },
        "state": {
            "goal": episode.initial_state.goal,
            "tools": list(episode.initial_state.tools),
            "capabilities": list(episode.initial_state.capabilities),
            "step_index": episode.initial_state.step_index,
            "fingerprint": episode.initial_state.fingerprint(),
        },
        "policy": chosen.describe(),
        "action": first.action.as_mapping() if first is not None else {},
        "execution": {
            "executed": bool(first.executed) if first is not None else False,
            "result": dict(first.result) if first is not None else {},
            "observation": dict(first.observation) if first is not None else {},
        },
        "verification": {
            "status": first.verification_result if first is not None else "not_run",
            "summary": dict(episode.verification_summary),
        },
        "reward": {
            "step_rewards": [round(value, 6) for value in episode.step_rewards()],
            "dimension_totals": dict(episode.dimension_totals),
            "breakdown": {
                key: value
                for key, value in as_mapping(episode.reward_breakdown).items()
                if key in {"total_reward", "terminal_reward", "intermediate_reward", "shaped_share"}
            },
            "safety_total": round(episode.safety_penalty, 6),
        },
        "credit_assignment": {
            "method": as_text(as_mapping(episode.reward_breakdown).get("credit_method")),
            "credits": dict(episode.credits),
            "returns": dict(episode.returns),
        },
        "state_transition": {
            "steps": episode.length,
            "transitions": [
                {
                    "index": step.index,
                    "action": step.action.name,
                    "next_state_step": step.next_state.step_index,
                    "terminal": step.terminal,
                }
                for step in episode.steps
            ],
        },
        "episode_termination": {
            "termination_reason": episode.termination_reason,
            "success": episode.success,
            "detail": as_text(as_mapping(episode.metadata).get("termination_detail")),
        },
        "evaluation": evaluation.to_dict(),
        "checkpoint": {
            "integrity_ok": checkpoint_integrity_ok(checkpoint),
            "policy_version": checkpoint.get("policy_version"),
            "reward_version": checkpoint.get("reward_version"),
            "curriculum_version": checkpoint.get("curriculum_version"),
            "environment_version": checkpoint.get("environment_version"),
            "payload": checkpoint,
        },
        "registration": {
            "policy_id": settings.policy_id,
            "status": record.status if record is not None else "",
            "promoted": bool(record.live) if record is not None else False,
            "record": record.to_dict() if record is not None else {},
        },
    }
    return DryRunReport(
        stages=stages,
        config=settings.to_mapping(),
        trained=False,
        model_loaded=False,
        notes=(
            "no training happened and no model was loaded: every number here comes "
            "from one deterministic episode in an in-memory environment",
            "the registered policy is EXPERIMENTAL: a dry run cannot promote anything",
            *validation.warnings,
        ),
    )


def agentic_trainer_for(
    config: AgenticRLConfig,
    *,
    estimator: ResourceEstimator | None = None,
    capabilities: HardwareCapabilities | None = None,
    optimizer: AgenticPolicyOptimizer | None = None,
    policy: AgentPolicy | None = None,
    manager: AgenticRolloutManager | None = None,
    curriculum: CurriculumManager | None = None,
    registry: PolicyRegistry | None = None,
) -> AgenticRLTrainer:
    """The trainer one configuration asks for. One place decides this."""
    return AgenticRLTrainer(
        config,
        estimator=estimator,
        capabilities=capabilities,
        optimizer=optimizer,
        policy=policy,
        manager=manager,
        curriculum=curriculum,
        registry=registry,
    )


def optimizer_descriptions() -> dict[str, dict[str, Any]]:
    """What each agentic optimizer is, without building one."""
    return {name: dict(row) for name, row in AGENTIC_OPTIMIZERS.items()}


def _with_errors(validation: Any, problems: Sequence[str]) -> Any:
    """A validation result with extra errors appended (same type, same shape)."""
    if not problems:
        return validation
    return type(validation)(
        errors=tuple(validation.errors) + tuple(problems),
        warnings=tuple(validation.warnings),
    )


__all__ = [
    "AGENTIC_OPTIMIZERS",
    "DEFAULT_EVALUATION_TASKS",
    "DRY_RUN_STAGES",
    "AgenticPolicyOptimizer",
    "AgenticRLTrainer",
    "AgenticResourceEstimator",
    "AgenticTrainingSummary",
    "DryRunReport",
    "MockAgenticPolicyOptimizer",
    "agentic_optimizer_for",
    "agentic_trainer_for",
    "checkpoint_digest",
    "checkpoint_integrity_ok",
    "checkpoint_payload",
    "optimizer_descriptions",
    "rollback_target",
    "run_agentic_dry_run",
]
