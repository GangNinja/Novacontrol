"""Phase 18's records: feedback, ratings, integrity, rollouts, reward datasets.

Everything here is a PROJECTION of something NovaControl already records. A
trajectory is the run; feedback is what a person said about it; a rating is what
an evaluator said; a rollout is what a policy did inside an environment; a reward
dataset is the collection a future policy-optimization step would consume. None
of these replaces a Phase 15 record — they attach to it and reference its id.

Two rules shape every dataclass in this module:

**No hidden reasoning, ever.** Nothing here has a field for a model's private
chain of thought, and there is deliberately nowhere to put one. Feedback is the
short structured form (a decision, a rating, a correction, a reason category);
an AI rating is criterion scores with the evidence each one cites. A reader can
always reconstruct WHY a number exists without reading anybody's mind.

**Provenance is part of the value.** A reward from a person, a reward from an AI
evaluator and a reward from a deterministic rule are three different things, and
they carry source, evaluator, version and confidence so a later analysis can tell
them apart — and notice when they disagree. AI feedback is never silently treated
as equivalent to human feedback.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4

from novacontrol.evaluation.models import now_iso
from novacontrol.evaluation.reward import REWARD_VERSION, RewardResult

#: The vocabulary version of this phase's records.
RLHF_VERSION = "phase18.1"

RLHF_SCHEMA_VERSION = 1
FEEDBACK_SCHEMA_VERSION = 1
RATING_SCHEMA_VERSION = 1
INTEGRITY_SCHEMA_VERSION = 1
ROLLOUT_SCHEMA_VERSION = 1
REWARD_DATASET_SCHEMA_VERSION = 1
POLICY_UPDATE_SCHEMA_VERSION = 1

#: Feedback is a short structured statement, not an essay. The rating scale is
#: the one every consumer survey uses; a correction is text and is redacted
#: before it is stored.
RATING_MIN = 1.0
RATING_MAX = 5.0

#: The observable facts a reward may cite. A reward's evidence names the facts it
#: saw, which is what makes it explainable without a reasoning trace.
EVIDENCE_TASK_SUCCEEDED = "task_succeeded"
EVIDENCE_TASK_FAILED = "task_failed"
EVIDENCE_VERIFICATION_PASSED = "verification_passed"
EVIDENCE_VERIFICATION_FAILED = "verification_failed"
EVIDENCE_CORRECT_TOOL_SELECTED = "correct_tool_selected"
EVIDENCE_WRONG_TOOL_SELECTED = "wrong_tool_selected"
EVIDENCE_UNNECESSARY_ACTION = "unnecessary_action_detected"
EVIDENCE_SAFETY_VIOLATION_PREVENTED = "safety_violation_prevented"
EVIDENCE_UNSAFE_ACTION = "unsafe_action"
EVIDENCE_LATENCY_EXCEEDED = "latency_threshold_exceeded"
EVIDENCE_HUMAN_ACCEPT = "human_accepted"
EVIDENCE_HUMAN_REJECT = "human_rejected"
EVIDENCE_AI_RATING = "ai_rating"
EVIDENCE_FEEDBACK_CORRECTION = "human_corrected"
EVIDENCE_ERROR_REPORTED = "error_reported"
EVIDENCE_UNSAFE_REPORTED = "unsafe_reported"

#: The statuses every quality judgement in this phase shares.
FEEDBACK_ACCEPTED = "accepted"
FEEDBACK_REJECTED = "rejected"
FEEDBACK_NEEDS_REVIEW = "needs_review"


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _flag(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _whole(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _real(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _mappings(value: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, (list, tuple)):
        return tuple(_mapping(item) for item in value if isinstance(item, Mapping))
    return ()


def _texts(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


# ── vocabularies ────────────────────────────────────────────────────────────


class RLMode(StrEnum):
    """Which feedback loop a run learns from."""

    RLHF = "rlhf"
    RLAIF = "rlaif"


MODES: tuple[str, ...] = tuple(member.value for member in RLMode)


class PolicyAlgorithm(StrEnum):
    """The policy-optimization algorithms this phase knows about.

    Only ``mock_policy`` is implemented: it is a deterministic, dependency-free
    walk used by dry runs and tests. ``ppo`` and ``grpo`` are in the vocabulary
    so a configuration can NAME the algorithm it intends to use and be priced
    and validated — but a run that asks for one is refused with an explanation,
    because this phase builds the plug, not the algorithm. Pretending a mock
    optimizer is real RL training is exactly what this enum's split prevents.
    """

    MOCK = "mock_policy"
    PPO = "ppo"
    GRPO = "grpo"


POLICY_ALGORITHMS: tuple[str, ...] = tuple(member.value for member in PolicyAlgorithm)
#: What can actually run today.
IMPLEMENTED_ALGORITHMS: tuple[str, ...] = (PolicyAlgorithm.MOCK.value,)
#: What the architecture is ready for but this phase does not implement.
PLANNED_ALGORITHMS: tuple[str, ...] = (
    PolicyAlgorithm.PPO.value,
    PolicyAlgorithm.GRPO.value,
)


class RewardSource(StrEnum):
    """Where a reward came from. Never collapsed: human ≠ AI ≠ rule ≠ verifier."""

    HUMAN = "human"
    AI = "ai"
    RULE = "rule"
    VERIFIER = "verifier"
    COMPOSITE = "composite"


REWARD_SOURCES: tuple[str, ...] = tuple(member.value for member in RewardSource)


class HumanFeedbackType(StrEnum):
    """The short structured feedback a person can give. No essays required."""

    ACCEPT = "accept"
    REJECT = "reject"
    PREFER_A = "prefer_a"
    PREFER_B = "prefer_b"
    RATING = "rating"
    CORRECTION = "correction"
    REPORT_ERROR = "report_error"
    REPORT_UNSAFE = "report_unsafe"


FEEDBACK_TYPES: tuple[str, ...] = tuple(member.value for member in HumanFeedbackType)

#: Feedback that says "this was bad". Used for disagreement detection and for
#: the sign of the reward the feedback maps to.
NEGATIVE_FEEDBACK_TYPES: frozenset[str] = frozenset(
    {
        HumanFeedbackType.REJECT.value,
        HumanFeedbackType.REPORT_ERROR.value,
        HumanFeedbackType.REPORT_UNSAFE.value,
        HumanFeedbackType.PREFER_B.value,
    }
)

#: Feedback that names a preferred side rather than a verdict.
PREFERENCE_FEEDBACK_TYPES: frozenset[str] = frozenset(
    {HumanFeedbackType.PREFER_A.value, HumanFeedbackType.PREFER_B.value}
)


class FeedbackStatus(StrEnum):
    """The verdict a quality filter reaches about a piece of feedback."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


FEEDBACK_STATUSES: tuple[str, ...] = tuple(member.value for member in FeedbackStatus)


class RewardIntegrityStatus(StrEnum):
    """How much a reward can be trusted as training signal."""

    VALID = "valid"
    SUSPICIOUS = "suspicious"
    INVALID = "invalid"
    NEEDS_REVIEW = "needs_review"


INTEGRITY_STATUSES: tuple[str, ...] = tuple(
    member.value for member in RewardIntegrityStatus
)


class DisagreementKind(StrEnum):
    """The three disagreements this phase can detect."""

    HUMAN_VS_AI = "human_vs_ai"
    VERIFIER_VS_AI = "verifier_vs_ai"
    AI_VS_AI = "ai_vs_ai"


class RolloutStatus(StrEnum):
    """How a rollout ended."""

    COMPLETED = "completed"
    TRUNCATED = "truncated"
    FAILED = "failed"


class EvaluatorKind(StrEnum):
    """What kind of evaluator produced a rating."""

    HUMAN = "human"
    LOCAL = "local_ai"
    RULE = "rule_based"
    EXTERNAL = "external_ai"


# ── human feedback ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FeedbackIssue:
    """One thing wrong with a feedback row, with a stable code."""

    code: str
    severity: str = "warning"
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FeedbackIssue:
        return cls(
            code=_text(data.get("code")),
            severity=_text(data.get("severity"), "warning"),
            detail=_text(data.get("detail")),
        )


@dataclass(frozen=True, slots=True)
class FeedbackVerdict:
    """What the quality filter decided about one feedback row, and why."""

    status: str = FeedbackStatus.NEEDS_REVIEW.value
    issues: tuple[FeedbackIssue, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.status == FeedbackStatus.ACCEPTED.value

    @property
    def rejected(self) -> bool:
        return self.status == FeedbackStatus.REJECTED.value

    @property
    def needs_review(self) -> bool:
        return self.status == FeedbackStatus.NEEDS_REVIEW.value

    def codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "issues": [issue.to_dict() for issue in self.issues],
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FeedbackVerdict:
        rows = data.get("issues")
        issues: list[FeedbackIssue] = []
        if isinstance(rows, (list, tuple)):
            issues = [
                FeedbackIssue.from_dict(row) for row in rows if isinstance(row, Mapping)
            ]
        return cls(
            status=_text(data.get("status"), FeedbackStatus.NEEDS_REVIEW.value),
            issues=tuple(issues),
            notes=_texts(data.get("notes")),
        )


@dataclass(frozen=True, slots=True)
class HumanFeedback:
    """One short, structured statement a person made about one trajectory.

    The shape is deliberately narrow: a verdict (accept/reject/prefer/…), an
    optional rating, an optional correction and a reason category. There is no
    field for reasoning, because none is collected; a correction is text and is
    redacted before it is stored, so a person cannot accidentally hand a
    credential to the training loop.

    ``status``/``review`` carry the quality filter's reading the same way Phase
    17's pairs do: feedback is never deleted, it is annotated.
    """

    feedback_id: str = field(default_factory=lambda: uuid4().hex)
    task_id: str = ""
    trajectory_id: str = ""
    user_ref: str = ""
    session_ref: str = ""
    feedback_type: str = HumanFeedbackType.ACCEPT.value
    rating: float | None = None
    selected_candidate: str = ""
    correction: str = ""
    reason_category: str = ""
    confidence: float | None = None
    source: str = RewardSource.HUMAN.value
    status: str = ""
    review: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=now_iso)
    schema_version: int = FEEDBACK_SCHEMA_VERSION

    # -- reading ---------------------------------------------------------------

    @property
    def is_negative(self) -> bool:
        return self.feedback_type in NEGATIVE_FEEDBACK_TYPES

    @property
    def is_preference(self) -> bool:
        return self.feedback_type in PREFERENCE_FEEDBACK_TYPES

    @property
    def typed(self) -> bool:
        """Whether ``feedback_type`` is a real member of the vocabulary."""
        return any(member.value == self.feedback_type for member in HumanFeedbackType)

    @property
    def rating_normalized(self) -> float | None:
        """The rating on a -1..1 scale, or ``None`` when there is no rating."""
        if self.rating is None:
            return None
        midpoint = (RATING_MIN + RATING_MAX) / 2.0
        half = (RATING_MAX - RATING_MIN) / 2.0
        return max(-1.0, min(1.0, (self.rating - midpoint) / half))

    @property
    def reviewed(self) -> bool:
        return bool(_text(self.review.get("decision")))

    def describes(self, trajectory_id: str) -> bool:
        return bool(trajectory_id) and self.trajectory_id == trajectory_id

    def target(self) -> str:
        """What this feedback is about: the trajectory if known, else the task."""
        return self.trajectory_id or self.task_id

    def fingerprint(self) -> str:
        """A content hash: two identical statements collapse to one.

        Timestamps and ids are excluded on purpose — the same person pressing
        the same button twice is the same feedback, and a deduplicator has to be
        able to see that.
        """
        payload = json.dumps(
            {
                "task_id": self.task_id,
                "trajectory_id": self.trajectory_id,
                "user_ref": self.user_ref,
                "type": self.feedback_type,
                "rating": self.rating,
                "selected_candidate": self.selected_candidate,
                "correction": self.correction,
                "reason_category": self.reason_category,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    # -- writing ---------------------------------------------------------------

    def with_status(
        self, status: FeedbackStatus | str, *, issues: Sequence[FeedbackIssue] = ()
    ) -> HumanFeedback:
        value = status.value if isinstance(status, FeedbackStatus) else str(status)
        return HumanFeedback.from_dict(
            {
                **self.to_dict(),
                "status": value,
                "review": {
                    **dict(self.review),
                    "issues": [issue.to_dict() for issue in issues],
                },
            }
        )

    def with_review(self, decision: str, reviewer: str = "", reason: str = "") -> HumanFeedback:
        return HumanFeedback.from_dict(
            {
                **self.to_dict(),
                "review": {
                    **dict(self.review),
                    "decision": decision,
                    "reviewer": reviewer,
                    "reason": reason,
                    "reviewed_at": now_iso(),
                },
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "feedback_id": self.feedback_id,
            "task_id": self.task_id,
            "trajectory_id": self.trajectory_id,
            "user_ref": self.user_ref,
            "session_ref": self.session_ref,
            "feedback_type": self.feedback_type,
            "rating": self.rating,
            "selected_candidate": self.selected_candidate,
            "correction": self.correction,
            "reason_category": self.reason_category,
            "confidence": self.confidence,
            "source": self.source,
            "status": self.status,
            "review": dict(self.review),
            "evidence": list(self.evidence),
            "metadata": dict(self.metadata),
            "timestamp": self.timestamp,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HumanFeedback:
        status = _text(data.get("status"))
        if status and not any(member.value == status for member in FeedbackStatus):
            status = ""
        return cls(
            feedback_id=_text(data.get("feedback_id")) or uuid4().hex,
            task_id=_text(data.get("task_id")),
            trajectory_id=_text(data.get("trajectory_id")),
            user_ref=_text(data.get("user_ref")),
            session_ref=_text(data.get("session_ref")),
            feedback_type=_text(
                data.get("feedback_type"), HumanFeedbackType.ACCEPT.value
            ),
            rating=_real(data.get("rating")),
            selected_candidate=_text(data.get("selected_candidate")),
            correction=_text(data.get("correction")),
            reason_category=_text(data.get("reason_category")),
            confidence=_real(data.get("confidence")),
            source=_text(data.get("source"), RewardSource.HUMAN.value),
            status=status,
            review=_mapping(data.get("review")),
            evidence=_texts(data.get("evidence")),
            metadata=_mapping(data.get("metadata")),
            timestamp=_text(data.get("timestamp")) or now_iso(),
            schema_version=_whole(data.get("schema_version"), FEEDBACK_SCHEMA_VERSION)
            or FEEDBACK_SCHEMA_VERSION,
        )


# ── AI ratings ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CriterionScore:
    """One criterion's score, with the observable fact it was read from."""

    name: str
    score: float = 0.0
    weight: float = 1.0
    reason: str = ""

    @property
    def contribution(self) -> float:
        return max(0.0, min(1.0, self.score)) * self.weight

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": round(self.score, 6),
            "weight": self.weight,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CriterionScore:
        return cls(
            name=_text(data.get("name")),
            score=_real(data.get("score"), 0.0) or 0.0,
            weight=_real(data.get("weight"), 1.0) or 1.0,
            reason=_text(data.get("reason")),
        )


@dataclass(frozen=True, slots=True)
class AIRating:
    """An evaluator's structured judgement of one observable candidate.

    This is RLAIF's unit of feedback. It contains criterion scores, each with a
    reason naming what was observed, plus a confidence and the evaluator's
    identity — and nothing resembling a chain of thought. An evaluator that
    cannot answer structurally is unavailable rather than improvised.
    """

    rating_id: str = field(default_factory=lambda: uuid4().hex)
    trajectory_id: str = ""
    task_id: str = ""
    evaluator_id: str = ""
    evaluator_kind: str = EvaluatorKind.RULE.value
    evaluator_version: str = RLHF_VERSION
    score: float = 0.0
    criteria: tuple[CriterionScore, ...] = ()
    confidence: float | None = None
    evidence: tuple[str, ...] = ()
    candidate: Mapping[str, Any] = field(default_factory=dict)
    mode: str = RLMode.RLAIF.value
    status: str = FeedbackStatus.ACCEPTED.value
    created_at: str = field(default_factory=now_iso)
    schema_version: int = RATING_SCHEMA_VERSION

    def criterion(self, name: str) -> CriterionScore | None:
        for item in self.criteria:
            if item.name == name:
                return item
        return None

    def agrees_with(self, other: AIRating, *, tolerance: float = 0.25) -> bool:
        """Whether two ratings read the same way, within a stated tolerance."""
        return abs(self.score - other.score) <= max(0.0, tolerance)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rating_id": self.rating_id,
            "trajectory_id": self.trajectory_id,
            "task_id": self.task_id,
            "evaluator_id": self.evaluator_id,
            "evaluator_kind": self.evaluator_kind,
            "evaluator_version": self.evaluator_version,
            "score": round(self.score, 6),
            "criteria": [item.to_dict() for item in self.criteria],
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "candidate": dict(self.candidate),
            "mode": self.mode,
            "status": self.status,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AIRating:
        rows = data.get("criteria")
        criteria: list[CriterionScore] = []
        if isinstance(rows, (list, tuple)):
            criteria = [
                CriterionScore.from_dict(row) for row in rows if isinstance(row, Mapping)
            ]
        return cls(
            rating_id=_text(data.get("rating_id")) or uuid4().hex,
            trajectory_id=_text(data.get("trajectory_id")),
            task_id=_text(data.get("task_id")),
            evaluator_id=_text(data.get("evaluator_id")),
            evaluator_kind=_text(data.get("evaluator_kind"), EvaluatorKind.RULE.value),
            evaluator_version=_text(data.get("evaluator_version"), RLHF_VERSION),
            score=max(0.0, min(1.0, _real(data.get("score"), 0.0) or 0.0)),
            criteria=tuple(criteria),
            confidence=_real(data.get("confidence")),
            evidence=_texts(data.get("evidence")),
            candidate=_mapping(data.get("candidate")),
            mode=_text(data.get("mode"), RLMode.RLAIF.value),
            status=_text(data.get("status"), FeedbackStatus.ACCEPTED.value),
            created_at=_text(data.get("created_at")) or now_iso(),
            schema_version=_whole(data.get("schema_version"), RATING_SCHEMA_VERSION)
            or RATING_SCHEMA_VERSION,
        )


# ── reward integrity ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RewardIntegrityFinding:
    """One specific reason a reward is not fully trusted."""

    code: str
    severity: str = "warning"
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RewardIntegrityFinding:
        return cls(
            code=_text(data.get("code")),
            severity=_text(data.get("severity"), "warning"),
            detail=_text(data.get("detail")),
        )


@dataclass(frozen=True, slots=True)
class RewardIntegrityCheck:
    """The reward-hacking audit of one reward, with its findings attached.

    The statuses are a ladder, not a label: ``valid`` means the reward survived,
    ``suspicious`` means it is held for review, ``invalid`` means it must not be
    learned from, and ``needs_review`` means the evidence contradicts itself.
    A reward that is not ``valid`` never silently becomes training data — the
    dataset builder refuses it or holds it, and the reason is stored here.
    """

    check_id: str = field(default_factory=lambda: uuid4().hex)
    subject_id: str = ""
    reward_id: str = ""
    source: str = ""
    status: str = RewardIntegrityStatus.VALID.value
    findings: tuple[RewardIntegrityFinding, ...] = ()
    total_reward: float = 0.0
    checked_at: str = field(default_factory=now_iso)
    schema_version: int = INTEGRITY_SCHEMA_VERSION

    @property
    def trusted(self) -> bool:
        return self.status == RewardIntegrityStatus.VALID.value

    @property
    def flagged(self) -> bool:
        return not self.trusted

    def codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "subject_id": self.subject_id,
            "reward_id": self.reward_id,
            "source": self.source,
            "status": self.status,
            "findings": [item.to_dict() for item in self.findings],
            "total_reward": round(self.total_reward, 6),
            "checked_at": self.checked_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RewardIntegrityCheck:
        rows = data.get("findings")
        findings: list[RewardIntegrityFinding] = []
        if isinstance(rows, (list, tuple)):
            findings = [
                RewardIntegrityFinding.from_dict(row)
                for row in rows
                if isinstance(row, Mapping)
            ]
        status = _text(data.get("status"), RewardIntegrityStatus.VALID.value)
        if not any(member.value == status for member in RewardIntegrityStatus):
            status = RewardIntegrityStatus.NEEDS_REVIEW.value
        return cls(
            check_id=_text(data.get("check_id")) or uuid4().hex,
            subject_id=_text(data.get("subject_id")),
            reward_id=_text(data.get("reward_id")),
            source=_text(data.get("source")),
            status=status,
            findings=tuple(findings),
            total_reward=_real(data.get("total_reward"), 0.0) or 0.0,
            checked_at=_text(data.get("checked_at")) or now_iso(),
            schema_version=_whole(data.get("schema_version"), INTEGRITY_SCHEMA_VERSION)
            or INTEGRITY_SCHEMA_VERSION,
        )


# ── disagreement ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Disagreement:
    """Two signals reading the same subject differently, recorded, not resolved.

    The detector's whole job is to NOTICE. Choosing a winner here would hide the
    very cases a person should look at, so the record names both readings, the
    gap between them and what to do next (``review`` or ``verify``).
    """

    disagreement_id: str = field(default_factory=lambda: uuid4().hex)
    kind: str = DisagreementKind.HUMAN_VS_AI.value
    subject_id: str = ""
    left_source: str = ""
    right_source: str = ""
    left_reading: float | None = None
    right_reading: float | None = None
    gap: float | None = None
    detail: str = ""
    recommended: str = "review"
    detected_at: str = field(default_factory=now_iso)
    schema_version: int = RLHF_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "disagreement_id": self.disagreement_id,
            "kind": self.kind,
            "subject_id": self.subject_id,
            "left_source": self.left_source,
            "right_source": self.right_source,
            "left_reading": self.left_reading,
            "right_reading": self.right_reading,
            "gap": self.gap,
            "detail": self.detail,
            "recommended": self.recommended,
            "detected_at": self.detected_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Disagreement:
        kind = _text(data.get("kind"), DisagreementKind.HUMAN_VS_AI.value)
        return cls(
            disagreement_id=_text(data.get("disagreement_id")) or uuid4().hex,
            kind=kind,
            subject_id=_text(data.get("subject_id")),
            left_source=_text(data.get("left_source")),
            right_source=_text(data.get("right_source")),
            left_reading=_real(data.get("left_reading")),
            right_reading=_real(data.get("right_reading")),
            gap=_real(data.get("gap")),
            detail=_text(data.get("detail")),
            recommended=_text(data.get("recommended"), "review"),
            detected_at=_text(data.get("detected_at")) or now_iso(),
            schema_version=_whole(data.get("schema_version"), RLHF_SCHEMA_VERSION)
            or RLHF_SCHEMA_VERSION,
        )


# ── rollouts ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RolloutStep:
    """One turn of a rollout: what the policy did, what the environment said."""

    index: int = 0
    action: Mapping[str, Any] = field(default_factory=dict)
    observation: Mapping[str, Any] = field(default_factory=dict)
    reward: Mapping[str, Any] = field(default_factory=dict)
    terminal: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": dict(self.action),
            "observation": dict(self.observation),
            "reward": dict(self.reward),
            "terminal": self.terminal,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RolloutStep:
        return cls(
            index=max(0, _whole(data.get("index"))),
            action=_mapping(data.get("action")),
            observation=_mapping(data.get("observation")),
            reward=_mapping(data.get("reward")),
            terminal=_flag(data.get("terminal")),
            metadata=_mapping(data.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class RewardBreakdown:
    """A run's rewards split the way an RL learner needs them.

    Step rewards wherever a step was scored, the terminal reward as the outcome
    of the episode, the cumulative sum and — when a discount was configured — the
    discounted return. Phase 15's single total remains the total; this is the
    same reading viewed as a sequence, stored beside it rather than instead of it.
    """

    step_rewards: tuple[float, ...] = ()
    intermediate_reward: float = 0.0
    terminal_reward: float = 0.0
    cumulative_reward: float = 0.0
    discounted_reward: float = 0.0
    gamma: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_rewards": [round(value, 6) for value in self.step_rewards],
            "intermediate_reward": round(self.intermediate_reward, 6),
            "terminal_reward": round(self.terminal_reward, 6),
            "cumulative_reward": round(self.cumulative_reward, 6),
            "discounted_reward": round(self.discounted_reward, 6),
            "gamma": self.gamma,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RewardBreakdown:
        steps = data.get("step_rewards")
        return cls(
            step_rewards=tuple(
                float(item) for item in steps if isinstance(item, (int, float))
            )
            if isinstance(steps, (list, tuple))
            else (),
            intermediate_reward=_real(data.get("intermediate_reward"), 0.0) or 0.0,
            terminal_reward=_real(data.get("terminal_reward"), 0.0) or 0.0,
            cumulative_reward=_real(data.get("cumulative_reward"), 0.0) or 0.0,
            discounted_reward=_real(data.get("discounted_reward"), 0.0) or 0.0,
            gamma=_real(data.get("gamma"), 1.0) or 1.0,
        )


@dataclass(frozen=True, slots=True)
class Rollout:
    """One episode: a policy acting in an environment, with the reward it earned.

    The abstraction is deliberately environment-agnostic. A rollout is a state,
    a sequence of actions and observations, and a reward — whether the
    environment is a NovaControl task, a simulator or a benchmark loop. Nothing
    here assumes a game, and nothing here executes destructive actions: the
    environment decides what ``step`` means, and Phase 8's permission layer stays
    the gate for anything in the real world.
    """

    rollout_id: str = field(default_factory=lambda: uuid4().hex)
    task_id: str = ""
    trajectory_id: str = ""
    environment: str = ""
    policy: str = ""
    seed: int = 0
    status: str = RolloutStatus.COMPLETED.value
    steps: tuple[RolloutStep, ...] = ()
    total_reward: float = 0.0
    source: str = RewardSource.RULE.value
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    schema_version: int = ROLLOUT_SCHEMA_VERSION

    @property
    def length(self) -> int:
        return len(self.steps)

    @property
    def terminal_step(self) -> RolloutStep | None:
        for step in reversed(self.steps):
            if step.terminal:
                return step
        return self.steps[-1] if self.steps else None

    def step_rewards(self) -> tuple[float, ...]:
        return tuple(
            _real(step.reward.get("total_reward"), 0.0) or 0.0 for step in self.steps
        )

    def breakdown(self, *, gamma: float = 1.0) -> RewardBreakdown:
        """The same rewards as a step/terminal/discounted reading."""
        rewards = self.step_rewards()
        terminal = rewards[-1] if rewards else 0.0
        intermediate = sum(rewards[:-1])
        cumulative = sum(rewards)
        discounted = sum(
            reward * (max(0.0, min(1.0, gamma)) ** index)
            for index, reward in enumerate(rewards)
        )
        return RewardBreakdown(
            step_rewards=rewards,
            intermediate_reward=intermediate,
            terminal_reward=terminal,
            cumulative_reward=cumulative,
            discounted_reward=discounted,
            gamma=gamma,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rollout_id": self.rollout_id,
            "task_id": self.task_id,
            "trajectory_id": self.trajectory_id,
            "environment": self.environment,
            "policy": self.policy,
            "seed": self.seed,
            "status": self.status,
            "steps": [step.to_dict() for step in self.steps],
            "total_reward": round(self.total_reward, 6),
            "source": self.source,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Rollout:
        rows = data.get("steps")
        steps: list[RolloutStep] = []
        if isinstance(rows, (list, tuple)):
            steps = [RolloutStep.from_dict(row) for row in rows if isinstance(row, Mapping)]
        return cls(
            rollout_id=_text(data.get("rollout_id")) or uuid4().hex,
            task_id=_text(data.get("task_id")),
            trajectory_id=_text(data.get("trajectory_id")),
            environment=_text(data.get("environment")),
            policy=_text(data.get("policy")),
            seed=_whole(data.get("seed")),
            status=_text(data.get("status"), RolloutStatus.COMPLETED.value),
            steps=tuple(steps),
            total_reward=_real(data.get("total_reward"), 0.0) or 0.0,
            source=_text(data.get("source"), RewardSource.RULE.value),
            metadata=_mapping(data.get("metadata")),
            created_at=_text(data.get("created_at")) or now_iso(),
            schema_version=_whole(data.get("schema_version"), ROLLOUT_SCHEMA_VERSION)
            or ROLLOUT_SCHEMA_VERSION,
        )


# ── policy updates ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PolicyUpdate:
    """What one optimization pass did, in numbers a reader can check.

    ``simulated`` is the field that keeps this honest: the mock optimizer's
    update is a deterministic walk over rewards, not a gradient step, and a run
    built on it says so. A future PPO/GRPO optimizer fills the same record with
    real readings; nothing else has to change.
    """

    algorithm: str = PolicyAlgorithm.MOCK.value
    steps: int = 0
    examples: int = 0
    epochs: int = 0
    mean_reward: float = 0.0
    reward_std: float = 0.0
    min_reward: float = 0.0
    max_reward: float = 0.0
    mean_advantage: float = 0.0
    advantage_std: float = 0.0
    policy_delta: float = 0.0
    entropy: float = 0.0
    clip_fraction: float = 0.0
    simulated: bool = True
    notes: tuple[str, ...] = ()
    timestamp: str = field(default_factory=now_iso)
    schema_version: int = POLICY_UPDATE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "steps": self.steps,
            "examples": self.examples,
            "epochs": self.epochs,
            "mean_reward": round(self.mean_reward, 6),
            "reward_std": round(self.reward_std, 6),
            "min_reward": round(self.min_reward, 6),
            "max_reward": round(self.max_reward, 6),
            "mean_advantage": round(self.mean_advantage, 6),
            "advantage_std": round(self.advantage_std, 6),
            "policy_delta": round(self.policy_delta, 6),
            "entropy": round(self.entropy, 6),
            "clip_fraction": round(self.clip_fraction, 6),
            "simulated": self.simulated,
            "notes": list(self.notes),
            "timestamp": self.timestamp,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PolicyUpdate:
        return cls(
            algorithm=_text(data.get("algorithm"), PolicyAlgorithm.MOCK.value),
            steps=max(0, _whole(data.get("steps"))),
            examples=max(0, _whole(data.get("examples"))),
            epochs=max(0, _whole(data.get("epochs"))),
            mean_reward=_real(data.get("mean_reward"), 0.0) or 0.0,
            reward_std=_real(data.get("reward_std"), 0.0) or 0.0,
            min_reward=_real(data.get("min_reward"), 0.0) or 0.0,
            max_reward=_real(data.get("max_reward"), 0.0) or 0.0,
            mean_advantage=_real(data.get("mean_advantage"), 0.0) or 0.0,
            advantage_std=_real(data.get("advantage_std"), 0.0) or 0.0,
            policy_delta=_real(data.get("policy_delta"), 0.0) or 0.0,
            entropy=_real(data.get("entropy"), 0.0) or 0.0,
            clip_fraction=_real(data.get("clip_fraction"), 0.0) or 0.0,
            simulated=_flag(data.get("simulated"), True),
            notes=_texts(data.get("notes")),
            timestamp=_text(data.get("timestamp")) or now_iso(),
            schema_version=_whole(data.get("schema_version"), POLICY_UPDATE_SCHEMA_VERSION)
            or POLICY_UPDATE_SCHEMA_VERSION,
        )


# ── reward datasets ─────────────────────────────────────────────────────────

#: A reward example is either a preference-style pair reading or a single
#: candidate with a reward. ``pair`` carries both sides when the feedback was a
#: PREFER_* or an RLHF comparison; ``single`` is one trajectory with one reward.
REWARD_EXAMPLE_KINDS: tuple[str, ...] = ("single", "pair")


@dataclass(frozen=True, slots=True)
class RewardExample:
    """One reward-labelled row: a trajectory (or a pair) and what it was worth.

    ``status`` is the quality verdict (accepted / rejected / needs_review) and
    ``integrity`` is the reward-hacking audit. Both travel with the row, so a
    dataset can be re-read years later and still say why each row is there.
    """

    example_id: str = field(default_factory=lambda: uuid4().hex)
    kind: str = "single"
    trajectory_id: str = ""
    task_id: str = ""
    mode: str = ""
    task: Mapping[str, Any] = field(default_factory=dict)
    candidate: Mapping[str, Any] = field(default_factory=dict)
    rejected_candidate: Mapping[str, Any] = field(default_factory=dict)
    reward: RewardResult = field(default_factory=RewardResult)
    reward_source: str = RewardSource.RULE.value
    integrity: RewardIntegrityCheck = field(default_factory=RewardIntegrityCheck)
    status: str = FeedbackStatus.ACCEPTED.value
    reasons: tuple[str, ...] = ()
    feedback_ids: tuple[str, ...] = ()
    rating_ids: tuple[str, ...] = ()
    rollout_id: str = ""
    difficulty: str = "simple"
    tags: tuple[str, ...] = ()
    created_at: str = field(default_factory=now_iso)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = REWARD_DATASET_SCHEMA_VERSION

    @property
    def accepted(self) -> bool:
        return self.status == FeedbackStatus.ACCEPTED.value

    @property
    def trusted(self) -> bool:
        return self.accepted and self.integrity.trusted

    @property
    def total_reward(self) -> float:
        return self.reward.total_reward

    def group_key(self) -> str:
        """What must not be split across train and test: the task, when known."""
        return self.task_id or self.trajectory_id or self.example_id

    def fingerprint(self) -> str:
        """Content identity of the row (ids and timestamps excluded)."""
        payload = json.dumps(
            {
                "kind": self.kind,
                "trajectory_id": self.trajectory_id,
                "task": self.task,
                "candidate": self.candidate,
                "rejected_candidate": self.rejected_candidate,
                "reward": round(self.reward.total_reward, 6),
                "source": self.reward_source,
                "status": self.status,
                "reasons": sorted(self.reasons),
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def as_sft_example(self) -> Any:
        """The row as a Phase 16 example, so the shared splitter can place it.

        Only used for its GROUPING behaviour — the splitter walks examples and
        this projection gives it a stable id and the task group key. The reward
        is carried as the target so the projection is meaningful, not a stub.
        """
        from novacontrol.training.models import SFTTrainingExample

        return SFTTrainingExample(
            example_id=self.example_id,
            input=dict(self.task),
            target={"reward": round(self.reward.total_reward, 6)},
            source_trajectory_id=self.trajectory_id,
            metadata={"task_id": self.task_id},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "example_id": self.example_id,
            "kind": self.kind,
            "trajectory_id": self.trajectory_id,
            "task_id": self.task_id,
            "mode": self.mode,
            "task": dict(self.task),
            "candidate": dict(self.candidate),
            "rejected_candidate": dict(self.rejected_candidate),
            "reward": self.reward.to_dict(),
            "reward_source": self.reward_source,
            "integrity": self.integrity.to_dict(),
            "status": self.status,
            "reasons": list(self.reasons),
            "feedback_ids": list(self.feedback_ids),
            "rating_ids": list(self.rating_ids),
            "rollout_id": self.rollout_id,
            "difficulty": self.difficulty,
            "tags": list(self.tags),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RewardExample:
        reward = data.get("reward")
        integrity = data.get("integrity")
        return cls(
            example_id=_text(data.get("example_id")) or uuid4().hex,
            kind=_text(data.get("kind"), "single"),
            trajectory_id=_text(data.get("trajectory_id")),
            task_id=_text(data.get("task_id")),
            mode=_text(data.get("mode")),
            task=_mapping(data.get("task")),
            candidate=_mapping(data.get("candidate")),
            rejected_candidate=_mapping(data.get("rejected_candidate")),
            reward=RewardResult.from_dict(dict(reward))
            if isinstance(reward, Mapping)
            else RewardResult(),
            reward_source=_text(data.get("reward_source"), RewardSource.RULE.value),
            integrity=RewardIntegrityCheck.from_dict(dict(integrity))
            if isinstance(integrity, Mapping)
            else RewardIntegrityCheck(),
            status=_text(data.get("status"), FeedbackStatus.ACCEPTED.value),
            reasons=_texts(data.get("reasons")),
            feedback_ids=_texts(data.get("feedback_ids")),
            rating_ids=_texts(data.get("rating_ids")),
            rollout_id=_text(data.get("rollout_id")),
            difficulty=_text(data.get("difficulty"), "simple"),
            tags=_texts(data.get("tags")),
            created_at=_text(data.get("created_at")) or now_iso(),
            metadata=_mapping(data.get("metadata")),
            schema_version=_whole(
                data.get("schema_version"), REWARD_DATASET_SCHEMA_VERSION
            )
            or REWARD_DATASET_SCHEMA_VERSION,
        )


@dataclass(frozen=True, slots=True)
class RewardDatasetStatistics:
    """What a built dataset contains, counted rather than described."""

    total: int = 0
    accepted: int = 0
    rejected: int = 0
    needs_review: int = 0
    trusted: int = 0
    flagged: int = 0
    by_source: Mapping[str, int] = field(default_factory=dict)
    by_mode: Mapping[str, int] = field(default_factory=dict)
    by_integrity: Mapping[str, int] = field(default_factory=dict)
    average_reward: float = 0.0
    reward_std: float = 0.0
    duplicates_removed: int = 0
    sensitive_removed: int = 0
    malformed_removed: int = 0
    groups: int = 0
    source_trajectories: int = 0
    estimated_tokens: int = 0
    by_split: Mapping[str, int] = field(default_factory=dict)
    skipped: Mapping[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "needs_review": self.needs_review,
            "trusted": self.trusted,
            "flagged": self.flagged,
            "by_source": dict(self.by_source),
            "by_mode": dict(self.by_mode),
            "by_integrity": dict(self.by_integrity),
            "average_reward": round(self.average_reward, 6),
            "reward_std": round(self.reward_std, 6),
            "duplicates_removed": self.duplicates_removed,
            "sensitive_removed": self.sensitive_removed,
            "malformed_removed": self.malformed_removed,
            "groups": self.groups,
            "source_trajectories": self.source_trajectories,
            "estimated_tokens": self.estimated_tokens,
            "by_split": dict(self.by_split),
            "skipped": dict(self.skipped),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RewardDatasetStatistics:
        def counts(key: str) -> dict[str, int]:
            raw = data.get(key)
            result: dict[str, int] = {}
            if isinstance(raw, Mapping):
                for name, value in raw.items():
                    result[str(name)] = _whole(value)
            return result

        return cls(
            total=_whole(data.get("total")),
            accepted=_whole(data.get("accepted")),
            rejected=_whole(data.get("rejected")),
            needs_review=_whole(data.get("needs_review")),
            trusted=_whole(data.get("trusted")),
            flagged=_whole(data.get("flagged")),
            by_source=counts("by_source"),
            by_mode=counts("by_mode"),
            by_integrity=counts("by_integrity"),
            average_reward=_real(data.get("average_reward"), 0.0) or 0.0,
            reward_std=_real(data.get("reward_std"), 0.0) or 0.0,
            duplicates_removed=_whole(data.get("duplicates_removed")),
            sensitive_removed=_whole(data.get("sensitive_removed")),
            malformed_removed=_whole(data.get("malformed_removed")),
            groups=_whole(data.get("groups")),
            source_trajectories=_whole(data.get("source_trajectories")),
            estimated_tokens=_whole(data.get("estimated_tokens")),
            by_split=counts("by_split"),
            skipped=counts("skipped"),
        )


@dataclass(frozen=True, slots=True)
class RewardDatasetVersion:
    """An immutable, splittable collection of reward-labelled rows.

    The same rules Phase 16 and 17 learnt apply here: a ``name@version`` is
    written once, its fingerprint is a content hash, its splits keep a task
    together, and a run stores the string as its only reference. What is new is
    that rows carry their reward's SOURCE and INTEGRITY, so a future optimizer
    can be told to learn only from trusted human feedback, for example, instead
    of having to trust the dataset blindly.
    """

    dataset_version_id: str = ""
    name: str = ""
    version: str = ""
    description: str = ""
    mode: str = "mixed"
    examples: tuple[RewardExample, ...] = ()
    splits: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    selection: Mapping[str, Any] = field(default_factory=dict)
    split_config: Mapping[str, Any] = field(default_factory=dict)
    statistics: RewardDatasetStatistics = field(default_factory=RewardDatasetStatistics)
    sources: Mapping[str, int] = field(default_factory=dict)
    source_datasets: tuple[str, ...] = ()
    reward_version: str = REWARD_VERSION
    reward_policy: Mapping[str, Any] = field(default_factory=dict)
    source_data_version: str = ""
    preprocessing_version: str = RLHF_VERSION
    tags: tuple[str, ...] = ()
    schema_version: int = REWARD_DATASET_SCHEMA_VERSION
    created_at: str = field(default_factory=now_iso)

    def __len__(self) -> int:
        return len(self.examples)

    def split(self, name: str) -> tuple[RewardExample, ...]:
        wanted = _text(name)
        ids = set(self.splits.get(wanted, ()))
        if not ids:
            return ()
        return tuple(item for item in self.examples if item.example_id in ids)

    def split_names(self) -> tuple[str, ...]:
        return tuple(name for name, ids in self.splits.items() if ids)

    def accepted_examples(self) -> tuple[RewardExample, ...]:
        return tuple(item for item in self.examples if item.accepted)

    def trusted_examples(self) -> tuple[RewardExample, ...]:
        return tuple(item for item in self.examples if item.trusted)

    def by_source(self, source: str) -> tuple[RewardExample, ...]:
        wanted = _text(source).lower()
        return tuple(item for item in self.examples if item.reward_source == wanted)

    def fingerprint(self) -> str:
        """A content hash of the version: rows, verdicts, integrity, splits."""
        identity = [
            {
                "example": item.fingerprint(),
                "status": item.status,
                "reasons": sorted(item.reasons),
                "integrity": item.integrity.status,
                "source": item.reward_source,
            }
            for item in self.examples
        ]
        payload = json.dumps(
            {
                "name": self.name,
                "version": self.version,
                "mode": self.mode,
                "selection": self.selection,
                "reward_policy": self.reward_policy,
                "examples": identity,
                "splits": {
                    name: sorted(ids) for name, ids in self.splits.items() if ids
                },
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def validate(self) -> tuple[str, ...]:
        """Structural problems with this version (the builder owns the rules)."""
        from novacontrol.rlhf.datasets import RewardDatasetBuilder

        return RewardDatasetBuilder().validate(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_version_id": self.dataset_version_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "mode": self.mode,
            "examples": [item.to_dict() for item in self.examples],
            "splits": {name: list(ids) for name, ids in self.splits.items()},
            "selection": dict(self.selection),
            "split_config": dict(self.split_config),
            "statistics": self.statistics.to_dict(),
            "sources": dict(self.sources),
            "source_datasets": list(self.source_datasets),
            "reward_version": self.reward_version,
            "reward_policy": dict(self.reward_policy),
            "source_data_version": self.source_data_version,
            "preprocessing_version": self.preprocessing_version,
            "tags": list(self.tags),
            "schema_version": self.schema_version,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RewardDatasetVersion:
        rows = data.get("examples")
        examples: list[RewardExample] = []
        if isinstance(rows, (list, tuple)):
            examples = [
                RewardExample.from_dict(row) for row in rows if isinstance(row, Mapping)
            ]
        raw_splits = data.get("splits")
        splits: dict[str, tuple[str, ...]] = {}
        if isinstance(raw_splits, Mapping):
            for name, ids in raw_splits.items():
                splits[str(name)] = _texts(ids)
        statistics = data.get("statistics")
        return cls(
            dataset_version_id=_text(data.get("dataset_version_id")),
            name=_text(data.get("name")),
            version=_text(data.get("version")),
            description=_text(data.get("description")),
            mode=_text(data.get("mode"), "mixed"),
            examples=tuple(examples),
            splits=splits,
            selection=_mapping(data.get("selection")),
            split_config=_mapping(data.get("split_config")),
            statistics=RewardDatasetStatistics.from_dict(dict(statistics))
            if isinstance(statistics, Mapping)
            else RewardDatasetStatistics(),
            sources={
                str(key): _whole(value)
                for key, value in _mapping(data.get("sources")).items()
            },
            source_datasets=_texts(data.get("source_datasets")),
            reward_version=_text(data.get("reward_version"), REWARD_VERSION),
            reward_policy=_mapping(data.get("reward_policy")),
            source_data_version=_text(data.get("source_data_version")),
            preprocessing_version=_text(data.get("preprocessing_version"), RLHF_VERSION),
            tags=_texts(data.get("tags")),
            schema_version=_whole(data.get("schema_version"), REWARD_DATASET_SCHEMA_VERSION)
            or REWARD_DATASET_SCHEMA_VERSION,
            created_at=_text(data.get("created_at")) or now_iso(),
        )


__all__ = [
    "AIRating",
    "CriterionScore",
    "Disagreement",
    "DisagreementKind",
    "EVIDENCE_AI_RATING",
    "EVIDENCE_CORRECT_TOOL_SELECTED",
    "EVIDENCE_ERROR_REPORTED",
    "EVIDENCE_FEEDBACK_CORRECTION",
    "EVIDENCE_HUMAN_ACCEPT",
    "EVIDENCE_HUMAN_REJECT",
    "EVIDENCE_LATENCY_EXCEEDED",
    "EVIDENCE_SAFETY_VIOLATION_PREVENTED",
    "EVIDENCE_TASK_FAILED",
    "EVIDENCE_TASK_SUCCEEDED",
    "EVIDENCE_UNNECESSARY_ACTION",
    "EVIDENCE_UNSAFE_ACTION",
    "EVIDENCE_UNSAFE_REPORTED",
    "EVIDENCE_VERIFICATION_FAILED",
    "EVIDENCE_VERIFICATION_PASSED",
    "EVIDENCE_WRONG_TOOL_SELECTED",
    "EvaluatorKind",
    "FEEDBACK_ACCEPTED",
    "FEEDBACK_NEEDS_REVIEW",
    "FEEDBACK_REJECTED",
    "FEEDBACK_STATUSES",
    "FEEDBACK_TYPES",
    "FEEDBACK_SCHEMA_VERSION",
    "FeedbackIssue",
    "FeedbackStatus",
    "FeedbackVerdict",
    "HumanFeedback",
    "HumanFeedbackType",
    "IMPLEMENTED_ALGORITHMS",
    "INTEGRITY_SCHEMA_VERSION",
    "INTEGRITY_STATUSES",
    "MODES",
    "NEGATIVE_FEEDBACK_TYPES",
    "PLANNED_ALGORITHMS",
    "POLICY_ALGORITHMS",
    "POLICY_UPDATE_SCHEMA_VERSION",
    "PREFERENCE_FEEDBACK_TYPES",
    "PolicyAlgorithm",
    "PolicyUpdate",
    "RATING_MAX",
    "RATING_MIN",
    "RATING_SCHEMA_VERSION",
    "REWARD_DATASET_SCHEMA_VERSION",
    "REWARD_EXAMPLE_KINDS",
    "REWARD_SOURCES",
    "RLHF_SCHEMA_VERSION",
    "RLHF_VERSION",
    "RLMode",
    "RewardBreakdown",
    "RewardDatasetStatistics",
    "RewardDatasetVersion",
    "RewardExample",
    "RewardIntegrityCheck",
    "RewardIntegrityFinding",
    "RewardIntegrityStatus",
    "RewardSource",
    "Rollout",
    "RolloutStatus",
    "RolloutStep",
    "ROLLOUT_SCHEMA_VERSION",
]
