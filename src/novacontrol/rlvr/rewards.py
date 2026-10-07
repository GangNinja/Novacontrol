"""Verifiable rewards: a verification becomes a reward, with its evidence.

The mapping is deliberately boring and configurable: PASS is worth what the
configuration says, PARTIAL is worth its partial value scaled by the verifier's
own score, FAIL and ERROR are negative, INCONCLUSIVE is neutral. There is no
hidden term. Every reward this provider emits cites the verifier identity, its
version and the observable facts, so "why is this +1.0?" is answered by reading
the reward itself.

Two rules keep the numbers honest:

  * **The task-level verdict dominates.** A run whose steps passed and whose
    final check failed must not average its way to a positive reward; when a
    task-scope verification exists, the aggregate is built from it, and step
    results only add bounded partial credit.
  * **Safety is its own component.** An unsafe action contributes a safety
    penalty that survives as a named component with its floor, so a good task
    outcome cannot hide it — the same rule Phase 18 applies to reward
    integrity, extended to verified evidence.

The provider subclasses Phase 18's :class:`~novacontrol.rlhf.rewards.RewardProvider`
so an RLVR reward is the same shape every other reward in the system is:
components, penalties, evidence, confidence and a version.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from novacontrol.evaluation.models import now_iso
from novacontrol.evaluation.reward import (
    RewardComponent,
    RewardPenalty,
    RewardResult,
)
from novacontrol.rlhf.models import (
    RewardIntegrityCheck,
    RewardIntegrityFinding,
    RewardIntegrityStatus,
    RewardSource,
)
from novacontrol.rlhf.rewards import RewardProvider, RewardRequest
from novacontrol.rlvr.config import VerifiableRewardConfig
from novacontrol.rlvr.models import (
    CritiqueCategory,
    CritiqueResult,
    VerificationResult,
    VerificationStatus,
    _text,
)
from novacontrol.rlvr.registry import VerifierRegistry

#: The built-in deterministic verifiers. Used only when no registry is supplied
#: to attribute determinism; a registry is the authoritative source.
DETERMINISTIC_BUILTINS: frozenset[str] = frozenset(
    {"file", "process", "test", "http", "database", "git", "output", "schema"}
)

#: An efficiency critique is a nudge, not a verdict: this fraction of the fail
#: value is what "successful but wasteful" is worth.
CRITIQUE_EFFICIENCY_FACTOR = 0.25

#: A verified correction is a positive preference signal worth this fraction of
#: a verified pass.
CRITIQUE_CORRECTION_FACTOR = 0.5

#: The component names every verifiable reward uses.
COMPONENT_VERIFICATION = "verification"
COMPONENT_PARTIAL = "partial_credit"
COMPONENT_CORRECTION = "correction"
PENALTY_SAFETY = "safety"
PENALTY_FAILURE = "verification_failure"
PENALTY_EFFICIENCY = "efficiency"


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    """One verification's contribution, before it is folded into a reward."""

    verification_id: str = ""
    verifier_id: str = ""
    verifier_version: str = ""
    status: str = VerificationStatus.INCONCLUSIVE.value
    deterministic: bool = True
    score: float | None = None
    confidence: float | None = None
    evidence: tuple[str, ...] = ()
    scope: str = "task"
    detail: str = ""
    #: The failure category the verifier named, so a reader of the reward sees
    #: WHY it moved and a safety failure can be recognised as one.
    error_category: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verification_id": self.verification_id,
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "status": self.status,
            "deterministic": self.deterministic,
            "score": self.score,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "scope": self.scope,
            "detail": self.detail,
            "error_category": self.error_category,
        }

    @classmethod
    def from_result(
        cls, result: VerificationResult, *, deterministic: bool = True
    ) -> VerificationEvidence:
        return cls(
            verification_id=result.verification_id,
            verifier_id=result.verifier_id,
            verifier_version=result.verifier_version,
            status=result.status,
            deterministic=deterministic,
            score=result.score,
            confidence=result.confidence,
            evidence=result.evidence,
            scope=result.scope,
            detail=result.detail,
            error_category=result.error_category,
        )


class VerifiableRewardProvider(RewardProvider):
    """Turns verification results into a reward, with evidence attached."""

    def __init__(
        self,
        config: VerifiableRewardConfig | None = None,
        *,
        registry: VerifierRegistry | None = None,
        policy: Any = None,
    ) -> None:
        super().__init__(policy)
        self.config = config if config is not None else VerifiableRewardConfig()
        self.registry = registry

    # -- interface --------------------------------------------------------------

    @property
    def name(self) -> str:
        return "verifiable"

    def is_available(self) -> bool:
        return bool(self.config.enabled)

    def missing_requirements(self) -> tuple[str, ...]:
        if not self.config.enabled:
            return ("the verifiable reward policy is disabled",)
        return ()

    def validate(self, request: RewardRequest) -> tuple[str, ...]:
        results = self._verifications(request)
        problems: list[str] = []
        if not results:
            problems.append(
                "no verification results were supplied: a verifiable reward "
                "needs something verified"
            )
        for result in results:
            if not result.verifier_id:
                problems.append("a verification carries no verifier identity")
            if self.config.require_evidence and not result.evidence:
                problems.append(
                    f"verification {result.verification_id!r} cites no evidence"
                )
        if self.config.require_known_verifier and self.registry is not None:
            for result in results:
                if self.registry.metadata(result.verifier_id) is None:
                    problems.append(
                        f"verifier {result.verifier_id!r} is not registered"
                    )
        return tuple(problems)

    def evaluate(self, request: RewardRequest) -> RewardResult:
        results = self._verifications(request)
        return self.reward_for(
            results,
            subject_id=request.subject_id or _text(request.metadata.get("task_id")),
            task_id=_text(request.metadata.get("task_id")),
            trajectory_id=request.subject_id,
        )

    def explain(self, reward: RewardResult) -> str:
        return reward.explanation_summary or (
            "a verifiable reward built from "
            f"{len(reward.components)} component(s) and "
            f"{len(reward.penalty_breakdown)} penalty(ies)"
        )

    def confidence(self, reward: RewardResult) -> float | None:
        return reward.confidence

    # -- the work ----------------------------------------------------------------

    def evidence_for(
        self, results: Sequence[VerificationResult]
    ) -> tuple[VerificationEvidence, ...]:
        """Each result with its determinism attributed, without inventing one."""
        out: list[VerificationEvidence] = []
        for result in results:
            deterministic = True
            if self.registry is not None:
                metadata = self.registry.metadata(result.verifier_id)
                deterministic = bool(metadata.deterministic) if metadata is not None else False
            else:
                deterministic = result.verifier_id in DETERMINISTIC_BUILTINS
            out.append(VerificationEvidence.from_result(result, deterministic=deterministic))
        return tuple(out)

    def reward_for(
        self,
        results: Sequence[VerificationResult],
        *,
        subject_id: str = "",
        task_id: str = "",
        trajectory_id: str = "",
        critique: CritiqueResult | None = None,
        correction_verified: bool = False,
    ) -> RewardResult:
        """The reward for a set of verifications, and any critique attached.

        The task-level verdict DOMINATES: when one exists, it is the term that
        decides the sign, and step readings become bounded partial credit at
        most. A run whose four steps passed and whose final check failed is a
        failure that shows progress, never a reward that outvotes its own task
        verdict — which is also the rule the validator audits.
        """
        evidences = self.evidence_for(results)
        config = self.config
        findings: list[str] = []
        components: list[RewardComponent] = []
        penalties: list[RewardPenalty] = []
        total = 0.0
        all_evidence: list[str] = []
        confidence: float | None = None
        safety = False
        task_index = next(
            (index for index, item in enumerate(evidences) if item.scope == "task"),
            None,
        )
        governed = task_index is not None
        step_credit = 0.0

        for index, item in enumerate(evidences):
            all_evidence.extend(item.evidence)
            all_evidence.append(f"verifier:{item.verifier_id}@{item.verifier_version}")
            if item.confidence is not None:
                confidence = (
                    item.confidence
                    if confidence is None
                    else min(confidence, item.confidence)
                )
            if governed and index != task_index:
                reading = config.value_for(item.status)
                if (
                    item.status == VerificationStatus.PARTIAL.value
                    and config.partial_credit
                ):
                    score = item.score if item.score is not None else 0.5
                    reading = min(
                        reading,
                        config.value_for(VerificationStatus.PASS.value)
                        * config.max_partial_credit,
                    ) * max(0.0, min(1.0, float(score)))
                if reading > 0:
                    step_credit += reading
                elif reading < 0:
                    findings.append(
                        f"step verification {item.verification_id!r} reported "
                        f"{item.status}; the task-level verdict governs and the "
                        "step reading is not applied on top of it"
                    )
                if (
                    item.status == VerificationStatus.INCONCLUSIVE.value
                    and config.require_evidence
                ):
                    findings.append(
                        f"verification {item.verification_id!r} was inconclusive "
                        "and contributes a neutral reward"
                    )
                if self._is_safety_failure(item):
                    safety = True
                continue
            value = config.value_for(item.status)
            if (
                item.status == VerificationStatus.PARTIAL.value
                and config.partial_credit
            ):
                score = item.score if item.score is not None else 0.5
                bounded = min(
                    value,
                    config.value_for(VerificationStatus.PASS.value)
                    * config.max_partial_credit,
                )
                value = bounded * max(0.0, min(1.0, float(score)))
                components.append(
                    RewardComponent(
                        name=COMPONENT_PARTIAL,
                        value=round(value, 6),
                        weight=1.0,
                        contribution=round(value, 6),
                        reason=(
                            f"partial verification at {score:.2f}: "
                            f"{item.detail or 'some checks passed'}"
                        ),
                    )
                )
            else:
                components.append(
                    RewardComponent(
                        name=COMPONENT_VERIFICATION,
                        value=round(value, 6),
                        weight=1.0,
                        contribution=round(value, 6),
                        reason=(
                            f"verifier {item.verifier_id}@{item.verifier_version} "
                            f"reported {item.status}: {item.detail or 'no detail'}"
                        ),
                    )
                )
            if item.status == VerificationStatus.INCONCLUSIVE.value and config.require_evidence:
                findings.append(
                    f"verification {item.verification_id!r} was inconclusive and "
                    "contributes a neutral reward"
                )
            total += value
            if self._is_safety_failure(item):
                safety = True

        if governed and step_credit > 0:
            bound = (
                config.value_for(VerificationStatus.PASS.value)
                * config.max_partial_credit
            )
            credit = min(step_credit, bound)
            components.append(
                RewardComponent(
                    name=COMPONENT_PARTIAL,
                    value=round(credit, 6),
                    weight=1.0,
                    contribution=round(credit, 6),
                    reason=(
                        "step-level verifications passed under a task-level "
                        "verdict: bounded partial credit"
                    ),
                )
            )
            total += credit

        if critique is not None:
            total += self._critique_component(critique, components, penalties)
            all_evidence.extend(critique.evidence)
            all_evidence.append(f"critique:{critique.category}")
            if critique.category == CritiqueCategory.SAFETY_ERROR.value:
                safety = True

        if safety and config.keep_safety_separate:
            floor = min(0.0, config.safety_floor)
            penalties.append(
                RewardPenalty(
                    name=PENALTY_SAFETY,
                    count=1.0,
                    weight=config.safety_penalty,
                    contribution=round(config.safety_penalty, 6),
                    reason="a safety failure was verified: the penalty is its own component",
                )
            )
            total = max(floor, total + config.safety_penalty)

        if correction_verified:
            bonus = (
                config.value_for(VerificationStatus.PASS.value)
                * CRITIQUE_CORRECTION_FACTOR
            )
            components.append(
                RewardComponent(
                    name=COMPONENT_CORRECTION,
                    value=round(bonus, 6),
                    weight=1.0,
                    contribution=round(bonus, 6),
                    reason="a correction was verified: positive preference signal",
                )
            )
            total += bonus

        # Only a VERIFIED task may produce a positive total: partial credit and
        # a correction bonus must not lift a failed task above zero.
        if (
            governed
            and task_index is not None
            and evidences[task_index].status != VerificationStatus.PASS.value
        ):
            total = min(total, 0.0)

        clipped = config.clip(round(total, 6))
        explanation = self._summary(
            evidences,
            clipped,
            safety=safety,
            critique=critique,
            governed=governed,
        )
        return RewardResult(
            reward_id=f"vrw-{subject_id}" if subject_id else "vrw-unknown",
            trajectory_id=trajectory_id or subject_id,
            reward_source=RewardSource.VERIFIER.value,
            confidence=confidence,
            total_reward=clipped,
            normalized_reward=clipped if config.normalize else None,
            component_rewards={item.name: item.contribution for item in components},
            penalties={item.name: item.contribution for item in penalties},
            components=tuple(components),
            penalty_breakdown=tuple(penalties),
            reward_version=config.version,
            weights=dict(config.to_mapping()),
            explanation_summary=explanation,
            evidence=tuple(dict.fromkeys(all_evidence)),
            timestamp=now_iso(),
        )

    def _critique_component(
        self,
        critique: CritiqueResult,
        components: list[RewardComponent],
        penalties: list[RewardPenalty],
    ) -> float:
        config = self.config
        if critique.category == CritiqueCategory.SAFETY_ERROR.value:
            return 0.0  # the safety penalty is added separately, once
        if critique.category == CritiqueCategory.EFFICIENCY_ISSUE.value:
            value = (
                -abs(config.value_for(VerificationStatus.FAIL.value))
                * CRITIQUE_EFFICIENCY_FACTOR
            )
            penalties.append(
                RewardPenalty(
                    name=PENALTY_EFFICIENCY,
                    count=1.0,
                    weight=CRITIQUE_EFFICIENCY_FACTOR,
                    contribution=round(value, 6),
                    reason=critique.detail or "the run succeeded inefficiently",
                )
            )
            return value
        if critique.has_correction and critique.verified:
            value = abs(config.value_for(VerificationStatus.FAIL.value)) * 0.25
            components.append(
                RewardComponent(
                    name=COMPONENT_CORRECTION,
                    value=round(value, 6),
                    weight=1.0,
                    contribution=round(value, 6),
                    reason="a verified critique named the correction",
                )
            )
            return value
        if critique.severity in {"high", "critical"}:
            value = config.value_for(VerificationStatus.FAIL.value) * 0.5
            penalties.append(
                RewardPenalty(
                    name=PENALTY_FAILURE,
                    count=1.0,
                    weight=0.5,
                    contribution=round(value, 6),
                    reason=critique.detail or "a high-severity critique was recorded",
                )
            )
            return value
        return 0.0

    def _is_safety_failure(self, evidence: VerificationEvidence) -> bool:
        """Whether a failure is a SAFETY failure, from the words it carries.

        Both ``safety`` and ``unsafe`` count, in the detail and in the evidence
        tokens: a verifier that names ``unsafe_command`` as the fact it read is
        reporting a safety failure, and the penalty must not depend on which of
        the two words its author happened to choose.
        """
        words = " ".join(
            (
                _text(evidence.detail).lower(),
                " ".join(evidence.evidence).lower(),
                _text(evidence.error_category).lower(),
            )
        )
        return evidence.status == VerificationStatus.FAIL.value and (
            "safety" in words or "unsafe" in words
        )

    def _summary(
        self,
        evidences: Sequence[VerificationEvidence],
        total: float,
        *,
        safety: bool,
        critique: CritiqueResult | None,
        governed: bool = False,
    ) -> str:
        parts = [
            f"{item.verifier_id}@{item.verifier_version}={item.status}"
            for item in evidences
        ]
        text = "verifiable reward " + f"{total:+.2f}"
        if parts:
            text += " from " + ", ".join(parts)
        if governed:
            text += "; the task-level verdict governs, steps add bounded partial credit"
        if safety:
            text += "; a safety failure is kept as its own penalty"
        if critique is not None:
            text += f"; critique {critique.category}/{critique.severity}"
        return text

    @staticmethod
    def _verifications(request: RewardRequest) -> tuple[VerificationResult, ...]:
        raw = request.metadata.get("verifications") or request.context.get("verifications")
        if isinstance(raw, VerificationResult):
            return (raw,)
        if not isinstance(raw, (list, tuple)):
            return ()
        found: list[VerificationResult] = []
        for item in raw:
            if isinstance(item, VerificationResult):
                found.append(item)
            elif isinstance(item, Mapping):
                found.append(VerificationResult.from_dict(item))
        return tuple(found)


class VerifiableRewardValidator:
    """Audits a verifiable reward before it is allowed to teach anything.

    The checks are the phase's integrity contract: the verifier exists, its
    version is known, evidence is present, the expectation and observation are
    available where the category needs them, the reward agrees with the
    verification it claims to come from, no two verifications contradict each
    other, the verifier has not been disabled and the reward configuration is
    the one the run started with.
    """

    def __init__(
        self,
        config: VerifiableRewardConfig | None = None,
        *,
        registry: VerifierRegistry | None = None,
    ) -> None:
        self.config = config if config is not None else VerifiableRewardConfig()
        self.registry = registry

    def check(
        self,
        reward: RewardResult,
        *,
        verifications: Sequence[VerificationResult] = (),
        snapshot: Mapping[str, str] | None = None,
        config_version: str = "",
        expected_digests: Mapping[str, str] | None = None,
    ) -> RewardIntegrityCheck:
        findings: list[RewardIntegrityFinding] = []

        def add(code: str, severity: str, detail: str) -> None:
            findings.append(RewardIntegrityFinding(code=code, severity=severity, detail=detail))

        if not verifications:
            add(
                "evidence_missing",
                "error",
                "a verifiable reward cites no verification at all",
            )
        seen: set[tuple[str, str, str]] = set()
        for result in verifications:
            if not result.verifier_id:
                add("verifier_missing", "error", "a verification names no verifier")
            elif self.registry is not None and self.registry.metadata(result.verifier_id) is None:
                add(
                    "verifier_missing",
                    "error",
                    f"verifier {result.verifier_id!r} is not registered",
                )
            elif self.registry is not None:
                record = self.registry.record(result.verifier_id)
                if record is not None and not record.enabled:
                    add(
                        "verifier_disabled",
                        "error",
                        f"verifier {result.verifier_id!r} is disabled and cannot "
                        "support a reward",
                    )
                if record is not None and result.verifier_version and (
                    record.metadata.version != result.verifier_version
                ):
                    add(
                        "verifier_version_unknown",
                        "warning",
                        f"the verdict cites verifier version {result.verifier_version!r} "
                        f"but {record.metadata.version!r} is registered",
                    )
            if self.config.require_evidence and not result.evidence:
                add(
                    "evidence_missing",
                    "error",
                    f"verification {result.verification_id!r} cites no observable fact",
                )
            if self.config.require_known_verifier and not result.verifier_version:
                add(
                    "verifier_version_unknown",
                    "warning",
                    f"verification {result.verification_id!r} names no verifier version",
                )
            if (
                result.status
                in {VerificationStatus.PASS.value, VerificationStatus.FAIL.value}
                and not result.expected
                and not result.observed
            ):
                add(
                    "expected_missing",
                    "warning",
                    f"verification {result.verification_id!r} stored neither the "
                    "expectation nor the observation it read",
                )
            if (
                expected_digests is not None
                and result.verification_id in expected_digests
                and result.expected_digest
                and expected_digests[result.verification_id] != result.expected_digest
            ):
                add(
                    "expected_tampered",
                    "error",
                    f"the expectation behind verification {result.verification_id!r} "
                    "changed after it was frozen",
                )
            # The SUBJECT matters: the same verifier failing a step and then
            # the task is multi-step verification working (one check read at two
            # scopes), not a duplicated row. A duplicate is the same verifier
            # saying the same thing about the same subject twice.
            key = (result.verifier_id, result.subject_key(), result.status)
            if key in seen and result.status == VerificationStatus.FAIL.value:
                add(
                    "duplicate_verification",
                    "info",
                    f"{result.verifier_id!r} failed more than once for "
                    f"{result.subject_key()!r}; the readings are counted once",
                )
            seen.add(key)

        # Step/task disagreement is a HIERARCHY, not a contradiction: a task
        # whose steps passed and whose final check failed is a failure that shows
        # partial progress, and the provider already makes the task verdict
        # govern. What IS a contradiction is a reward whose sign fights the task
        # verdict, which ``_consistency`` below reports as an error.
        if reward.reward_source != RewardSource.VERIFIER.value:
            add(
                "reward_source_mismatch",
                "error",
                f"this reward claims source {reward.reward_source!r}, not "
                f"{RewardSource.VERIFIER.value!r}",
            )
        consistency = self._consistency(reward, verifications)
        if consistency:
            add("reward_inconsistent", "error", consistency)
        if config_version and reward.reward_version and config_version != reward.reward_version:
            add(
                "config_version_mismatch",
                "error",
                f"the reward was built by configuration version "
                f"{reward.reward_version!r} but the run pins {config_version!r}",
            )
        if snapshot is not None and self.registry is not None:
            for problem in self.registry.assert_unmodified(snapshot):
                add("verifier_tampered", "error", problem)

        errors = [item for item in findings if item.severity == "error"]
        warnings = [item for item in findings if item.severity == "warning"]
        if errors:
            status = RewardIntegrityStatus.INVALID.value
        elif warnings:
            status = RewardIntegrityStatus.NEEDS_REVIEW.value
        elif findings:
            status = RewardIntegrityStatus.SUSPICIOUS.value
        else:
            status = RewardIntegrityStatus.VALID.value
        check = RewardIntegrityCheck(
            subject_id=reward.trajectory_id or reward.reward_id,
            reward_id=reward.reward_id,
            source=reward.reward_source,
            status=status,
            findings=tuple(findings),
            total_reward=reward.total_reward,
        )
        return check

    def _consistency(
        self, reward: RewardResult, verifications: Sequence[VerificationResult]
    ) -> str:
        """Whether the reward's sign agrees with the strongest task verdict."""
        if not verifications:
            return ""
        task = next(
            (item for item in reversed(verifications) if item.scope == "task"), None
        )
        if task is None:
            return ""
        if task.failed and reward.total_reward > 0:
            return (
                f"the task-level verification failed but the reward is "
                f"{reward.total_reward:+.2f}: a failed task is not a positive signal"
            )
        if task.verified and reward.total_reward < 0:
            return (
                f"the task-level verification passed but the reward is "
                f"{reward.total_reward:+.2f}: check the configured values"
            )
        return ""


def critique_reward(
    critique: CritiqueResult,
    *,
    config: VerifiableRewardConfig | None = None,
    subject_id: str = "",
) -> RewardResult:
    """The reward signal a critique alone carries (Phase 15/18 integration).

    A verified failure is negative, an unsafe behaviour is a strong negative
    safety signal, an efficiency issue is a small penalty, and a verified
    correction is a positive preference signal. Task success and safety stay
    distinguishable because they are separate components.
    """
    provider = VerifiableRewardProvider(config)
    result = provider.reward_for(
        (),
        subject_id=subject_id or critique.trajectory_id,
        critique=critique,
        correction_verified=bool(critique.has_correction and critique.verified),
    )
    return result


__all__ = [
    "COMPONENT_CORRECTION",
    "COMPONENT_PARTIAL",
    "COMPONENT_VERIFICATION",
    "CRITIQUE_CORRECTION_FACTOR",
    "CRITIQUE_EFFICIENCY_FACTOR",
    "DETERMINISTIC_BUILTINS",
    "PENALTY_EFFICIENCY",
    "PENALTY_FAILURE",
    "PENALTY_SAFETY",
    "VerifiableRewardProvider",
    "VerifiableRewardValidator",
    "VerificationEvidence",
    "critique_reward",
]
