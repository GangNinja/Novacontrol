"""Deterministic extraction: one feed entry in, one structured candidate out.

No model, no network, no clock drift: the same entry always produces the same
:class:`ExtractedEvent`, which is what makes a classification reproducible later.
The rule table is :data:`~novacontrol.novamarket.radar.models.SUBCATEGORIES` — a
new event family is a new row there, not a new branch here.

Two deliberate refusals:

* an entry no rule recognises becomes ``unclassified`` and is stored as evidence
  of what the source said, but it is never promoted to a market-moving event;
* a date the text does not actually state stays ``None``. "Today" is resolved
  only against the entry's own publication time, and only when that time exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Iterable, Sequence

from novacontrol.novamarket.radar.models import (
    SUBCATEGORIES,
    UNCLASSIFIED,
    EventCategory,
    ExtractedEvent,
    SourceEntry,
    SubcategoryDefinition,
    clip,
    normalize_text,
)

#: Matched term + strength, so a classification can quote what matched.
@dataclass(frozen=True, slots=True)
class Match:
    definition: SubcategoryDefinition
    term: str
    position: int


@dataclass(frozen=True, slots=True)
class EntityHint:
    """A canonical name/alias the extractor may match (an NM-02 instrument)."""

    name: str
    aliases: tuple[str, ...] = ()

    def terms(self) -> tuple[str, ...]:
        values = [self.name, *self.aliases]
        return tuple(
            sorted(
                {value.strip().lower() for value in values if str(value).strip()},
                key=len,
                reverse=True,
            )
        )


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """The candidate plus everything that was observed while producing it."""

    event: ExtractedEvent
    matches: tuple[Match, ...]
    unclassified: bool
    notes: tuple[str, ...] = ()


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
_DAY_MONTH_YEAR = re.compile(
    r"\b(\d{1,2})\s+(january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s+(\d{4})\b"
)
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_RELATIVE = re.compile(r"\b(today|yesterday)\b")


def _keyword_pattern(term: str) -> re.Pattern[str]:
    """A whole-phrase match; ``re.escape`` keeps punctuation out of the regex."""
    return re.compile(rf"\b{re.escape(term)}\b")


@lru_cache(maxsize=8192)
def _term_pattern(term: str) -> re.Pattern[str]:
    """Compile an entity term once, not once per feed entry.

    Radar extraction normalizes every entry and then tests every instrument
    name/alias against it.  Compiling each term inside that nested loop made a
    refresh a long CPU-bound burst on the event loop and the API stopped
    answering while it ran.  The pattern is a pure function of the term, so
    caching it changes no matching semantics.
    """
    return re.compile(rf"\b{re.escape(term)}\b")


_PATTERNS: dict[str, re.Pattern[str]] = {
    definition.id: re.compile(
        "|".join(
            rf"\b{re.escape(term)}\b" for term in definition.keywords
        )
    )
    for definition in SUBCATEGORIES
    if definition.keywords
}


def find_matches(text: str) -> tuple[Match, ...]:
    """Every subcategory rule that matched, with the strongest phrase it matched."""
    normalized = normalize_text(text)
    found: list[Match] = []
    for definition in SUBCATEGORIES:
        pattern = _PATTERNS.get(definition.id)
        if pattern is None:
            continue
        match = pattern.search(normalized)
        if match is None:
            continue
        found.append(Match(definition, match.group(0), match.start()))
    return tuple(found)


def _choose(matches: Sequence[Match]) -> Match | None:
    """The strongest match: longest phrase, then declared weight, then registry order."""
    if not matches:
        return None
    return max(
        matches,
        key=lambda item: (
            len(item.term.split()),
            item.definition.importance_weight,
            -SUBCATEGORIES.index(item.definition),
        ),
    )


def resolve_occurred_at(
    text: str, published_at: datetime | None
) -> datetime | None:
    """The stated occurrence time, or ``None`` when the text states none.

    ``today``/``yesterday`` are resolved against the entry's publication time and
    only when it exists. A relative word with no publication time yields ``None``
    rather than "now".
    """
    normalized = normalize_text(text)
    explicit = _DAY_MONTH_YEAR.search(normalized)
    if explicit is not None:
        day, month_name, year = explicit.groups()
        try:
            return datetime(
                int(year), _MONTHS[month_name], int(day), tzinfo=published_at.tzinfo
                if published_at
                else None,
            )
        except ValueError:
            return None
    iso = _ISO_DATE.search(normalized)
    if iso is not None:
        year, month, day = iso.groups()
        try:
            return datetime(
                int(year), int(month), int(day), tzinfo=published_at.tzinfo
                if published_at
                else None,
            )
        except ValueError:
            return None
    relative = _RELATIVE.search(normalized)
    if relative is not None and published_at is not None:
        offset = 0 if relative.group(1) == "today" else 1
        return (published_at - timedelta(days=offset)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    return None


def _matched_entities(
    text: str, entities: Iterable[EntityHint]
) -> tuple[str, ...]:
    normalized = normalize_text(text)
    hits: list[str] = []
    seen: set[str] = set()
    for hint in entities:
        for term in hint.terms():
            if len(term) < 2:
                continue
            if _term_pattern(term).search(normalized):
                if hint.name not in seen:
                    seen.add(hint.name)
                    hits.append(hint.name)
                break
    return tuple(hits)


def extract(
    entry: SourceEntry,
    *,
    entities: Iterable[EntityHint] = (),
    publisher: str = "",
) -> ExtractionResult:
    """Turn one retrieved entry into an event candidate, with its reasons."""
    text = f"{entry.title}. {entry.summary}".strip()
    matches = find_matches(text)
    chosen = _choose(matches)
    description = clip(entry.summary or entry.title, 2_000)
    notes: list[str] = []

    if chosen is None:
        event = ExtractedEvent(
            title=entry.title,
            description=description,
            category=EventCategory.UNCLASSIFIED,
            subcategory=UNCLASSIFIED.id,
            matched_terms=(),
            entity_names=_matched_entities(text, entities),
            occurred_at=None,
            market_wide=False,
        )
        notes.append(
            "no declared event rule matched; stored as evidence, not as a "
            "market-moving event"
        )
        return ExtractionResult(event=event, matches=(), unclassified=True, notes=tuple(notes))

    occurred = resolve_occurred_at(text, entry.published_at)
    if occurred is None:
        notes.append(
            "the entry states no occurrence date; occurred_at stays unknown "
            "instead of borrowing the publication time"
        )
    entity_names = _matched_entities(text, entities)
    if not entity_names and chosen.definition.category is EventCategory.COMPANY:
        notes.append(
            "a company-event rule matched but no known instrument was named; "
            "the event stays unmapped rather than guessed"
        )
    event = ExtractedEvent(
        title=entry.title,
        description=description,
        category=chosen.definition.category,
        subcategory=chosen.definition.id,
        matched_terms=tuple(
            sorted({match.term for match in matches if match.definition is chosen.definition})
        ),
        entity_names=entity_names,
        occurred_at=occurred,
        market_wide=chosen.definition.market_wide,
    )
    if len({match.definition.category for match in matches}) > 1:
        notes.append(
            "rules from more than one category matched; the strongest phrase "
            f"({chosen.term!r}) decided {chosen.definition.category.value}"
        )
    if publisher:
        notes.append(f"published by {publisher}")
    return ExtractionResult(event=event, matches=matches, unclassified=False, notes=tuple(notes))


__all__ = [
    "EntityHint",
    "ExtractionResult",
    "Match",
    "extract",
    "find_matches",
    "resolve_occurred_at",
]
