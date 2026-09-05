"""Knowledge base models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


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
    def from_dict(cls, payload: dict[str, Any]) -> "KnowledgeArticle":
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
