"""Evidence verification: what the record actually supports, and why.

The rules are intentionally boring and checkable, because this is the component
that decides whether a claim may be presented as established:

1. **One source never means confirmed.** A single non-official report is
   ``SINGLE_SOURCE``.
2. **An official statement confirms the claim it makes**, and only that — it is
   ``OFFICIALLY_CONFIRMED`` for the event whose evidence it is.
3. **Syndicated copies are not independence.** Evidence whose text is nearly
   identical counts once; two outlets republishing one wire report are one origin.
4. **Conflicts are preserved, not resolved.** A denial or a contradicting
   figure produces ``CONFLICTING_EVIDENCE`` with the conflict described.
5. **Retractions end the event.** A retraction from the original or an official
   source is ``RETRACTED`` (terminal); a retraction from an unrelated source is a
   conflict until the original itself withdraws.
6. **Every decision names its evidence** and its reasons.
7. **Confidence never replaces state**: the decision has no confidence field at
   all, so a number cannot be mistaken for confirmation.
8. **A rumour stays a rumour**: nothing here promotes an unverified claim.

Expiry is category-driven (`expiry_for`), never "old means irrelevant".
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from novacontrol.novamarket.contracts import EventValidationError
from novacontrol.novamarket.radar.models import (
    UNCLASSIFIED,
    EvidenceRecord,
    EventSourceDefinition,
    VerificationDecision,
    VerificationState,
    ensure_verification_transition,
    expiry_for,
    normalize_text,
    similarity,
)

#: Source types whose statement can establish the fact it declares.
OFFICIAL_TYPES = frozenset(
    {"exchange", "company_ir", "government", "regulator", "official_statistics"}
)

#: Text that withdraws an earlier claim.
RETRACTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"\b{phrase}\b")
    for phrase in (
        "withdraws the announcement",
        "withdrawn",
        "withdraws",
        "retracts",
        "retracted",
        "recalls the announcement",
        "calls off",
        "called off",
        "cancels the",
        "cancelled the",
        "aborts",
        "no longer valid",
        "issued in error",
        "erroneously",
        "regrets the error",
    )
)

#: Text that denies the claim without withdrawing the original publication.
DENIAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"\b{phrase}\b")
    for phrase in (
        "denies",
        "denied",
        "refutes",
        "no truth",
        "not accurate",
        "incorrect",
        "clarifies that no",
        "did not approve",
        "no such",
    )
)

#: Two evidence texts above this token overlap are treated as one origin.
SYNDICATION_THRESHOLD = 0.85

#: A numeric claim's context: a few words before a number, so "revenue 12%" and
#: "margin 12%" are not compared to each other.
_NUMBER = re.compile(
    r"(?P<context>(?:[a-z][a-z-]*\s+){0,3})"
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>percent|%|per cent|crore|billion|million|lakh)"
)


def _frames(text: str, patterns: Sequence[re.Pattern[str]]) -> str:
    normalized = normalize_text(text)
    for pattern in patterns:
        match = pattern.search(normalized)
        if match is not None:
            return match.group(0)
    return ""


def retraction_frame(text: str) -> str:
    """The retraction phrase this text uses, or an empty string."""
    return _frames(text, RETRACTION_PATTERNS)


def denial_frame(text: str) -> str:
    """The denial phrase this text uses, or an empty string."""
    return _frames(text, DENIAL_PATTERNS)


def numeric_claims(text: str) -> dict[str, float]:
    """``context -> value`` for figures with an explicit unit.

    The context is the last few words before the number, normalized; comparing
    only equal contexts is what keeps "revenue 12%" from contradicting
    "margin 12%".
    """
    claims: dict[str, float] = {}
    for match in _NUMBER.finditer(normalize_text(text)):
        context = " ".join(match.group("context").split()[-2:])
        unit = match.group("unit")
        key = f"{context}|{unit}"
        try:
            claims[key] = float(match.group("value"))
        except ValueError:
            continue
    return claims


def independence_groups(evidence: Sequence[EvidenceRecord]) -> list[list[EvidenceRecord]]:
    """Group evidence that is not independent of one another.

    Two records share an origin when their independence keys match, or when one
    is a near-copy of the other (a syndicated report). The first record in a group
    is the origin; later near-copies join it rather than counting again.
    """
    groups: list[list[EvidenceRecord]] = []
    for record in evidence:
        if record.status is not record.status.RETRIEVED:
            continue
        placed = False
        for group in groups:
            head = group[0]
            if head.independence_key and head.independence_key == record.independence_key:
                group.append(record)
                placed = True
                break
            if similarity(head.excerpt or head.title, record.excerpt or record.title) >= SYNDICATION_THRESHOLD:
                group.append(record)
                placed = True
                break
        if not placed:
            groups.append([record])
    return groups


def _is_official(record: EvidenceRecord, definition: EventSourceDefinition | None) -> bool:
    if definition is not None:
        return (
            definition.reliability.value == "official"
            and definition.source_type.value in OFFICIAL_TYPES
        )
    return record.reliability.value == "official"


@dataclass(frozen=True, slots=True)
class VerificationInput:
    """Everything verification is allowed to look at."""

    subject: str
    subcategory: str
    evidence: tuple[EvidenceRecord, ...]
    sources: Mapping[str, EventSourceDefinition]
    current: VerificationState | None = None
    published_at: datetime | None = None
    now: datetime | None = None


class VerificationEngine:
    """Applies the eight rules above and reports the verdict with its evidence."""

    def evaluate(self, data: VerificationInput) -> VerificationDecision:
        if not data.evidence:
            return VerificationDecision(
                state=VerificationState.UNVERIFIED,
                reasons=("no retrieved evidence supports this event",),
                evidence_ids=(),
                independent_sources=0,
            )

        retrieved = tuple(
            record for record in data.evidence if record.status.value == "retrieved"
        )
        if not retrieved:
            return VerificationDecision(
                state=VerificationState.UNVERIFIED,
                reasons=("every evidence record is unavailable or rejected",),
                evidence_ids=tuple(record.evidence_id for record in data.evidence),
                independent_sources=0,
            )

        reasons: list[str] = []
        conflicts: list[str] = []
        retracted = False
        retraction_sources: list[str] = []

        # Rule 5: a retraction from an official source, or from the source that
        # made the original claim, ends the event.
        for record in retrieved:
            frame = retraction_frame(f"{record.title} {record.excerpt}")
            if not frame:
                continue
            definition = data.sources.get(record.source_id)
            official = _is_official(record, definition)
            if official or record.independence_key == retrieved[0].independence_key:
                retracted = True
                retraction_sources.append(record.source_id)
                reasons.append(
                    f"{record.source_id} retracts the claim ({frame!r})"
                )
        if retracted:
            return VerificationDecision(
                state=VerificationState.RETRACTED,
                reasons=tuple(reasons),
                evidence_ids=tuple(record.evidence_id for record in retrieved),
                independent_sources=len(independence_groups(retrieved)),
                conflicts=(),
            )

        # Rule 4: conflicts are recorded, never resolved by preference.
        denial = [
            record
            for record in retrieved
            if denial_frame(f"{record.title} {record.excerpt}")
        ]
        base = [
            record
            for record in retrieved
            if not denial_frame(f"{record.title} {record.excerpt}")
        ]
        if denial and base:
            conflicts.append(
                "a denial contradicts the announcement: "
                + ", ".join(sorted(record.source_id for record in denial))
            )
        claim_sets = [numeric_claims(f"{record.title} {record.excerpt}") for record in retrieved]
        for key in set().union(*claim_sets) if claim_sets else set():
            values = {claims[key] for claims in claim_sets if key in claims}
            if len(values) > 1:
                conflicts.append(
                    f"contradicting figures for {key!r}: "
                    + ", ".join(f"{value:g}" for value in sorted(values))
                )
        if conflicts:
            reasons.extend(conflicts)
            return VerificationDecision(
                state=VerificationState.CONFLICTING_EVIDENCE,
                reasons=tuple(reasons),
                evidence_ids=tuple(record.evidence_id for record in retrieved),
                independent_sources=len(independence_groups(retrieved)),
                conflicts=tuple(conflicts),
            )

        # Rule 2: an official statement confirms the claim it makes.
        official_records = [
            record
            for record in retrieved
            if _is_official(record, data.sources.get(record.source_id))
        ]
        if official_records:
            reasons.append(
                "officially stated by "
                + ", ".join(sorted({record.publisher for record in official_records}))
            )
            return VerificationDecision(
                state=VerificationState.OFFICIALLY_CONFIRMED,
                reasons=tuple(reasons),
                evidence_ids=tuple(record.evidence_id for record in retrieved),
                independent_sources=len(independence_groups(retrieved)),
            )

        # Rules 1 and 3: independent origins decide corroboration.
        groups = independence_groups(retrieved)
        if len(groups) >= 2:
            reasons.append(
                f"{len(groups)} independent origins report the same claim"
            )
            if len(groups) < len(retrieved):
                reasons.append(
                    f"{len(retrieved) - len(groups)} near-identical record(s) count "
                    "as one origin (syndicated copy)"
                )
            return VerificationDecision(
                state=VerificationState.MULTI_SOURCE_CORROBORATED,
                reasons=tuple(reasons),
                evidence_ids=tuple(record.evidence_id for record in retrieved),
                independent_sources=len(groups),
            )

        reasons.append("a single independent origin reports the claim")
        return VerificationDecision(
            state=VerificationState.SINGLE_SOURCE,
            reasons=tuple(reasons),
            evidence_ids=tuple(record.evidence_id for record in retrieved),
            independent_sources=len(groups),
        )

    def decision_for(
        self,
        data: VerificationInput,
    ) -> VerificationDecision:
        """Evaluate, then refuse an illegal transition from the current state."""
        decision = self.evaluate(data)
        if data.current is not None:
            ensure_verification_transition(data.current, decision.state)
        return decision


def is_expired(
    *,
    subcategory: str,
    published_at: datetime | None,
    discovered_at: datetime,
    now: datetime,
    verified_expired: bool = False,
) -> bool:
    """Category-driven expiry. An old event is not automatically irrelevant."""
    if verified_expired:
        return True
    days = expiry_for(subcategory)
    anchor = published_at or discovered_at
    return (now - anchor).days >= days


def expiry_window(subcategory: str) -> int:
    return expiry_for(subcategory)


def describe_state(state: VerificationState) -> dict[str, Any]:
    """What a state means for a client, in one sentence each."""
    meanings = {
        VerificationState.UNVERIFIED: "nothing credible has been retrieved yet",
        VerificationState.SINGLE_SOURCE: "one independent source reports it; not corroborated",
        VerificationState.MULTI_SOURCE_CORROBORATED: "independent sources report the same claim",
        VerificationState.OFFICIALLY_CONFIRMED: "an official publisher states the claim",
        VerificationState.CONFLICTING_EVIDENCE: "sources disagree; the conflict is preserved",
        VerificationState.RETRACTED: "the claim was withdrawn and is not current fact",
        VerificationState.EXPIRED: "outside its category's review window; history retained",
    }
    return {
        "state": state.value,
        "meaning": meanings[state],
        "is_confirmed_fact": state is VerificationState.OFFICIALLY_CONFIRMED,
        "can_be_presented_as_confirmed": state is VerificationState.OFFICIALLY_CONFIRMED,
    }


def validate_unclassified(subcategory: str) -> None:
    """Guard: an unclassified record may never be treated as a market event."""
    if subcategory == UNCLASSIFIED.id:
        raise EventValidationError(
            "an unclassified record is evidence of what a source said, not a "
            "classified market event"
        )


__all__ = [
    "DENIAL_PATTERNS",
    "OFFICIAL_TYPES",
    "RETRACTION_PATTERNS",
    "SYNDICATION_THRESHOLD",
    "VerificationEngine",
    "VerificationInput",
    "denial_frame",
    "describe_state",
    "expiry_window",
    "independence_groups",
    "is_expired",
    "numeric_claims",
    "retraction_frame",
    "validate_unclassified",
]
