"""Optional semantic matching: a cached embedding index over intent exemplars.

Lexical similarity (TF-IDF) needs shared words: *"which programs are eating my
memory"* reaches ``memory_status`` because "memory" is in both. An embedding
closes the gap when the words differ — *"what is chewing up my ram"* shares
almost nothing with its exemplar — without a language-model call.

It is deliberately OPTIONAL in two directions:

* Nothing here downloads or requires a model. The default vector space is a
  deterministic hash of content-word, bigram and character n-gram features,
  weighted by how rare each is in the exemplar corpus, so the layer works
  offline, produces identical output on every run, and adds no dependency.
* A caller that HAS an embedding model supplies it as a backend (any object
  with ``embed``), and this index uses it instead — one seam, not a hard-coded
  provider.

The engine consults it only after its cheaper layers have declined, and the
score is treated as evidence rather than proof: a match below the configured
floor is not acted on at all.

Vectors and results are cached by normalized text, so asking the same thing
twice costs one dictionary lookup.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from novacontrol.intelligence.normalize import normalize

# Bucket count for the hashing embedder. Small enough to stay free, large
# enough that the exemplar corpus (a few hundred phrases) rarely collides.
_DIMENSIONS = 512

# Feature weights: words carry the meaning, adjacency and spelling refine it.
_WORD_WEIGHT = 1.0
_BIGRAM_WEIGHT = 0.6
_CHARGRAM_WEIGHT = 0.3
_CHARGRAM_SIZE = 4

# Function words are what every request has and no request is ABOUT. Left in,
# they dominate the vector: measured on the shipped corpus, "how much ram do i
# have" matched *battery_status* above *memory_status*, because "how much ... do
# i have left" is mostly function words. Dropping them raised top-threshold
# precision from 23% to 60% on the same leave-one-out run (~/dev docs).
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "my", "our", "your", "his", "her", "their",
        "me", "us", "them", "i", "you", "we", "they", "it", "its",
        "this", "that", "these", "those", "is", "are", "was", "were",
        "be", "been", "being", "am", "do", "does", "did", "doing",
        "have", "has", "had", "having", "will", "would", "can", "could",
        "should", "shall", "may", "might", "must", "of", "for", "to",
        "in", "on", "at", "by", "with", "from", "into", "over", "under",
        "about", "as", "and", "or", "but", "if", "then", "than", "so",
        "such", "no", "not", "only", "just", "now", "please", "give",
        "show", "tell", "also", "very", "really", "actually",
        # The question words. Left in, they dominate: "how much _ do i have" is
        # mostly function words, which is how a RAM question matched a battery
        # exemplar.
        "what", "which", "who", "whom", "whose", "how", "when", "where",
        "why", "much", "many", "more", "less", "any", "some", "there",
        "here", "again", "still", "yet", "even", "while", "during",
        "between",
    }
)


class Embedder(Protocol):
    """Anything that can turn text into vectors.

    A local hashing embedder and a model-backed one are interchangeable here;
    the index never learns which it has.
    """

    @property
    def name(self) -> str: ...  # pragma: no cover - protocol

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """One vector per input text, in the same order."""
        ...  # pragma: no cover - protocol


def content_words(text: str) -> list[str]:
    """The words a request is ABOUT, with function words removed."""
    return [word for word in text.split() if word not in _STOPWORDS]


def _features(text: str) -> dict[str, float]:
    """Weighted n-gram features over the content words. Pure function."""
    words = content_words(text)
    features: dict[str, float] = {}
    for word in words:
        features[word] = features.get(word, 0.0) + _WORD_WEIGHT
    for left, right in zip(words, words[1:], strict=False):
        key = f"{left} {right}"
        features[key] = features.get(key, 0.0) + _BIGRAM_WEIGHT
    # Character n-grams INSIDE a content word (typos, morphology), never across
    # the phrase: spanning the phrase would re-import the function words that
    # carrying no meaning is the whole point of dropping them.
    for word in words:
        for start in range(max(0, len(word) - _CHARGRAM_SIZE + 1)):
            gram = word[start:start + _CHARGRAM_SIZE]
            key = f"#{gram}"
            features[key] = features.get(key, 0.0) + _CHARGRAM_WEIGHT
    return features


class _Vectorizer:
    """Hashed n-gram vectors weighted by how rare each feature is.

    Inverse document frequency is what makes "ram" outweigh "much": the rare
    content word is the one that identifies the request. Fitted once on the
    exemplar corpus, then a pure lookup per request.
    """

    def __init__(self) -> None:
        self._idf: dict[str, float] = {}
        self._default = 1.0

    def fit(self, texts: Sequence[str]) -> None:
        counts: dict[str, int] = {}
        for text in texts:
            for key in _features(text):
                counts[key] = counts.get(key, 0) + 1
        total = max(1, len(texts))
        self._idf = {
            key: math.log((total + 1) / (count + 1)) + 1.0 for key, count in counts.items()
        }
        # A feature never seen in the corpus is as rare as they come.
        self._default = math.log(total + 1) + 1.0

    def encode(self, text: str) -> dict[int, float]:
        buckets: dict[int, float] = {}
        for key, weight in _features(text).items():
            dimension, sign = _bucket(key)
            buckets[dimension] = buckets.get(dimension, 0.0) + sign * weight * self._idf.get(
                key, self._default
            )
        return {dimension: value for dimension, value in buckets.items() if value}


def _bucket(key: str) -> tuple[int, float]:
    """Stable (dimension, sign) for a feature.

    ``hash()`` is salted per process, so it cannot be used here: the same
    request must score the same way in every run, and across processes.
    """
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % _DIMENSIONS, 1.0 if value & 1 else -1.0


class HashingEmbedder:
    """Deterministic, dependency-free vectors (the always-available default)."""

    @property
    def name(self) -> str:
        return "hashing"

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [self._vector(text) for text in texts]

    def sparse(self, text: str) -> dict[int, float]:
        """The same vector, as its non-zero entries only.

        ``embed`` fulfils the backend contract, but a hashing vector is sparse
        by construction: spending 512 float slots per phrase to store a few
        dozen numbers would make the index slower than the matching it exists
        to speed up. The index prefers this method when a backend offers it.
        """
        buckets: dict[int, float] = {}
        for key, weight in _features(text).items():
            dimension, sign = _bucket(key)
            buckets[dimension] = buckets.get(dimension, 0.0) + sign * weight
        return {dimension: value for dimension, value in buckets.items() if value}

    def _vector(self, text: str) -> tuple[float, ...]:
        sparse = self.sparse(text)
        vector = [0.0] * _DIMENSIONS
        for dimension, value in sparse.items():
            vector[dimension] = value
        norm = math.sqrt(sum(value * value for value in sparse.values()))
        if norm == 0.0:
            return tuple(vector)
        return tuple(value / norm for value in vector)


@dataclass(frozen=True, slots=True)
class SemanticMatch:
    """One candidate, in the shape the NLU contract publishes."""

    intent: Any  # IntentName — imported lazily to keep this module leaf-level
    score: float
    matched_examples: tuple[str, ...] = ()
    tool: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_intent": self.intent.value,
            "score": round(self.score, 4),
            "matched_examples": list(self.matched_examples),
            "tool": self.tool,
        }


def _norm(vector: dict[int, float]) -> float:
    return math.sqrt(sum(value * value for value in vector.values()))


def _dot(left: dict[int, float], right: dict[int, float]) -> float:
    """Dot product over the SPARSER side — the work scales with evidence."""
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(dimension, 0.0) for dimension, value in left.items())


def _sparse(text: str, embedder: Embedder) -> dict[int, float]:
    """A text as sparse dimensions, whichever kind of backend supplied it."""
    producer = getattr(embedder, "sparse", None)
    if callable(producer):
        return {int(k): float(v) for k, v in producer(text).items() if v}
    vector = embedder.embed([text])[0]
    return {index: float(value) for index, value in enumerate(vector) if value}


class EmbeddingIndex:
    """A cached nearest-exemplar index; consulted only when cheaper layers fail."""

    def __init__(
        self,
        exemplars: Iterable[tuple[Any, Sequence[str]]] | None = None,
        *,
        embedder: Embedder | None = None,
        catalog: Any | None = None,
    ) -> None:
        self.embedder: Embedder = embedder or HashingEmbedder()
        # A supplied MODEL backend brings its own vector space; the built-in
        # hashing space is improved by corpus statistics, so it keeps them even
        # when the caller supplies the (equivalent) default embedder explicitly.
        supplied_model = embedder is not None and not isinstance(embedder, HashingEmbedder)
        self._vectorizer: _Vectorizer | None = None if supplied_model else _Vectorizer()
        self.catalog = catalog
        self._intents: list[Any] = []
        self._phrases: list[str] = []
        self._vectors: list[dict[int, float]] = []
        self._norms: list[float] = []
        self._cache: dict[str, tuple[SemanticMatch, ...]] = {}
        self.hits = 0
        self.misses = 0
        corpus = exemplars if exemplars is not None else _default_corpus()
        self._build(corpus)

    # -- construction -----------------------------------------------------------

    def _build(self, exemplars: Iterable[tuple[Any, Sequence[str]]]) -> None:
        phrases: list[str] = []
        owners: list[Any] = []
        for intent, examples in exemplars:
            for phrase in examples:
                normalized = normalize(phrase)
                if normalized:
                    phrases.append(normalized)
                    owners.append(intent)
        if not phrases:
            return
        self._intents = owners
        self._phrases = phrases
        if self._vectorizer is not None:
            self._vectorizer.fit(phrases)
            self._vectors = [self._vectorizer.encode(phrase) for phrase in phrases]
        else:
            self._vectors = [_sparse(phrase, self.embedder) for phrase in phrases]
        self._norms = [_norm(vector) for vector in self._vectors]

    # -- queries ----------------------------------------------------------------

    @property
    def size(self) -> int:
        """How many exemplar phrases are indexed."""
        return len(self._phrases)

    @property
    def embedder_name(self) -> str:
        return self.embedder.name

    def rank(self, text: str, *, limit: int = 2) -> tuple[SemanticMatch, ...]:
        """Candidates for ``text``, best first, deduplicated by intent.

        Results are cached per normalized text: the same request costs one
        dictionary lookup, which matters because this layer sits on the path a
        user waits on.
        """
        normalized = normalize(text)
        if not normalized or not self._vectors:
            return ()
        cached = self._cache.get(normalized)
        if cached is None:
            self.misses += 1
            cached = self._rank_uncached(normalized, limit)
            self._cache[normalized] = cached
        else:
            self.hits += 1
        return cached[:limit]

    def _rank_uncached(self, normalized: str, limit: int) -> tuple[SemanticMatch, ...]:
        query = (
            self._vectorizer.encode(normalized)
            if self._vectorizer is not None
            else _sparse(normalized, self.embedder)
        )
        query_norm = _norm(query)
        if query_norm == 0.0:
            return ()
        best: dict[Any, tuple[float, str]] = {}
        for intent, phrase, vector, norm in zip(
            self._intents, self._phrases, self._vectors, self._norms, strict=False
        ):
            if norm == 0.0:
                continue
            score = _dot(query, vector) / (query_norm * norm)
            previous = best.get(intent)
            if previous is None or score > previous[0]:
                best[intent] = (score, phrase)
        ranked = sorted(best.items(), key=lambda item: item[1][0], reverse=True)
        return tuple(
            SemanticMatch(
                intent=intent,
                score=score,
                matched_examples=(phrase,),
                tool=self._tool_for(intent),
            )
            for intent, (score, phrase) in ranked[: max(limit, 1)]
        )

    def best(self, text: str) -> SemanticMatch | None:
        ranked = self.rank(text, limit=1)
        return ranked[0] if ranked else None

    def _tool_for(self, intent: Any) -> str:
        if self.catalog is None:
            return ""
        definition = self.catalog.get(intent)
        if definition is None:
            return ""
        tools = getattr(definition, "tools", ()) or ()
        return str(tools[0]) if tools else ""

    def to_dict(self) -> dict[str, object]:
        """Operational metadata: what is indexed, and how it is being used."""
        return {
            "embedder": self.embedder_name,
            "phrases": self.size,
            "cached_queries": len(self._cache),
            "cache_hits": self.hits,
            "cache_misses": self.misses,
        }


def _default_corpus() -> tuple[tuple[Any, tuple[str, ...]], ...]:
    """The shipped exemplars, read here so this module stays import-light."""
    from novacontrol.intelligence.exemplars import default_exemplars

    return default_exemplars()
