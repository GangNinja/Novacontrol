"""The training side of Phase 18: a policy optimizer plug, and honest trainers.

Two pieces, and the boundary between them is the point of the phase.

:class:`PolicyOptimizer` is the PLUG. Its interface is the only thing NovaControl
knows about how a policy improves: hand it reward-labelled examples and a
configuration, get back a :class:`PolicyUpdate` describing what changed. Today's
only implementation, :class:`MockPolicyOptimizer`, does NOT learn: it computes
reward statistics and a coefficient-shaped delta so the pipeline around it can be
built, tested and dry-run. A future PPO or GRPO optimizer implements the same
two methods, and every other module — the trainer, the manager, the registry,
the API — stays untouched.

:class:`RLTrainer` is the SCHEDULE. It prepares data, prices the run, walks a
deterministic simulated loop in dry-run mode, writes reward statistics onto the
run's ``rl_metrics``, checkpoints, and refuses a real run unless the optional
dependencies are present AND a concrete runner is wired. That refusal is
deliberate and worded: a phase that has built the rails must not pretend it has
built the engine.

RLHF and RLAIF differ in exactly one place — which feedback source the data must
contain and which mode the run records — and that lives in the two subclasses
rather than in a branch anywhere else.
"""

from __future__ import annotations

import abc
import hashlib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.evaluation.models import now_iso
from novacontrol.rlhf.config import RLTrainingConfig
from novacontrol.rlhf.datasets import RewardDatasetBuilder
from novacontrol.rlhf.evaluators import Evaluator, evaluator_for
from novacontrol.rlhf.models import (
    IMPLEMENTED_ALGORITHMS,
    MODES,
    PLANNED_ALGORITHMS,
    PolicyAlgorithm,
    PolicyUpdate,
    RewardDatasetVersion,
    RewardExample,
    RewardSource,
    RLMode,
)
from novacontrol.rlhf.resources import RLResourceEstimator
from novacontrol.rlhf.rewards import (
    CompositeRewardProvider,
    RewardProvider,
    default_composite,
)
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

#: What each policy optimizer is, written once. ``implemented`` is the honest
#: field: exactly one of these runs today.
POLICY_OPTIMIZERS: Mapping[str, Mapping[str, Any]] = {
    PolicyAlgorithm.MOCK.value: {
        "algorithm": PolicyAlgorithm.MOCK.value,
        "name": "Mock policy optimizer",
        "implemented": True,
        "learns": False,
        "description": (
            "a deterministic walk over reward statistics used for dry runs and "
            "tests: it produces a PolicyUpdate and writes measurements, but no "
            "weights change"
        ),
    },
    PolicyAlgorithm.PPO.value: {
        "algorithm": PolicyAlgorithm.PPO.value,
        "name": "Proximal Policy Optimization",
        "implemented": False,
        "learns": True,
        "description": (
            "planned: the interface accepts it and a configuration may name it, "
            "but no PPO implementation ships in this phase"
        ),
    },
    PolicyAlgorithm.GRPO.value: {
        "algorithm": PolicyAlgorithm.GRPO.value,
        "name": "Group Relative Policy Optimization",
        "implemented": False,
        "learns": True,
        "description": (
            "planned: the interface accepts it and a configuration may name it, "
            "but no GRPO implementation ships in this phase"
        ),
    },
}


def optimizer_for(algorithm: str) -> dict[str, Any]:
    """The documented description of one algorithm (empty for an unknown name)."""
    description = POLICY_OPTIMIZERS.get(str(algorithm))
    return dict(description) if description else {}


class PolicyOptimizerUnavailable(RuntimeError):
    """An optimizer that cannot run says so rather than acting like one that can."""


class PolicyOptimizer(abc.ABC):
    """The plug a future RL algorithm implements. Two methods, no library."""

    algorithm: str = PolicyAlgorithm.MOCK.value
    #: Whether this optimizer actually changes weights. The mock's is False and
    #: every record it produces says so.
    learns: bool = False

    @property
    def name(self) -> str:
        return self.algorithm

    def describe(self) -> dict[str, Any]:
        described = optimizer_for(self.algorithm)
        described["simulated"] = not self.learns
        return described

    @abc.abstractmethod
    def optimize(
        self,
        examples: Sequence[RewardExample],
        *,
        config: RLTrainingConfig,
        epoch: int = 1,
        step: int = 1,
    ) -> PolicyUpdate:
        """One optimization pass over reward-labelled examples."""


class MockPolicyOptimizer(PolicyOptimizer):
    """A deterministic stand-in that is loudly not training.

    It computes the reward statistics a real optimizer would need, derives an
    advantage-style reading and a coefficient-shaped ``policy_delta``, and marks
    the whole record ``simulated: True``. It never touches a model, never loads
    one, and cannot be mistaken for learning — which is exactly what makes it
    safe to run on every machine in this build.
    """

    algorithm = PolicyAlgorithm.MOCK.value
    learns = False

    def optimize(
        self,
        examples: Sequence[RewardExample],
        *,
        config: RLTrainingConfig,
        epoch: int = 1,
        step: int = 1,
    ) -> PolicyUpdate:
        usable = [row for row in examples if row.accepted]
        rewards = [row.reward.total_reward for row in usable]
        if not rewards:
            raise PolicyOptimizerUnavailable(
                "no accepted reward examples were supplied, so there is nothing "
                "to optimize over"
            )
        mean = sum(rewards) / len(rewards)
        variance = sum((value - mean) ** 2 for value in rewards) / len(rewards)
        std = float(variance**0.5)
        advantages = [value - mean for value in rewards]
        clip = max(1e-9, float(config.clip_range))
        clipped = sum(1 for value in advantages if abs(value) > clip) / len(advantages)
        delta = mean * float(config.learning_rate) * max(1, len(rewards)) ** 0.5
        return PolicyUpdate(
            algorithm=self.algorithm,
            steps=max(1, int(step)),
            examples=len(usable),
            epochs=max(0, int(epoch)),
            mean_reward=mean,
            reward_std=std,
            min_reward=min(rewards),
            max_reward=max(rewards),
            mean_advantage=mean - mean,
            advantage_std=std,
            policy_delta=delta,
            entropy=_entropy(rewards),
            clip_fraction=clipped,
            simulated=True,
            notes=(
                "a mock optimization pass: the numbers are statistics over the "
                "reward set, not a gradient step",
                f"{len(examples) - len(usable)} example(s) were not accepted and "
                "were left out",
            ),
        )


def _entropy(values: Sequence[float]) -> float:
    """A bounded spread reading, not a policy entropy: honest by its label."""
    if not values:
        return 0.0
    shift = [value - min(values) for value in values]
    total = sum(shift)
    if total <= 0:
        return 0.0
    shares = [value / total for value in shift if value > 0]
    return float(-sum(share * math.log(share) for share in shares))


#: A concrete training loop a deployment can wire in for a real run. It receives
#: the run, the dataset and the callbacks, and returns the finished run.
RLRunner = Callable[[TrainingRun, RewardDatasetVersion, TrainingCallbacks], TrainingRun]


@dataclass(frozen=True, slots=True)
class RewardAudit:
    """The reward set one run learns from, audited before anything runs."""

    examples: int = 0
    accepted: int = 0
    trusted: int = 0
    flagged: int = 0
    #: Rows whose signal includes what a person said, and rows whose signal
    #: includes an evaluator's rating. Counted separately from ``by_source``,
    #: because a COMPOSITE reward carries both and must satisfy either loop.
    human_backed: int = 0
    ai_backed: int = 0
    by_source: Mapping[str, int] = field(default_factory=dict)
    mode_ok: bool = True
    problems: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "examples": self.examples,
            "accepted": self.accepted,
            "trusted": self.trusted,
            "flagged": self.flagged,
            "human_backed": self.human_backed,
            "ai_backed": self.ai_backed,
            "by_source": dict(self.by_source),
            "mode_ok": self.mode_ok,
            "problems": list(self.problems),
        }


class RLTrainer(SFTTrainer):
    """The shared implementation every RL mode inherits.

    The constructor takes the RL configuration and hands its Phase 16 projection
    to the parent, so the inherited helpers — LoRA metadata, config
    re-validation, the estimate walk — read the fields they always read.
    """

    #: The feedback loop this trainer implements; subclasses name it.
    mode = RLMode.RLHF.value
    #: A stored run carries this name.
    backend = "rl"

    def __init__(
        self,
        config: RLTrainingConfig,
        *,
        estimator: ResourceEstimator | None = None,
        capabilities: HardwareCapabilities | None = None,
        runner: RLRunner | None = None,
        optimizer: PolicyOptimizer | None = None,
        provider: RewardProvider | None = None,
        evaluator: Evaluator | None = None,
        builder: RewardDatasetBuilder | None = None,
    ) -> None:
        super().__init__(config.as_training_config(), estimator=estimator)
        self.rl_config = config
        self.capabilities = capabilities if capabilities is not None else detect_hardware()
        self.runner = runner
        self.optimizer = (
            optimizer if optimizer is not None else policy_optimizer_for(config.algorithm)
        )
        self.evaluator = (
            evaluator
            if evaluator is not None
            else evaluator_for(config.evaluator, policy=config.reward_policy)
        )
        self.provider = (
            provider
            if provider is not None
            else provider_for_config(config, evaluator=self.evaluator)
        )
        self.rl_estimator = RLResourceEstimator(
            estimator=estimator, capabilities=self.capabilities
        )
        self._builder = builder if builder is not None else RewardDatasetBuilder()

    # -- interface -------------------------------------------------------------

    @property
    def name(self) -> str:
        return f"{self.mode}:{self.algorithm}"

    @property
    def algorithm(self) -> str:
        return self.optimizer.algorithm

    def missing_dependencies(self) -> tuple[str, ...]:
        quantised = self.rl_config.effective_method == "qlora"
        return self.capabilities.missing_dependencies(quantised=quantised)

    def is_available(self) -> bool:
        return not self.missing_dependencies()

    def validate_config(self) -> Any:
        validation = self.rl_config.validate()
        problems = list(validation.errors)
        if not self.provider.is_available():
            problems.extend(
                f"reward provider: {item}" for item in self.provider.missing_requirements()
            )
        if not self.evaluator.is_available():
            problems.extend(
                f"evaluator: {item}" for item in self.evaluator.missing_requirements()
            )
        return _with_errors(validation, problems)

    def initialize_policy(self) -> dict[str, Any]:
        """What policy would be initialised — described, never loaded."""
        return {
            "policy_model": self.rl_config.effective_policy,
            "base_model": self.rl_config.base_model,
            "reference_model": self.rl_config.reference_model,
            "kl_coefficient": self.rl_config.kl_coefficient,
            "needs_reference_model": self.rl_config.needs_reference_model,
            "training_method": self.rl_config.effective_method,
            "lora": {
                "rank": self.rl_config.lora_rank,
                "alpha": self.rl_config.lora_alpha,
                "dropout": self.rl_config.lora_dropout,
                "target_modules": list(self.rl_config.target_modules),
            },
            "loaded": False,
            "note": (
                "no model is loaded by a dry run or by this description; a real run "
                "loads the policy only once its dependencies, its permission and an "
                "explicit confirmation are all present"
            ),
        }

    def initialize_reward(self) -> dict[str, Any]:
        """What reward machinery the run would use, and its readiness."""
        return {
            "provider": self.provider.describe(),
            "evaluator": {
                "evaluator_id": self.evaluator.evaluator_id,
                "kind": self.evaluator.evaluator_kind,
                "version": self.evaluator.version,
                "available": self.evaluator.is_available(),
                "missing": list(self.evaluator.missing_requirements()),
            },
            "policy": dict(self.rl_config.reward_policy.to_mapping()),
            "built": False,
        }

    def audit_rewards(self, dataset: RewardDatasetVersion) -> RewardAudit:
        """Audit the reward set against the mode this trainer implements."""
        rows = list(dataset.examples)
        accepted = [row for row in rows if row.accepted]
        by_source: dict[str, int] = {}
        for row in rows:
            key = row.reward_source or "unknown"
            by_source[key] = by_source.get(key, 0) + 1
        problems: list[str] = []
        if not accepted:
            problems.append("no accepted reward examples are available")
        if not any(row.trusted for row in accepted):
            problems.append(
                "no accepted example carries a fully trusted reward: every row is "
                "flagged or held"
            )
        if self.mode == RLMode.RLHF.value and not sum(
            1
            for row in accepted
            if row.reward_source == RewardSource.HUMAN.value or row.feedback_ids
        ):
            problems.append(
                "mode=rlhf needs human feedback in the dataset and none is present"
            )
        if self.mode == RLMode.RLAIF.value and not sum(
            1
            for row in accepted
            if row.reward_source == RewardSource.AI.value or row.rating_ids
        ):
            problems.append(
                "mode=rlaif needs AI ratings in the dataset and none is present"
            )
        human_backed = sum(
            1
            for row in accepted
            if row.reward_source == RewardSource.HUMAN.value or row.feedback_ids
        )
        ai_backed = sum(
            1
            for row in accepted
            if row.reward_source == RewardSource.AI.value or row.rating_ids
        )
        return RewardAudit(
            examples=len(rows),
            accepted=len(accepted),
            trusted=sum(1 for row in accepted if row.trusted),
            flagged=sum(1 for row in accepted if row.integrity.flagged),
            human_backed=human_backed,
            ai_backed=ai_backed,
            by_source=by_source,
            mode_ok=not any("mode=" in problem for problem in problems),
            problems=tuple(problems),
        )

    def prepare_data(self, dataset: SFTDatasetVersion | RewardDatasetVersion) -> dict[str, Any]:
        """What this trainer would actually feed the optimizer, as a summary."""
        rewards = self._rewards(dataset)
        audit = self.audit_rewards(rewards)
        train_rows = len(rewards.split("train")) or len(rewards.accepted_examples())
        micro_batches = math.ceil(train_rows / max(1, self.rl_config.batch_size))
        steps_per_epoch = max(
            1,
            math.ceil(
                micro_batches / max(1, self.rl_config.gradient_accumulation_steps)
            ),
        )
        return {
            "dataset_version": rewards.dataset_version_id,
            "mode": self.mode,
            "algorithm": self.algorithm,
            "examples": len(rewards),
            "accepted": audit.accepted,
            "trusted": audit.trusted,
            "flagged": audit.flagged,
            "by_source": dict(audit.by_source),
            "problems": list(audit.problems),
            "train_examples": train_rows,
            "validation_examples": len(rewards.split("validation")),
            "test_examples": len(rewards.split("test")),
            "rollouts": max(1, int(self.rl_config.rollout_count)),
            "max_steps": max(1, int(self.rl_config.max_steps)),
            "gamma": self.rl_config.gamma,
            "steps_per_epoch": steps_per_epoch,
            "total_steps": steps_per_epoch * max(1, self.rl_config.epochs),
            "max_sequence_length": self.rl_config.max_sequence_length,
            "policy": self.initialize_policy(),
            "reward": self.initialize_reward(),
        }

    def estimate_resources(
        self, dataset: SFTDatasetVersion | RewardDatasetVersion | None = None
    ) -> Any:
        rewards = self._rewards(dataset) if dataset is not None else None
        return self.rl_estimator.estimate(self.rl_config, dataset=rewards)

    def evaluate(self, dataset: SFTDatasetVersion | RewardDatasetVersion) -> dict[str, Any]:
        """Backend-local checks — not the model comparison, which is separate."""
        rewards = self._rewards(dataset)
        issues = self._builder.validate(rewards)
        audit = self.audit_rewards(rewards)
        return {
            "dataset_version": rewards.dataset_version_id,
            "valid": not issues and not audit.problems,
            "issues": [*issues, *audit.problems],
            "rewards": audit.to_dict(),
        }

    def checkpoint(self, run: TrainingRun, callbacks: TrainingCallbacks, kind: str) -> None:
        callbacks.checkpoint(run, kind)

    def cancel(self, run: TrainingRun) -> TrainingRun:
        return run.with_status(TrainingRunStatus.CANCELLED, end_time=now_iso())

    def model_metadata(self) -> dict[str, Any]:
        """What this run produced, including how it was trained."""
        metadata = super().model_metadata()
        metadata.update(
            {
                "training_method": self.rl_config.effective_method,
                "rl_mode": self.mode,
                "algorithm": self.algorithm,
                "reward_provider": self.provider.provider_id,
                "reward_source": self.provider.source,
                "evaluator": self.evaluator.evaluator_id,
                "evaluator_version": self.evaluator.version,
                "reward_dataset_version": self.rl_config.reward_dataset_version,
                "kl_coefficient": self.rl_config.kl_coefficient,
                "simulated": not self.optimizer.learns,
                "dry_run": self.rl_config.dry_run,
                "output_directory": str(self.rl_config.output_directory),
            }
        )
        return metadata

    # -- the loop ---------------------------------------------------------------

    def _samples(
        self, dataset: RewardDatasetVersion, *, seed: int
    ) -> list[RewardExample]:
        """Accepted rows in a deterministic, seed-shuffled order."""
        rows = [
            row
            for row in dataset.accepted_examples()
            if not (self.rl_config.reward_policy.require_integrity and not row.trusted)
        ]
        return sorted(
            rows,
            key=lambda row: hashlib.sha256(
                f"{seed}:{row.example_id}".encode()
            ).hexdigest(),
        )

    def _walk(
        self,
        run: TrainingRun,
        dataset: RewardDatasetVersion,
        callbacks: TrainingCallbacks,
        *,
        first_step: int = 0,
    ) -> TrainingRun:
        """The shared simulation: a real schedule, honestly labelled as simulated."""
        config = self.rl_config
        prepared = self.prepare_data(dataset)
        steps_per_epoch = int(prepared["steps_per_epoch"])
        total_steps = max(1, int(prepared["total_steps"]))
        samples = self._samples(dataset, seed=int(config.seed))
        if not samples:
            return self.finalize(
                run.with_status(
                    TrainingRunStatus.RUNNING,
                    start_time=run.start_time or now_iso(),
                    rl_metrics={
                        "mode": self.mode,
                        "algorithm": self.algorithm,
                        "simulated": True,
                        "examples": 0,
                        "note": (
                            "no accepted, trusted reward example was available, so "
                            "the schedule was walked without a single optimization "
                            "pass"
                        ),
                    },
                )
            )
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
        last_update: dict[str, Any] = {}
        while epoch < config.epochs:
            epoch += 1
            for _ in range(steps_per_epoch):
                if callbacks.cancelled():
                    return current.with_status(
                        TrainingRunStatus.CANCELLED, end_time=now_iso()
                    )
                step += 1
                batch_size = min(len(samples), max(1, int(config.batch_size)))
                start = (step * batch_size) % max(1, len(samples))
                batch = [
                    samples[(start + offset) % len(samples)]
                    for offset in range(batch_size)
                ]
                update = self.optimizer.optimize(
                    batch, config=config, epoch=epoch, step=step
                )
                last_update = update.to_dict()
                loss = round(1.0 / (1.0 + max(0.0, update.mean_reward)), 6)
                history.append(
                    {
                        "epoch": epoch,
                        "step": step,
                        "loss": loss,
                        "mean_reward": round(update.mean_reward, 6),
                    }
                )
                current = current.with_status(
                    TrainingRunStatus.RUNNING,
                    current_epoch=epoch,
                    current_step=step,
                    training_loss=loss,
                    loss_history=tuple(history[-MAX_LOSS_POINTS:]),
                    rl_metrics={
                        "mode": self.mode,
                        "algorithm": self.algorithm,
                        "simulated": True,
                        "reward_provider": self.provider.provider_id,
                        "reward_source": self.provider.source,
                        "evaluator": self.evaluator.evaluator_id,
                        "mean_reward": update.mean_reward,
                        "reward_std": update.reward_std,
                        "min_reward": update.min_reward,
                        "max_reward": update.max_reward,
                        "entropy": update.entropy,
                        "clip_fraction": update.clip_fraction,
                        "policy_delta": update.policy_delta,
                        "examples_used": update.examples,
                        "known_flagged": sum(
                            1 for row in dataset.examples if row.integrity.flagged
                        ),
                        "by_source": dict(
                            dataset.statistics.by_source
                        ),
                        "rollouts": max(1, int(config.rollout_count)),
                        "gamma": config.gamma,
                        "policy_update": update.to_dict(),
                        "note": (
                            "a dry run produces measurements over the reward set, "
                            "not a policy: no weights change"
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
        current = current.with_status(
            TrainingRunStatus.RUNNING,
            rl_metrics={
                **dict(current.rl_metrics),
                "policy_update": last_update,
            },
        )
        callbacks.checkpoint(current, "best")
        return self.finalize(current)

    def _run(
        self,
        run: TrainingRun,
        dataset: RewardDatasetVersion,
        callbacks: TrainingCallbacks,
    ) -> TrainingRun:
        """Either the injected runner, or the simulation, or a clear refusal."""
        config = self.rl_config
        if config.dry_run:
            return self._walk(run, dataset, callbacks)
        missing = self.missing_dependencies()
        if missing:
            raise TrainingBackendUnavailable(
                f"a real {self.mode.upper()} run needs the optional training "
                "dependencies; missing: "
                + ", ".join(missing)
                + ". Dry-run, dataset validation, reward generation and resource "
                "estimation work without them."
            )
        if not self.optimizer.learns:
            raise TrainingBackendUnavailable(
                f"algorithm={self.algorithm!r} is a simulated optimizer: it cannot "
                "run a real training pass. This phase ships the interface and the "
                "mock; a future phase adds the algorithm."
            )
        if self.runner is None:
            raise TrainingBackendUnavailable(
                f"the {self.mode.upper()} backend is available but no training runner "
                "is wired: Phase 18 ships the pipeline, the reward providers, the "
                "evaluators, the resource estimate and the evaluation, and a runner "
                "is how a concrete RL loop attaches. Provide "
                f"{type(self).__name__}(runner=...)."
            )
        prepared = self.prepare_data(dataset)
        started = run.with_status(
            TrainingRunStatus.RUNNING,
            start_time=run.start_time or now_iso(),
            total_steps=int(prepared["total_steps"]),
            rl_metrics={
                "mode": self.mode,
                "algorithm": self.algorithm,
                "simulated": False,
                "reward_provider": self.provider.provider_id,
            },
        )
        result = self.runner(started, dataset, callbacks)
        return result if isinstance(result, TrainingRun) else self.finalize(started)

    def start_training(
        self,
        run: TrainingRun,
        dataset: SFTDatasetVersion | RewardDatasetVersion,
        callbacks: TrainingCallbacks,
    ) -> TrainingRun:
        return self._run(run, self._rewards(dataset), callbacks)

    def resume_training(
        self,
        run: TrainingRun,
        dataset: SFTDatasetVersion | RewardDatasetVersion,
        checkpoint_step: int,
        callbacks: TrainingCallbacks,
    ) -> TrainingRun:
        rewards = self._rewards(dataset)
        if not self.rl_config.dry_run:
            return self._run(run, rewards, callbacks)
        return self._walk(run, rewards, callbacks, first_step=max(0, int(checkpoint_step)))

    def train(
        self,
        run: TrainingRun,
        dataset: SFTDatasetVersion | RewardDatasetVersion,
        callbacks: TrainingCallbacks,
    ) -> TrainingRun:
        """The spec's ``train()``: the same entry point as ``start_training``."""
        return self.start_training(run, dataset, callbacks)

    def _rewards(
        self, dataset: SFTDatasetVersion | RewardDatasetVersion
    ) -> RewardDatasetVersion:
        """The reward version this backend can train on, or a clear refusal."""
        if not isinstance(dataset, RewardDatasetVersion):
            raise TrainingBackendUnavailable(
                f"the {self.mode} backend trains REWARD datasets and was handed a "
                f"{type(dataset).__name__}; build a reward dataset version and point "
                "the run at that"
            )
        return dataset


class RLHFTrainer(RLTrainer):
    """Learning from human feedback: the dataset must contain what people said."""

    mode = RLMode.RLHF.value
    backend = "rlhf"


class RLAIFTrainer(RLTrainer):
    """Learning from AI feedback: the dataset must contain evaluator ratings."""

    mode = RLMode.RLAIF.value
    backend = "rlaif"


class DryRunRLTrainer(RLTrainer):
    """Walks the schedule and trains nothing — the default on this machine."""

    backend = "dry_run"

    def __init__(self, config: RLTrainingConfig, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        if config.mode in MODES:
            self.mode = config.mode


def policy_optimizer_for(algorithm: str) -> PolicyOptimizer:
    """The optimizer one algorithm asks for. One place decides this."""
    wanted = str(algorithm)
    if wanted == PolicyAlgorithm.MOCK.value:
        return MockPolicyOptimizer()
    if wanted in IMPLEMENTED_ALGORITHMS:
        return MockPolicyOptimizer()
    if wanted in PLANNED_ALGORITHMS:
        raise PolicyOptimizerUnavailable(
            f"algorithm={wanted!r} is not implemented in this phase: the policy "
            "optimizer interface is ready for it, but the only optimizer that runs "
            f"today is {PolicyAlgorithm.MOCK.value!r}"
        )
    raise PolicyOptimizerUnavailable(
        f"unknown policy algorithm {wanted!r}: expected "
        + ", ".join(sorted({*IMPLEMENTED_ALGORITHMS, *PLANNED_ALGORITHMS}))
    )


def rl_trainer_for(
    config: RLTrainingConfig,
    *,
    estimator: ResourceEstimator | None = None,
    capabilities: HardwareCapabilities | None = None,
    runner: RLRunner | None = None,
    optimizer: PolicyOptimizer | None = None,
    provider: RewardProvider | None = None,
    evaluator: Evaluator | None = None,
) -> RLTrainer:
    """The backend one configuration asks for. One place decides this."""
    shared: dict[str, Any] = {
        "estimator": estimator,
        "capabilities": capabilities,
        "runner": runner,
        "optimizer": optimizer,
        "provider": provider,
        "evaluator": evaluator,
    }
    if config.dry_run:
        return DryRunRLTrainer(config, **shared)
    if config.mode == RLMode.RLAIF.value:
        return RLAIFTrainer(config, **shared)
    return RLHFTrainer(config, **shared)


def provider_for_config(
    config: RLTrainingConfig, *, evaluator: Evaluator | None = None
) -> RewardProvider:
    """The provider a configuration asks for, defaulting to a composite.

    ``auto`` means "whatever this run's data can support", and the composite
    provider is how several sources coexist without pretending to be one. A
    provider named explicitly gets only its own sources wired — a human-only run
    cannot accidentally learn from an AI score.
    """
    from novacontrol.rlhf.rewards import (
        AIRatingRewardProvider,
        EvaluationRewardProvider,
        HumanRewardProvider,
    )

    policy = config.reward_policy
    wanted = config.reward_provider
    if wanted in {"human", "human_feedback"}:
        return HumanRewardProvider(policy)
    if wanted in {"ai", "ai_rating"}:
        return AIRatingRewardProvider(
            evaluator, policy=policy, criteria=()
        )
    if wanted in {"rule", "evaluation", "verifier"}:
        return EvaluationRewardProvider(policy=policy)
    if wanted == "composite":
        return default_composite(evaluator=evaluator, policy=policy)
    # ``auto``: everything the policy allows, composed.
    return CompositeRewardProvider(
        [
            HumanRewardProvider(policy),
            AIRatingRewardProvider(evaluator, policy=policy),
            EvaluationRewardProvider(policy=policy),
        ],
        policy=policy,
    )


def _with_errors(validation: Any, problems: Sequence[str]) -> Any:
    """A validation result with extra errors appended (same type, same shape)."""
    if not problems:
        return validation
    return type(validation)(
        errors=tuple(validation.errors) + tuple(problems),
        warnings=tuple(validation.warnings),
    )


__all__ = [
    "DryRunRLTrainer",
    "MockPolicyOptimizer",
    "POLICY_OPTIMIZERS",
    "PolicyOptimizer",
    "PolicyOptimizerUnavailable",
    "RLAIFTrainer",
    "RLHFTrainer",
    "RLRunner",
    "RLTrainer",
    "RewardAudit",
    "optimizer_for",
    "policy_optimizer_for",
    "provider_for_config",
    "rl_trainer_for",
]
