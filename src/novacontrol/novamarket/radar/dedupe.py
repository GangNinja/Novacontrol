"""Duplicate detection: is this a new event, an update, or already recorded?

The rules deliberately require more than a similar headline:

* **identical content** (same digest) is a duplicate — nothing new was said;
* **same event key** (subject + subcategory + publication day) is an update;
* **same subcategory, overlapping instruments, within the window and textually
  similar** is an update — a follow-up report of one story;
* **similar titles with no shared instrument or subject** are *distinct* events.
  Two companies each reporting results produce similar headlines; merging them
  would destroy both records.

Every decision carries its reasons, so a "duplicate" can be inspected rather than
believed. Updates preserve every source record: the earlier version is never
overwritten.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from novacontrol.novamarket.radar.models import similarity

#: A follow-up report within this window can be an update of the same event.
DEFAULT_WINDOW_HOURS = 72

#: Text overlap that makes two reports the same story rather than two stories.
UPDATE_SIMILARITY = 0.55

#: Overlap below this is treated as a different event, never merged.
DISTINCT_SIMILARITY = 0.35

#: A withdrawal may arrive long after the report it withdraws.
RETRACTION_WINDOW_HOURS = 24 * 30


@dataclass(frozen=True, slots=True)
class Candidate:
    """The incoming report, in the terms dedupe compares."""

    subject: str
    subcategory: str
    title: str
    digest: str
    instrument_ids: tuple[str, ...] = ()
    published_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Existing:
    """A stored event's comparison fields (one row per event, latest version)."""

    event_id: str
    event_key: str
    subject: str
    subcategory: str
    title: str
    digest: str
    instrument_ids: tuple[str, ...] = ()
    published_at: datetime | None = None
    version: int = 1


@dataclass(frozen=True, slots=True)
class Decision:
    """What the report is, and why."""

    kind: str  # new | duplicate | update
    event_id: str = ""
    event_key: str = ""
    reasons: tuple[str, ...] = ()
    similarity: float = 0.0

    @property
    def creates_event(self) -> bool:
        return self.kind == "new"

    @property
    def is_update(self) -> bool:
        return self.kind == "update"

    @property
    def is_duplicate(self) -> bool:
        return self.kind == "duplicate"

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "event_id": self.event_id,
            "event_key": self.event_key,
            "reasons": list(self.reasons),
            "similarity": round(self.similarity, 3),
        }


def _within_window(
    left: datetime | None, right: datetime | None, hours: int
) -> bool:
    if left is None or right is None:
        # An unknown publication time is not evidence of closeness; the other
        # rules still apply.
        return False
    return abs(left - right) <= timedelta(hours=hours)


def decide(
    candidate: Candidate,
    existing: Sequence[Existing],
    *,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    retracting: bool = False,
) -> Decision:
    """Classify one incoming report against the events already stored.

    ``retracting`` marks a withdrawal or correction: such a report names the
    claim it withdraws, so it is an update of that event even when the new
    wording no longer shares the earlier subject terms.
    """
    # 1. Identical content already recorded: a duplicate, full stop.
    for row in existing:
        if candidate.digest and candidate.digest == row.digest:
            return Decision(
                kind="duplicate",
                event_id=row.event_id,
                event_key=row.event_key,
                reasons=("identical content digest already stored",),
                similarity=1.0,
            )

    best: tuple[float, Existing] | None = None
    for row in existing:
        if row.subcategory != candidate.subcategory:
            continue
        overlap = similarity(
            f"{candidate.title} {candidate.subject}",
            f"{row.title} {row.subject}",
        )
        shared = set(candidate.instrument_ids) & set(row.instrument_ids)
        same_subject = bool(
            candidate.subject and row.subject and candidate.subject == row.subject
        )
        same_key = bool(candidate.subject and row.subject and candidate.subcategory == row.subcategory and candidate.published_at and row.published_at and candidate.published_at.date() == row.published_at.date() and same_subject)
        if same_key:
            return Decision(
                kind="update",
                event_id=row.event_id,
                event_key=row.event_key,
                reasons=(
                    "same subject, subcategory and publication day; a further "
                    "report of one event",
                ),
                similarity=overlap,
            )
        qualifies = (
            (same_subject or bool(shared))
            and overlap >= UPDATE_SIMILARITY
            and _within_window(candidate.published_at, row.published_at, window_hours)
        )
        if retracting:
            qualifies = overlap >= DISTINCT_SIMILARITY and _within_window(
                candidate.published_at, row.published_at, RETRACTION_WINDOW_HOURS
            )
        if qualifies and (best is None or overlap > best[0]):
            best = (overlap, row)

    if best is not None:
        overlap, row = best
        reasons = [
            f"text overlap {overlap:.2f} with an existing report of the same "
            "subcategory inside the review window"
        ]
        shared_ids = sorted(set(candidate.instrument_ids) & set(row.instrument_ids))
        if shared_ids:
            reasons.append(f"shared instrument(s): {', '.join(shared_ids)}")
        if retracting:
            reasons.append("the report withdraws or corrects an earlier claim")
        return Decision(
            kind="update",
            event_id=row.event_id,
            event_key=row.event_key,
            reasons=tuple(reasons),
            similarity=overlap,
        )

    # Similar titles with no shared subject or instrument are separate events.
    for row in existing:
        if row.subcategory != candidate.subcategory:
            continue
        overlap = similarity(candidate.title, row.title)
        if overlap >= DISTINCT_SIMILARITY:
            shared = set(candidate.instrument_ids) & set(row.instrument_ids)
            if not shared and candidate.subject != row.subject:
                return Decision(
                    kind="new",
                    reasons=(
                        f"title overlap {overlap:.2f} but no shared instrument or "
                        "subject: this is a different event, not a duplicate",
                    ),
                    similarity=overlap,
                )

    return Decision(kind="new", reasons=("no matching stored event",))


__all__ = [
    "Candidate",
    "Decision",
    "DEFAULT_WINDOW_HOURS",
    "DISTINCT_SIMILARITY",
    "Existing",
    "UPDATE_SIMILARITY",
    "decide",
]
