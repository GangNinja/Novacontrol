"""The RLVR and critique pipelines, planned and simulated end to end.

A pipeline here is an ORDER of named stages, each of which says whether it is
ready, blocked, skipped or done for THIS configuration, task set and machine.
That is what makes it useful before anything runs: a dry run answers "would this
work here?" stage by stage, and a blocked stage names exactly what is missing.

    environment → model action → verifier selection → verification
    → reward calculation → reward validation → critique generation
    → dataset generation → training configuration → evaluation

Every stage that runs in a dry run runs on DETERMINISTIC inputs: Phase 18's
mock environment and a scripted policy, expectations frozen before verification
begins, and Phase 19's verifiers reading observations, not opinions. The stages
that cannot be honest in a dry run say so — evaluation compares verifications
against labels recorded before the run, so without labels it reports itself
skipped rather than inventing a truth to score against.

No model is loaded, no weights change, and no optimizer runs. The stage list,
the reward configuration and the verifier snapshot are the artefacts; they are
what a later real run would be held to.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.rlhf.rollout import MockEnvironment, RolloutRunner, ScriptedPolicy
from novacontrol.rlvr.config import RLVRTrainingConfig
from novacontrol.rlvr.datasets import CritiqueDatasetRules
from novacontrol.rlvr.evaluation import RLVREvaluator
from novacontrol.rlvr.models import (
    CritiqueDatasetVersion,
    CritiqueResult,
    VerificationResult,
    _text,
)
from novacontrol.rlvr.trainer import (
    CritiqueTrainingMethod,
    RLVRTrainer,
    VerifiedReward,
)

#: The ten stages, in the order the phase names them. Named once so a report, a
#: test and a future reader all mean the same thing by "stage 5".
RLVR_STAGES: tuple[str, ...] = (
    "environment",
    "model_action",
    "verifier_selection",
    "verification",
    "reward_calculation",
    "reward_validation",
    "critique_generation",
    "dataset_generation",
    "training_configuration",
    "evaluation",
)

#: A stage that can proceed now.
STATUS_READY = "ready"
#: A stage that ran during this dry run.
STATUS_DONE = "done"
#: A stage that cannot proceed; the reason is in ``detail``.
STATUS_BLOCKED = "blocked"
#: A stage that this configuration does not use.
STATUS_SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class RLVRStage:
    """One named stage: whether it can run, why, and what it produced."""

    name: str = ""
    status: str = STATUS_READY
    detail: str = ""
    artifacts: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status != STATUS_BLOCKED

    @property
    def blocked(self) -> bool:
        return self.status == STATUS_BLOCKED

    def with_artifacts(self, artifacts: Mapping[str, Any]) -> RLVRStage:
        return RLVRStage(
            name=self.name,
            status=STATUS_DONE,
            detail=self.detail,
            artifacts=dict(artifacts),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.name,
            "status": self.status,
            "ok": self.ok,
            "detail": self.detail,
            "artifacts": dict(self.artifacts),
        }


@dataclass(frozen=True, slots=True)
class RLVRPipelinePlan:
    """Every stage's reading for one configuration, before anything runs."""

    config_fingerprint: str = ""
    stages: tuple[RLVRStage, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.blocked()

    def blocked(self) -> tuple[str, ...]:
        return tuple(stage.name for stage in self.stages if stage.blocked)

    def stage(self, name: str) -> RLVRStage | None:
        wanted = _text(name)
        for stage in self.stages:
            if stage.name == wanted:
                return stage
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "config_fingerprint": self.config_fingerprint,
            "blocked": list(self.blocked()),
            "stages": [stage.to_dict() for stage in self.stages],
            "notes": list(self.notes),
        }


class RLVRPipeline:
    """Plans and simulates an RLVR run on this machine, without training."""

    def __init__(
        self,
        *,
        trainer_factory: Callable[[RLVRTrainingConfig], RLVRTrainer | None] | None = None,
        registry: Any = None,
        dataset_lookup: Any = None,
        runner: RolloutRunner | None = None,
        max_steps: int = 8,
        corrector: Any = None,
    ) -> None:
        self._trainer_factory = trainer_factory
        self.registry = registry
        self.dataset_lookup = dataset_lookup
        self.runner = runner if runner is not None else RolloutRunner()
        self.max_steps = max(1, int(max_steps))
        self.corrector = corrector

    # -- the actors ----------------------------------------------------------------

    def trainer_for(self, config: RLVRTrainingConfig) -> RLVRTrainer:
        if self._trainer_factory is not None:
            found = self._trainer_factory(config)
            if found is not None:
                return found
        return RLVRTrainer(config, registry=self.registry, corrector=self.corrector)

    def method_for(self, config: RLVRTrainingConfig) -> CritiqueTrainingMethod:
        return CritiqueTrainingMethod(
            method="sft",
            config=config.critique,
            corrector=self.corrector,
            reward_config=config.reward,
        )

    # -- planning ------------------------------------------------------------------

    def plan(
        self,
        config: RLVRTrainingConfig,
        *,
        dataset: CritiqueDatasetVersion | None = None,
        tasks: Sequence[Mapping[str, Any]] = (),
    ) -> RLVRPipelinePlan:
        """Every stage's readiness for this configuration and task set."""
        trainer = self.trainer_for(config)
        rows = list(tasks or ())
        policy = config.verifiers
        metadata = trainer.registry.list()
        deterministic = [item for item in metadata if item.deterministic]
        stages: list[RLVRStage] = []
        notes: list[str] = [
            "a dry run simulates every stage on deterministic inputs; no model is "
            "loaded and no weights change",
            f"reward configuration version {config.reward.version} is pinned for "
            "the whole run",
        ]

        stages.append(
            RLVRStage(
                "environment",
                STATUS_READY if rows else STATUS_SKIPPED,
                (
                    "a deterministic, read-only mock environment runs each task"
                    if rows
                    else "no task was supplied: there is no environment to run"
                ),
                {"tasks": len(rows), "max_steps": self.max_steps},
            )
        )
        actions = sum(len(list(task.get("actions") or ())) for task in rows)
        stages.append(
            RLVRStage(
                "model_action",
                STATUS_READY if actions else STATUS_SKIPPED,
                (
                    f"a scripted policy replays {actions} action(s); no model is "
                    "involved and the episode is reproducible"
                    if actions
                    else "no actions were scripted, so no policy turn would happen"
                ),
                {"actions": actions},
            )
        )
        stages.append(
            RLVRStage(
                "verifier_selection",
                STATUS_READY if (policy.enabled and metadata) else STATUS_BLOCKED,
                (
                    f"{len(metadata)} verifier(s) are registered, {len(deterministic)} "
                    "deterministic; selection prefers a deterministic verifier and "
                    "never invokes an LLM when one can decide"
                    if policy.enabled and metadata
                    else (
                        "verification is switched off by policy"
                        if not policy.enabled
                        else "no verifier is registered"
                    )
                ),
                {
                    "registered": [item.verifier_id for item in metadata],
                    "deterministic": [item.verifier_id for item in deterministic],
                },
            )
        )
        stages.append(
            RLVRStage(
                "verification",
                STATUS_READY if metadata else STATUS_BLOCKED,
                (
                    "each expectation is frozen before its verifier runs, so a "
                    "goalpost cannot move after the score is known"
                    if metadata
                    else "nothing can verify an observation without a verifier"
                ),
            )
        )
        stages.append(
            RLVRStage(
                "reward_calculation",
                STATUS_READY if config.reward.enabled else STATUS_BLOCKED,
                (
                    f"the verifiable reward policy is enabled (partial credit "
                    f"capped at {config.reward.max_partial_credit})"
                    if config.reward.enabled
                    else "the verifiable reward policy is disabled"
                ),
                {"reward_config_version": config.reward.version},
            )
        )
        stages.append(
            RLVRStage(
                "reward_validation",
                STATUS_READY,
                "every reward is audited against the verifier snapshot, the frozen "
                "expectations and the pinned reward configuration",
                {"config_version": config.reward.version},
            )
        )
        if config.critique.enabled:
            stages.append(
                RLVRStage(
                    "critique_generation",
                    STATUS_READY,
                    f"the engine may speak for {len(config.critique.categories)} "
                    "category(ies) at severity "
                    f"{config.critique.min_severity} or above",
                    {"sources": list(config.critique.sources)},
                )
            )
            stages.append(
                RLVRStage(
                    "dataset_generation",
                    STATUS_READY,
                    "a critique dataset version would be built from the critiques "
                    "and their verified corrections",
                    {"rules": config.critique.to_mapping()},
                )
            )
        else:
            stages.append(
                RLVRStage(
                    "critique_generation",
                    STATUS_SKIPPED,
                    "critique generation is switched off for this configuration",
                )
            )
            stages.append(
                RLVRStage(
                    "dataset_generation",
                    STATUS_SKIPPED,
                    "no critique dataset is built when critique generation is off",
                )
            )
        validation = config.validate()
        stages.append(
            RLVRStage(
                "training_configuration",
                STATUS_READY if validation.valid else STATUS_BLOCKED,
                (
                    "the configuration is valid; a real run would still need the "
                    "optional training dependencies, a wired runner and an explicit "
                    "confirmation"
                    if validation.valid
                    else "the configuration is invalid: " + "; ".join(validation.errors)
                ),
                {
                    "dry_run": bool(config.dry_run),
                    "algorithm": config.algorithm,
                    "warnings": list(validation.warnings),
                },
            )
        )
        stages.append(
            RLVRStage(
                "evaluation",
                STATUS_SKIPPED,
                "an evaluation compares verifications against labels recorded "
                "before the run; a plan carries none, so nothing is scored",
            )
        )
        return RLVRPipelinePlan(
            config_fingerprint=config.fingerprint(), stages=tuple(stages), notes=tuple(notes)
        )

    # -- the dry run ----------------------------------------------------------------

    def dry_run(
        self,
        config: RLVRTrainingConfig,
        *,
        tasks: Sequence[Mapping[str, Any]] = (),
        dataset: CritiqueDatasetVersion | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Walk all ten stages on deterministic inputs and report what happened.

        Each task is a mapping with ``task_id``, ``actions`` (the scripted policy
        turns), ``script`` (the observations the mock environment returns) and
        ``expectations`` (``{"step:N": {...}, "task": {...}}``, the frozen
        expected result for a step and for the task as a whole).
        """
        trainer = self.trainer_for(config)
        method = self.method_for(config)
        rows = list(tasks or ())
        task_reports: list[dict[str, Any]] = []
        verifications: list[VerificationResult] = []
        rewards: list[VerifiedReward] = []
        critiques: list[CritiqueResult] = []
        for index, task in enumerate(rows):
            report = self._walk_task(trainer, config, index, task)
            task_reports.append(report)
            verifications.extend(report.pop("_results"))
            reward = report.pop("_reward")
            if reward is not None:
                rewards.append(reward)
            critiques.extend(report.pop("_critiques"))

        stages = self._executed_stages(
            trainer,
            config,
            method,
            rows,
            task_reports,
            verifications,
            rewards,
            critiques,
            dataset,
            labels,
        )
        blocked = tuple(stage.name for stage in stages if stage.blocked)
        return {
            "ok": not blocked,
            "dry_run": True,
            "config_fingerprint": config.fingerprint(),
            "blocked": list(blocked),
            "stages": [stage.to_dict() for stage in stages],
            "tasks": task_reports,
            "verification_summary": trainer.verification_report(verifications),
            "rewards": [item.to_dict() for item in rewards],
            "reward_total": round(sum(item.total for item in rewards), 6),
            "critiques": [item.to_dict() for item in critiques],
            "note": (
                "every stage ran on deterministic inputs; no model was loaded, no "
                "optimizer ran and no weights changed"
            ),
        }

    # -- one task -------------------------------------------------------------------

    def _walk_task(
        self,
        trainer: RLVRTrainer,
        config: RLVRTrainingConfig,
        index: int,
        task: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Roll out one task, verify it and derive its reward and critiques.

        The private ``_results``/``_reward``/``_critiques`` keys are the objects
        the caller aggregates; the public keys are the report.
        """
        task_id = _text(task.get("task_id")) or f"task-{index}"
        actions = [dict(item) for item in task.get("actions") or ()]
        script = [dict(item) for item in task.get("script") or ()]
        expectations = {
            str(key): dict(value)
            for key, value in dict(task.get("expectations") or {}).items()
            if isinstance(value, Mapping)
        }
        environment = MockEnvironment(
            script, max_steps=self.max_steps, metadata={"task_id": task_id}
        )
        policy = ScriptedPolicy(actions)
        rollout = self.runner.run(
            environment,
            policy,
            task={"task_id": task_id, **dict(task.get("task") or {})},
            seed=int(config.rl.seed),
            task_id=task_id,
        )
        questions = trainer.rollout_questions(
            rollout,
            expectations=expectations,
            task_type=_text(task.get("task_type")),
        )
        results = trainer.verify(questions)
        critiques: tuple[CritiqueResult, ...] = ()
        reward: VerifiedReward | None = None
        if results:
            critiques = trainer.critique(results)
            reward = trainer.reward_for(
                results,
                task_id=task_id,
                trajectory_id=rollout.trajectory_id or rollout.rollout_id,
            )
        selections = [
            trainer.select_verifier(
                action=dict(question.get("action") or {}),
                expected=dict(question.get("expected") or {}),
                observation=dict(question.get("observation") or {}),
                task_type=_text(question.get("task_type")),
            ).to_dict()
            for question in questions
        ]
        return {
            "task_id": task_id,
            "rollout_id": rollout.rollout_id,
            "trajectory_id": rollout.trajectory_id,
            "environment": {"kind": environment.kind, "read_only": bool(environment.read_only)},
            "actions": len(actions),
            "steps": rollout.length,
            "status": rollout.status,
            "selections": selections,
            "verifications": [item.to_dict() for item in results],
            "reward": reward.reward.to_dict() if reward is not None else None,
            "integrity": reward.integrity.to_dict() if reward is not None else None,
            "critiques": [item.to_dict() for item in critiques],
            "_results": results,
            "_reward": reward,
            "_critiques": critiques,
        }

    # -- stage readings -------------------------------------------------------------

    def _executed_stages(
        self,
        trainer: RLVRTrainer,
        config: RLVRTrainingConfig,
        method: CritiqueTrainingMethod,
        rows: Sequence[Mapping[str, Any]],
        task_reports: Sequence[Mapping[str, Any]],
        verifications: Sequence[VerificationResult],
        rewards: Sequence[VerifiedReward],
        critiques: Sequence[CritiqueResult],
        dataset: CritiqueDatasetVersion | None,
        labels: Mapping[str, str] | None,
    ) -> list[RLVRStage]:
        stages: list[RLVRStage] = []
        stages.append(
            RLVRStage(
                "environment",
                STATUS_DONE if rows else STATUS_SKIPPED,
                (
                    f"ran {len(rows)} deterministic, read-only episode(s)"
                    if rows
                    else "no task was supplied: nothing was rolled out"
                ),
                {"tasks": len(rows), "max_steps": self.max_steps},
            )
        )
        actions = sum(int(report.get("actions", 0)) for report in task_reports)
        steps = sum(int(report.get("steps", 0)) for report in task_reports)
        stages.append(
            RLVRStage(
                "model_action",
                STATUS_DONE if actions else STATUS_SKIPPED,
                (
                    f"a scripted policy produced {actions} action(s) over {steps} "
                    "step(s); no model was loaded"
                    if actions
                    else "no actions were scripted, so no policy turn happened"
                ),
                {"actions": actions, "steps": steps, "policy": "scripted"},
            )
        )
        selections = [item for report in task_reports for item in report.get("selections", ())]
        unselected = sum(1 for item in selections if not item.get("ok"))
        stages.append(
            RLVRStage(
                "verifier_selection",
                STATUS_DONE if verifications else STATUS_SKIPPED,
                (
                    f"selected a verifier for {len(selections)} question(s); "
                    f"{unselected} had no deterministic answer"
                    if verifications
                    else "no expectation was supplied, so nothing was selected"
                ),
                {
                    "selections": selections,
                    "fallback_used": any(
                        item.get("fallback_used") for item in selections
                    ),
                },
            )
        )
        report = trainer.verification_report(verifications)
        stages.append(
            RLVRStage(
                "verification",
                STATUS_DONE if verifications else STATUS_SKIPPED,
                (
                    f"{report['total']} verification(s): {report['passed']} passed, "
                    f"{report['failed']} failed, {report['partial']} partial, "
                    f"{report['inconclusive']} inconclusive"
                    if verifications
                    else "there was nothing verifiable in this run"
                ),
                {
                    "by_status": report["by_status"],
                    "verifiers": report["verifiers"],
                },
            )
        )
        stages.append(
            RLVRStage(
                "reward_calculation",
                STATUS_DONE if rewards else STATUS_SKIPPED,
                (
                    f"{len(rewards)} verifiable reward(s) totalling "
                    f"{sum(item.total for item in rewards):+.2f}"
                    if rewards
                    else "no verification produced a reward"
                ),
                {
                    "rewards": len(rewards),
                    "total": round(sum(item.total for item in rewards), 6),
                    "partial_credit_cap": config.reward.max_partial_credit,
                },
            )
        )
        integrity = [item.integrity for item in rewards]
        invalid = sum(1 for item in integrity if not item.trusted)
        stages.append(
            RLVRStage(
                "reward_validation",
                STATUS_DONE if rewards else STATUS_SKIPPED,
                (
                    f"audited {len(integrity)} reward(s): {len(integrity) - invalid} "
                    f"valid, {invalid} held or rejected"
                    if rewards
                    else "no reward was produced, so there was nothing to audit"
                ),
                {
                    "checked": len(integrity),
                    "findings": [
                        code for item in integrity for code in item.codes()
                    ],
                },
            )
        )
        if not config.critique.enabled:
            stages.append(
                RLVRStage(
                    "critique_generation",
                    STATUS_SKIPPED,
                    "critique generation is switched off for this configuration",
                )
            )
        else:
            by_category: dict[str, int] = {}
            for item in critiques:
                by_category[item.category] = by_category.get(item.category, 0) + 1
            stages.append(
                RLVRStage(
                    "critique_generation",
                    STATUS_DONE if critiques else STATUS_SKIPPED,
                    (
                        f"the engine produced {len(critiques)} structured critique(s)"
                        if critiques
                        else "nothing classifiable failed, so no critique was invented"
                    ),
                    {"critiques": len(critiques), "by_category": by_category},
                )
            )
        build: CritiqueDatasetVersion | None = None
        build_error = ""
        if config.critique.enabled and critiques:
            try:
                build = method.build_dataset(
                    "rlvr-dry-run",
                    critiques=critiques,
                    rules=CritiqueDatasetRules(min_severity=config.critique.min_severity),
                    description="built by an RLVR dry run from verified failures",
                    tags=("dry_run", "rlvr"),
                )
            except ValueError as error:
                build_error = str(error)
        if isinstance(build, CritiqueDatasetVersion):
            stages.append(
                RLVRStage(
                    "dataset_generation",
                    STATUS_DONE,
                    (
                        f"built critique dataset {build.dataset_version_id!r} with "
                        f"{len(build)} example(s)"
                    ),
                    {
                        "dataset_version_id": build.dataset_version_id,
                        "examples": len(build),
                        "accepted": len(build.accepted_examples()),
                        "splits": {
                            name: len(ids)
                            for name, ids in build.splits.items()
                            if ids
                        },
                        "statistics": build.statistics.to_dict(),
                        "validated": list(method.validate(build)),
                        "supplied": dataset.dataset_version_id if dataset else "",
                    },
                )
            )
        else:
            stages.append(
                RLVRStage(
                    "dataset_generation",
                    STATUS_SKIPPED,
                    (
                        "no critique dataset was built: " + build_error
                        if build_error
                        else "no critique was produced to build a dataset from"
                    ),
                    {"supplied": dataset.dataset_version_id if dataset else ""},
                )
            )
        validation = trainer.validate_config()
        estimate = trainer.estimate_resources(dataset=None)
        stages.append(
            RLVRStage(
                "training_configuration",
                STATUS_BLOCKED if not validation.valid else STATUS_DONE,
                (
                    "the configuration is valid; a real run would still need the "
                    "optional training dependencies, a wired runner and an explicit "
                    "confirmation"
                    if validation.valid
                    else "the configuration is invalid: " + "; ".join(validation.errors)
                ),
                {
                    "valid": bool(validation.valid),
                    "errors": list(validation.errors),
                    "warnings": list(validation.warnings),
                    "verifier_snapshot": trainer.verifier_snapshot(),
                    "resource_status": getattr(estimate, "status", ""),
                    "reward_config_version": trainer.reward_config_version,
                },
            )
        )
        if labels:
            evaluation = RLVREvaluator().evaluate(
                results=verifications,
                ground_truth=dict(labels),
                critiques=critiques,
                integrity=integrity,
                dataset_version=(dataset.dataset_version_id if dataset else ""),
            )
            stages.append(
                RLVRStage(
                    "evaluation",
                    STATUS_DONE,
                    (
                        f"scored {evaluation.verifications} verification(s) against "
                        f"{len(labels)} recorded label(s)"
                    ),
                    evaluation.to_dict(),
                )
            )
        else:
            stages.append(
                RLVRStage(
                    "evaluation",
                    STATUS_SKIPPED,
                    "an evaluation compares verifications against labels recorded "
                    "before the run; a dry run without labels scores nothing",
                    {"performed": False},
                )
            )
        return stages


def stage_map(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """The stage rows of a dry-run payload, keyed by name, for callers."""
    found: dict[str, Mapping[str, Any]] = {}
    for row in payload.get("stages", ()) or ():
        if isinstance(row, Mapping):
            found[_text(row.get("stage"))] = row
    return found


def greenlight(config: RLVRTrainingConfig, plan: RLVRPipelinePlan) -> dict[str, Any]:
    """Whether a real run is worth attempting, and what it would still need."""
    missing: list[str] = []
    validation = config.validate()
    missing.extend(validation.errors)
    if not config.dry_run:
        missing.append(
            "a real run needs the optional training dependencies, a wired runner, "
            "permission and an explicit confirmation"
        )
    return {
        "ok": plan.ok and validation.valid,
        "blocked_stages": list(plan.blocked()),
        "missing": missing,
        "dry_run": bool(config.dry_run),
        "note": (
            "dry_run is the development path and needs nothing; flipping it off is "
            "an operator decision, never something this pipeline does"
        ),
    }


__all__ = [
    "RLVR_PIPELINE_STAGES",
    "STATUS_BLOCKED",
    "STATUS_DONE",
    "STATUS_READY",
    "STATUS_SKIPPED",
    "RLVRPipeline",
    "RLVRPipelinePlan",
    "RLVRStage",
    "greenlight",
    "stage_map",
]


#: Public alias, named the way a reader looks for it.
RLVR_PIPELINE_STAGES = RLVR_STAGES
