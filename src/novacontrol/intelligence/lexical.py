"""Lightweight lexical matching: TF-IDF cosine over intent exemplars.

The deterministic rule registry matches exact prefixes, needles, and regexes.
That is fast and predictable but blind to paraphrase: "which programs are
eating my memory" shares no prefix with "show memory usage". This layer closes
that gap with a small sparse TF-IDF index built from example phrasings.

Deliberately dependency-free (no scikit-learn, no numpy): the corpus is a few
hundred short phrases, so a pure-Python sparse index answers in microseconds —
and CI installs only ``.[dev]``, so a heavier dependency would mean the
matching layer is untested exactly where it matters.

Two signals are blended:

  * token TF-IDF cosine  — captures meaning ("memory" vs "ram" still differ,
    so the exemplar corpus is where synonym knowledge lives);
  * character trigram similarity — absorbs typos and inflection, which token
    statistics handle badly ("eating"/"eats", "usage"/"using").

Nothing here calls a model. This is the cheap middle layer that runs BEFORE
the language-model fallback, and it reports its score so the caller can apply
the configured threshold instead of trusting an opaque answer.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from novacontrol.intelligence.intent import IntentName
from novacontrol.intelligence.normalize import normalize, normalized_tokens

# Blend weight for token vs character evidence. Token statistics carry most of
# the meaning; character n-grams only break ties and absorb typos, so they get
# the smaller share.
_TOKEN_WEIGHT = 0.75
_CHAR_WEIGHT = 0.25

# Bounded query cache: NLU runs per keystroke-ish in some UI paths, and the
# same short phrasings repeat constantly.
_CACHE_LIMIT = 1024
_NGRAM = 3


@dataclass(frozen=True, slots=True)
class LexicalMatch:
    """One scored candidate."""

    intent: IntentName
    score: float
    phrase: str

    def to_dict(self) -> dict[str, object]:
        """The candidate in the shape the NLU contract publishes.

        ``candidate_intent`` and ``matched_examples`` are the agreed names for
        a lexical match, so anything consuming a candidate (a UI readout, a
        benchmark, a future embedding reranker) reads the same keys.
        """
        return {
            "candidate_intent": self.intent.value,
            "score": round(self.score, 4),
            "matched_examples": [self.phrase],
        }


class LexicalMatcher:
    """Sparse TF-IDF index over intent exemplar phrases."""

    def __init__(self, exemplars: Iterable[tuple[IntentName, Sequence[str]]]) -> None:
        self._intents: list[IntentName] = []
        self._phrases: list[str] = []
        self._vectors: list[Counter[str]] = []
        self._norms: list[float] = []
        self._idf: dict[str, float] = {}
        self._cache: dict[str, LexicalMatch | None] = {}
        self._build(exemplars)

    # -- construction ---------------------------------------------------------

    def _build(self, exemplars: Iterable[tuple[IntentName, Sequence[str]]]) -> None:
        documents: list[tuple[IntentName, str, Counter[str]]] = []
        for intent, phrases in exemplars:
            for phrase in phrases:
                normalized = normalize(phrase)
                if not normalized:
                    continue
                documents.append((intent, normalized, _term_counts(normalized)))
        if not documents:
            return

        total = len(documents)
        document_frequency: Counter[str] = Counter()
        for _intent, _phrase, counts in documents:
            document_frequency.update(counts.keys())
        # Smoothed IDF: a term that appears in every exemplar keeps a small
        # positive weight instead of collapsing to zero.
        self._idf = {
            term: math.log((1.0 + total) / (1.0 + frequency)) + 1.0
            for term, frequency in document_frequency.items()
        }

        for intent, phrase, counts in documents:
            weighted = Counter({term: count * self._idf.get(term, 1.0) for term, count in counts.items()})
            self._intents.append(intent)
            self._phrases.append(phrase)
            self._vectors.append(weighted)
            self._norms.append(_norm(weighted))

    # -- matching -------------------------------------------------------------

    def rank(self, text: str, *, limit: int = 3) -> tuple[LexicalMatch, ...]:
        """Best candidates for ``text``, highest score first."""
        normalized = normalize(text)
        if not normalized or not self._vectors:
            return ()
        query = _term_counts(normalized)
        query_vector = Counter({term: count * self._idf.get(term, 1.0) for term, count in query.items()})
        query_norm = _norm(query_vector)
        scored: list[LexicalMatch] = []
        for index, vector in enumerate(self._vectors):
            token_score = _cosine(query_vector, query_norm, vector, self._norms[index])
            char_score = _trigram_similarity(normalized, self._phrases[index])
            scored.append(
                LexicalMatch(
                    intent=self._intents[index],
                    score=_TOKEN_WEIGHT * token_score + _CHAR_WEIGHT * char_score,
                    phrase=self._phrases[index],
                )
            )
        scored.sort(key=lambda match: match.score, reverse=True)
        return tuple(scored[:limit])

    def best(self, text: str) -> LexicalMatch | None:
        """Highest-scoring candidate, or ``None`` when nothing relates.

        The caller compares ``score`` against the configured lexical threshold;
        returning the raw score (rather than pre-thresholding) is what lets the
        threshold be tuned after benchmarking without touching this module.
        """
        normalized = normalize(text)
        if not normalized:
            return None
        if normalized in self._cache:
            return self._cache[normalized]
        ranked = self.rank(normalized, limit=1)
        match = ranked[0] if ranked else None
        if len(self._cache) >= _CACHE_LIMIT:
            self._cache.clear()
        self._cache[normalized] = match
        return match

    @property
    def size(self) -> int:
        """Number of indexed exemplar phrases."""
        return len(self._phrases)

    def intents(self) -> tuple[IntentName, ...]:
        """Intents present in the corpus, in first-seen order."""
        return tuple(dict.fromkeys(self._intents))


def _term_counts(normalized: str) -> Counter[str]:
    return Counter(token for token in normalized_tokens(normalized) if token)


def _norm(vector: Counter[str]) -> float:
    return math.sqrt(sum(value * value for value in vector.values()))


def _cosine(
    left: Counter[str],
    left_norm: float,
    right: Counter[str],
    right_norm: float,
) -> float:
    if not left or not right or left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    shared = left.keys() & right.keys()
    if not shared:
        return 0.0
    numerator = sum(left[term] * right[term] for term in shared)
    return numerator / (left_norm * right_norm)


def _trigrams(text: str) -> set[str]:
    padded = f" {text} "
    if len(padded) < _NGRAM:
        return {padded}
    return {padded[index : index + _NGRAM] for index in range(len(padded) - _NGRAM + 1)}


def _trigram_similarity(left: str, right: str) -> float:
    """Jaccard similarity of character trigrams (typo/inflection tolerant)."""
    left_grams = _trigrams(left)
    right_grams = _trigrams(right)
    if not left_grams or not right_grams:
        return 0.0
    union = left_grams | right_grams
    return len(left_grams & right_grams) / len(union) if union else 0.0
