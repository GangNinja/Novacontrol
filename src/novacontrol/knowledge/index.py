"""The index: BM25 over chunks, plus optional embedding vectors over the same.

Two retrieval signals, one index, and a rule about which one is allowed to be
missing:

* **Lexical (BM25) is always available.** It needs no model, no download and no
  network, and for the queries this feature exists for — "where is the retry
  policy configured?", "which file has the approval token" — a rare term in the
  document is exactly what identifies it. BM25 is what makes the engine work on
  a machine with no embedding model at all.
* **Embeddings are optional and pluggable.** The engine takes any object with
  ``embed`` (the same :class:`~novacontrol.intelligence.semantic.Embedder` seam
  the intent matcher and the tool retriever already use), and its default is the
  dependency-free hashing embedder. Supplying a real model improves paraphrase
  recall; NOT supplying one changes nothing about whether a query answers.

Fusion is a weighted sum with the weights stated in the code, and every hit
reports which signal produced it — a hit that only embeddings found says so.

Updates are per SOURCE: re-ingesting one file deletes that file's chunks and
terms and adds the new ones, so the index never rebuilds because a file changed,
and a stale chunk can never be retrieved.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from novacontrol.intelligence.normalize import normalize, normalized_tokens
from novacontrol.intelligence.semantic import Embedder, HashingEmbedder
from novacontrol.knowledge.models import KnowledgeChunk, KnowledgeSource

#: BM25's term-frequency saturation and length-normalisation parameters — the
#: documented defaults (k1 = 1.2, b = 0.75), not tuned on a corpus that is not
#: this one.
_BM25_K1 = 1.2
_BM25_B = 0.75

#: How much of the fused score each signal carries. Lexical leads because it is
#: the signal that is always there and the one a rare identifier rides on;
#: embeddings are the recall the model adds on top.
_LEXICAL_WEIGHT = 1.0
_EMBEDDING_WEIGHT = 0.6

#: The floor an embedding-only match must clear to be reported at all: a hash
#: vector matches everything a little, and a document that shares nothing with
#: the query must not be cited as if it did.
_EMBEDDING_FLOOR = 0.15

#: Function words. Kept local rather than shared so the retrieval vocabulary can
#: change without moving the intent matcher's behaviour with it.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "if", "then", "than", "so", "as",
        "of", "for", "to", "in", "on", "at", "by", "with", "from", "into",
        "over", "under", "about", "is", "are", "was", "were", "be", "been",
        "being", "am", "do", "does", "did", "done", "have", "has", "had",
        "will", "would", "can", "could", "should", "shall", "may", "might",
        "must", "it", "its", "this", "that", "these", "those", "i", "you",
        "we", "they", "he", "she", "them", "us", "my", "our", "your", "their",
        "me", "him", "her", "not", "no", "yes", "there", "here", "just",
        "only", "very", "also", "please", "how", "what", "which", "who",
        "when", "where", "why", "much", "many", "more", "less", "any", "some",
    }
)


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    """One candidate from the index, with the evidence for its score."""

    chunk: KnowledgeChunk
    score: float
    lexical: float = 0.0
    embedding: float = 0.0
    reasons: tuple[str, ...] = ()

    @property
    def matched_terms(self) -> tuple[str, ...]:
        """The query terms this chunk shares — the trace a person can check."""
        for reason in self.reasons:
            if reason.startswith("terms: "):
                return tuple(reason.removeprefix("terms: ").split(", "))
        return ()


def terms(text: str) -> list[str]:
    """The words a query or a chunk is about, normalized and de-stopped."""
    return [token for token in normalized_tokens(normalize(text)) if token not in _STOPWORDS]


class KnowledgeIndex:
    """Chunk store + BM25 postings + optional vectors, updated per source."""

    def __init__(self, *, embedder: Embedder | None = None) -> None:
        self.embedder: Embedder = embedder or HashingEmbedder()
        self._chunks: dict[str, KnowledgeChunk] = {}
        self._sources: dict[str, KnowledgeSource] = {}
        self._by_source: dict[str, tuple[str, ...]] = {}
        self._postings: dict[str, dict[str, int]] = {}
        self._lengths: dict[str, int] = {}
        self._term_sets: dict[str, frozenset[str]] = {}
        self._vectors: dict[str, dict[int, float]] = {}
        self._vectors_from: str = ""

    # -- writing ---------------------------------------------------------------

    def add_source(
        self,
        source: KnowledgeSource,
        chunks: Sequence[KnowledgeChunk],
        *,
        embedder: Embedder | None = None,
    ) -> None:
        """Install (or replace) every chunk of one source.

        A re-ingest of the same source removes the old chunks first, so nothing
        the file no longer says can be retrieved — and the vectors are recomputed
        for exactly these chunks, which is the only moment an embedding model is
        needed.
        """
        self.remove_source(source.source_id)
        backend = embedder or self.embedder
        vectors = _vectors_for(chunks, backend) if chunks else {}
        for chunk in chunks:
            self._chunks[chunk.chunk_id] = chunk
            self._lengths[chunk.chunk_id] = max(1, chunk.tokens)
            chunk_terms = Counter(terms(chunk.text))
            self._term_sets[chunk.chunk_id] = frozenset(chunk_terms)
            for term, count in chunk_terms.items():
                self._postings.setdefault(term, {})[chunk.chunk_id] = count
            if chunk.chunk_id in vectors:
                self._vectors[chunk.chunk_id] = vectors[chunk.chunk_id]
        self._sources[source.source_id] = source
        self._by_source[source.source_id] = tuple(chunk.chunk_id for chunk in chunks)
        self._vectors_from = getattr(backend, "name", type(backend).__name__)

    def remove_source(self, source_id: str) -> int:
        """Forget one source entirely, and say how many chunks went with it."""
        chunk_ids = self._by_source.pop(source_id, ())
        for chunk_id in chunk_ids:
            self._chunks.pop(chunk_id, None)
            self._lengths.pop(chunk_id, None)
            self._term_sets.pop(chunk_id, None)
            self._vectors.pop(chunk_id, None)
        if chunk_ids:
            for term in list(self._postings):
                bucket = self._postings[term]
                for chunk_id in chunk_ids:
                    bucket.pop(chunk_id, None)
                if not bucket:
                    del self._postings[term]
        self._sources.pop(source_id, None)
        return len(chunk_ids)

    # -- reading ---------------------------------------------------------------

    @property
    def backend_name(self) -> str:
        return self._vectors_from or getattr(self.embedder, "name", type(self.embedder).__name__)

    @property
    def embedding_available(self) -> bool:
        """Whether a real embedding model (rather than the hashing default) is in use."""
        return not isinstance(self.embedder, HashingEmbedder)

    def chunk_count(self) -> int:
        return len(self._chunks)

    def source_count(self) -> int:
        return len(self._sources)

    def sources(self) -> tuple[KnowledgeSource, ...]:
        return tuple(self._sources[key] for key in sorted(self._sources))

    def source(self, source_id: str) -> KnowledgeSource | None:
        return self._sources.get(source_id)

    def chunks_of(self, source_id: str) -> tuple[KnowledgeChunk, ...]:
        return tuple(self._chunks[key] for key in self._by_source.get(source_id, ()))

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        embedder: Embedder | None = None,
        source_ids: Sequence[str] | None = None,
    ) -> tuple[ScoredChunk, ...]:
        """Rank chunks against a query, lexically and (when possible) semantically.

        ``source_ids`` RESTRICTS the search to those sources (the project-scoped
        case) and is translated to their chunk ids here, because that is what the
        postings and the vectors are keyed by — see :meth:`_chunk_ids_for`; the
        two were once compared directly, which silently emptied every scoped
        search.

        Both signals are computed and fused; an embedding-only match below the
        floor is not reported, so "the hashing vector liked it a little" never
        becomes a citation.
        """
        query_terms = terms(query)
        if not query_terms and not query.strip():
            return ()
        allowed = self._chunk_ids_for(source_ids)
        lexical = self._lexical_scores(query_terms, allowed)
        semantic = self._embedding_scores(query, allowed, embedder)
        scores: dict[str, float] = {}
        for chunk_id, value in lexical.items():
            scores[chunk_id] = scores.get(chunk_id, 0.0) + _LEXICAL_WEIGHT * value
        for chunk_id, value in semantic.items():
            if value >= _EMBEDDING_FLOOR or chunk_id in lexical:
                scores[chunk_id] = scores.get(chunk_id, 0.0) + _EMBEDDING_WEIGHT * value
        results: list[ScoredChunk] = []
        for chunk_id, score in scores.items():
            chunk = self._chunks.get(chunk_id)
            if chunk is None or score <= 0:
                continue
            lexical_score = lexical.get(chunk_id, 0.0)
            embedding_score = semantic.get(chunk_id, 0.0)
            shared = sorted(set(query_terms) & self._term_sets.get(chunk_id, frozenset()))
            reasons: list[str] = []
            if shared:
                reasons.append(f"terms: {', '.join(shared)}")
            if lexical_score:
                reasons.append(f"bm25={lexical_score:.2f}")
            if embedding_score >= _EMBEDDING_FLOOR:
                reasons.append(f"embedding={embedding_score:.2f}")
            results.append(
                ScoredChunk(
                    chunk=chunk,
                    score=score,
                    lexical=lexical_score,
                    embedding=embedding_score,
                    reasons=tuple(reasons),
                )
            )
        results.sort(key=lambda item: (-item.score, item.chunk.chunk_id))
        return tuple(results[: max(1, int(limit))])

    def stats(self) -> dict[str, Any]:
        return {
            "sources": self.source_count(),
            "chunks": self.chunk_count(),
            "terms": len(self._postings),
            "vectors": len(self._vectors),
            "embedding_backend": self.backend_name,
            "embedding_model": self.embedding_available,
        }

    def _chunk_ids_for(self, source_ids: Sequence[str] | None) -> set[str] | None:
        """The chunk ids a source-scoped search may look at (None = everywhere).

        A caller names SOURCES ("only this project's files") and the index stores
        chunks: the translation has to happen somewhere, and it happens here.
        """
        if source_ids is None:
            return None
        return {
            chunk_id
            for source_id in source_ids
            for chunk_id in self._by_source.get(source_id, ())
        }

    # -- scoring ---------------------------------------------------------------

    def _lexical_scores(
        self, query_terms: Sequence[str], allowed: set[str] | None
    ) -> dict[str, float]:
        total = max(1, self.chunk_count())
        average_length = (
            sum(self._lengths.values()) / len(self._lengths) if self._lengths else 1.0
        )
        scores: dict[str, float] = {}
        for term in dict.fromkeys(query_terms):
            postings = self._postings.get(term)
            if not postings:
                continue
            document_frequency = len(postings)
            idf = math.log(1 + (total - document_frequency + 0.5) / (document_frequency + 0.5))
            for chunk_id, frequency in postings.items():
                if allowed is not None and chunk_id not in allowed:
                    continue
                length = self._lengths.get(chunk_id, 1)
                saturation = (frequency * (_BM25_K1 + 1)) / (
                    frequency + _BM25_K1 * (1 - _BM25_B + _BM25_B * (length / average_length))
                )
                scores[chunk_id] = scores.get(chunk_id, 0.0) + idf * saturation
        if not scores:
            return {}
        peak = max(scores.values())
        return {chunk_id: value / peak for chunk_id, value in scores.items()}

    def _embedding_scores(
        self, query: str, allowed: set[str] | None, embedder: Embedder | None
    ) -> dict[str, float]:
        backend = embedder or self.embedder
        # Vectors and queries must come from the SAME backend to be comparable:
        # a query embedded by a different model than the chunks were is noise,
        # so the vector signal is skipped rather than mis-measured.
        if self._vectors and self._vectors_from and not _same_backend(backend, self._vectors_from):
            return {}
        query_vector = _sparse_vector(query, backend)
        if not query_vector:
            return {}
        query_norm = math.sqrt(sum(value * value for value in query_vector.values()))
        if query_norm == 0:
            return {}
        scores: dict[str, float] = {}
        for chunk_id, vector in self._vectors.items():
            if allowed is not None and chunk_id not in allowed:
                continue
            norm = math.sqrt(sum(value * value for value in vector.values()))
            if norm == 0:
                continue
            overlap = sum(
                value * vector.get(dimension, 0.0) for dimension, value in query_vector.items()
            )
            score = overlap / (query_norm * norm)
            if score > 0:
                scores[chunk_id] = score
        return scores


def _same_backend(embedder: Embedder, vectors_from: str) -> bool:
    """Whether this embedder is the one the stored vectors were built with."""
    name = getattr(embedder, "name", type(embedder).__name__)
    return str(name) == vectors_from


def _vectors_for(
    chunks: Sequence[KnowledgeChunk], embedder: Embedder
) -> dict[str, dict[int, float]]:
    """Sparse unit vectors for the chunks, from whichever backend is in use."""
    vectors: dict[str, dict[int, float]] = {}
    for chunk in chunks:
        vector = _sparse_vector(chunk.text, embedder)
        if vector:
            vectors[chunk.chunk_id] = vector
    return vectors


def _sparse_vector(text: str, embedder: Embedder) -> dict[int, float]:
    """A text as sparse dimensions, from a hashing or a model-backed embedder."""
    producer = getattr(embedder, "sparse", None)
    if callable(producer):
        return {int(key): float(value) for key, value in producer(text).items() if value}
    try:
        vector = list(embedder.embed([text])[0])
    except Exception:  # noqa: BLE001 - a backend that cannot embed is a missing signal
        return {}
    return {index: float(value) for index, value in enumerate(vector) if value}


def index_from_sources(
    entries: Iterable[tuple[KnowledgeSource, Sequence[KnowledgeChunk]]],
    *,
    embedder: Embedder | None = None,
) -> KnowledgeIndex:
    """Build an index from a snapshot (used when restoring persisted knowledge)."""
    index = KnowledgeIndex(embedder=embedder)
    for source, chunks in entries:
        index.add_source(source, chunks)
    return index


__all__ = [
    "KnowledgeIndex",
    "ScoredChunk",
    "index_from_sources",
    "terms",
]
