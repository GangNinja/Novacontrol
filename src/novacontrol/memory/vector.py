"""Vector index interfaces and a lightweight lexical implementation."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import math
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class VectorDocument:
    id: str
    text: str
    metadata: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class VectorIndex(Protocol):
    async def upsert(self, document: VectorDocument) -> None:
        """Insert or replace a vector-searchable document."""

    async def query(self, text: str, *, limit: int = 10) -> Sequence[tuple[VectorDocument, float]]:
        """Return documents and similarity scores."""


class HashingVectorIndex:
    """Simple cosine-similarity index for local development and tests."""

    def __init__(self) -> None:
        self._documents: dict[str, VectorDocument] = {}
        self._vectors: dict[str, Counter[str]] = {}

    async def upsert(self, document: VectorDocument) -> None:
        self._documents[document.id] = document
        self._vectors[document.id] = _vectorize(document.text)

    async def query(self, text: str, *, limit: int = 10) -> Sequence[tuple[VectorDocument, float]]:
        query_vector = _vectorize(text)
        scored = [
            (self._documents[document_id], _cosine(query_vector, vector))
            for document_id, vector in self._vectors.items()
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        return tuple(item for item in scored[:limit] if item[1] > 0)


def _vectorize(text: str) -> Counter[str]:
    return Counter(term for term in text.lower().split() if term)


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    numerator = sum(left[term] * right[term] for term in left.keys() & right.keys())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return numerator / (left_norm * right_norm)
