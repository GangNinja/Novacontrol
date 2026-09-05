"""Knowledge base manager."""

from __future__ import annotations

from typing import Any

from novacontrol.knowledge.models import KnowledgeArticle, KnowledgeSearchResult


class KnowledgeBase:
    """Stores and searches curated knowledge articles."""

    def __init__(self) -> None:
        self._articles: dict[str, KnowledgeArticle] = {}

    def add_article(
        self,
        title: str,
        body: str,
        *,
        tags: tuple[str, ...] = (),
        source_url: str | None = None,
    ) -> KnowledgeArticle:
        article = KnowledgeArticle(title=title, body=body, tags=tags, source_url=source_url)
        self._articles[article.id] = article
        return article

    def get_article(self, article_id: str) -> KnowledgeArticle:
        try:
            return self._articles[article_id]
        except KeyError as exc:
            raise KeyError(f"Knowledge article not found: {article_id}") from exc

    def search(self, query: str, *, limit: int = 10) -> tuple[KnowledgeSearchResult, ...]:
        normalized = query.lower()
        results = []
        for article in self._articles.values():
            haystack = f"{article.title} {article.body} {' '.join(article.tags)}".lower()
            score = sum(1 for term in normalized.split() if term in haystack)
            if score or not normalized.strip():
                results.append(KnowledgeSearchResult(article=article, score=float(score or 1)))
        results.sort(key=lambda result: result.score, reverse=True)
        return tuple(results[:limit])

    def to_dict(self) -> dict[str, Any]:
        return {"articles": [article.to_dict() for article in self._articles.values()]}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "KnowledgeBase":
        from novacontrol.knowledge.models import KnowledgeArticle

        knowledge = cls()
        for item in payload.get("articles", []):
            if isinstance(item, dict):
                article = KnowledgeArticle.from_dict(item)
                knowledge._articles[article.id] = article
        return knowledge
