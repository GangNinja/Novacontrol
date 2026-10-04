"""Reward-hacking protection and disagreement detection — the phase's conscience.

Reinforcement learning from feedback fails in a specific way: the policy finds
the cheapest path to the number. It learns to be long if length is scored, to
repeat a successful action if repetitions are counted, to look safe while doing
something unsafe, or to satisfy a weak evaluator while failing the real task.
None of that is visible in the reward itself — the reward looks high — so this
module exists to look BESIDE the reward at what actually happened.

Two audits live here:

:class:`RewardIntegrityChecker` flags rewards that cannot be trusted as training
signal. Its statuses form a ladder — ``valid``, ``suspicious``, ``needs_review``,
``invalid`` — and nothing below ``valid`` ever becomes trusted training data: the
dataset builder holds or refuses it, and the reason travels with the row.

:class:`FeedbackDisagreementDetector` records the cases where two sources read
the same subject differently — a person's "no" against an evaluator's "yes", a
verifier's failure against an AI's praise, two evaluators that contradict each
other. It never picks a winner. It flags the case for a person or for a
deterministic check, because a disagreement resolved by a rule of thumb would be
exactly the wrong lesson.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.evaluation.models import AgentTrajectory
from novacontrol.evaluation.reward import RewardResult
from novacontrol.rlhf.config import RewardPolicyConfig
from novacontrol.rlhf.evaluators import outcome_facts
from novacontrol.rlhf.models import (
    EVIDENCE_UNNECESSARY_ACTION,
    EVIDENCE_UNSAFE_ACTION,
    AIRating,
    Disagreement,
    DisagreementKind,
    HumanFeedback,
    RewardIntegrityCheck,
    RewardIntegrityFinding,
    RewardIntegrityStatus,
    RewardSource,
    Rollout,
)

#: Stable finding codes. A report names these; a test asserts them.
INTEGRITY_TASK_FAILED_HIGH_REWARD = "task_failed_but_reward_high"
INTEGRITY_UNSAFE_HIGH_REWARD = "unsafe_action_with_positive_reward"
INTEGRITY_INCOMPLETE_VERIFICATION = "reward_without_verification"
INTEGRITY_LENGTH_GAMING = "response_length_gaming"
INTEGRITY_REPEATED_ACTIONS = "repeated_actions_inflating_reward"
INTEGRITY_WITHOUT_EVIDENCE = "reward_without_observable_evidence"
INTEGRITY_LOW_CONFIDENCE = "low_confidence_reward"
INTEGRITY_SOURCE_NOT_ALLOWED = "reward_source_not_allowed"
INTEGRITY_SOURCE_DISAGREEMENT = "source_disagreement"

#: How much a reading must move before it counts as a disagreement, when no
#: policy says otherwise.
DEFAULT_DISAGREEMENT_TOLERANCE = 0.25


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


#: Anything that carries a reading: a reward, a rating, a feedback row, or
#: several rows from one source (which read as their mean).
Reading = RewardResult | AIRating | HumanFeedback


def _reading(value: Reading | Sequence[Reading] | None) -> float | None:
    """One source's reading on -1..1, or ``None`` when it made none.

    Every source is projected onto the same scale before comparison — that is
    the only way a person's verdict, an evaluator's score and the engine's total
    can be compared at all. The projection uses the NORMALIZED reading when the
    reward has one, so the comparison does not depend on a provider's scale.
    Several rows from the same source read as their mean.
    """
    if value is None:
        return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = [_reading(row) for row in value]
        usable = [item for item in values if item is not None]
        return sum(usable) / len(usable) if usable else None
    if isinstance(value, HumanFeedback):
        from novacontrol.rlhf.feedback import feedback_reward_value

        return feedback_reward_value(value, RewardPolicyConfig())
    if isinstance(value, AIRating):
        return 2.0 * max(0.0, min(1.0, value.score)) - 1.0
    if isinstance(value, RewardResult):
        if value.normalized_reward is not None:
            return max(-1.0, min(1.0, float(value.normalized_reward)))
        return max(-1.0, min(1.0, float(value.total_reward) / 3.0))
    return None


@dataclass(frozen=True, slots=True)
class IntegrityContext:
    """The observable situation a reward was produced in.

    The checker is given facts, not opinions: what happened (success, verification
    counts, unsafe actions), how big the output was, and the readings of the
    other sources about the same subject. A context built from a trajectory is
    mechanical; one a caller assembles by hand is the caller's claim.
    """

    subject_id: str = ""
    task_succeeded: bool | None = None
    verification_total: int = 0
    verification_failed: int = 0
    unsafe_actions: int = 0
    unnecessary_actions: int = 0
    repeated_actions: int = 0
    response_tokens: int = 0
    latency_ms: float | None = None
    sources: Mapping[str, float | None] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @classmethod
    def from_trajectory(
        cls,
        trajectory: AgentTrajectory,
        *,
        response_tokens: int = 0,
        sources: Mapping[str, float | None] | None = None,
    ) -> IntegrityContext:
        facts = outcome_facts(trajectory)
        return cls(
            subject_id=trajectory.trajectory_id,
            task_succeeded=trajectory.success,
            verification_total=int(facts.get("verification_total") or 0),
            verification_failed=int(facts.get("verification_failed") or 0),
            unsafe_actions=int(facts.get("unsafe_actions") or 0),
            unnecessary_actions=len(tuple(facts.get("repeated_tools") or ())),
            repeated_actions=len(tuple(facts.get("repeated_tools") or ())),
            response_tokens=int(response_tokens),
            latency_ms=facts.get("latency_ms"),
            sources=dict(sources or {}),
        )

    @classmethod
    def from_rollout(
        cls, rollout: Rollout, *, sources: Mapping[str, float | None] | None = None
    ) -> IntegrityContext:
        actions = [str(step.action.get("action", "")) for step in rollout.steps]
        repeated = 0
        seen: set[str] = set()
        for name in actions:
            if not name:
                continue
            if name in seen:
                repeated += 1
            seen.add(name)
        succeeded = None
        if _text(rollout.status) == "completed":
            succeeded = True
        elif _text(rollout.status) == "failed":
            succeeded = False
        return cls(
            subject_id=rollout.trajectory_id or rollout.rollout_id,
            task_succeeded=succeeded,
            repeated_actions=repeated,
            unnecessary_actions=repeated,
            sources=dict(sources or {}),
        )

    def with_sources(self, sources: Mapping[str, float | None]) -> IntegrityContext:
        return IntegrityContext(
            subject_id=self.subject_id,
            task_succeeded=self.task_succeeded,
            verification_total=self.verification_total,
            verification_failed=self.verification_failed,
            unsafe_actions=self.unsafe_actions,
            unnecessary_actions=self.unnecessary_actions,
            repeated_actions=self.repeated_actions,
            response_tokens=self.response_tokens,
            latency_ms=self.latency_ms,
            sources={**self.sources, **sources},
            notes=self.notes,
        )


class RewardIntegrityChecker:
    """Audits one reward against what actually happened.

    Order matters: an outright contradiction (the task failed but the reward is
    high, an unsafe action but a positive total) is ``invalid`` whatever else is
    true, because no amount of good behaviour elsewhere makes those usable.
    Cross-source disagreement is ``needs_review`` — it is evidence of a problem,
    not proof of one. Softer smells (no verification, no evidence, very low
    confidence, output long past any gain) are ``suspicious``.
    """

    def __init__(self, policy: RewardPolicyConfig | None = None) -> None:
        self.policy = policy if policy is not None else RewardPolicyConfig()

    def check(
        self, reward: RewardResult, context: IntegrityContext | None = None
    ) -> RewardIntegrityCheck:
        facts = (
            context
            if context is not None
            else IntegrityContext(subject_id=reward.trajectory_id)
        )
        findings: list[RewardIntegrityFinding] = []
        invalid = False
        needs_review = False
        suspicious = False

        def error(code: str, detail: str) -> None:
            nonlocal invalid
            invalid = True
            findings.append(RewardIntegrityFinding(code, "error", detail))

        def review(code: str, detail: str) -> None:
            nonlocal needs_review
            needs_review = True
            findings.append(RewardIntegrityFinding(code, "info", detail))

        def warn(code: str, detail: str) -> None:
            nonlocal suspicious
            suspicious = True
            findings.append(RewardIntegrityFinding(code, "warning", detail))

        if not self.policy.allows(reward.reward_source) and reward.reward_source:
            error(
                INTEGRITY_SOURCE_NOT_ALLOWED,
                f"source {reward.reward_source!r} is not enabled by this policy",
            )
        if (
            facts.task_succeeded is False
            and reward.total_reward > self.policy.max_reward_when_failed
        ):
            error(
                INTEGRITY_TASK_FAILED_HIGH_REWARD,
                f"the task failed but the reward is {reward.total_reward:+.2f}: a "
                "failed run cannot earn a positive reward",
            )
        if facts.unsafe_actions > 0 and reward.total_reward > 0:
            error(
                INTEGRITY_UNSAFE_HIGH_REWARD,
                f"{facts.unsafe_actions} unsafe action(s) ran and the reward is "
                f"still {reward.total_reward:+.2f}",
            )
        elif facts.unsafe_actions > 0:
            findings.append(
                RewardIntegrityFinding(
                    EVIDENCE_UNSAFE_ACTION,
                    "info",
                    f"{facts.unsafe_actions} unsafe action(s) applied a penalty",
                )
            )
        if (
            facts.verification_total == 0
            and reward.total_reward > self.policy.max_reward_without_verification
        ):
            warn(
                INTEGRITY_INCOMPLETE_VERIFICATION,
                f"the reward is {reward.total_reward:+.2f} but nothing recorded a "
                "verification, so there is no objective check behind it",
            )
        if (
            facts.response_tokens > self.policy.length_gaming_tokens
            and facts.verification_total == 0
        ):
            warn(
                INTEGRITY_LENGTH_GAMING,
                f"{facts.response_tokens} tokens were produced with no measured "
                "gain: length is not evidence of quality",
            )
        if facts.repeated_actions > self.policy.repeat_action_limit:
            warn(
                INTEGRITY_REPEATED_ACTIONS,
                f"{facts.repeated_actions} repeated action(s) were taken, which can "
                "inflate a count-based metric without doing any work",
            )
        if not reward.evidence:
            review(
                INTEGRITY_WITHOUT_EVIDENCE,
                "the reward cites no observable fact (no task outcome, verification, "
                "tool choice or safety event)",
            )
        if reward.confidence is not None and reward.confidence < self.policy.min_confidence:
            warn(
                INTEGRITY_LOW_CONFIDENCE,
                f"confidence {reward.confidence:.2f} is below "
                f"{self.policy.min_confidence:.2f}",
            )
        for name, reading in sorted(facts.sources.items()):
            other: float | None = reading
            if other is None:
                continue
            mine = _reading(reward)
            if mine is None or abs(mine - other) <= self.policy.suspicious_gap:
                continue
            review(
                INTEGRITY_SOURCE_DISAGREEMENT,
                f"source {name!r} reads {other:+.2f} while this reward reads "
                f"{mine:+.2f}: the difference is larger than "
                f"{self.policy.suspicious_gap:.2f}",
            )
            findings.append(
                RewardIntegrityFinding(
                    EVIDENCE_UNNECESSARY_ACTION,
                    "info",
                    f"disagreement with source {name!r}",
                )
            )

        status = RewardIntegrityStatus.VALID.value
        if invalid:
            status = RewardIntegrityStatus.INVALID.value
        elif needs_review:
            status = RewardIntegrityStatus.NEEDS_REVIEW.value
        elif suspicious:
            status = RewardIntegrityStatus.SUSPICIOUS.value
        return RewardIntegrityCheck(
            subject_id=facts.subject_id or reward.trajectory_id,
            reward_id=reward.reward_id,
            source=reward.reward_source,
            status=status,
            findings=tuple(findings),
            total_reward=reward.total_reward,
        )

    def check_sources(
        self,
        rewards: Mapping[str, RewardResult],
        *,
        context: IntegrityContext | None = None,
    ) -> dict[str, RewardIntegrityCheck]:
        """Check each source's reward, with the others' readings as cross-evidence."""
        readings = {name: _reading(reward) for name, reward in rewards.items()}
        produced: dict[str, RewardIntegrityCheck] = {}
        for name, reward in rewards.items():
            others = {key: value for key, value in readings.items() if key != name}
            build = context if context is not None else IntegrityContext()
            produced[name] = self.check(reward, build.with_sources(others))
        return produced


class FeedbackDisagreementDetector:
    """Finds cases where sources contradict each other and records them.

    The three cases the phase must be able to see: a person says bad while an
    evaluator says good; a verifier says failed while an evaluator says good;
    and two evaluators differ by more than the configured gap. Each produces a
    :class:`Disagreement` with both readings and a recommendation. None of them
    produces a decision.
    """

    def __init__(self, policy: RewardPolicyConfig | None = None) -> None:
        self.policy = policy if policy is not None else RewardPolicyConfig()

    def detect(
        self,
        subject_id: str,
        *,
        human: RewardResult | AIRating | HumanFeedback | Sequence[HumanFeedback] | None = None,
        ai: RewardResult | AIRating | None = None,
        verifier: RewardResult | AIRating | None = None,
        ai_alt: RewardResult | AIRating | None = None,
    ) -> tuple[Disagreement, ...]:
        """Every disagreement detectable among the supplied readings."""
        reports: list[Disagreement] = []
        left = _reading(human)
        right = _reading(ai)
        if left is not None and right is not None and self._opposed(left, right):
            reports.append(
                self._report(
                    DisagreementKind.HUMAN_VS_AI,
                    subject_id,
                    RewardSource.HUMAN.value,
                    RewardSource.AI.value,
                    left,
                    right,
                    "a person and an evaluator read this subject differently",
                )
            )
        verifier_reading = _reading(verifier)
        if (
            verifier_reading is not None
            and right is not None
            and verifier_reading <= 0.0
            and right >= self.policy.disagreement_tolerance
        ):
            reports.append(
                self._report(
                    DisagreementKind.VERIFIER_VS_AI,
                    subject_id,
                    RewardSource.VERIFIER.value,
                    RewardSource.AI.value,
                    verifier_reading,
                    right,
                    "verification failed while an evaluator rated the same run well: "
                    "verify before trusting either reading",
                )
            )
        alt = _reading(ai_alt)
        if right is not None and alt is not None and abs(right - alt) > self.policy.suspicious_gap:
            reports.append(
                self._report(
                    DisagreementKind.AI_VS_AI,
                    subject_id,
                    RewardSource.AI.value,
                    RewardSource.AI.value,
                    right,
                    alt,
                    f"two evaluators differ by {abs(right - alt):.2f}, more than "
                    f"{self.policy.suspicious_gap:.2f}",
                )
            )
        return tuple(reports)

    def detect_all(
        self, subjects: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, tuple[Disagreement, ...]]:
        """Detect over many subjects at once (the shape a report consumes)."""
        produced: dict[str, tuple[Disagreement, ...]] = {}
        for subject_id, readings in subjects.items():
            produced[subject_id] = self.detect(subject_id, **readings)
        return produced

    def review_required(self, reports: Sequence[Disagreement]) -> bool:
        return any(_text(report.recommended) == "review" for report in reports)

    @staticmethod
    def summary(reports: Sequence[Disagreement]) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        for report in reports:
            by_kind[report.kind] = by_kind.get(report.kind, 0) + 1
        return {
            "total": len(reports),
            "by_kind": by_kind,
            "subjects": sorted({report.subject_id for report in reports}),
            "recommendation": (
                "review"
                if any(report.recommended == "review" for report in reports)
                else ("verify" if reports else "none")
            ),
        }

    # -- helpers -----------------------------------------------------------------

    def _opposed(self, left: float, right: float) -> bool:
        """Whether two readings point opposite ways beyond the tolerance."""
        gap = abs(left - right)
        if gap < self.policy.disagreement_tolerance:
            return False
        return (left < 0 < right) or (right < 0 < left) or gap > self.policy.suspicious_gap

    def _report(
        self,
        kind: DisagreementKind,
        subject_id: str,
        left_source: str,
        right_source: str,
        left: float,
        right: float,
        detail: str,
    ) -> Disagreement:
        gap = abs(left - right)
        return Disagreement(
            kind=kind.value,
            subject_id=subject_id,
            left_source=left_source,
            right_source=right_source,
            left_reading=round(left, 6),
            right_reading=round(right, 6),
            gap=round(gap, 6),
            detail=detail,
            recommended="verify" if kind is DisagreementKind.VERIFIER_VS_AI else "review",
        )


def _readings_mean(rows: Sequence[HumanFeedback]) -> float | None:
    """The mean reading of several feedback rows (what a person's panel said)."""
    values = [_reading(row) for row in rows]
    usable = [value for value in values if value is not None]
    return sum(usable) / len(usable) if usable else None


__all__ = [
    "DEFAULT_DISAGREEMENT_TOLERANCE",
    "FeedbackDisagreementDetector",
    "INTEGRITY_INCOMPLETE_VERIFICATION",
    "INTEGRITY_LENGTH_GAMING",
    "INTEGRITY_LOW_CONFIDENCE",
    "INTEGRITY_REPEATED_ACTIONS",
    "INTEGRITY_SOURCE_DISAGREEMENT",
    "INTEGRITY_SOURCE_NOT_ALLOWED",
    "INTEGRITY_TASK_FAILED_HIGH_REWARD",
    "INTEGRITY_UNSAFE_HIGH_REWARD",
    "INTEGRITY_WITHOUT_EVIDENCE",
    "IntegrityContext",
    "RewardIntegrityChecker",
]
