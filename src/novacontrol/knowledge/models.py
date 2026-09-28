"""Knowledge base models.

Two generations of the same idea live here, on purpose:

* :class:`KnowledgeArticle` — a curated note a person taught NovaControl (the
  Learn tab's *Teach* box). Short, hand-written, searchable by term overlap.
* The Phase 11 sources, chunks and hits — documents read off disk (Markdown,
  text, code, PDFs, project docs), the pieces they are indexed as, and what a
  query actually matched.

The second generation is an EXTENSION of the first: a taught article can be
ingested as a NOTE source, so there is one searchable index over everything the
installation knows rather than two that disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

#: Rough characters per token. Deliberately the SAME convention the chat
#: context window already uses (``brain/conversation.py`` budgets
#: ``max_context_tokens * 4`` characters), so a knowledge budget and a chat
#: budget are never two different opinions about what a token costs.
CHARS_PER_TOKEN = 4


class SourceType(StrEnum):
    """What kind of thing a source is — which also decides how it is chunked."""

    TEXT = "text"
    MARKDOWN = "markdown"
    DOCUMENTATION = "documentation"
    CODE = "code"
    PDF = "pdf"
    NOTE = "note"


class IngestStatus(StrEnum):
    """What one ingest did to one source."""

    ADDED = "added"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    FAILED = "failed"
    #: The other half of an incremental update: the source was REMOVED because
    #: the file behind it is gone. Deliberately not "skipped" — nothing was
    #: read, something was forgotten.
    PRUNED = "pruned"


@dataclass(frozen=True, slots=True)
class KnowledgeArticle:
    title: str
    body: str
    tags: tuple[str, ...] = ()
    source_url: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "tags": self.tags,
            "source_url": self.source_url,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> KnowledgeArticle:
        return cls(
            title=str(payload["title"]),
            body=str(payload["body"]),
            tags=tuple(payload.get("tags", ())),
            source_url=payload.get("source_url"),
            id=str(payload["id"]),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
        )


@dataclass(frozen=True, slots=True)
class KnowledgeSearchResult:
    article: KnowledgeArticle
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {"article": self.article.to_dict(), "score": self.score}


@dataclass(frozen=True, slots=True)
class KnowledgeSource:
    """One ingested source, and the facts needed to keep it up to date.

    ``content_hash`` is what makes incremental indexing possible: the same bytes
    are UNCHANGED and cost nothing to re-ingest, different bytes bump
    ``version`` and replace exactly that source's chunks — never the whole
    index. ``source_id`` is derived from the project and the path (see
    :func:`source_id_for`), so a file keeps its identity across restarts, and
    ``chunk_count`` lets a reader see how much of the index it owns.
    """

    source_id: str
    source_type: SourceType
    path: str
    project: str = ""
    content_hash: str = ""
    version: int = 1
    chunk_count: int = 0
    bytes: int = 0
    indexed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def title(self) -> str:
        """A human name for the source: its file name, or the path itself."""
        return self.metadata.get("title") or self.path.replace("\\", "/").rsplit("/", 1)[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type.value,
            "path": self.path,
            "project": self.project,
            "content_hash": self.content_hash,
            "version": self.version,
            "chunk_count": self.chunk_count,
            "bytes": self.bytes,
            "indexed_at": self.indexed_at.isoformat(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> KnowledgeSource:
        return cls(
            source_id=str(payload["source_id"]),
            source_type=SourceType(str(payload["source_type"])),
            path=str(payload["path"]),
            project=str(payload.get("project", "")),
            content_hash=str(payload.get("content_hash", "")),
            version=int(payload.get("version", 1)),
            chunk_count=int(payload.get("chunk_count", 0)),
            bytes=int(payload.get("bytes", 0)),
            indexed_at=datetime.fromisoformat(str(payload["indexed_at"])),
            metadata={str(k): str(v) for k, v in payload.get("metadata", {}).items()},
        )


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    """A retrievable piece of one source.

    ``chunk_id`` is ``<source_id>:<ordinal>`` so a citation survives a re-index
    of the same file as long as the chunk is still there, and
    ``start_line``/``end_line`` make a code hit point at a place a person can go
    and look at.
    """

    chunk_id: str
    source_id: str
    ordinal: int
    text: str
    tokens: int = 0
    start_line: int = 1
    end_line: int = 1
    heading: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "source_id": self.source_id,
            "ordinal": self.ordinal,
            "text": self.text,
            "tokens": self.tokens,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "heading": self.heading,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> KnowledgeChunk:
        return cls(
            chunk_id=str(payload["chunk_id"]),
            source_id=str(payload["source_id"]),
            ordinal=int(payload["ordinal"]),
            text=str(payload["text"]),
            tokens=int(payload.get("tokens", 0)),
            start_line=int(payload.get("start_line", 1)),
            end_line=int(payload.get("end_line", 1)),
            heading=str(payload.get("heading", "")),
        )


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    """One retrieved chunk and WHY it was retrieved.

    The score is the fused number the ranking uses; the component scores and the
    ``reasons`` travel with it so a surprising result can be traced to the
    signal that produced it instead of appearing by magic.
    """

    chunk: KnowledgeChunk
    score: float
    lexical_score: float = 0.0
    embedding_score: float = 0.0
    priority: float = 1.0
    recency: float = 1.0
    reasons: tuple[str, ...] = ()
    source: KnowledgeSource | None = None

    @property
    def path(self) -> str:
        return self.source.path if self.source is not None else ""

    @property
    def project(self) -> str:
        return self.source.project if self.source is not None else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk.chunk_id,
            "source_id": self.chunk.source_id,
            "path": self.path,
            "project": self.project,
            "heading": self.chunk.heading,
            "lines": [self.chunk.start_line, self.chunk.end_line],
            "tokens": self.chunk.tokens,
            "score": round(self.score, 4),
            "lexical_score": round(self.lexical_score, 4),
            "embedding_score": round(self.embedding_score, 4),
            "priority": round(self.priority, 3),
            "recency": round(self.recency, 3),
            "reasons": list(self.reasons),
            "text": self.chunk.text,
        }


@dataclass(frozen=True, slots=True)
class IngestReport:
    """What one ingest of one source did."""

    source: KnowledgeSource
    status: IngestStatus
    reason: str = ""
    chunks: int = 0

    @property
    def ok(self) -> bool:
        return self.status is not IngestStatus.FAILED

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "status": self.status.value,
            "reason": self.reason,
            "chunks": self.chunks,
        }


@dataclass(frozen=True, slots=True)
class KnowledgeContext:
    """What actually goes into the model's context, and what was left out.

    ``truncated`` and ``omitted`` exist because a budget that silently drops the
    eighth source is indistinguishable from a query that only matched seven.
    """

    query: str
    text: str
    hits: tuple[KnowledgeHit, ...] = ()
    tokens: int = 0
    budget: int = 0
    considered: int = 0
    omitted: int = 0
    truncated: bool = False
    embedding_backend: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "text": self.text,
            "hits": [hit.to_dict() for hit in self.hits],
            "citations": [hit.chunk.chunk_id for hit in self.hits],
            "tokens": self.tokens,
            "budget": self.budget,
            "considered": self.considered,
            "omitted": self.omitted,
            "truncated": self.truncated,
            "embedding_backend": self.embedding_backend,
        }


def estimate_tokens(text: str) -> int:
    """A character-based token estimate — the convention this build already uses.

    Exact token counts need the model's own tokenizer, which is the thing the
    budget exists to avoid depending on. Four characters per token is the same
    approximation the chat context window budgets with, so the two agree.
    """
    if not text:
        return 0
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def positive_int(value: object, *, default: int, maximum: int | None = None) -> int:
    """A positive integer from loosely-typed input (a tool call, an event payload).

    Tool arguments and event payloads arrive as whatever the caller sent — a
    string, a float, nothing at all — and a limit that is zero, negative or a
    word is a caller mistake, not an instruction. The default is used instead of
    raising, because every caller here wants "a sane cap" rather than an
    exception in the middle of a request.
    """
    number: int | None = None
    if isinstance(value, bool):
        number = None
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float):
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(value.strip())
        except ValueError:
            number = None
    if number is None or number <= 0:
        return default
    return min(number, maximum) if maximum is not None else number


def source_id_for(project: str, path: str) -> str:
    """A stable id for a source: the project and the path, nothing else.

    Deliberately NOT the content: the same file keeps its id (and its version
    history) when it changes, which is what makes "what changed?" answerable
    and what lets a re-index replace one source instead of everything.
    """
    cleaned = path.replace("\\", "/").strip()
    return f"{project.strip() or 'local'}::{cleaned}"


__all__ = [
    "CHARS_PER_TOKEN",
    "IngestReport",
    "IngestStatus",
    "KnowledgeArticle",
    "KnowledgeChunk",
    "KnowledgeContext",
    "KnowledgeHit",
    "KnowledgeSearchResult",
    "KnowledgeSource",
    "SourceType",
    "estimate_tokens",
    "positive_int",
    "source_id_for",
]
