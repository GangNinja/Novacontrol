"""The human review queue: a person's decision, recorded as evidence.

Some pairs cannot be settled by the record alone. An evaluation margin inside the
tolerance, two candidates that both look right, a preference nobody observed —
these are exactly the pairs a filter should hold rather than guess about, and this
is where a person settles them.

What a reviewer is shown is deliberately the whole observable case: the prompt and
its context, BOTH candidate behaviours, the outcomes behind them, the verification
readings, the evaluation scores, and the evidence the extractor used. What a
reviewer is never asked for is prose reasoning about why — a review is a decision
between two behaviours plus, optionally, a short structured reason, and that
decision is stored as :data:`~novacontrol.preference.models.EVIDENCE_HUMAN_REVIEW`
evidence on the pair itself. Nothing here writes a chain of thought anywhere.

"Candidate A" always means the side the system currently holds as ``chosen``, and
"B" the side it holds as ``rejected``. Choosing B therefore SWAPS the pair rather
than inventing a new one, so a reviewer's disagreement and the extractor's guess
are the same object with one history.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.audit.redact import Redactor
from novacontrol.preference.datasets import PreferenceDatasetBuilder
from novacontrol.preference.models import (
    PreferenceDatasetType,
    PreferenceExample,
    PreferenceQualityStatus,
    PreferenceReviewDecision,
)
from novacontrol.preference.quality import PreferenceQualityFilter
from novacontrol.preference.storage import PreferenceReviewRepository
from novacontrol.training.models import reasoning_violations

#: The decisions a reviewer may give, as a set the caller's answer is checked
#: against. ``tie`` and ``reject`` are both refusals; they are kept apart because
#: a tie says "these are equally good" and a rejection says "this pair is wrong",
#: and only the second is a statement about the extractor.
DECISIONS: tuple[str, ...] = tuple(member.value for member in PreferenceReviewDecision)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """One pair as a reviewer sees it: both sides, and everything that decided them."""

    preference_id: str
    dataset_type: str = PreferenceDatasetType.NLU.value
    prompt: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)
    candidate_a: Mapping[str, Any] = field(default_factory=dict)
    candidate_b: Mapping[str, Any] = field(default_factory=dict)
    chosen_outcome: Mapping[str, Any] = field(default_factory=dict)
    rejected_outcome: Mapping[str, Any] = field(default_factory=dict)
    chosen_evaluation: Mapping[str, Any] = field(default_factory=dict)
    rejected_evaluation: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[Mapping[str, Any], ...] = ()
    confidence: float = 0.0
    strength: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""
    verification: Mapping[str, Any] = field(default_factory=dict)
    quality: Mapping[str, Any] = field(default_factory=dict)
    review: Mapping[str, Any] = field(default_factory=dict)
    difficulty: str = "simple"
    tags: tuple[str, ...] = ()
    dataset_version: str = ""
    created_at: str = ""

    @classmethod
    def of(cls, pair: PreferenceExample) -> ReviewItem:
        """Present one stored pair (A is what is currently held as ``chosen``)."""
        return cls(
            preference_id=pair.preference_id,
            dataset_type=pair.dataset_type,
            prompt=dict(pair.prompt),
            context=dict(pair.context),
            candidate_a=dict(pair.chosen),
            candidate_b=dict(pair.rejected),
            chosen_outcome=dict(pair.chosen_outcome),
            rejected_outcome=dict(pair.rejected_outcome),
            chosen_evaluation=dict(pair.chosen_evaluation),
            rejected_evaluation=dict(pair.rejected_evaluation),
            evidence=tuple(item.to_dict() for item in pair.strength.evidence),
            confidence=pair.confidence,
            strength=pair.strength.to_dict(),
            source=pair.preference_source,
            verification={
                "candidate_a": dict(pair.chosen_outcome.get("verification") or {}),
                "candidate_b": dict(pair.rejected_outcome.get("verification") or {}),
                "verified": pair.strength.verified,
            },
            quality=dict(pair.quality),
            review=dict(pair.review),
            difficulty=pair.difficulty,
            tags=tuple(pair.tags),
            dataset_version=pair.dataset_version,
            created_at=pair.created_at,
        )

    @property
    def decided(self) -> bool:
        return bool(_text(self.review.get("decision")))

    def to_dict(self) -> dict[str, Any]:
        return {
            "preference_id": self.preference_id,
            "dataset_type": self.dataset_type,
            "prompt": dict(self.prompt or {}),
            "context": dict(self.context or {}),
            "candidate_a": dict(self.candidate_a or {}),
            "candidate_b": dict(self.candidate_b or {}),
            "chosen_outcome": dict(self.chosen_outcome or {}),
            "rejected_outcome": dict(self.rejected_outcome or {}),
            "chosen_evaluation": dict(self.chosen_evaluation or {}),
            "rejected_evaluation": dict(self.rejected_evaluation or {}),
            "evidence": [dict(item) for item in self.evidence],
            "confidence": self.confidence,
            "strength": dict(self.strength or {}),
            "source": self.source,
            "verification": dict(self.verification or {}),
            "quality": dict(self.quality or {}),
            "review": dict(self.review or {}),
            "difficulty": self.difficulty,
            "tags": list(self.tags),
            "dataset_version": self.dataset_version,
            "created_at": self.created_at,
            "decided": self.decided,
            "actions": list(DECISIONS),
        }


class PreferenceReviewQueue:
    """The queue a person works through, and the decisions it records."""

    def __init__(
        self,
        repository: PreferenceReviewRepository,
        *,
        builder: PreferenceDatasetBuilder | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.repository = repository
        self.builder = builder if builder is not None else PreferenceDatasetBuilder()
        self.quality_filter = PreferenceQualityFilter(
            redactor=redactor or self.builder.quality.redactor
        )
        self.failures = 0

    # -- getting pairs in --------------------------------------------------------

    def enqueue(self, pair: PreferenceExample) -> dict[str, Any]:
        """Put a pair in front of a person, keeping its quality verdict.

        A pair that carries reasoning anywhere is REFUSED rather than queued: a
        review screen is still a place a trace could be stored, and the phase's
        absolute rule does not have an exception for the queue.
        """
        violations = reasoning_violations(pair.to_dict(), path="pair")
        if violations:
            self.failures += 1
            return {
                "ok": False,
                "reason": (
                    "the pair carries hidden reasoning at "
                    + ", ".join(violations)
                    + " and was not queued"
                ),
            }
        held = (
            pair
            if pair.quality_status
            else pair.with_quality(
                PreferenceQualityStatus.NEEDS_REVIEW.value,
                reasons=("held for human review",),
            )
        )
        self.repository.save(held)
        return {
            "ok": True,
            "preference_id": held.preference_id,
            "item": ReviewItem.of(held).to_dict(),
        }

    def submit(
        self,
        *,
        dataset_type: str,
        prompt: Mapping[str, Any],
        chosen: Mapping[str, Any],
        rejected: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
        chosen_outcome: Mapping[str, Any] | None = None,
        rejected_outcome: Mapping[str, Any] | None = None,
        reviewer: str = "",
        reason: str = "",
        confidence: float = 1.0,
        group_key: str = "",
        tags: Sequence[str] = (),
        enqueue: bool = False,
    ) -> dict[str, Any]:
        """A pair a person puts together: the strongest preference there is.

        It is built exactly like an extracted pair — same sanitising, same quality
        pass, same evidence — so a submitted pair cannot arrive with fewer checks
        than a derived one. ``enqueue`` puts the built pair in the queue instead of
        returning it, for a workflow where the pair needs a second person.
        """
        pair = self.builder.human_pair(
            dataset_type,
            prompt=prompt,
            chosen=chosen,
            rejected=rejected,
            context=context,
            chosen_outcome=chosen_outcome,
            rejected_outcome=rejected_outcome,
            reviewer=reviewer,
            reason=reason,
            confidence=confidence,
            group_key=group_key,
            tags=tags,
        )
        cleaned, verdict = self.quality_filter.apply(pair)
        if cleaned is None:
            return {
                "ok": False,
                "reason": "the submitted pair was rejected: " + "; ".join(verdict.reasons),
                "verdict": verdict.to_dict(),
            }
        if enqueue:
            return self.enqueue(cleaned)
        self.repository.save(cleaned)
        return {
            "ok": True,
            "preference_id": cleaned.preference_id,
            "pair": cleaned.to_dict(),
            "verdict": verdict.to_dict(),
        }

    # -- looking at them ---------------------------------------------------------

    def queue(
        self, *, pending_only: bool = True, limit: int = 0, dataset_version: str = ""
    ) -> tuple[ReviewItem, ...]:
        wanted = _text(dataset_version)
        found = self.repository.list(pending_only=pending_only, limit=0)
        if wanted:
            found = tuple(pair for pair in found if pair.dataset_version == wanted)
        if limit and limit > 0:
            found = found[:limit]
        return tuple(ReviewItem.of(pair) for pair in found)

    def item(self, preference_id: str) -> ReviewItem | None:
        pair = self.repository.get(preference_id)
        return ReviewItem.of(pair) if pair is not None else None

    def pending_count(self) -> int:
        return len(self.repository.pending())

    # -- deciding -----------------------------------------------------------------

    def decide(
        self,
        preference_id: str,
        decision: PreferenceReviewDecision | str,
        *,
        reviewer: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        """Record a reviewer's decision: choose A, choose B, tie, or reject.

        The decision is applied to the pair (choosing B swaps its sides) and stored
        as a review entry plus human-review evidence, so the resulting preference is
        as well-sourced as any other and the reviewer's name travels with it.
        """
        pair = self.repository.get(preference_id)
        if pair is None:
            return {"ok": False, "reason": f"no queued pair {preference_id!r}"}
        wanted = (
            decision.value
            if isinstance(decision, PreferenceReviewDecision)
            else _text(decision)
        )
        if wanted not in DECISIONS:
            return {
                "ok": False,
                "reason": f"unknown decision {wanted!r}: expected one of " + ", ".join(DECISIONS),
            }
        refusals = {PreferenceReviewDecision.TIE.value, PreferenceReviewDecision.REJECT.value}
        if wanted in refusals and not reason:
            return {
                "ok": False,
                "reason": (
                    f"a {wanted} decision needs a reason: it refuses a pair for every "
                    "later build, so the reason is what makes that auditable"
                ),
            }
        settled = pair.with_review(wanted, reviewer=reviewer, reason=reason)
        self.repository.save(settled)
        return {
            "ok": True,
            "preference_id": settled.preference_id,
            "decision": wanted,
            "pair": settled.to_dict(),
            "item": ReviewItem.of(settled).to_dict(),
        }

    def discard(self, preference_id: str) -> dict[str, Any]:
        """Remove a pair from the queue entirely (its id and row are gone)."""
        removed = self.repository.remove(preference_id)
        if not removed:
            return {"ok": False, "reason": f"no queued pair {preference_id!r}"}
        return {"ok": True, "preference_id": preference_id, "removed": removed}

    # -- reporting ----------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """What is waiting, what has been decided, and who decided it."""
        rows = self.repository.list(pending_only=False)
        counts = self.repository.counts()
        by_reviewer: dict[str, int] = {}
        for pair in rows:
            if not pair.reviewed:
                continue
            name = _text(pair.review.get("reviewer"), "unattributed")
            by_reviewer[name] = by_reviewer.get(name, 0) + 1
        return {
            "queued": len(rows),
            "pending": counts.get("pending", 0),
            "by_status": {name: count for name, count in counts.items() if name != "pending"},
            "decided": sum(1 for pair in rows if pair.reviewed),
            "by_reviewer": by_reviewer,
            "decisions": list(DECISIONS),
            "failures": self.failures,
        }

    def resolved_pairs(self, *, decision: str = "") -> tuple[PreferenceExample, ...]:
        """The pairs a reviewer has settled, for a later dataset build.

        ONLY settled rows: a pair still sitting in the queue is a question, not an
        answer, and letting it into a build because it happens to be stored would
        pull an undecided review into a dataset that claims a person made it.
        """
        wanted = _text(decision).lower()
        return tuple(
            pair
            for pair in self.repository.list(pending_only=False)
            if pair.reviewed
            and (
                not wanted
                or _text(pair.review.get("decision")).lower() == wanted
            )
        )

    def clear(self) -> dict[str, int]:
        removed = self.repository.clear()
        return {"removed": removed}


__all__ = [
    "DECISIONS",
    "PreferenceReviewQueue",
    "ReviewItem",
]
