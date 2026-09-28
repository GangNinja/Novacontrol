"""Knowledge subsystem: curated articles, and the Phase 11 local RAG engine.

Two generations, one package, on purpose:

* :class:`KnowledgeBase` — the small curated store behind the Learn tab's
  *Teach* box. Articles a person wrote, searched by term overlap.
* :class:`KnowledgeManager` — the Phase 11 engine: documents read off disk,
  chunked, indexed, retrieved, reranked and budgeted into context. It is
  local-first, needs no embedding model, and can ingest a taught article as a
  NOTE source, so one search covers everything the installation knows.

:class:`ProjectContext` is the other half of the phase: which code project the
user is standing in, read from the workspace and never written to.
"""

from novacontrol.knowledge.base import KnowledgeBase
from novacontrol.knowledge.manager import KnowledgeManager
from novacontrol.knowledge.models import (
    IngestReport,
    IngestStatus,
    KnowledgeArticle,
    KnowledgeChunk,
    KnowledgeContext,
    KnowledgeHit,
    KnowledgeSearchResult,
    KnowledgeSource,
    SourceType,
    estimate_tokens,
    source_id_for,
)
from novacontrol.knowledge.project import ProjectContext, ProjectDetector
from novacontrol.knowledge.runtime import KnowledgeModule
from novacontrol.knowledge.tools import knowledge_tools

__all__ = [
    "IngestReport",
    "IngestStatus",
    "KnowledgeArticle",
    "KnowledgeBase",
    "KnowledgeChunk",
    "KnowledgeContext",
    "KnowledgeHit",
    "KnowledgeManager",
    "KnowledgeModule",
    "KnowledgeSearchResult",
    "KnowledgeSource",
    "ProjectContext",
    "ProjectDetector",
    "SourceType",
    "estimate_tokens",
    "knowledge_tools",
    "source_id_for",
]
