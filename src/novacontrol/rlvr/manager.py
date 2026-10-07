"""The RLVR manager: Phase 19's orchestration, built on Phase 18's and Phase 16's.

This class subclasses :class:`~novacontrol.rlhf.manager.RLHFManager` for the same
reason Phase 18 subclassed the training manager: the hard parts of a training run
already exist and are inherited unchanged — the run store, the estimate-then-
confirm gate, the refusal to start an unsafe run, checkpoints, resume and cancel,
the model registry, and the rule that approval and promotion are explicit
operations. What RLVR adds is local:

  * three STORES (critiques, corrections, critique dataset versions),
  * a VERIFIER REGISTRY the run consults instead of an opinion,
  * a CRITIQUE-to-DATASET path through :class:`~novacontrol.rlvr.trainer.CritiqueTrainingMethod`,
  * the PIPELINE PLAN a dry run shows before anything starts,
  * a run whose configuration is :class:`RLVRTrainingConfig` and whose dataset is
    a critique dataset version.

The rails hold here too: nothing starts by accident, a real run needs the
optional training dependencies AND a wired runner AND permission AND an explicit
confirmation, dry runs are the development path, and no model is loaded to answer
a question. A verifier can be disabled — never edited into passing — and the
expected result behind a verdict is frozen before the verifier runs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, cast

from novacontrol.evaluation.models import AgentTrajectory
from novacontrol.evaluation.reward import RewardResult
from novacontrol.rlhf.backends import POLICY_OPTIMIZERS
from novacontrol.rlhf.config import RLTrainingConfig
from novacontrol.rlhf.evaluators import evaluator_for
from novacontrol.rlhf.manager import RLHFManager
from novacontrol.rlvr.config import RLVR_MODE, RLVRTrainingConfig
from novacontrol.rlvr.critique import CritiqueEngine
from novacontrol.rlvr.datasets import CritiqueDatasetRules
from novacontrol.rlvr.models import (
    CorrectedExample,
    CritiqueDatasetVersion,
    CritiqueResult,
    VerificationResult,
    _text,
)
from novacontrol.rlvr.pipeline import RLVRPipeline, stage_map
from novacontrol.rlvr.registry import VerifierRegistry
from novacontrol.rlvr.rewards import critique_reward
from novacontrol.rlvr.storage import (
    CorrectionRepository,
    CritiqueDatasetRepository,
    CritiqueRepository,
)
from novacontrol.rlvr.trainer import CritiqueTrainingMethod, RLVRTrainer
from novacontrol.rlvr.verifiers import Verifier
from novacontrol.training.config import TrainingConfig
from novacontrol.training.manager import RUN_CREATED
from novacontrol.training.models import ResourceEstimate, TrainingRun

#: Phase 19's own events. The run lifecycle events stay Phase 16's: an RLVR run
#: IS a training run, and a second vocabulary for the same lifecycle would be a
#: second thing to keep in sync.
RLVR_VERIFIER_REGISTERED = "rlvr.verifier_registered"
RLVR_CRITIQUE_RECORDED = "rlvr.critique_recorded"
RLVR_CORRECTION_RECORDED = "rlvr.correction_recorded"
RLVR_DATASET_BUILT = "rlvr.critique_dataset_built"
RLVR_DRY_RUN = "rlvr.dry_run_completed"
RLVR_RUN_CREATED = "rlvr.run_created"

#: What a run's ``algorithm`` field carries: the mode, which is what a reader
#: filters by. The policy algorithm lives in the configuration.
ALGORITHMS: tuple[str, ...] = (RLVR_MODE,)

#: The wrapper's own keys. Anything else that names a Phase 18 field is routed
#: into the ``rl`` block, so a flat ``base_model=...`` (what a Phase 18 caller
#: writes, and what a CLI ``--set`` produces) is understood rather than silently
#: dropped by the nested config.
_RLVR_KEYS = frozenset(
    {
        "mode",
        "rl",
        "verifiers",
        "reward",
        "critique",
        "critique_dataset_version",
        "correction_dataset_version",
        "verifier_ids",
        "task_types",
        "output_directory",
        "notes",
    }
)
_RL_KEYS = frozenset(RLTrainingConfig().to_mapping().keys())


class RLVRManager(RLHFManager):
    """Verifies outcomes, records critiques, builds critique datasets, runs RLVR."""

    def __init__(
        self,
        *,
        critiques: CritiqueRepository,
        corrections: CorrectionRepository,
        critique_datasets: CritiqueDatasetRepository,
        verifier_registry: VerifierRegistry | None = None,
        pipeline: RLVRPipeline | None = None,
        method: CritiqueTrainingMethod | None = None,
        corrector: Any = None,
        **rlhf_kwargs: Any,
    ) -> None:
        super().__init__(**rlhf_kwargs)
        self.critiques = critiques
        self.corrections = corrections
        self.critique_datasets = critique_datasets
        # NOT ``self.registry``: the inherited attribute is the MODEL registry
        # (``SFTModelRegistry``), which the shared lifecycle and reporting read.
        # A verifier is a check, not a model, so its registry lives beside it
        # under its own name instead of shadowing the parent's.
        self.verifiers_registry = (
            verifier_registry
            if verifier_registry is not None
            else VerifierRegistry(policy=self.rlvr_defaults.verifiers)
        )
        self.corrector = corrector
        self.method = (
            method
            if method is not None
            else CritiqueTrainingMethod(
                method="sft",
                config=self.rlvr_defaults.critique,
                corrector=corrector,
                reward_config=self.rlvr_defaults.reward,
            )
        )
        # ``self.rlvr_pipeline``, not ``self.pipeline``: the inherited attribute is
        # Phase 18's RLPipeline and the inherited lifecycle reads it. The RLVR
        # overrides of ``pipeline_plan``/``dry_run`` are the only callers.
        self.rlvr_pipeline = (
            pipeline
            if pipeline is not None
            else RLVRPipeline(
                trainer_factory=lambda config: self._rlvr_trainer(config, None),
                registry=self.verifiers_registry,
                corrector=corrector,
            )
        )

    # ── configuration ────────────────────────────────────────────────────────

    @property
    def rlvr_defaults(self) -> RLVRTrainingConfig:
        """This deployment's RLVR defaults, derived from the RL ones."""
        found = getattr(self, "_rlvr_defaults", None)
        if found is None:
            found = RLVRTrainingConfig(rl=self.rl_defaults)
            self._rlvr_defaults = found
        return found

    @rlvr_defaults.setter
    def rlvr_defaults(self, config: RLVRTrainingConfig) -> None:
        self._rlvr_defaults = config

    def resolve_rlvr_config(
        self,
        config: RLVRTrainingConfig | RLTrainingConfig | TrainingConfig | Mapping[str, Any] | None = None,
        *,
        algorithm: str = "",
    ) -> RLVRTrainingConfig:
        """This deployment's RLVR defaults, overridden by what the caller named."""
        defaults = dict(self.rlvr_defaults.to_mapping())
        merged: dict[str, Any] = dict(defaults)
        if isinstance(config, RLVRTrainingConfig):
            merged = {**defaults, **dict(config.to_mapping())}
        elif isinstance(config, RLTrainingConfig):
            merged = {**defaults, "rl": dict(config.to_mapping())}
        elif isinstance(config, TrainingConfig):
            # A Phase 16 caller's configuration, projected onto the RL schedule
            # the way ``RLTrainingConfig.from_training_config`` already does.
            merged = {
                **defaults,
                "rl": dict(RLTrainingConfig.from_training_config(config).to_mapping()),
            }
        else:
            for key, value in dict(config or {}).items():
                if value is None:
                    continue
                name = str(key)
                if name in {"rl", "verifiers", "reward", "critique"} and isinstance(
                    value, Mapping
                ):
                    merged[name] = {**dict(merged.get(name) or {}), **dict(value)}
                elif name in _RL_KEYS and name not in _RLVR_KEYS:
                    merged["rl"] = {**dict(merged.get("rl") or {}), name: value}
                else:
                    merged[name] = value
        if _text(algorithm):
            merged["rl"] = {**dict(merged.get("rl") or {}), "algorithm": _text(algorithm)}
        return RLVRTrainingConfig.from_mapping(merged).with_defaults(
            output_directory=str(self.output_root)
        )

    # ── verifiers ────────────────────────────────────────────────────────────

    def register_verifier(
        self, verifier: Verifier, *, replace_existing: bool = False
    ) -> dict[str, Any]:
        """Register a verifier (a plugin registers its domain checks here)."""
        try:
            metadata = self.verifiers_registry.register(
                verifier, replace=replace_existing
            )
        except ValueError as error:
            return {"ok": False, "reason": str(error)}
        self._announce(
            RLVR_VERIFIER_REGISTERED,
            {"verifier_id": metadata.verifier_id, "version": metadata.version},
        )
        return {"ok": True, "verifier": metadata.to_dict()}

    def verifiers(self) -> dict[str, Any]:
        """Every registered verifier, with the registry's integrity state."""
        return self.verifiers_registry.describe()

    def disable_verifier(self, verifier_id: str, *, reason: str = "") -> dict[str, Any]:
        if not verifier_id or not self.verifiers_registry.disable(
            verifier_id, reason=reason
        ):
            return {"ok": False, "reason": f"no verifier {verifier_id!r} is registered"}
        return {"ok": True, "verifier_id": verifier_id, "disabled": True, "reason": reason}

    def enable_verifier(self, verifier_id: str) -> dict[str, Any]:
        if not verifier_id or not self.verifiers_registry.enable(verifier_id):
            return {"ok": False, "reason": f"no verifier {verifier_id!r} is registered"}
        return {"ok": True, "verifier_id": verifier_id, "disabled": False}

    def verifier_snapshot(self) -> dict[str, str]:
        return self.verifiers_registry.snapshot()

    # ── verification and verifiable rewards ─────────────────────────────────

    def verify(
        self,
        questions: Sequence[Mapping[str, Any]],
        *,
        config: RLVRTrainingConfig | RLTrainingConfig | TrainingConfig | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Verify a batch of questions with the configured verifier policy."""
        cfg = self.resolve_rlvr_config(config)
        trainer = self._rlvr_trainer(cfg, None)
        results = trainer.verify(questions)
        return {
            "ok": True,
            "config_fingerprint": cfg.fingerprint(),
            "report": trainer.verification_report(results),
            "results": [item.to_dict() for item in results],
        }

    def reward_for(
        self,
        results: Sequence[VerificationResult | Mapping[str, Any]],
        *,
        config: RLVRTrainingConfig | RLTrainingConfig | TrainingConfig | Mapping[str, Any] | None = None,
        task_id: str = "",
        trajectory_id: str = "",
        correction_verified: bool = False,
    ) -> dict[str, Any]:
        """A verifiable reward from verifications, with its integrity verdict."""
        cfg = self.resolve_rlvr_config(config)
        trainer = self._rlvr_trainer(cfg, None)
        rows = tuple(
            item
            if isinstance(item, VerificationResult)
            else VerificationResult.from_dict(dict(item))
            for item in results
        )
        verified = trainer.reward_for(
            rows,
            task_id=task_id,
            trajectory_id=trajectory_id,
            correction_verified=correction_verified,
        )
        return {"ok": True, "verified_reward": verified.to_dict()}

    def critique_reward_for(
        self,
        critique: CritiqueResult | Mapping[str, Any],
        *,
        config: RLVRTrainingConfig | RLTrainingConfig | TrainingConfig | Mapping[str, Any] | None = None,
        subject_id: str = "",
    ) -> dict[str, Any]:
        """The reward a critique alone carries (Phase 15/18 integration)."""
        cfg = self.resolve_rlvr_config(config)
        row = (
            critique
            if isinstance(critique, CritiqueResult)
            else CritiqueResult.from_dict(dict(critique))
        )
        reward: RewardResult = critique_reward(
            row, config=cfg.reward, subject_id=subject_id
        )
        return {"ok": True, "reward": reward.to_dict()}

    def audit_reward(
        self,
        reward: RewardResult | Mapping[str, Any],
        *,
        verifications: Sequence[VerificationResult | Mapping[str, Any]] = (),
        config: RLVRTrainingConfig | RLTrainingConfig | TrainingConfig | Mapping[str, Any] | None = None,
        snapshot: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Audit a reward this manager did not build, against the same rules."""
        cfg = self.resolve_rlvr_config(config)
        trainer = self._rlvr_trainer(cfg, None)
        row = (
            reward
            if isinstance(reward, RewardResult)
            else RewardResult.from_dict(dict(reward))
        )
        rows = tuple(
            item
            if isinstance(item, VerificationResult)
            else VerificationResult.from_dict(dict(item))
            for item in verifications
        )
        check = trainer.validate_reward(
            row,
            verifications=rows,
            expected_digests={
                item.verification_id: item.expected_digest
                for item in rows
                if item.verification_id and item.expected_digest
            },
        )
        problems = trainer.registry.assert_unmodified(snapshot) if snapshot else ()
        for problem in problems:
            check = replace(
                check,
                status="invalid",
                findings=(
                    *check.findings,
                    type(check.findings[0])(
                        code="verifier_tampered", severity="error", detail=problem
                    )
                    if check.findings
                    else check.findings,
                ),
            )
        return {"ok": check.trusted, "check": check.to_dict()}

    # ── critiques ────────────────────────────────────────────────────────────

    def record_critiques(
        self,
        results: Sequence[VerificationResult | Mapping[str, Any]] = (),
        *,
        trajectory: AgentTrajectory | Mapping[str, Any] | None = None,
        evaluation: Any = None,
        config: RLVRTrainingConfig | RLTrainingConfig | TrainingConfig | Mapping[str, Any] | None = None,
        store: bool = True,
    ) -> dict[str, Any]:
        """Generate structured critiques from verification/trajectory evidence."""
        cfg = self.resolve_rlvr_config(config)
        trainer = self._rlvr_trainer(cfg, None)
        rows = tuple(
            item
            if isinstance(item, VerificationResult)
            else VerificationResult.from_dict(dict(item))
            for item in results
        )
        if trajectory is None:
            found = trainer.critique(rows, evaluation=evaluation)
        else:
            record = (
                trajectory
                if isinstance(trajectory, AgentTrajectory)
                else AgentTrajectory.from_dict(dict(trajectory))
            )
            found = trainer.critique(
                rows, trajectory=record, evaluation=evaluation
            )
        if store:
            self.critiques.save_many(found)
            for item in found:
                self._announce(
                    RLVR_CRITIQUE_RECORDED,
                    {
                        "critique_id": item.critique_id,
                        "trajectory_id": item.trajectory_id,
                        "category": item.category,
                        "severity": item.severity,
                    },
                )
        return {
            "ok": True,
            "critiques": [item.to_dict() for item in found],
            "summary": CritiqueEngine(cfg.critique).summarise(found).to_dict(),
        }

    def critiques_list(
        self,
        *,
        trajectory_id: str = "",
        category: str = "",
        severity: str = "",
        source: str = "",
        limit: int = 0,
        newest_first: bool = False,
    ) -> tuple[CritiqueResult, ...]:
        return self.critiques.list(
            trajectory_id=trajectory_id,
            category=category,
            severity=severity,
            source=source,
            limit=limit,
            newest_first=newest_first,
        )

    def critique_stats(self) -> dict[str, Any]:
        return {
            "total": len(self.critiques.rows()),
            "by_category": self.critiques.counts(),
        }

    def corrections_list(
        self,
        *,
        status: str = "",
        trajectory_id: str = "",
        limit: int = 0,
        newest_first: bool = False,
    ) -> tuple[CorrectedExample, ...]:
        return self.corrections.list(
            status=status,
            trajectory_id=trajectory_id,
            limit=limit,
            newest_first=newest_first,
        )

    def corrections_pending(self, limit: int = 0) -> tuple[CorrectedExample, ...]:
        """Corrections nothing settled: they wait for a person, they train nothing."""
        return self.corrections.pending(limit=limit)

    def correction_stats(self) -> dict[str, Any]:
        return {
            "total": len(self.corrections.rows()),
            "by_status": self.corrections.counts(),
            "pending": len(self.corrections.pending()),
        }

    def propose_corrections(
        self,
        *,
        critiques: Sequence[CritiqueResult | Mapping[str, Any]] = (),
        critique_ids: Sequence[str] = (),
        proposals: Mapping[str, Mapping[str, Any]] | None = None,
        original_inputs: Mapping[str, Mapping[str, Any]] | None = None,
        original_outputs: Mapping[str, Mapping[str, Any]] | None = None,
        config: RLVRTrainingConfig | RLTrainingConfig | TrainingConfig | Mapping[str, Any] | None = None,
        store: bool = True,
    ) -> dict[str, Any]:
        """Turn critiques plus proposed corrections into evidence-checked rows.

        A correction with a PASS verification is accepted; one that failed is
        rejected; one nobody could verify is held for review — never silently
        accepted. Hidden reasoning is rejected outright by the builder.
        """
        cfg = self.resolve_rlvr_config(config)
        rows = list(self._critique_rows(critiques, critique_ids))
        if not rows:
            return {"ok": False, "reason": "no critique was named or stored"}
        method = CritiqueTrainingMethod(
            method=self.method.method,
            config=cfg.critique,
            corrector=self.corrector,
            reward_config=cfg.reward,
        )

        def provider(critique: CritiqueResult, context: Mapping[str, Any]) -> Any:
            offered = dict((proposals or {}).get(critique.critique_id) or {})
            return offered or None

        found = method.corrections(
            rows,
            original_inputs=original_inputs,
            original_outputs=original_outputs,
            provider=provider,
        )
        if store:
            for item in found:
                self.corrections.save(item)
                self._announce(
                    RLVR_CORRECTION_RECORDED,
                    {
                        "example_id": item.example_id,
                        "critique_id": item.critique.critique_id,
                        "status": item.quality_status,
                    },
                )
        payload = {
            "ok": True,
            "corrections": [item.to_dict() for item in found],
            "accepted": sum(1 for item in found if item.accepted),
            "held": sum(1 for item in found if item.needs_review),
            "rejected": sum(1 for item in found if item.rejected),
        }
        return payload

    def _critique_rows(
        self,
        critiques: Sequence[CritiqueResult | Mapping[str, Any]],
        critique_ids: Sequence[str],
    ) -> tuple[CritiqueResult, ...]:
        found: list[CritiqueResult] = [
            item
            if isinstance(item, CritiqueResult)
            else CritiqueResult.from_dict(dict(item))
            for item in critiques
        ]
        for critique_id in critique_ids:
            stored = self.critiques.get(critique_id)
            if stored is not None:
                found.append(stored)
        return tuple(found)

    # ── critique datasets ─────────────────────────────────────────────────────

    def build_critique_dataset(
        self,
        name: str,
        *,
        critiques: Sequence[CritiqueResult | Mapping[str, Any]] = (),
        critique_ids: Sequence[str] = (),
        corrections: Sequence[CorrectedExample | Mapping[str, Any]] = (),
        correction_ids: Sequence[str] = (),
        rules: CritiqueDatasetRules | None = None,
        version: str = "",
        description: str = "",
        tags: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Build and store one immutable critique dataset version."""
        cfg = self.rlvr_defaults
        critique_rows = list(self._critique_rows(critiques, critique_ids))
        correction_rows: list[CorrectedExample] = [
            item
            if isinstance(item, CorrectedExample)
            else CorrectedExample.from_dict(dict(item))
            for item in corrections
        ]
        for example_id in correction_ids:
            stored = self.corrections.get(example_id)
            if stored is not None:
                correction_rows.append(stored)
        if not critique_rows and not correction_rows:
            return {
                "ok": False,
                "reason": (
                    "a critique dataset needs at least one critique or correction; "
                    "nothing was named"
                ),
            }
        chosen_rules = rules if rules is not None else CritiqueDatasetRules()
        existing = [item.version for item in self.critique_datasets.versions(name)]
        try:
            dataset = self.method.build_dataset(
                name,
                critiques=critique_rows,
                corrections=correction_rows,
                rules=chosen_rules,
                version=version,
                description=description,
                tags=tags,
                existing_versions=existing,
            )
        except ValueError as error:
            return {"ok": False, "reason": str(error)}
        issues = self.method.validate(dataset)
        if not dataset.examples:
            return {
                "ok": False,
                "reason": "every candidate row was filtered out by the dataset rules",
                "skipped": dict(dataset.statistics.skipped),
            }
        try:
            self.critique_datasets.save(dataset)
        except ValueError as error:
            return {"ok": False, "reason": str(error)}
        self._announce(
            RLVR_DATASET_BUILT,
            {
                "dataset_version_id": dataset.dataset_version_id,
                "examples": len(dataset),
                "accepted": len(dataset.accepted_examples()),
            },
        )
        return {
            "ok": True,
            "dataset": dataset.to_dict(),
            "examples": len(dataset),
            "accepted": len(dataset.accepted_examples()),
            "splits": {name: len(ids) for name, ids in dataset.splits.items() if ids},
            "statistics": dataset.statistics.to_dict(),
            "issues": list(issues),
            "rules": dict(cfg.critique.to_mapping()),
        }

    def critique_dataset(self, dataset_version: str) -> CritiqueDatasetVersion | None:
        return self.critique_datasets.get(dataset_version)

    def critique_datasets_list(
        self, *, name: str = "", limit: int = 0
    ) -> tuple[CritiqueDatasetVersion, ...]:
        return self.critique_datasets.list(name=name, limit=limit)

    def dataset_preview(self, dataset_version: str, *, limit: int = 10) -> dict[str, Any]:
        """A page of one critique dataset, with its validation verdict."""
        dataset = self.critique_dataset(dataset_version)
        if dataset is None:
            return {"ok": False, "reason": f"no critique dataset {dataset_version!r}"}
        count = limit if limit and limit > 0 else 10
        return {
            "ok": True,
            "dataset": {
                "dataset_version_id": dataset.dataset_version_id,
                "examples": len(dataset),
                "accepted": len(dataset.accepted_examples()),
                "splits": {
                    name: len(ids) for name, ids in dataset.splits.items() if ids
                },
                "by_kind": {
                    kind: len(dataset.by_kind(kind))
                    for kind in {row.kind for row in dataset.examples}
                },
                "statistics": dataset.statistics.to_dict(),
                "fingerprint": dataset.fingerprint(),
            },
            "rows": [row.to_dict() for row in dataset.examples[:count]],
            "issues": list(self.method.validate(dataset)),
        }

    def validate_critique_dataset(self, dataset_version: str) -> dict[str, Any]:
        dataset = self.critique_dataset(dataset_version)
        if dataset is None:
            return {"ok": False, "reason": f"no critique dataset {dataset_version!r}"}
        issues = list(self.method.validate(dataset))
        return {"ok": not issues, "dataset_version_id": dataset_version, "issues": issues}

    def to_preference_pairs(
        self, dataset_version: str, *, verified_only: bool = True
    ) -> dict[str, Any]:
        """The Phase 17 pairs this dataset yields — corrections over failures."""
        dataset = self.critique_dataset(dataset_version)
        if dataset is None:
            return {"ok": False, "reason": f"no critique dataset {dataset_version!r}"}
        pairs = self.method.to_preference(dataset, verified_only=verified_only)
        return {
            "ok": True,
            "dataset_version_id": dataset_version,
            "verified_only": bool(verified_only),
            "pairs": [item.to_dict() for item in pairs],
        }

    def held_critique_examples(
        self, dataset_version: str
    ) -> tuple[CorrectedExample, ...]:
        """Stored corrections this dataset held back, so a person can settle them."""
        dataset = self.critique_dataset(dataset_version)
        if dataset is None:
            return ()
        held_ids = {
            row.critique_id for row in dataset.examples if not row.accepted
        }
        return tuple(
            item
            for item in self.corrections.pending()
            if item.critique.critique_id in held_ids
        )

    # ── pipeline and dry run ─────────────────────────────────────────────────

    def pipeline_plan(
        self,
        config: Mapping[str, Any] | None = None,
        dataset_version: str = "",
        *,
        tasks: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        cfg = self.resolve_rlvr_config(config)
        if dataset_version:
            cfg = replace(cfg, critique_dataset_version=dataset_version)
        dataset = (
            self.critique_dataset(cfg.critique_dataset_version)
            if cfg.critique_dataset_version
            else None
        )
        plan = self.rlvr_pipeline.plan(cfg, dataset=dataset, tasks=tasks)
        return {
            "ok": plan.ok,
            "mode": RLVR_MODE,
            "dataset": dataset.dataset_version_id if dataset else "",
            "plan": plan.to_dict(),
        }

    def estimate_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        """Price an RLVR configuration without starting anything."""
        cfg = self.resolve_rlvr_config(config)
        validation = cfg.validate()
        estimate = self.rl_estimator.estimate(cfg.rl, dataset=None)
        return {
            "valid": validation.valid,
            "errors": list(validation.errors),
            "warnings": list(validation.warnings),
            "mode": RLVR_MODE,
            "algorithm": cfg.algorithm,
            "policy_optimizer": dict(POLICY_OPTIMIZERS.get(cfg.algorithm, {})),
            "effective_config": cfg.to_mapping(),
            "estimate": estimate.to_dict(),
            "dry_run": bool(cfg.dry_run),
        }

    def dry_run(
        self,
        model: str = "",
        dataset_version: str = "",
        *,
        config: RLVRTrainingConfig | RLTrainingConfig | TrainingConfig | Mapping[str, Any] | None = None,
        mode: str = "",
        algorithm: str = "",
        tasks: Sequence[Mapping[str, Any]] = (),
        labels: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Walk all ten RLVR stages on deterministic inputs; train nothing.

        The shape matches the Phase 18 dry run so one operator habit works in
        both phases: ``model`` names the base model, ``dataset_version`` the
        critique dataset, and the RLVR configuration arrives as ``config``.
        ``mode`` is accepted for that compatibility and refused when it names
        anything other than RLVR — this pipeline only ever simulates RLVR.
        """
        if _text(mode) and _text(mode) != RLVR_MODE:
            return {
                "ok": False,
                "refused": True,
                "mode": RLVR_MODE,
                "errors": [f"an RLVR dry run cannot run in mode {mode!r}"],
            }
        cfg = self.resolve_rlvr_config(config, algorithm=algorithm)
        cfg = replace(
            cfg,
            rl=replace(cfg.rl, base_model=_text(model) or cfg.rl.base_model),
            critique_dataset_version=dataset_version or cfg.critique_dataset_version,
        )
        dataset = (
            self.critique_dataset(cfg.critique_dataset_version)
            if cfg.critique_dataset_version
            else None
        )
        result = self.rlvr_pipeline.dry_run(
            cfg, tasks=tasks, dataset=dataset, labels=labels
        )
        self._announce(
            RLVR_DRY_RUN,
            {
                "ok": result["ok"],
                "tasks": len(tasks),
                "reward_total": result["reward_total"],
                "critiques": len(result["critiques"]),
            },
        )
        return {
            "ok": result["ok"],
            "mode": RLVR_MODE,
            "result": result,
            "stages": stage_map(result),
            "config_fingerprint": cfg.fingerprint(),
        }

    # ── runs ─────────────────────────────────────────────────────────────────

    def create_run(
        self,
        model: str,
        critique_dataset_version: str,
        *,
        config: RLVRTrainingConfig | RLTrainingConfig | TrainingConfig | Mapping[str, Any] | None = None,
        name: str = "",
    ) -> TrainingRun:
        """Validate an RLVR configuration and critique dataset, store a CREATED run."""
        dataset = self.critique_dataset(critique_dataset_version)
        if dataset is None:
            raise ValueError(
                f"no critique dataset version {critique_dataset_version!r}: build it first"
            )
        if not dataset.examples:
            raise ValueError(f"critique dataset {critique_dataset_version!r} has no examples")
        cfg = self.resolve_rlvr_config(config)
        cfg = replace(
            cfg,
            rl=replace(
                cfg.rl, base_model=_text(model) or cfg.rl.base_model
            ),
            critique_dataset_version=critique_dataset_version,
        )
        validation = cfg.validate()
        if not validation.valid:
            raise ValueError(
                "invalid RLVR training configuration: " + "; ".join(validation.errors)
            )
        issues = list(self.method.validate(dataset))
        blocking = [
            item
            for item in issues
            if "no accepted" in item or "needs at least one" in item
        ]
        if blocking:
            raise ValueError(
                f"critique dataset {critique_dataset_version!r} cannot train an RLVR "
                "run: " + "; ".join(blocking)
            )
        estimate = self.rl_estimator.estimate(cfg.rl, dataset=None)
        run = TrainingRun(
            name=(
                name
                or f"{cfg.base_model or 'model'}+rlvr/{cfg.algorithm}@{dataset.version}"
            ),
            model=cfg.base_model,
            dataset_version=critique_dataset_version,
            dataset_type="critique",
            training_config=self._stored_config(cfg, critique_dataset_version),
            estimate=estimate.to_dict(),
            backend="dry_run" if cfg.dry_run else estimate.backend,
            algorithm=RLVR_MODE,
            rl_metrics={
                "mode": RLVR_MODE,
                "algorithm": cfg.algorithm,
                "simulated": bool(
                    cfg.dry_run
                    or not POLICY_OPTIMIZERS.get(cfg.algorithm, {}).get("learns", False)
                ),
                "verifiers": [
                    item.verifier_id for item in self.verifiers_registry.list()
                ],
                "verifier_snapshot": self.verifier_snapshot(),
                "reward_config_version": cfg.reward.version,
                "critique_dataset_version": critique_dataset_version,
                "critique_audit": {"issues": list(issues)},
            },
            random_seed=cfg.rl.seed,
        )
        self.runs.save(run)
        self._announce(
            RUN_CREATED,
            {
                "run_id": run.run_id,
                "dataset_version": critique_dataset_version,
                "algorithm": RLVR_MODE,
                "dry_run": cfg.dry_run,
            },
        )
        return run

    def rlvr_runs(self, *, status: str = "", limit: int = 0) -> tuple[TrainingRun, ...]:
        """The RLVR runs this manager owns."""
        found = [
            run
            for run in self.runs_list(status=status, dataset_version="", limit=0)
            if run.algorithm == RLVR_MODE
        ]
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)

    # ── the seams ────────────────────────────────────────────────────────────

    def _dataset_for(self, run: TrainingRun) -> Any:
        if run.algorithm == RLVR_MODE or run.dataset_type == "critique":
            found = self.critique_dataset(run.dataset_version)
            if found is not None:
                return found
        return super()._dataset_for(run)

    def _config_from_run(self, run: TrainingRun) -> Any:
        if run.algorithm == RLVR_MODE or "rlvr" in str(run.training_config.get("mode", "")):
            return RLVRTrainingConfig.from_mapping(run.training_config).with_defaults(
                output_directory=str(self.output_root)
            )
        return super()._config_from_run(run)

    def _rlvr_trainer(self, cfg: RLVRTrainingConfig, run: TrainingRun | None) -> RLVRTrainer:
        if self._rl_trainer_factory is not None:
            found = self._rl_trainer_factory(cfg, run)  # type: ignore[arg-type]
            if found is not None:
                return cast(RLVRTrainer, found)
        evaluator = evaluator_for(
            cfg.rl.evaluator, judge=self._judge, policy=cfg.rl.reward_policy
        )
        return RLVRTrainer(
            cfg,
            estimator=self.estimator,
            runner=self._runner,
            evaluator=evaluator,
            registry=self.verifiers_registry,
            corrector=self.corrector,
        )

    def _rl_trainer(self, cfg: Any, run: TrainingRun | None) -> Any:
        if isinstance(cfg, RLVRTrainingConfig):
            return self._rlvr_trainer(cfg, run)
        return super()._rl_trainer(cfg, run)

    def _trainer_for(self, cfg: TrainingConfig, run: TrainingRun | None = None) -> Any:
        if run is not None and run.algorithm == RLVR_MODE:
            return self._rlvr_trainer(self._config_from_run(run), run)
        return super()._trainer_for(cfg, run)

    def _estimate_for(
        self, cfg: TrainingConfig, dataset: Any, run: TrainingRun | None = None
    ) -> ResourceEstimate:
        if run is not None and run.algorithm == RLVR_MODE:
            return self.rl_estimator.estimate(self._config_from_run(run).rl, dataset=None)
        return super()._estimate_for(cfg, dataset, run)

    @staticmethod
    def _stored_config(
        cfg: RLVRTrainingConfig | RLTrainingConfig, dataset_version: str
    ) -> dict[str, Any]:
        """A run row a reader of either phase can open.

        The Phase 16/18 fields stay at the top level (so the shared lifecycle
        validator reads them), the Phase 19 blocks ride beside their Phase 18
        projection, and ``dataset_version`` aliases the critique dataset the run
        learned from. A Phase 18 object arrives unwrapped and is wrapped here,
        which keeps the override compatible with the method it overrides.
        """
        wrapped = cfg if isinstance(cfg, RLVRTrainingConfig) else RLVRTrainingConfig(rl=cfg)
        stored = dict(wrapped.rl.to_mapping())
        stored.update(wrapped.to_mapping())
        stored.pop("dataset_type", None)
        stored["dataset_version"] = dataset_version
        return stored

    # ── reporting ────────────────────────────────────────────────────────────

    def rlvr_status(self) -> dict[str, Any]:
        """The Phase 19 headline state: verifiers, critiques, datasets, runs."""
        metadata = self.verifiers_registry.list()
        return {
            "mode": RLVR_MODE,
            "verifiers": len(metadata),
            "deterministic": sum(1 for item in metadata if item.deterministic),
            "categories": sorted({item.category for item in metadata}),
            "critiques": len(self.critiques.rows()),
            "corrections": len(self.corrections.rows()),
            "corrections_pending": len(self.corrections.pending()),
            "critique_datasets": len(self.critique_datasets.rows()),
            "runs": len(self.rlvr_runs()),
            "dry_run": bool(self.rlvr_defaults.dry_run),
            "reward_config_version": self.rlvr_defaults.reward.version,
            "critique_method": self.method.name,
            "note": (
                "RLVR and critique learning are optional, experimental paths: "
                "normal operation does not use them, nothing starts automatically "
                "and a verifier can be disabled but never talked into passing"
            ),
        }

    def status(self) -> dict[str, Any]:
        return {**super().status(), "rlvr": self.rlvr_status()}

    def rlvr_summary(self) -> dict[str, Any]:
        payload = self.rlvr_status()
        payload["critique_stats"] = self.critique_stats()
        payload["correction_stats"] = self.correction_stats()
        payload["datasets"] = [
            item.dataset_version_id for item in self.critique_datasets_list(limit=10)
        ]
        payload["critiques_newest"] = [
            {
                "critique_id": item.critique_id,
                "category": item.category,
                "severity": item.severity,
                "source": item.source,
            }
            for item in self.critiques_list(limit=10, newest_first=True)
        ]
        payload["runs_newest"] = [
            {
                "run_id": run.run_id,
                "status": run.status,
                "dataset_version": run.dataset_version,
                "algorithm": run.algorithm,
            }
            for run in self.rlvr_runs(limit=20)
        ]
        return payload

    def summary(self) -> dict[str, Any]:
        return {**super().summary(), "rlvr": self.rlvr_summary()}


__all__ = [
    "ALGORITHMS",
    "RLVR_CRITIQUE_RECORDED",
    "RLVR_CORRECTION_RECORDED",
    "RLVR_DATASET_BUILT",
    "RLVR_DRY_RUN",
    "RLVR_RUN_CREATED",
    "RLVR_VERIFIER_REGISTERED",
    "RLVRManager",
]

