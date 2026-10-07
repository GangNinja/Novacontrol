"""The RLVR trainer and the critique training method.

:class:`RLVRTrainer` extends Phase 18's :class:`~novacontrol.rlhf.backends.RLTrainer`
rather than replacing it: the schedule, the run lifecycle, checkpoints, resume,
cancellation and the dry-run walk are inherited unchanged, and what RLVR adds is
local to this file:

  * a VERIFIER REGISTRY the run consults instead of an opinion,
  * a VERIFICATION step that turns observations into
    :class:`~novacontrol.rlvr.models.VerificationResult` rows,
  * a :class:`~novacontrol.rlvr.rewards.VerifiableRewardProvider` that turns
    those rows into a configured reward, with evidence,
  * a :class:`~novacontrol.rlvr.rewards.VerifiableRewardValidator` that audits
    the reward before it teaches anything,
  * a :class:`~novacontrol.rlvr.critique.CritiqueEngine` that produces structured
    critiques for what failed.

The trainer never loads a model and never starts real training: ``dry_run``
walks the schedule over verifiable rewards and labels every metric as simulated.
A real run is refused by the inherited machinery unless the optional training
dependencies, a wired runner and an explicit confirmation are all present.

:class:`CritiqueTrainingMethod` is the other half: the pluggable target a
critique dataset is projected onto (SFT examples, preference pairs, reward
examples). Phase 19 ships the projections and their validation; a future phase
runs the optimization.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from novacontrol.evaluation.reward import RewardResult
from novacontrol.rlhf.backends import RLTrainer
from novacontrol.rlhf.models import (
    FeedbackStatus,
    RewardDatasetVersion,
    RewardExample,
    RewardSource,
)
from novacontrol.rlvr.config import (
    RLVR_MODE,
    CritiqueConfig,
    RLVRTrainingConfig,
    VerifiableRewardConfig,
)
from novacontrol.rlvr.critique import CritiqueEngine
from novacontrol.rlvr.datasets import (
    CorrectedExampleBuilder,
    CritiqueDatasetBuilder,
    CritiqueDatasetRequest,
    CritiqueDatasetRules,
)
from novacontrol.rlvr.models import (
    CorrectedExample,
    CritiqueCategory,
    CritiqueDatasetVersion,
    CritiqueExample,
    CritiqueResult,
    CritiqueSeverity,
    CritiqueSource,
    VerificationRequest,
    VerificationResult,
    VerificationStatus,
    VerifierMetadata,
    _text,
)
from novacontrol.rlvr.registry import (
    SelectionDecision,
    VerifierRegistry,
    VerifierSelector,
)
from novacontrol.rlvr.rewards import (
    VerifiableRewardProvider,
    VerifiableRewardValidator,
    critique_reward,
)
from novacontrol.training.backends import TrainingCallbacks
from novacontrol.training.config import TrainingConfigValidation
from novacontrol.training.models import (
    SFTTrainingExample,
    TrainingRun,
)


@dataclass(frozen=True, slots=True)
class VerifiedReward:
    """One reward and its integrity verdict, kept together so neither drifts."""

    reward: RewardResult
    integrity: Any
    verifications: tuple[VerificationResult, ...] = ()

    @property
    def trusted(self) -> bool:
        return bool(getattr(self.integrity, "trusted", False))

    @property
    def total(self) -> float:
        return self.reward.total_reward

    def to_dict(self) -> dict[str, Any]:
        return {
            "reward": self.reward.to_dict(),
            "integrity": self.integrity.to_dict(),
            "verifications": [item.to_dict() for item in self.verifications],
        }


class RLVRTrainer(RLTrainer):
    """Learning from verifiable rewards: a verifier decides, then a reward teaches."""

    mode = RLVR_MODE
    backend = "rlvr"

    def __init__(
        self,
        config: RLVRTrainingConfig,
        *,
        estimator: Any = None,
        capabilities: Any = None,
        runner: Any = None,
        optimizer: Any = None,
        evaluator: Any = None,
        builder: Any = None,
        registry: VerifierRegistry | None = None,
        provider: VerifiableRewardProvider | None = None,
        validator: VerifiableRewardValidator | None = None,
        critique_engine: CritiqueEngine | None = None,
        corrector: Any = None,
    ) -> None:
        self.rlvr_config = config
        self.registry = (
            registry
            if registry is not None
            else VerifierRegistry(policy=config.verifiers)
        )
        self.selector = VerifierSelector(self.registry)
        verifiable = (
            provider
            if provider is not None
            else VerifiableRewardProvider(config.reward, registry=self.registry)
        )
        self.reward_validator = (
            validator
            if validator is not None
            else VerifiableRewardValidator(config.reward, registry=self.registry)
        )
        self.critique_engine = (
            critique_engine
            if critique_engine is not None
            else CritiqueEngine(config.critique)
        )
        self.corrector = corrector
        super().__init__(
            config.as_rl_training_config(),
            estimator=estimator,
            capabilities=capabilities,
            runner=runner,
            optimizer=optimizer,
            provider=verifiable,
            evaluator=evaluator,
            builder=builder,
        )
        #: The provider the parent stored IS the verifiable one; keep the name.
        self.reward_provider: VerifiableRewardProvider = verifiable

    # -- identity and validation -------------------------------------------------

    @property
    def name(self) -> str:
        return f"{self.mode}:{self.algorithm}"

    @property
    def reward_config_version(self) -> str:
        return self.rlvr_config.reward.version

    def verifiers(self) -> tuple[VerifierMetadata, ...]:
        """The registered verifiers, narrowed by the run's own allow-list."""
        wanted = {item for item in self.rlvr_config.verifier_ids if item}
        found = tuple(
            item
            for item in self.registry.list()
            if not wanted or item.verifier_id in wanted
        )
        return found

    def verifier_snapshot(self) -> dict[str, str]:
        """Every registered verifier's code fingerprint, for tamper detection."""
        return self.registry.snapshot()

    def validate_config(self) -> TrainingConfigValidation:
        shared = self.rlvr_config.validate()
        problems = list(shared.errors)
        problems.extend(self._verifier_problems())
        if not self.provider.is_available():
            problems.extend(
                f"reward provider: {item}" for item in self.provider.missing_requirements()
            )
        if not self.evaluator.is_available():
            problems.extend(
                f"evaluator: {item}" for item in self.evaluator.missing_requirements()
            )
        return TrainingConfigValidation(
            errors=tuple(problems), warnings=tuple(shared.warnings)
        )

    def _verifier_problems(self) -> tuple[str, ...]:
        problems: list[str] = []
        if not self.registry.policy.enabled:
            problems.append("verification is switched off by verifier policy")
        if not self.registry.list():
            problems.append("no verifier is registered")
        for verifier_id in self.rlvr_config.verifier_ids:
            problems.extend(
                f"verifier {verifier_id!r}: {item}"
                for item in self.registry.validate(verifier_id)
            )
        if self.rlvr_config.verifiers.deterministic_only and not any(
            item.deterministic for item in self.registry.list()
        ):
            problems.append(
                "deterministic_only is set but no deterministic verifier is registered"
            )
        return tuple(problems)

    # -- verification ------------------------------------------------------------

    def select_verifier(
        self,
        *,
        action: Mapping[str, Any] | None = None,
        expected: Mapping[str, Any] | None = None,
        observation: Mapping[str, Any] | None = None,
        task_type: str = "",
    ) -> SelectionDecision:
        """Which verifier would decide this question, and why (or why not)."""
        return self.selector.select(
            action=action, expected=expected, observation=observation, task_type=task_type
        )

    def verify_row(self, question: Mapping[str, Any]) -> VerificationResult:
        """One checkable question in, one verifier's verdict out.

        The expectation is FROZEN before the verifier runs (its digest is
        recomputed from the content), so a question whose expected result was
        edited after the fact is refused by the verifier's own ``validate``.
        A question no deterministic verifier can answer yields an INCONCLUSIVE
        result naming the reason — never a guess.
        """
        raw = dict(question)
        request = VerificationRequest.from_dict(raw)
        selection: SelectionDecision | None = None
        if not request.verifier_id:
            selection = self.select_verifier(
                action=request.action,
                expected=request.expected,
                observation=request.observation,
                task_type=_text(raw.get("task_type")),
            )
            if not selection.ok:
                return self._unselected_result(request, selection)
            request = replace(request, verifier_id=selection.verifier_id)
        problems = self.registry.validate(request.verifier_id)
        verifier = self.registry.get(request.verifier_id)
        if problems or verifier is None:
            return self._error_result(
                request,
                problems or (f"no verifier {request.verifier_id!r} is registered",),
            )
        request = request.frozen()
        problems = verifier.validate(request)
        if problems:
            return self._error_result(request, problems)
        result = verifier.verify(request)
        return self._stamp(result, request)

    def verify(self, questions: Sequence[Mapping[str, Any]]) -> tuple[VerificationResult, ...]:
        """Verify a batch of questions, in order, never raising."""
        return tuple(self.verify_row(question) for question in questions)

    def rollout_questions(
        self,
        rollout: Any,
        *,
        expectations: Mapping[str, Mapping[str, Any]] | None = None,
        task_type: str = "",
    ) -> tuple[dict[str, Any], ...]:
        """The checkable questions a recorded rollout supports, in order.

        ``expectations`` maps a step id (``"step:0"``) or the whole task
        (``"task"``) to the expected result; a step that carries its own
        ``expected`` in its metadata is used when none is passed for it. A
        rollout whose steps were never given expectations yields no questions
        rather than invented ones. Each question is frozen before it is asked.
        """
        wanted = dict(expectations or {})
        questions: list[dict[str, Any]] = []
        task_id = _text(getattr(rollout, "task_id", "")) or _text(
            getattr(rollout, "rollout_id", "")
        )
        trajectory_id = _text(getattr(rollout, "trajectory_id", "")) or _text(
            getattr(rollout, "rollout_id", "")
        )
        steps = tuple(getattr(rollout, "steps", ()) or ())
        for step in steps:
            index = getattr(step, "index", "")
            step_id = f"step:{index}"
            expected = (
                wanted.get(step_id)
                or wanted.get(str(index))
                or dict(dict(getattr(step, "metadata", {}) or {}).get("expected") or {})
            )
            if not expected:
                continue
            questions.append(
                {
                    "task_id": task_id,
                    "trajectory_id": trajectory_id,
                    "step_id": step_id,
                    "scope": "step",
                    "action": dict(getattr(step, "action", {}) or {}),
                    "expected": dict(expected),
                    "observation": dict(getattr(step, "observation", {}) or {}),
                    "task_type": task_type,
                }
            )
        final = wanted.get("task") or wanted.get("final")
        terminal = getattr(rollout, "terminal_step", None)
        if terminal is None and steps:
            # An episode can end because the policy stopped rather than because
            # the environment said ``done``; the last recorded step is then the
            # outcome state. The EXPECTATION still comes from the caller, so this
            # reads evidence that exists instead of asking a verifier to judge
            # an empty observation and reporting ``inconclusive`` for a task
            # whose result was never in doubt.
            terminal = steps[-1]
        if final and terminal is not None:
            questions.append(
                {
                    "task_id": task_id,
                    "trajectory_id": trajectory_id,
                    "scope": "task",
                    "action": dict(getattr(terminal, "action", {}) or {}),
                    "expected": dict(final),
                    "observation": dict(getattr(terminal, "observation", {}) or {}),
                    "task_type": task_type,
                }
            )
        return tuple(questions)

    def verify_rollout(
        self,
        rollout: Any,
        *,
        expectations: Mapping[str, Mapping[str, Any]] | None = None,
        task_type: str = "",
    ) -> tuple[VerificationResult, ...]:
        """Verify a recorded rollout step by step and as a whole."""
        return self.verify(
            self.rollout_questions(
                rollout, expectations=expectations, task_type=task_type
            )
        )

    def _unselected_result(
        self, request: VerificationRequest, selection: SelectionDecision
    ) -> VerificationResult:
        return VerificationResult(
            task_id=request.task_id,
            trajectory_id=request.trajectory_id,
            step_id=request.step_id,
            verifier_id="",
            verifier_version="",
            status=VerificationStatus.INCONCLUSIVE.value,
            passed=False,
            score=None,
            scope=request.scope,
            evidence=(),
            expected=dict(request.expected),
            observed=dict(request.observation),
            expected_digest=request.expected_digest,
            error_category="",
            confidence=None,
            detail=f"no verifier was selected: {selection.reason}",
        )

    def _error_result(
        self, request: VerificationRequest, problems: Sequence[str]
    ) -> VerificationResult:
        return VerificationResult(
            task_id=request.task_id,
            trajectory_id=request.trajectory_id,
            step_id=request.step_id,
            verifier_id=request.verifier_id,
            verifier_version="",
            status=VerificationStatus.ERROR.value,
            passed=False,
            score=None,
            scope=request.scope,
            evidence=tuple(str(item) for item in problems),
            expected=dict(request.expected),
            observed=dict(request.observation),
            expected_digest=request.expected_digest,
            error_category="other",
            confidence=None,
            detail="; ".join(str(item) for item in problems),
        )

    @staticmethod
    def _stamp(
        result: VerificationResult, request: VerificationRequest
    ) -> VerificationResult:
        """A verdict that carries the identity fields of the question asked."""
        if (
            result.task_id == request.task_id
            and result.trajectory_id == request.trajectory_id
            and result.step_id == request.step_id
            and result.expected_digest == request.expected_digest
        ):
            return result
        return VerificationResult.from_dict(
            {
                **result.to_dict(),
                "task_id": result.task_id or request.task_id,
                "trajectory_id": result.trajectory_id or request.trajectory_id,
                "step_id": result.step_id or request.step_id,
                "scope": request.scope or result.scope,
                "expected": dict(result.expected or request.expected),
                "observed": dict(result.observed or request.observation),
                "expected_digest": request.expected_digest,
            }
        )

    # -- verifiable rewards -------------------------------------------------------

    def reward_for(
        self,
        results: Sequence[VerificationResult],
        *,
        task_id: str = "",
        trajectory_id: str = "",
        critique: CritiqueResult | None = None,
        correction_verified: bool = False,
    ) -> VerifiedReward:
        """A reward built from verifications, with its integrity verdict.

        The reward is derived by the configured provider (no weight lives in
        this file) and then audited by the configured validator against the
        verifier snapshot the run started with. Both travel together: a reward
        that failed its audit is returned with the findings rather than hidden.
        """
        reward = self.reward_provider.reward_for(
            results,
            subject_id=task_id or trajectory_id,
            task_id=task_id,
            trajectory_id=trajectory_id,
            critique=critique,
            correction_verified=correction_verified,
        )
        integrity = self.reward_validator.check(
            reward,
            verifications=results,
            snapshot=self.verifier_snapshot(),
            config_version=self.reward_config_version,
            expected_digests={
                item.verification_id: item.expected_digest
                for item in results
                if item.verification_id and item.expected_digest
            },
        )
        return VerifiedReward(
            reward=reward, integrity=integrity, verifications=tuple(results)
        )

    def validate_reward(
        self,
        reward: RewardResult,
        *,
        verifications: Sequence[VerificationResult] = (),
        expected_digests: Mapping[str, str] | None = None,
    ) -> Any:
        """Audit a reward this trainer did not build (a stored row, a request)."""
        return self.reward_validator.check(
            reward,
            verifications=verifications,
            snapshot=self.verifier_snapshot(),
            config_version=self.reward_config_version,
            expected_digests=expected_digests,
        )

    # -- critiques ----------------------------------------------------------------

    def critique(
        self,
        results: Sequence[VerificationResult] = (),
        *,
        trajectory: Any = None,
        evaluation: Any = None,
        ignore_verification_ids: Sequence[str] = (),
    ) -> tuple[CritiqueResult, ...]:
        """Structured critiques for every observable failure in the evidence."""
        if not self.rlvr_config.critique.enabled:
            return ()
        if trajectory is not None:
            return self.critique_engine.critique_trajectory(
                trajectory,
                verifications=results,
                evaluation=evaluation,
                ignore_verification_ids=ignore_verification_ids,
            )
        found = list(self.critique_engine.critique(results))
        if evaluation is not None:
            found.extend(self.critique_engine.critique_evaluation(evaluation))
        return self.critique_engine._sorted(found)

    def correct(
        self,
        critiques: Sequence[CritiqueResult],
        *,
        originals: Mapping[str, Mapping[str, Any]] | None = None,
        proposals: Mapping[str, Mapping[str, Any]] | None = None,
        inputs: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> tuple[CorrectedExample, ...]:
        """Corrections for critiques, each with the quality verdict it earned.

        A proposal is accepted as a correction only when it can be verified or
        is explicitly marked as provisional; ``corrected_output`` from a
        critique's own structured correction counts as a proposal too. Hidden
        reasoning in any proposal is rejected by the builder.
        """
        builder = CorrectedExampleBuilder(self.rlvr_config.critique)
        original_inputs = dict(inputs or {})
        original_outputs = dict(originals or {})
        offered = dict(proposals or {})
        found: list[CorrectedExample] = []
        for critique in critiques:
            corrected = dict(offered.get(critique.critique_id) or critique.correction or {})
            if not corrected:
                continue
            found.append(
                builder.build(
                    original_input=dict(original_inputs.get(critique.critique_id) or {}),
                    original_output=dict(
                        original_outputs.get(critique.critique_id)
                        or critique.observed_behavior
                        or {}
                    ),
                    critique=critique,
                    corrected_output=corrected,
                    verification=None,
                    source_trajectory_id=critique.trajectory_id,
                    correction_source=critique.source,
                )
            )
        return tuple(found)

    # -- what the run reports ----------------------------------------------------

    def initialize_reward(self) -> dict[str, Any]:
        info = super().initialize_reward()
        info["verifiers"] = {
            "policy": self.rlvr_config.verifiers.to_mapping(),
            "registered": [item.verifier_id for item in self.registry.list()],
            "deterministic": sum(
                1 for item in self.registry.list() if item.deterministic
            ),
            "snapshot": self.verifier_snapshot(),
        }
        info["reward_config_version"] = self.reward_config_version
        info["critique_policy"] = self.rlvr_config.critique.to_mapping()
        return info

    def prepare_data(self, dataset: Any) -> dict[str, Any]:
        prepared = super().prepare_data(dataset)
        prepared["rlvr"] = {
            "verifiers": [item.verifier_id for item in self.verifiers()],
            "verifier_snapshot": self.verifier_snapshot(),
            "reward_config_version": self.reward_config_version,
            "critique_dataset_version": self.rlvr_config.critique_dataset_version,
            "correction_dataset_version": self.rlvr_config.correction_dataset_version,
            "critique_enabled": bool(self.rlvr_config.critique.enabled),
            "verification_counts": self._verification_counts(dataset),
            "note": (
                "a dry run verifies recorded evidence and derives rewards from it; "
                "no model is loaded and no weights change"
            ),
        }
        return prepared

    def _verification_counts(self, dataset: Any) -> dict[str, Any]:
        """How many verifications the dataset's rows were built from, by status."""
        statuses: Counter[str] = Counter()
        verifiers: Counter[str] = Counter()
        evidence_backed = 0
        rows = list(getattr(dataset, "examples", ()) or ())
        for row in rows:
            metadata = dict(getattr(row, "metadata", {}) or {})
            block = dict(metadata.get("rlvr") or {})
            for item in block.get("verifications", ()) or ():
                if not isinstance(item, Mapping):
                    continue
                statuses[str(item.get("status", "") or "unknown")] += 1
                verifiers[str(item.get("verifier_id", "") or "unknown")] += 1
                if item.get("evidence"):
                    evidence_backed += 1
        return {
            "rows": len(rows),
            "with_verifications": sum(
                1
                for row in rows
                if dict(dict(getattr(row, "metadata", {}) or {}).get("rlvr") or {})
                .get("verifications")
            ),
            "verifications": sum(statuses.values()),
            "by_status": dict(statuses),
            "by_verifier": dict(verifiers),
            "evidence_backed": evidence_backed,
        }

    def _walk(
        self,
        run: TrainingRun,
        dataset: RewardDatasetVersion,
        callbacks: TrainingCallbacks,
        *,
        first_step: int = 0,
    ) -> TrainingRun:
        """The inherited simulation, labelled with what verified its rewards."""
        finished = super()._walk(run, dataset, callbacks, first_step=first_step)
        block = self._verification_counts(dataset)
        block["reward_config_version"] = self.reward_config_version
        block["verifier_snapshot"] = self.verifier_snapshot()
        return finished.with_status(
            finished.status,
            rl_metrics={**dict(finished.rl_metrics), "rlvr": block},
        )

    def model_metadata(self) -> dict[str, Any]:
        metadata = super().model_metadata()
        metadata["rlvr"] = {
            "verifiers": self.verifier_snapshot(),
            "verifier_ids": [item.verifier_id for item in self.verifiers()],
            "verifier_policy": self.rlvr_config.verifiers.to_mapping(),
            "reward_config": self.rlvr_config.reward.to_mapping(),
            "reward_config_version": self.reward_config_version,
            "critique_dataset_version": self.rlvr_config.critique_dataset_version,
            "correction_dataset_version": self.rlvr_config.correction_dataset_version,
            "critique_policy": self.rlvr_config.critique.to_mapping(),
            "verification_required": self.rlvr_config.reward.require_evidence,
            "simulated": not self.optimizer.learns,
        }
        metadata["verifiable"] = True
        return metadata

    def verification_report(self, results: Sequence[VerificationResult]) -> dict[str, Any]:
        """A summary of a verification batch a report or an API can print."""
        statuses: Counter[str] = Counter(item.status for item in results)
        return {
            "total": len(results),
            "by_status": dict(statuses),
            "passed": statuses.get(VerificationStatus.PASS.value, 0),
            "failed": statuses.get(VerificationStatus.FAIL.value, 0),
            "partial": statuses.get(VerificationStatus.PARTIAL.value, 0),
            "inconclusive": statuses.get(VerificationStatus.INCONCLUSIVE.value, 0),
            "error": statuses.get(VerificationStatus.ERROR.value, 0),
            "deterministic": sum(1 for item in results if item.factual),
            "verifiers": sorted({item.verifier_id for item in results if item.verifier_id}),
            "results": [item.to_dict() for item in results],
        }

    def summary(self) -> dict[str, Any]:
        """The RLVR reading of this trainer: what would verify, and how."""
        return {
            "mode": self.mode,
            "algorithm": self.algorithm,
            "backend": self.backend,
            "available": self.is_available(),
            "missing_dependencies": list(self.missing_dependencies()),
            "dry_run": bool(self.rlvr_config.dry_run),
            "verifiers": [item.to_dict() for item in self.verifiers()],
            "verifier_policy": self.rlvr_config.verifiers.to_mapping(),
            "reward": self.rlvr_config.reward.to_mapping(),
            "critique": self.rlvr_config.critique.to_mapping(),
            "critique_dataset_version": self.rlvr_config.critique_dataset_version,
            "correction_dataset_version": self.rlvr_config.correction_dataset_version,
        }


# ── critique-based learning ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CorrectionProposal:
    """What a corrector proposes for one critique, before it is checked.

    ``verification``, when present, is the evidence that settles whether the
    proposal worked; without it the builder holds the correction rather than
    accepting an unverified suggestion.
    """

    corrected_output: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""
    evidence: tuple[str, ...] = ()
    verification: VerificationResult | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "corrected_output": dict(self.corrected_output),
            "source": self.source,
            "evidence": list(self.evidence),
            "verification": self.verification.to_dict() if self.verification else None,
            "metadata": dict(self.metadata),
        }


#: A corrector is handed a critique and its context and returns a proposal.
#: It is deliberately a plain callable: a rule, a plugin or a person can supply
#: one without this module knowing what produced the correction.
CorrectionProvider = Callable[[CritiqueResult, Mapping[str, Any]], Any]

#: The projections Phase 19 ships. A future phase adds the optimizers.
CRITIQUE_METHODS: tuple[str, ...] = ("sft", "preference", "reward")


class CritiqueTrainingMethod:
    """Projects a critique dataset onto a learning method — and stops there.

    The optimization method is pluggable by construction: this class prepares a
    critique dataset, proposes and verifies corrections, and projects the rows
    into Phase 16 SFT examples, Phase 17 preference pairs or Phase 18 reward
    examples. It does not train anything, and it does not choose a method that
    the configuration did not name.
    """

    def __init__(
        self,
        *,
        method: str = "sft",
        config: CritiqueConfig | None = None,
        builder: CritiqueDatasetBuilder | None = None,
        corrector: CorrectionProvider | None = None,
        reward_config: VerifiableRewardConfig | None = None,
    ) -> None:
        self.method = method if method in CRITIQUE_METHODS else "sft"
        self.config = config if config is not None else CritiqueConfig()
        self.reward_config = (
            reward_config if reward_config is not None else VerifiableRewardConfig()
        )
        self.builder = (
            builder if builder is not None else CritiqueDatasetBuilder(config=self.config)
        )
        self.corrector = corrector

    @property
    def name(self) -> str:
        return f"critique:{self.method}"

    # -- corrections ---------------------------------------------------------------

    def corrections(
        self,
        critiques: Sequence[CritiqueResult],
        *,
        original_inputs: Mapping[str, Mapping[str, Any]] | None = None,
        original_outputs: Mapping[str, Mapping[str, Any]] | None = None,
        provider: CorrectionProvider | None = None,
        verifications: Mapping[str, VerificationResult] | None = None,
    ) -> tuple[CorrectedExample, ...]:
        """Corrections for critiques, each with the quality verdict it earned.

        The correction is verified when a verification is supplied (keyed by
        critique id); otherwise the builder's policy decides whether to accept
        it or hold it for review. Hidden reasoning is rejected outright.
        """
        corrector = provider if provider is not None else self.corrector
        inputs = dict(original_inputs or {})
        outputs = dict(original_outputs or {})
        checks = dict(verifications or {})
        builder = CorrectedExampleBuilder(self.config)
        found: list[CorrectedExample] = []
        for critique in critiques:
            context: dict[str, Any] = {
                "input": dict(inputs.get(critique.critique_id) or {}),
                "original": dict(
                    outputs.get(critique.critique_id) or critique.observed_behavior or {}
                ),
                "critique": critique,
            }
            proposal: Any = None
            if corrector is not None:
                proposal = corrector(critique, context)
            verification = checks.get(critique.critique_id)
            if isinstance(proposal, CorrectionProposal):
                corrected = dict(proposal.corrected_output)
                source = proposal.source
                verification = proposal.verification or verification
            elif isinstance(proposal, Mapping):
                corrected = dict(proposal)
                source = ""
            else:
                corrected = dict(critique.correction or {})
                source = critique.source
            if not corrected:
                continue
            found.append(
                builder.build(
                    original_input=context["input"],
                    original_output=context["original"],
                    critique=critique,
                    corrected_output=corrected,
                    verification=verification,
                    source_trajectory_id=critique.trajectory_id,
                    correction_source=source or critique.source,
                )
            )
        return tuple(found)

    # -- datasets ------------------------------------------------------------------

    def build_dataset(
        self,
        name: str,
        *,
        critiques: Sequence[CritiqueResult],
        corrections: Sequence[CorrectedExample] = (),
        rules: CritiqueDatasetRules | None = None,
        version: str = "",
        description: str = "",
        tags: Sequence[str] = (),
        existing_versions: Sequence[str] = (),
        source_datasets: Sequence[str] = (),
    ) -> CritiqueDatasetVersion:
        """Build one critique dataset version (does not store it)."""
        request = CritiqueDatasetRequest.of(
            name, rules=rules, version=version, description=description, tags=tags
        )
        return self.builder.build(
            request,
            critiques=critiques,
            corrections=corrections,
            rules=rules,
            version=version,
            description=description,
            tags=tags,
            existing_versions=existing_versions,
            source_datasets=source_datasets,
        )

    def validate(self, dataset: CritiqueDatasetVersion | None) -> tuple[str, ...]:
        problems: list[str] = []
        if dataset is None:
            return ("no critique dataset was supplied",)
        problems.extend(self.builder.validate(dataset))
        if not dataset.accepted_examples():
            problems.append("no accepted critique example is available")
        if self.method == "preference" and not self.to_preference(
            dataset, verified_only=True
        ):
            problems.append(
                "method=preference needs at least one accepted, verified correction"
            )
        return tuple(dict.fromkeys(problems))

    # -- projections ---------------------------------------------------------------

    def to_sft(self, dataset: CritiqueDatasetVersion) -> tuple[SFTTrainingExample, ...]:
        """Phase 16's example shape, corrections included."""
        return tuple(row.to_sft_example() for row in dataset.examples)

    def to_preference(
        self, dataset: CritiqueDatasetVersion, *, verified_only: bool = True
    ) -> tuple[Any, ...]:
        """Phase 17 pairs: chosen is the correction, rejected is the failure."""
        return self.builder.to_preference_pairs(dataset, verified_only=verified_only)

    def to_reward_examples(
        self,
        dataset: CritiqueDatasetVersion,
        *,
        critiques: Sequence[CritiqueResult] = (),
    ) -> tuple[RewardExample, ...]:
        """Phase 18 reward rows whose signal comes from the critique itself.

        A verified failure is negative, a safety failure strongly negative, an
        efficiency note a small penalty, and a verified correction a positive
        preference signal — the mapping lives in the reward configuration, not
        in this file. Rows say in their reasons that the reward is
        critique-derived, so a later reader can tell them from verifier-derived
        rows.
        """
        by_id = {item.critique_id: item for item in critiques if item.critique_id}
        found: list[RewardExample] = []
        for row in dataset.examples:
            critique = by_id.get(row.critique_id) or self._critique_for(row)
            reward = critique_reward(
                critique,
                config=self.reward_config,
                subject_id=row.source_trajectory_id or row.example_id,
            )
            status = (
                FeedbackStatus.ACCEPTED.value
                if row.accepted
                else FeedbackStatus.NEEDS_REVIEW.value
                if row.status == "needs_review"
                else FeedbackStatus.REJECTED.value
            )
            found.append(
                RewardExample(
                    kind="critique",
                    trajectory_id=row.source_trajectory_id,
                    task_id=critique.task_id,
                    mode="rlvr",
                    task=dict(row.prompt),
                    candidate=dict(row.corrected) or dict(row.original),
                    reward=reward,
                    reward_source=(
                        RewardSource.VERIFIER.value
                        if critique.source == CritiqueSource.VERIFIER.value
                        else RewardSource.RULE.value
                    ),
                    status=status,
                    reasons=("critique_derived", *row.reasons),
                    difficulty=row.difficulty,
                    tags=(*row.tags, "critique"),
                    metadata={
                        "critique_id": row.critique_id,
                        "critique_category": critique.category,
                        "severity": critique.severity,
                        "verified": row.verified,
                        "dataset_version": row.dataset_version,
                    },
                )
            )
        return tuple(found)

    def _critique_for(self, row: CritiqueExample) -> CritiqueResult:
        """A minimal, factual critique reconstructed from a dataset row."""
        return CritiqueResult(
            critique_id=row.critique_id or f"critique-for-{row.example_id}",
            trajectory_id=row.source_trajectory_id,
            category=row.critique_category or CritiqueCategory.OTHER.value,
            severity=row.severity or CritiqueSeverity.MEDIUM.value,
            failed_component=row.kind,
            evidence=tuple(row.reasons) or (f"critique_dataset_row:{row.example_id}",),
            observed_behavior=dict(row.original),
            correction=dict(row.corrected),
            source=(
                CritiqueSource.VERIFIER.value
                if row.verified
                else CritiqueSource.RULE_BASED.value
            ),
            detail=f"critique dataset row {row.example_id}",
        )

    # -- reporting -----------------------------------------------------------------

    def prepare(self, dataset: CritiqueDatasetVersion) -> dict[str, Any]:
        """What this method would feed a learner, as a summary."""
        kinds: Counter[str] = Counter(row.kind for row in dataset.examples)
        return {
            "method": self.method,
            "dataset_version": dataset.dataset_version_id,
            "examples": len(dataset),
            "accepted": len(dataset.accepted_examples()),
            "verified": sum(1 for row in dataset.examples if row.verified),
            "by_kind": dict(kinds),
            "by_status": dict(
                Counter(row.status for row in dataset.examples)
            ),
            "splits": {
                name: len(ids) for name, ids in dataset.splits.items() if ids
            },
            "issues": list(self.validate(dataset)),
        }

    def plan(self, dataset: CritiqueDatasetVersion) -> dict[str, Any]:
        """The projections this dataset supports, counted — nothing trained."""
        return {
            **self.prepare(dataset),
            "projections": {
                "sft": len(self.to_sft(dataset)),
                "preference": len(self.to_preference(dataset)),
                "reward": len(self.to_reward_examples(dataset)),
            },
            "trained": False,
            "note": (
                "the projections are the learning signal; running an optimizer is "
                "a separate, explicitly confirmed operation"
            ),
        }

    def dry_run(
        self,
        dataset: CritiqueDatasetVersion,
        *,
        critiques: Sequence[CritiqueResult] = (),
    ) -> dict[str, Any]:
        """The full method walk on a built dataset, with nothing optimized."""
        reward_rows = self.to_reward_examples(dataset, critiques=critiques)
        # A RewardExample has no ``verified`` field: whether the correction was
        # verified travels in its metadata, which is where ``to_reward_examples``
        # writes it. Reading the row directly would be reading a column that does
        # not exist.
        trust = {
            "accepted": sum(1 for row in reward_rows if row.accepted),
            "verified": sum(
                1
                for row in reward_rows
                if bool(dict(getattr(row, "metadata", {}) or {}).get("verified"))
            ),
            "total": len(reward_rows),
        }
        return {
            **self.plan(dataset),
            "reward_rows": [row.reward.total_reward for row in reward_rows],
            "reward_row_trust": trust,
            "verifications": [],
            "trained": False,
            "note": (
                "a dry run prepares and projects the critique dataset; no "
                "optimizer runs and no model is loaded"
            ),
        }


def rlvr_trainer_for(
    config: RLVRTrainingConfig | None = None, **kwargs: Any
) -> RLVRTrainer:
    """The RLVR trainer for a configuration, with this phase's defaults."""
    chosen = config if config is not None else RLVRTrainingConfig()
    return RLVRTrainer(chosen, **kwargs)


__all__ = [
    "CRITIQUE_METHODS",
    "CorrectionProposal",
    "CorrectionProvider",
    "CritiqueTrainingMethod",
    "RLVRTrainer",
    "VerifiedReward",
    "rlvr_trainer_for",
]
