"""Ranked sentence evidence: what the sources actually say, in answer order.

Owns: turning sources (read page text when we have it, search blurbs otherwise)
into one ranked, de-duplicated pool of sentences, ordered by how well each one
answers the question.

Rule-free by construction — nothing here knows any topic. Relevance comes from
the question's own words, weighted by how RARE each word is across this result
set (a document-frequency weight), so a sentence naming the specific thing beats
one that merely repeats the question's common words: across law articles about
"abetment", every page says "abetment", so that word carries almost no weight,
while the case or section number appears once and carries a lot. The remaining
signals are shape signals that apply to any topic at all:

* **information** — a sentence built from words the whole result set repeats
  ("Purring is common in cats of every age") says almost nothing; one that names
  things nothing else does ("…rapid vibration of the vocal folds, at roughly 25
  cycles per second") says a great deal. This is a document-frequency weight
  over the sentence pool, so boilerplate scores low without anyone knowing the
  topic.
* **specificity** — a sentence with digits, units, or a name is likelier to state
  a fact than a vague one.
* **position** — in an article body, early paragraphs carry the answer and late
  ones trail off into related links.
* **agreement** — a sentence whose distinctive terms recur in a DIFFERENT
  independent source is likelier to be true than one that appears once.

All four are properties of prose, not of any subject, which is why an answer
composed from them can be about anything.

Coverage alone is NOT enough, and that is the point: matching the question's own
words is what makes a sentence about the topic, but ranking on it alone is how
the answer became a re-listing of keyword matches. A sentence has to both speak
to the question AND say something.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
import math
import re

from novacontrol.explore.models import ResearchSource
from novacontrol.explore.query import QueryFrame, content_terms

# Sentence boundary: keep the mark with the sentence it ends. A colon counts
# because a list lead-in ("…vibrations within their body that can:") otherwise
# glues itself onto the first list item and both read as one broken sentence.
# Colons inside numbers and URLs have no whitespace after them, so they are safe.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?:])\s+")

# A candidate shorter than this cannot answer anything; a longer one is a
# paragraph that survived without punctuation.
_MIN_SENTENCE_CHARS = 40
_MAX_SENTENCE_CHARS = 420

# Ranking more than this costs more than it finds — the answer uses a handful.
_MAX_CANDIDATES = 400

# Two sentences this similar are the same claim said twice (syndicated copies of
# one wire story are the common case), so only the better-scored one is kept.
_DUPLICATE_SIMILARITY = 0.72

_URL_IN_TEXT = re.compile(r"https?:\S*", re.IGNORECASE)

# Characters a sentence may legitimately open with (a quote, a bracket).
_SENTENCE_OPENERS = frozenset('"\'([')
_LOWER_WORD = re.compile(r"[a-z0-9]{3,}")
_DIGIT = re.compile(r"\d")
# A capitalized word that is not the sentence's first word: a name, a place, a
# law section, a product. "The Court held…" counts, "The study" does not.
_CAPITALIZED = re.compile(r"\b[A-Z][a-z]{2,}\b")


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_LOWER_WORD.findall(text.lower()))


def _key(text: str) -> str:
    """Case/punctuation-insensitive identity for de-duplication."""
    return " ".join(text.lower().split())[:120]


@dataclass(frozen=True, slots=True)
class Sentence:
    """One candidate sentence plus why it ranked where it did."""

    text: str
    source_index: int          # 1-based, matching the report's source list
    score: float
    from_page: bool            # True = read from the page, False = search blurb


@dataclass(frozen=True, slots=True)
class Evidence:
    """A ranked pool of source sentences, ready for answer composition."""

    sentences: tuple[Sentence, ...] = ()
    question: str = ""
    source_count: int = 0
    pages_read: int = 0
    weights: dict[str, float] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.sentences)

    def __len__(self) -> int:
        return len(self.sentences)

    @property
    def has_page_text(self) -> bool:
        """True when at least one sentence came from a page we read."""
        return any(sentence.from_page for sentence in self.sentences)

    def take(
        self,
        count: int,
        *,
        exclude: Iterable[str] = (),
        complete_only: bool = False,
        pages_only: bool = False,
        min_score_ratio: float = 0.0,
    ) -> tuple[Sentence, ...]:
        """The top `count` sentences not already used elsewhere in the report.

        `exclude` is the anti-duplication mechanism: the answer takes the best
        sentences, and the highlights and sections below it take the NEXT best,
        so one page never shows the reader the same sentence twice.

        `min_score_ratio` drops sentences the ranking itself judged far weaker
        than the leader (measured against the best sentence still available), so
        a thin result set cannot pad an answer with its own boilerplate.
        """
        blocked = {_key(text) for text in exclude}
        floor = 0.0
        if min_score_ratio > 0:
            best = max(
                (sentence.score for sentence in self.sentences if _key(sentence.text) not in blocked),
                default=0.0,
            )
            floor = best * min_score_ratio
        picked: list[Sentence] = []
        for sentence in self.sentences:
            if _key(sentence.text) in blocked:
                continue
            if complete_only and text_is_cut_off(sentence.text):
                continue
            if pages_only and not sentence.from_page:
                continue
            if sentence.score < floor:
                continue
            picked.append(sentence)
            if len(picked) >= count:
                break
        return tuple(picked)


def text_is_cut_off(text: str) -> bool:
    """True when a sentence ends in a truncation mark (a search blurb, not prose)."""
    return text.rstrip().endswith("\u2026")


def clean_sentence(text: str, *, strict_ending: bool = False) -> str:
    """Normalize one candidate sentence, then reject anything that is not one.

    Reuses `clean_snippet` for the sentence-level cleanup the whole Explore
    package already shares — URLs, chrome sentences, date prefixes, filler
    prefixes, trailing "read more" — so a read page is normalized exactly like a
    search blurb. A live answer quoted "Jul 16, 2026 · A purring cat isn't
    always a happy cat." before this ran, because the page path skipped that
    cleanup. The import is lazy: `synthesizer` consumes this module.

    `strict_ending` is on for PAGE prose and off for search blurbs: a heading or
    a label on a page does not end with punctuation and must be dropped, while a
    search blurb is truncated by definition ("…that electronically minimizes")
    and has to be allowed through anyway.
    """
    from novacontrol.explore.synthesizer import clean_snippet

    text = " ".join(_URL_IN_TEXT.sub("", text).split()).strip()
    text = re.sub(r"^\s*(?:[•·▪>»\-\u2013\u2014\u2022*]|\d+[.)])\s*", "", text)
    if strict_ending and not text.rstrip().endswith((".", "!", "?", "\u2026")):
        return ""
    text = clean_snippet(text).strip()
    if len(text) < _MIN_SENTENCE_CHARS or len(text) > _MAX_SENTENCE_CHARS:
        return ""
    if not _LOWER_WORD.search(text):
        return ""
    # A sentence does not begin this way: the text was cut mid-sentence, which is
    # how "common calls from mature cats included purring…" led a live answer.
    # Shape, not vocabulary — it applies to any topic.
    if not (text[0].isupper() or text[0].isdigit() or text[0] in _SENTENCE_OPENERS):
        return ""
    return text


def sentences_of(text: str, *, source_index: int, from_page: bool) -> list[Sentence]:
    """Split one block of text into candidate sentences (unranked, score 0)."""
    candidates: list[Sentence] = []
    for raw in _SENTENCE_SPLIT.split(text):
        cleaned = clean_sentence(raw, strict_ending=from_page)
        if cleaned:
            candidates.append(Sentence(cleaned, source_index, 0.0, from_page))
    return candidates


def page_text_of(source: ResearchSource) -> str:
    """The best text a source offers: the page we read, else the search blurb."""
    return (source.content or "").strip() or (source.snippet or "").strip()


def _source_texts(sources: Sequence[ResearchSource]) -> list[str]:
    return [f"{source.title} {page_text_of(source)}".lower() for source in sources]


def _document_frequency(terms: Sequence[str], texts: Sequence[str]) -> dict[str, float]:
    """Weight each question term by how rare it is across these sources.

    A word every source repeats names the topic and nothing else; a word one
    source uses names the specific thing being asked about. This is the whole
    "relevance" mechanism, and it is derived from the result set rather than
    from any list of important words.
    """
    total = float(len(texts)) or 1.0
    return {term: total / max(sum(1 for text in texts if term in text), 1) for term in terms}


def _position_factor(index: int) -> float:
    """Early sentences answer; late ones trail off into related links.

    Gentle, and measured within the SOURCE rather than across the pool: a page's
    opening answer must not lose to a second source's boilerplate purely because
    the pool happened to visit the other source first.
    """
    return 1.0 / (1.0 + 0.04 * index)


def _information_scores(term_lists: Sequence[tuple[str, ...]]) -> list[float]:
    """How much each sentence says, judged against the rest of the pool.

    Mean inverse document frequency of a sentence's content words, normalized so
    the most informative sentence scores 1.0. A sentence whose words the pool
    repeats everywhere lands near 0 no matter what the topic is.
    """
    counts: dict[str, int] = {}
    for terms in term_lists:
        for term in set(terms):
            counts[term] = counts.get(term, 0) + 1
    total = float(len(term_lists)) or 1.0
    idf = {term: math.log(1.0 + total / count) for term, count in counts.items()}
    raw = [
        (sum(idf[term] for term in set(terms)) / len(set(terms))) if terms else 0.0
        for terms in term_lists
    ]
    peak = max(raw) if raw else 0.0
    return [value / peak for value in raw] if peak else raw


def _shape_factor(text: str) -> float:
    """A full sentence beats a fragment; an over-long one is a wall of text."""
    length = len(text)
    if length < _MIN_SENTENCE_CHARS:
        return 0.5
    if length <= 260:
        return 1.0
    return 0.8


def _specificity_factor(text: str) -> float:
    """Digits, units, and names are what a fact looks like."""
    factor = 0.0
    if _DIGIT.search(text):
        factor += 0.15
    # A capitalized word anywhere but position 0: the opening word of a sentence
    # is capitalized for grammar, so only a mid-sentence capital names something.
    if any(match.start() > 0 for match in _CAPITALIZED.finditer(text)):
        factor += 0.10
    return factor


def _agreement_factor(tokens: frozenset[str], distinctive: Sequence[str], texts: Sequence[str], own_index: int) -> float:
    """How much of this sentence's distinctive vocabulary appears in ANOTHER source."""
    marks = [term for term in distinctive if term in tokens]
    if not marks:
        return 0.0
    corroborated = 0
    for term in marks:
        if any(term in text for index, text in enumerate(texts) if index != own_index):
            corroborated += 1
    return corroborated / len(marks)


def _similar(left: frozenset[str], right: frozenset[str]) -> bool:
    if not left or not right:
        return False
    union = left | right
    return len(left & right) / len(union) >= _DUPLICATE_SIMILARITY


def build_evidence(
    question: str,
    frame: QueryFrame,
    sources: Sequence[ResearchSource],
    *,
    limit: int = _MAX_CANDIDATES,
) -> Evidence:
    """Rank every usable sentence in `sources` against `question`.

    Reads page text where a page was read and falls back to the search blurb,
    so this works identically whether or not fetching succeeded.
    """
    if not sources:
        return Evidence()

    terms = content_terms(" ".join((question, " ".join(frame.items), frame.context)))
    texts = _source_texts(sources)
    weights = _document_frequency(terms, texts)
    total_weight = sum(weights.values()) or 1.0
    distinctive = tuple(term for term, weight in weights.items() if weight > 1.0)

    # (sentence, its content words, its index within its own source)
    candidates: list[tuple[Sentence, tuple[str, ...], int]] = []
    pages_read = 0
    for index, source in enumerate(sources, start=1):
        text = page_text_of(source)
        if not text:
            continue
        from_page = bool((source.content or "").strip())
        if from_page:
            pages_read += 1
        for position, sentence in enumerate(sentences_of(text, source_index=index, from_page=from_page)):
            candidates.append((sentence, content_terms(sentence.text), position))
    if not candidates:
        return Evidence(question=question, source_count=len(sources), weights=weights)

    information = _information_scores([terms for _sentence, terms, _position in candidates])
    scored: list[Sentence] = []
    for (candidate, _terms, position), info in zip(candidates[:limit], information[:limit]):
        lower = candidate.text.lower()
        tokens = _tokens(candidate.text)
        coverage = sum(weight for term, weight in weights.items() if term in lower) / total_weight
        agreement = _agreement_factor(tokens, distinctive, texts, candidate.source_index - 1)
        raw = (
            0.5 * coverage
            + 0.4 * info
            + 0.3 * agreement
            + _specificity_factor(candidate.text)
        ) * _shape_factor(candidate.text)
        scored.append(Sentence(
            candidate.text,
            candidate.source_index,
            raw * _position_factor(position),
            candidate.from_page,
        ))

    # No question term matched anything (an oddly phrased or resolved follow-up):
    # rank on what the sentences SAY rather than returning nothing.
    if not any(sentence.score > 0 for sentence in scored):
        scored = [
            Sentence(sentence.text, sentence.source_index, info, sentence.from_page)
            for (sentence, _terms, _position), info in zip(candidates[:limit], information[:limit])
        ]

    scored.sort(key=lambda sentence: sentence.score, reverse=True)

    ranked: list[Sentence] = []
    seen_keys: set[str] = set()
    seen_tokens: list[frozenset[str]] = []
    for sentence in scored:
        key = _key(sentence.text)
        if key in seen_keys:
            continue
        tokens = _tokens(sentence.text)
        if any(_similar(tokens, previous) for previous in seen_tokens):
            continue
        seen_keys.add(key)
        seen_tokens.append(tokens)
        ranked.append(sentence)

    return Evidence(
        sentences=tuple(ranked),
        question=question,
        source_count=len(sources),
        pages_read=pages_read,
        weights=weights,
    )


def join_sentences(sentences: Sequence[Sentence]) -> str:
    """Join sentence objects into one readable paragraph."""
    return " ".join(sentence.text.strip() for sentence in sentences if sentence.text.strip())
