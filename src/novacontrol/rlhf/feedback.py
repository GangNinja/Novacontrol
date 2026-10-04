"""Human feedback and the filter that decides what may be learnt from it.

Feedback arrives from people, which means it arrives messy: two answers about the
same run that contradict each other, a rating on the wrong scale, a correction
with an API key pasted into it, a verdict about a task that never finished. This
module's job is to CLASSIFY that mess, not to hide it.

The rule the whole phase rests on: FEEDBACK IS NEVER SILENTLY DELETED. Every row
keeps its identity and gets a status (accepted / rejected / needs_review) plus the
reasons, so a person can look at what a filter rejected and change its mind. A
reviewed row is annotated again, never discarded.

It also does the one transformation that turns a person's answer into a signal: a
short structured statement (accept / reject / prefer / rating / correction) maps to
a reward on a -1..1 scale through the versioned :class:`RewardPolicyConfig`, so
what "a rejected run is worth" means is configuration, not a constant scattered
through the code.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.audit.redact import Redactor
from novacontrol.evaluation.models import AgentTrajectory
from novacontrol.rlhf.config import RewardPolicyConfig
from novacontrol.rlhf.models import (
    EVIDENCE_ERROR_REPORTED,
    EVIDENCE_FEEDBACK_CORRECTION,
    EVIDENCE_HUMAN_ACCEPT,
    EVIDENCE_HUMAN_REJECT,
    EVIDENCE_UNSAFE_REPORTED,
    FEEDBACK_ACCEPTED,
    FEEDBACK_NEEDS_REVIEW,
    FEEDBACK_REJECTED,
    RATING_MAX,
    RATING_MIN,
    FeedbackIssue,
    FeedbackVerdict,
    HumanFeedback,
    HumanFeedbackType,
)

#: Stable issue codes: a report a person reads names these, and a test asserts
#: them rather than a sentence that will drift.
REASON_UNKNOWN_TYPE = "unknown_feedback_type"
REASON_MISSING_TARGET = "missing_feedback_target"
REASON_INVALID_RATING = "invalid_rating"
REASON_MISSING_RATING = "missing_rating"
REASON_LOW_CONFIDENCE = "low_confidence"
REASON_DUPLICATE = "duplicate_feedback"
REASON_CONTRADICTORY = "contradictory_feedback"
REASON_TASK_UNFINISHED = "feedback_on_incomplete_task"
REASON_SENSITIVE = "sensitive_content"
REASON_IMPOSSIBLE = "impossible_values"
REASON_IMPOSSIBLE_CANDIDATE = "invalid_candidate_reference"

#: The correction field is stored, so it is bounded. A person explaining a fix in
#: more than a few hundred characters is writing a document, not a correction.
MAX_CORRECTION_LENGTH = 2000


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _sign(feedback: HumanFeedback) -> int:
    """-1, 0 or +1 for a row, used for contradiction detection."""
    if feedback.feedback_type in {
        HumanFeedbackType.REJECT.value,
        HumanFeedbackType.REPORT_ERROR.value,
        HumanFeedbackType.REPORT_UNSAFE.value,
    }:
        return -1
    if feedback.feedback_type in {
        HumanFeedbackType.ACCEPT.value,
        HumanFeedbackType.CORRECTION.value,
    }:
        return 1 if feedback.feedback_type == HumanFeedbackType.ACCEPT.value else 0
    if feedback.feedback_type == HumanFeedbackType.RATING.value:
        normalized = feedback.rating_normalized
        if normalized is None:
            return 0
        if normalized > 0.2:
            return 1
        if normalized < -0.2:
            return -1
        return 0
    if feedback.is_preference:
        return 1 if feedback.feedback_type == HumanFeedbackType.PREFER_A.value else -1
    return 0


@dataclass(frozen=True, slots=True)
class FeedbackQualityConfig:
    """What this installation refuses, holds or accepts in human feedback."""

    enabled: bool = True
    min_confidence: float = 0.2
    require_target: bool = True
    require_finished_task: bool = True
    detect_duplicates: bool = True
    detect_contradictions: bool = True
    reject_sensitive: bool = True
    redact_corrections: bool = True
    max_correction_length: int = MAX_CORRECTION_LENGTH
    #: A safety report is always accepted for review — refusing it because it
    #: mentions an unsafe action would delete the very signal the phase exists to
    #: capture. The flag exists so an installation can make this explicit.
    always_accept_unsafe_reports: bool = True
    keep_notes: bool = True

    def validate(self) -> tuple[str, ...]:
        problems: list[str] = []
        if not 0.0 <= self.min_confidence <= 1.0:
            problems.append("min_confidence must be between 0 and 1")
        if self.max_correction_length < 1:
            problems.append("max_correction_length must be positive")
        return tuple(problems)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "min_confidence": self.min_confidence,
            "require_target": self.require_target,
            "require_finished_task": self.require_finished_task,
            "detect_duplicates": self.detect_duplicates,
            "detect_contradictions": self.detect_contradictions,
            "reject_sensitive": self.reject_sensitive,
            "redact_corrections": self.redact_corrections,
            "max_correction_length": self.max_correction_length,
            "always_accept_unsafe_reports": self.always_accept_unsafe_reports,
            "keep_notes": self.keep_notes,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> FeedbackQualityConfig:
        defaults = cls()
        if not isinstance(data, Mapping):
            return defaults
        def flag(key: str, current: bool) -> bool:
            value = data.get(key)
            return value if isinstance(value, bool) else current

        return cls(
            enabled=flag("enabled", defaults.enabled),
            min_confidence=float(data.get("min_confidence", defaults.min_confidence)),
            require_target=bool(data.get("require_target", defaults.require_target)),
            require_finished_task=bool(
                data.get("require_finished_task", defaults.require_finished_task)
            ),
            detect_duplicates=bool(
                data.get("detect_duplicates", defaults.detect_duplicates)
            ),
            detect_contradictions=bool(
                data.get("detect_contradictions", defaults.detect_contradictions)
            ),
            reject_sensitive=bool(data.get("reject_sensitive", defaults.reject_sensitive)),
            redact_corrections=bool(
                data.get("redact_corrections", defaults.redact_corrections)
            ),
            max_correction_length=int(
                data.get("max_correction_length", defaults.max_correction_length)
            ),
            always_accept_unsafe_reports=bool(
                data.get("always_accept_unsafe_reports", defaults.always_accept_unsafe_reports)
            ),
            keep_notes=bool(data.get("keep_notes", defaults.keep_notes)),
        )


@dataclass(frozen=True, slots=True)
class ScreenedFeedback:
    """One row after screening: the redacted feedback and the verdict about it."""

    feedback: HumanFeedback
    verdict: FeedbackVerdict
    redactions: int = 0
    redaction_kinds: tuple[str, ...] = field(default=())

    @property
    def accepted(self) -> bool:
        return self.verdict.accepted

    @property
    def status(self) -> str:
        return self.verdict.status


class FeedbackQualityFilter:
    """Screens feedback: contradiction, duplication, validity, privacy, timing.

    The filter answers three questions in order. Is this row USABLE (a real type,
    a target, a rating where a rating belongs)? Is it CONSISTENT with what is
    already known about the same target (no duplicates, no flat contradictions)?
    Is it SAFE to store (nothing private left in it)? Anything unusable is
    REJECTED with reasons; anything inconsistent or unverifiable is HELD for a
    person; everything else is ACCEPTED.

    ``redact`` runs on every correction before the row is judged, so a secret can
    never reach storage even when the surrounding statement is rejected.
    """

    def __init__(
        self,
        config: FeedbackQualityConfig | None = None,
        *,
        redactor: Redactor | None = None,
        policy: RewardPolicyConfig | None = None,
    ) -> None:
        self.config = config if config is not None else FeedbackQualityConfig()
        self.redactor = redactor if redactor is not None else Redactor()
        self.policy = policy if policy is not None else RewardPolicyConfig()

    # -- public -----------------------------------------------------------------

    def apply(
        self,
        feedback: HumanFeedback,
        *,
        known: Sequence[HumanFeedback] = (),
        trajectory: AgentTrajectory | None = None,
    ) -> tuple[HumanFeedback | None, FeedbackVerdict]:
        """Redact, judge and annotate one row. ``None`` means it was dropped.

        A rejected row is still returned when it carries an id: the caller stores
        it annotated so the rejection is auditable. ``None`` is reserved for
        input that is not feedback at all (an empty row with no target and no
        type), which would otherwise pollute the review queue with noise.
        """
        cleaned, redactions, kinds = self.sanitise(feedback)
        verdict = self.assess(
            cleaned,
            known=known,
            trajectory=trajectory,
            redactions=redactions,
            redaction_kinds=kinds,
        )
        if not cleaned.feedback_type and not cleaned.target():
            return None, verdict
        annotated = cleaned.with_status(verdict.status, issues=verdict.issues)
        if redactions:
            annotated = HumanFeedback.from_dict(
                {
                    **annotated.to_dict(),
                    "evidence": list(
                        tuple(annotated.evidence) + ("sensitive_content_redacted",)
                    ),
                }
            )
        return annotated, verdict

    def sanitise(self, feedback: HumanFeedback) -> tuple[HumanFeedback, int, tuple[str, ...]]:
        """Redact the free-text parts of a row; report what was removed."""
        if not self.config.enabled or not self.config.redact_corrections:
            return feedback, 0, ()
        outcome = self.redactor.redact_text(feedback.correction)
        cleaned_text = outcome.text
        if len(cleaned_text) > self.config.max_correction_length:
            cleaned_text = cleaned_text[: self.config.max_correction_length] + "…"
        if outcome.count == 0 and cleaned_text == feedback.correction:
            return feedback, 0, ()
        return (
            HumanFeedback.from_dict({**feedback.to_dict(), "correction": cleaned_text}),
            int(outcome.count),
            tuple(outcome.kinds),
        )

    def assess(
        self,
        feedback: HumanFeedback,
        *,
        known: Sequence[HumanFeedback] = (),
        trajectory: AgentTrajectory | None = None,
        redactions: int = 0,
        redaction_kinds: Sequence[str] = (),
    ) -> FeedbackVerdict:
        """The verdict for an already-sanitised row (no further redaction)."""
        if not self.config.enabled:
            return FeedbackVerdict(
                status=FEEDBACK_ACCEPTED,
                notes=("the feedback filter is switched off for this installation",),
            )
        issues: list[FeedbackIssue] = []
        notes: list[str] = []

        if not feedback.typed:
            issues.append(
                FeedbackIssue(
                    REASON_UNKNOWN_TYPE,
                    "error",
                    f"{feedback.feedback_type!r} is not a feedback type this phase knows",
                )
            )
        if self.config.require_target and not feedback.target():
            issues.append(
                FeedbackIssue(
                    REASON_MISSING_TARGET,
                    "error",
                    "neither trajectory_id nor task_id names what was judged",
                )
            )
        self._judge_rating(feedback, issues)
        if feedback.confidence is not None and feedback.confidence < self.config.min_confidence:
            issues.append(
                FeedbackIssue(
                    REASON_LOW_CONFIDENCE,
                    "warning",
                    f"confidence {feedback.confidence} is below {self.config.min_confidence}",
                )
            )
        if feedback.selected_candidate and feedback.selected_candidate not in {"a", "b"}:
            issues.append(
                FeedbackIssue(
                    REASON_IMPOSSIBLE_CANDIDATE,
                    "error",
                    f"selected_candidate {feedback.selected_candidate!r} is not 'a' or 'b'",
                )
            )
        if redactions and self.config.reject_sensitive:
            notes.append(
                f"{redactions} sensitive value(s) were redacted "
                f"({', '.join(redaction_kinds) or 'unknown kind'}); the row kept its "
                "verdict and carries redactable text no further"
            )
        self._judge_task(feedback, trajectory, issues, notes)
        self._judge_against_known(feedback, known, issues, notes)

        status = self._status(feedback, issues)
        if (
            status == FEEDBACK_REJECTED
            and feedback.feedback_type == HumanFeedbackType.REPORT_UNSAFE.value
            and self.config.always_accept_unsafe_reports
        ):
            status = FEEDBACK_NEEDS_REVIEW
            notes.append(
                "an unsafe-action report is held for review rather than rejected: "
                "the report is the safety signal, not noise"
            )
        return FeedbackVerdict(status=status, issues=tuple(issues), notes=tuple(notes))

    def has_sensitive_content(self, feedback: HumanFeedback) -> bool:
        if not feedback.correction:
            return False
        return int(self.redactor.redact_text(feedback.correction).count) > 0

    # -- the individual judgements ----------------------------------------------

    @staticmethod
    def _judge_rating(feedback: HumanFeedback, issues: list[FeedbackIssue]) -> None:
        if feedback.feedback_type == HumanFeedbackType.RATING.value:
            if feedback.rating is None:
                issues.append(
                    FeedbackIssue(
                        REASON_MISSING_RATING, "error", "a rating row carries no rating"
                    )
                )
                return
        elif feedback.rating is not None and not feedback.is_preference:
            issues.append(
                FeedbackIssue(
                    REASON_INVALID_RATING,
                    "error",
                    f"feedback_type {feedback.feedback_type!r} does not take a rating",
                )
            )
        if feedback.rating is None:
            return
        if (
            isinstance(feedback.rating, bool)
            or not RATING_MIN <= float(feedback.rating) <= RATING_MAX
        ):
            issues.append(
                FeedbackIssue(
                    REASON_INVALID_RATING,
                    "error",
                    f"rating {feedback.rating!r} is outside {RATING_MIN:g}..{RATING_MAX:g}",
                )
            )
        if feedback.rating != round(feedback.rating, 6):
            issues.append(
                FeedbackIssue(
                    REASON_IMPOSSIBLE,
                    "warning",
                    f"rating {feedback.rating!r} has more precision than the scale",
                )
            )

    @staticmethod
    def _judge_task(
        feedback: HumanFeedback,
        trajectory: AgentTrajectory | None,
        issues: list[FeedbackIssue],
        notes: list[str],
    ) -> None:
        if trajectory is None:
            return
        if feedback.trajectory_id and trajectory.trajectory_id != feedback.trajectory_id:
            issues.append(
                FeedbackIssue(
                    REASON_MISSING_TARGET,
                    "warning",
                    "the feedback names a different trajectory than the one supplied",
                )
            )
        if not trajectory.terminal:
            issues.append(
                FeedbackIssue(
                    REASON_TASK_UNFINISHED,
                    "warning",
                    "the run this feedback judges had not finished",
                )
            )
        elif trajectory.success is None:
            notes.append(
                "the run ended without a recorded outcome, so this feedback is "
                "the only reading of it"
            )

    @staticmethod
    def _judge_against_known(
        feedback: HumanFeedback,
        known: Sequence[HumanFeedback],
        issues: list[FeedbackIssue],
        notes: list[str],
    ) -> None:
        target = feedback.target()
        if not target:
            return
        mine = feedback.fingerprint()
        for other in known:
            if other.feedback_id == feedback.feedback_id or other.target() != target:
                continue
            if other.fingerprint() == mine:
                issues.append(
                    FeedbackIssue(
                        REASON_DUPLICATE,
                        "warning",
                        f"the same feedback was already recorded as {other.feedback_id}",
                    )
                )
                continue
            same_type = other.feedback_type == feedback.feedback_type
            left = _sign(other)
            right = _sign(feedback)
            if same_type and left and right and left != right:
                issues.append(
                    FeedbackIssue(
                        REASON_CONTRADICTORY,
                        "warning",
                        f"feedback {other.feedback_id} says "
                        f"{'good' if left > 0 else 'bad'} about {target} while this "
                        f"row says {'good' if right > 0 else 'bad'}",
                    )
                )
            elif (
                other.feedback_type == HumanFeedbackType.ACCEPT.value
                and feedback.feedback_type == HumanFeedbackType.REJECT.value
            ) or (
                other.feedback_type == HumanFeedbackType.REJECT.value
                and feedback.feedback_type == HumanFeedbackType.ACCEPT.value
            ):
                issues.append(
                    FeedbackIssue(
                        REASON_CONTRADICTORY,
                        "warning",
                        f"feedback {other.feedback_id} and this row disagree about "
                        f"whether {target} was acceptable",
                    )
                )

    @staticmethod
    def _status(feedback: HumanFeedback, issues: Sequence[FeedbackIssue]) -> str:
        del feedback
        if any(issue.severity == "error" for issue in issues):
            return FEEDBACK_REJECTED
        if issues:
            return FEEDBACK_NEEDS_REVIEW
        return FEEDBACK_ACCEPTED


def feedback_reward_value(feedback: HumanFeedback, policy: RewardPolicyConfig) -> float:
    """One feedback row as a -1..1 reward signal, through the versioned policy.

    A ``rating`` uses the rating itself; every other type uses the policy's
    mapping. A ``prefer_b`` row is deliberately negative: from THIS candidate's
    point of view the other side was better, and the sign is what the learner
    consumes. Nothing here reads a reason or a hidden trace.
    """
    if feedback.feedback_type == HumanFeedbackType.RATING.value:
        normalized = feedback.rating_normalized
        return 0.0 if normalized is None else float(normalized)
    return policy.value(feedback.feedback_type)


def feedback_evidence(feedback: HumanFeedback) -> tuple[str, ...]:
    """The observable facts a feedback row cites."""
    kind = feedback.feedback_type
    if kind == HumanFeedbackType.ACCEPT.value:
        return (EVIDENCE_HUMAN_ACCEPT,)
    if kind in {
        HumanFeedbackType.REJECT.value,
        HumanFeedbackType.PREFER_B.value,
    }:
        return (EVIDENCE_HUMAN_REJECT,)
    if kind == HumanFeedbackType.PREFER_A.value:
        return (EVIDENCE_HUMAN_ACCEPT,)
    if kind == HumanFeedbackType.RATING.value:
        accepted = (feedback.rating_normalized or 0.0) >= 0
        return (EVIDENCE_HUMAN_ACCEPT if accepted else EVIDENCE_HUMAN_REJECT,)
    if kind == HumanFeedbackType.CORRECTION.value:
        return (EVIDENCE_FEEDBACK_CORRECTION,)
    if kind == HumanFeedbackType.REPORT_ERROR.value:
        return (EVIDENCE_ERROR_REPORTED,)
    if kind == HumanFeedbackType.REPORT_UNSAFE.value:
        return (EVIDENCE_UNSAFE_REPORTED,)
    return ()


__all__ = [
    "MAX_CORRECTION_LENGTH",
    "REASON_CONTRADICTORY",
    "REASON_DUPLICATE",
    "REASON_IMPOSSIBLE",
    "REASON_IMPOSSIBLE_CANDIDATE",
    "REASON_INVALID_RATING",
    "REASON_LOW_CONFIDENCE",
    "REASON_MISSING_RATING",
    "REASON_MISSING_TARGET",
    "REASON_SENSITIVE",
    "REASON_TASK_UNFINISHED",
    "REASON_UNKNOWN_TYPE",
    "FeedbackQualityConfig",
    "FeedbackQualityFilter",
    "ScreenedFeedback",
    "feedback_evidence",
    "feedback_reward_value",
]
