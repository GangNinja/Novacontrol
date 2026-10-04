"""The RLHF and RLAIF pipelines, planned and simulated end to end.

A pipeline here is an ORDER of named stages, each of which says whether it is
ready, blocked or skipped for THIS configuration, dataset and machine. That is
what makes it useful before anything runs: a dry run answers "would this work
here?" stage by stage, and a blocked stage names exactly what is missing.

    trajectory → feedback/ratings → reward → reward validation → policy
    optimizer → candidate model → evaluation → model registry

Both loops are the same shape. RLHF's reward comes from what a person said;
RLAIF's comes from an evaluator's structured rating. The stage list is built
once and its inputs differ, which is why there is one planner and not two
copies of one.

The simulation is honest about what it is: rewards are re-derived from stored
rows with the configured provider, rollouts are run in the deterministic mock
environment, and the optimizer stage reports the mock's status. A dry run never
loads a model and never starts training — the stage list says so in words.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.rlhf.backends import (
    POLICY_OPTIMIZERS,
    RewardAudit,
    RLTrainer,
    policy_optimizer_for,
)
from novacontrol.rlhf.config import RLTrainingConfig
from novacontrol.rlhf.integrity import FeedbackDisagreementDetector, RewardIntegrityChecker
from novacontrol.rlhf.models import (
    IMPLEMENTED_ALGORITHMS,
    RewardDatasetVersion,
    RLMode,
)
from novacontrol.rlhf.rewards import (
    RewardProvider,
    RewardProviderUnavailable,
    RewardRequest,
)
from novacontrol.rlhf.rollout import (
    MockEnvironment,
    RolloutRunner,
    ScriptedPolicy,
    summarise,
)
from novacontrol.training.models import ResourceEstimate

#: The stage order both pipelines share. Named once so a report, a test and a
#: future reader all mean the same thing by "stage 6".
PIPELINE_STAGES: tuple[str, ...] = (
    "data_preparation",
    "reward_generation",
    "reward_validation",
    "training_configuration",
    "resource_estimation",
    "rollout_simulation",
    "policy_optimization",
    "checkpointing",
    "evaluation",
    "registration",
)

#: Stages where a block stops a real run (the rest can be simulated).
BLOCKING_STAGES: frozenset[str] = frozenset(
    {
        "data_preparation",
        "training_configuration",
        "resource_estimation",
        "policy_optimization",
    }
)


@dataclass(frozen=True, slots=True)
class PipelineStage:
    """One step of a pipeline, with its status and what it saw."""

    name: str
    status: str = "ready"
    detail: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.name,
            "status": self.status,
            "detail": self.detail,
            "data": dict(self.data),
            "blocking": self.name in BLOCKING_STAGES,
        }


@dataclass(frozen=True, slots=True)
class RLPipelinePlan:
    """A pipeline, stage by stage, for one configuration and dataset."""

    mode: str = RLMode.RLHF.value
    algorithm: str = ""
    stages: tuple[PipelineStage, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(
            stage.blocked for stage in self.stages if stage.name in BLOCKING_STAGES
        )

    def blocked(self) -> tuple[str, ...]:
        return tuple(stage.name for stage in self.stages if stage.blocked)

    def stage(self, name: str) -> PipelineStage | None:
        for item in self.stages:
            if item.name == name:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "algorithm": self.algorithm,
            "ok": self.ok,
            "blocked": list(self.blocked()),
            "stages": [stage.to_dict() for stage in self.stages],
            "notes": list(self.notes),
        }


#: How a pipeline gets the pieces it needs without importing the manager.
DatasetLookup = Callable[[str], RewardDatasetVersion | None]
TrainerFactory = Callable[[RLTrainingConfig], RLTrainer]


class RLPipeline:
    """Plans a pipeline and simulates its reward/rollout stages."""

    def __init__(
        self,
        *,
        trainer_factory: TrainerFactory,
        dataset_lookup: DatasetLookup | None = None,
        checker: RewardIntegrityChecker | None = None,
        detector: FeedbackDisagreementDetector | None = None,
        runner: RolloutRunner | None = None,
        max_examples: int = 50,
    ) -> None:
        self._trainer_factory = trainer_factory
        self._dataset_lookup = dataset_lookup
        self.checker = checker if checker is not None else RewardIntegrityChecker()
        self.detector = detector if detector is not None else FeedbackDisagreementDetector()
        self.runner = runner if runner is not None else RolloutRunner()
        self.max_examples = max(1, int(max_examples))

    # -- planning ---------------------------------------------------------------

    def plan(
        self,
        config: RLTrainingConfig,
        *,
        dataset: RewardDatasetVersion | None = None,
        estimate: ResourceEstimate | None = None,
        trainer: RLTrainer | None = None,
    ) -> RLPipelinePlan:
        """Build the stage list for one configuration and dataset."""
        validation = config.validate()
        resolved_dataset = dataset
        if resolved_dataset is None and config.reward_dataset_version and self._dataset_lookup:
            resolved_dataset = self._dataset_lookup(config.reward_dataset_version)
        resolved_trainer = trainer
        notes: tuple[str, ...] = ()
        if resolved_trainer is None:
            try:
                resolved_trainer = self._trainer_factory(config)
            except Exception as exc:  # noqa: BLE001 - a plan must not raise
                resolved_trainer = None
                notes = (f"the trainer could not be built: {type(exc).__name__}: {exc}",)

        audit = (
            resolved_trainer.audit_rewards(resolved_dataset)
            if resolved_trainer is not None and resolved_dataset is not None
            else RewardAudit()
        )
        stages: list[PipelineStage] = []

        # 1. data preparation
        if resolved_dataset is None:
            stages.append(
                PipelineStage(
                    "data_preparation",
                    "blocked",
                    f"no reward dataset version {config.reward_dataset_version!r} exists",
                )
            )
        elif not audit.accepted:
            stages.append(
                PipelineStage(
                    "data_preparation",
                    "blocked",
                    "the dataset has no accepted reward examples"
                    + ("; " + "; ".join(audit.problems) if audit.problems else ""),
                    greenlight_data(resolved_dataset),
                )
            )
        elif audit.problems:
            stages.append(
                PipelineStage(
                    "data_preparation",
                    "blocked",
                    "; ".join(audit.problems),
                    greenlight_data(resolved_dataset),
                )
            )
        else:
            stages.append(
                PipelineStage(
                    "data_preparation",
                    "ready",
                    f"{audit.accepted} accepted example(s), {audit.trusted} trusted",
                    greenlight_data(resolved_dataset),
                )
            )

        # 2. reward generation
        provider = resolved_trainer.provider if resolved_trainer is not None else None
        if provider is None:
            stages.append(
                PipelineStage("reward_generation", "skipped", "no trainer was built")
            )
        elif not provider.is_available():
            stages.append(
                PipelineStage(
                    "reward_generation",
                    "blocked",
                    "the reward provider is unavailable: "
                    + ", ".join(provider.missing_requirements()),
                    provider.describe(),
                )
            )
        else:
            stages.append(
                PipelineStage(
                    "reward_generation",
                    "ready",
                    f"provider {provider.provider_id} ({provider.source})"
                    + (" — simulated in a dry run" if config.dry_run else ""),
                    provider.describe(),
                )
            )

        # 3. reward validation
        flagged = sum(
            1 for row in resolved_dataset.examples if row.integrity.flagged
        ) if resolved_dataset is not None else 0
        stages.append(
            PipelineStage(
                "reward_validation",
                "ready",
                f"integrity checks are configured; {flagged} reward(s) are flagged "
                "and will be held or refused by the dataset rules",
                {"required": config.reward_policy.require_integrity, "flagged": flagged},
            )
        )

        # 4. training configuration
        stages.append(
            PipelineStage(
                "training_configuration",
                "ready" if validation.valid else "blocked",
                "; ".join(validation.errors)
                or f"mode={config.mode}, algorithm={config.algorithm}",
                {"errors": list(validation.errors), "warnings": list(validation.warnings)},
            )
        )

        # 5. resource estimation
        if estimate is None:
            estimate = (
                resolved_trainer.estimate_resources(resolved_dataset)
                if resolved_trainer is not None
                else None
            )
        if estimate is None:
            stages.append(
                PipelineStage("resource_estimation", "skipped", "nothing was priced")
            )
        else:
            status = "ready" if estimate.allows_training else "blocked"
            stages.append(
                PipelineStage(
                    "resource_estimation",
                    status,
                    f"{estimate.level}: "
                    + ("; ".join(estimate.reasons[:3]) or "no reasons recorded")
                    + ("" if estimate.allows_training else " — an operator override is required"),
                    estimate.to_dict(),
                )
            )

        # 6. rollout simulation
        simulated = self.simulate_rollouts(config)
        stages.append(
            PipelineStage(
                "rollout_simulation",
                "ready",
                f"{simulated['rollouts']} deterministic mock rollout(s), mean reward "
                f"{simulated['mean_reward']:+.2f}",
                simulated,
            )
        )

        # 7. policy optimization
        algorithm = config.algorithm
        described = POLICY_OPTIMIZERS.get(algorithm, {})
        if algorithm in IMPLEMENTED_ALGORITHMS:
            stages.append(
                PipelineStage(
                    "policy_optimization",
                    "ready",
                    "the mock optimizer runs; it produces measurements, not weights",
                    dict(described),
                )
            )
        else:
            stages.append(
                PipelineStage(
                    "policy_optimization",
                    "blocked",
                    f"algorithm {algorithm!r} is not implemented in this phase",
                    dict(described),
                )
            )

        # 8. checkpointing
        checkpoint = self.validate_checkpoints(config)
        stages.append(
            PipelineStage(
                "checkpointing",
                "ready" if checkpoint["ok"] else "blocked",
                "; ".join(checkpoint["problems"]) or checkpoint["detail"],
                checkpoint,
            )
        )

        # 9. evaluation
        if resolved_trainer is not None and not resolved_trainer.evaluator.is_available():
            stages.append(
                PipelineStage(
                    "evaluation",
                    "blocked",
                    "the evaluator is unavailable: "
                    + ", ".join(resolved_trainer.evaluator.missing_requirements()),
                )
            )
        else:
            stages.append(
                PipelineStage(
                    "evaluation",
                    "ready",
                    "the candidate is compared against base, SFT and preference "
                    "models on held-out behaviour; the reward is not the verdict",
                )
            )

        # 10. registration
        stages.append(
            PipelineStage(
                "registration",
                "ready",
                "a completed run is registered EXPERIMENTAL; approval and promotion "
                "stay explicit registry operations",
            )
        )
        return RLPipelinePlan(
            mode=config.mode,
            algorithm=algorithm,
            stages=tuple(stages),
            notes=notes,
        )

    # -- simulation -------------------------------------------------------------

    def simulate_rewards(
        self,
        dataset: RewardDatasetVersion,
        provider: RewardProvider,
        *,
        limit: int = 0,
    ) -> dict[str, Any]:
        """Re-derive rewards for stored rows with the configured provider."""
        rows = list(dataset.accepted_examples())[: (limit or self.max_examples)]
        produced: list[dict[str, Any]] = []
        unavailable = 0
        for row in rows:
            request = RewardRequest(
                feedback=(),
                task=dict(row.task),
                candidate=dict(row.candidate),
                outcome=dict(row.metadata),
                mode=row.mode,
            )
            origin = "derived"
            try:
                reward = provider.evaluate(request)
            except RewardProviderUnavailable:
                # The stored reward is still a reading; a simulation says which
                # readings it could re-derive and which it could only read back,
                # rather than silently counting them as the same thing.
                reward = row.reward
                origin = "recorded"
                unavailable += 1
            check = self.checker.check(reward)
            produced.append(
                {
                    "example_id": row.example_id,
                    "reward": round(reward.total_reward, 6),
                    "source": reward.reward_source,
                    "origin": origin,
                    "integrity": check.status,
                    "evidence": list(reward.evidence),
                }
            )
        mean = (
            sum(item["reward"] for item in produced) / len(produced) if produced else 0.0
        )
        return {
            "simulated": True,
            "examples": len(rows),
            "measured": len(produced),
            "readings": produced,
            "unavailable": unavailable,
            "recorded": sum(1 for item in produced if item["origin"] == "recorded"),
            "mean_reward": round(mean, 6),
            "integrity": self._integrity_counts(produced),
            "note": (
                "rewards were re-derived from stored rows with the configured "
                "provider where it could measure them, and read back from the "
                "stored reward where it could not; no model was consulted and "
                "nothing was trained"
            ),
        }

    def simulate_rollouts(self, config: RLTrainingConfig, *, limit: int = 0) -> dict[str, Any]:
        """Run deterministic mock episodes, the way a dry run collects experience."""
        count = max(1, int(limit or config.rollout_count))
        rollouts = []
        for index in range(count):
            environment = MockEnvironment(
                max_steps=max(1, int(config.max_steps)),
                metadata={"episode": index},
            )
            actions: list[dict[str, Any]] = [{"action": "observe", "episode": index}]
            actions.extend(
                {"action": "act", "episode": index, "step": step}
                for step in range(max(0, int(config.max_steps) - 2))
            )
            actions.append({"action": "finish", "episode": index})
            policy = ScriptedPolicy(actions, policy_id="mock_policy")
            rollouts.append(
                self.runner.run(
                    environment,
                    policy,
                    task={"task_id": f"mock-{index}"},
                    seed=int(config.seed) + index,
                    task_id=f"mock-{index}",
                )
            )
        summary = summarise(rollouts)
        return {
            "simulated": True,
            "rollouts": summary.rollouts,
            "steps": summary.steps,
            "mean_reward": round(summary.mean_reward, 6),
            "mean_length": round(summary.mean_length, 6),
            "truncated": summary.truncated,
            "environments": dict(summary.by_environment),
            "sample": rollouts[0].to_dict() if rollouts else {},
            "note": (
                "rollouts were collected in the deterministic mock environment: "
                "nothing outside this process was touched"
            ),
        }

    @staticmethod
    def _integrity_counts(items: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            key = str(item.get("integrity", "unknown"))
            counts[key] = counts.get(key, 0) + 1
        return counts

    @staticmethod
    def validate_checkpoints(config: RLTrainingConfig) -> dict[str, Any]:
        """Checkpoint settings a run needs before it may start."""
        problems: list[str] = []
        if config.max_checkpoints < 1:
            problems.append("max_checkpoints must be at least 1")
        if config.checkpoint_frequency < 0:
            problems.append("checkpoint_frequency cannot be negative")
        if not config.output_directory and not config.dry_run:
            problems.append("a real run needs an output_directory")
        return {
            "ok": not problems,
            "problems": problems,
            "detail": (
                f"periodic every {config.checkpoint_frequency or 'epoch'} step(s), "
                f"retention {config.max_checkpoints}, best checkpoint kept, "
                "resume supported"
            ),
            "frequency": config.checkpoint_frequency,
            "max_checkpoints": config.max_checkpoints,
            "resume_from": config.resume_from_checkpoint,
        }

    # -- the full dry run -------------------------------------------------------

    def dry_run(
        self,
        config: RLTrainingConfig,
        *,
        dataset: RewardDatasetVersion | None = None,
        trainer: RLTrainer | None = None,
    ) -> dict[str, Any]:
        """§27's dry run: validate everything, simulate the pipeline, start nothing."""
        resolved_trainer = trainer
        build_error = ""
        if resolved_trainer is None:
            try:
                resolved_trainer = self._trainer_factory(config)
            except Exception as exc:  # noqa: BLE001 - a dry run reports, never raises
                build_error = f"{type(exc).__name__}: {exc}"
        resolved_dataset = dataset
        if resolved_dataset is None and config.reward_dataset_version and self._dataset_lookup:
            resolved_dataset = self._dataset_lookup(config.reward_dataset_version)
        estimate = (
            resolved_trainer.estimate_resources(resolved_dataset)
            if resolved_trainer is not None
            else None
        )
        plan = self.plan(
            config,
            dataset=resolved_dataset,
            estimate=estimate,
            trainer=resolved_trainer,
        )
        validation = config.validate()
        prepared: dict[str, Any] = {}
        reward_simulation: dict[str, Any] = {}
        audit: RewardAudit = RewardAudit()
        if resolved_trainer is not None and resolved_dataset is not None:
            prepared = resolved_trainer.prepare_data(resolved_dataset)
            audit = resolved_trainer.audit_rewards(resolved_dataset)
            reward_simulation = self.simulate_rewards(
                resolved_dataset, resolved_trainer.provider
            )
        dataset_issues = (
            resolved_trainer.evaluate(resolved_dataset).get("issues", ())
            if resolved_trainer is not None and resolved_dataset is not None
            else ()
        )
        payload: dict[str, Any] = {
            "ok": plan.ok and validation.valid and bool(build_error == ""),
            "dry_run": True,
            "started": False,
            "mode": config.mode,
            "algorithm": config.algorithm,
            "policy_optimizer": dict(POLICY_OPTIMIZERS.get(config.algorithm, {})),
            "config": config.to_mapping(),
            "errors": list(validation.errors),
            "warnings": list(validation.warnings),
            "build_error": build_error,
            "dataset": dataset_summary(resolved_dataset),
            "dataset_issues": list(dataset_issues),
            "reward_audit": audit.to_dict(),
            "reward_simulation": reward_simulation,
            "prepared": prepared,
            "plan": plan.to_dict(),
            "estimate": estimate.to_dict() if estimate is not None else {},
            "backend": (
                {
                    "name": resolved_trainer.name,
                    "backend": resolved_trainer.backend,
                    "mode": resolved_trainer.mode,
                    "algorithm": resolved_trainer.algorithm,
                    "available": resolved_trainer.is_available(),
                    "missing_dependencies": list(resolved_trainer.missing_dependencies()),
                    "policy": resolved_trainer.initialize_policy(),
                    "reward": resolved_trainer.initialize_reward(),
                }
                if resolved_trainer is not None
                else {}
            ),
            "checkpoints": self.validate_checkpoints(config),
            "note": (
                "a dry run validates the reward provider, the evaluator, the "
                "dataset, the policy optimizer, the environment and the checkpoint "
                "settings, simulates rewards and rollouts, and starts nothing"
            ),
        }
        return payload


def greenlight_data(dataset: RewardDatasetVersion) -> dict[str, Any]:
    """The dataset facts a pipeline report shows."""
    return {
        "dataset_version": dataset.dataset_version_id,
        "mode": dataset.mode,
        "examples": len(dataset),
        "accepted": len(dataset.accepted_examples()),
        "trusted": len(dataset.trusted_examples()),
        "sources": dict(dataset.sources),
        "splits": {name: len(ids) for name, ids in dataset.splits.items() if ids},
    }


def dataset_summary(dataset: RewardDatasetVersion | None) -> dict[str, Any]:
    if dataset is None:
        return {}
    return {
        **greenlight_data(dataset),
        "statistics": dataset.statistics.to_dict(),
        "fingerprint": dataset.fingerprint(),
    }


def optimizer_status(algorithm: str) -> dict[str, Any]:
    """What one algorithm's optimizer can do today, without building one."""
    try:
        optimizer = policy_optimizer_for(algorithm)
    except Exception as exc:  # noqa: BLE001 - a report, not a gate
        return {
            "algorithm": algorithm,
            "available": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }
    return {"available": True, **optimizer.describe()}


__all__ = [
    "BLOCKING_STAGES",
    "DatasetLookup",
    "PIPELINE_STAGES",
    "PipelineStage",
    "RLPipeline",
    "RLPipelinePlan",
    "TrainerFactory",
    "dataset_summary",
    "greenlight_data",
    "optimizer_status",
]
