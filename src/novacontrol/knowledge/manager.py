"""KnowledgeManager: local knowledge in, relevant context out (Phase 11.1–11.4).

The pipeline the phase names is the shape of this file:

    INGEST → CHUNK → INDEX → RETRIEVE → RERANK → CONTEXT → (the LLM)

Each stage is owned by a module next door — extraction
(:mod:`~novacontrol.knowledge.extract`), chunking
(:mod:`~novacontrol.knowledge.chunking`), the hybrid index
(:mod:`~novacontrol.knowledge.index`), and reranking with the budget
(:mod:`~novacontrol.knowledge.retrieve`) — and this class is the pipeline: it
decides what to read, keeps the bookkeeping that makes re-ingesting cheap, and
answers the two questions a caller actually asks (``search`` for hits,
``retrieve``/``context_for`` for a budgeted block of context).

Three promises hold the design together:

* **Local-first.** Nothing here reaches the network, needs an API key, or
  downloads anything. Reading files, hashing them, chunking them and ranking
  them are all local operations.
* **An embedding model is optional, and never held.** The default backend is the
  dependency-free hashing embedder the rest of the system already uses; a caller
  with a real model supplies it through ``use_embedding_model`` or a lazy
  ``embedding_provider``, and ``release_embedding_model`` puts the index back on
  the cheap backend. The lexical (BM25) signal never depends on any of that, so
  a query answers on a machine with no model at all.
* **Incremental.** A source is identified by project + path and versioned by its
  content hash: unchanged bytes cost one hash and return UNCHANGED, changed
  bytes replace exactly that source's chunks. A duplicate never enters the index
  twice, however many times its directory is ingested.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from novacontrol.core.events import EventBus, EventType
from novacontrol.intelligence.semantic import Embedder, HashingEmbedder
from novacontrol.knowledge.chunking import DEFAULT_CHUNK_TOKENS, chunk_document
from novacontrol.knowledge.extract import (
    UnsupportedDocument,
    read_source,
    source_type_for,
)
from novacontrol.knowledge.index import KnowledgeIndex
from novacontrol.knowledge.models import (
    IngestReport,
    IngestStatus,
    KnowledgeArticle,
    KnowledgeChunk,
    KnowledgeContext,
    KnowledgeHit,
    KnowledgeSource,
    SourceType,
    estimate_tokens,
    source_id_for,
)
from novacontrol.knowledge.project import (
    IGNORED_DIRECTORIES,
    ProjectContext,
    ProjectDetector,
)
from novacontrol.knowledge.retrieve import assemble, cut_text, rank

_logger = logging.getLogger(__name__)

#: Files above this size are skipped rather than indexed: a 40 MB log dump is
#: not knowledge, it is a way to spend a context budget on nothing.
DEFAULT_MAX_FILE_BYTES = 2_000_000

#: The default knowledge budget. Deliberately a fraction of the chat window: the
#: knowledge block is context, not the conversation.
DEFAULT_CONTEXT_TOKENS = 2000

#: How many files one directory ingest will consider. A cap exists because a
#: mistaken path points at a home directory; it is REPORTED rather than silent,
#: because "5000 files ingested" and "5000 of 120000 files ingested" are
#: different facts and only one of them is useful.
DEFAULT_MAX_FILES_PER_TREE = 5000

#: The share of a budget the project header may take before it is cut. A header
#: is a handful of lines; this stops a project with forty open errors from
#: spending the whole budget on its own summary.
PROJECT_SHARE = 0.25

#: How many candidates the index is asked for before reranking narrows them.
DEFAULT_CANDIDATES = 40

#: Embedding resolver: returns an embedder when one is available right now.
EmbeddingProvider = Callable[[], "Embedder | None"]


class KnowledgeManager:
    """The local knowledge engine for one installation (or one project)."""

    def __init__(
        self,
        *,
        index: KnowledgeIndex | None = None,
        embedder: Embedder | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        detector: ProjectDetector | None = None,
        event_bus: EventBus | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_context_tokens: int = DEFAULT_CONTEXT_TOKENS,
        max_per_source: int = 3,
        chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
        max_files_per_tree: int = DEFAULT_MAX_FILES_PER_TREE,
    ) -> None:
        self._index = index or KnowledgeIndex(embedder=embedder)
        if embedder is not None:
            self._index.embedder = embedder
        self._provider = embedding_provider
        self.detector = detector or ProjectDetector()
        self._events = event_bus
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.max_context_tokens = max(1, int(max_context_tokens))
        self.max_per_source = max(1, int(max_per_source))
        self.chunk_tokens = max(40, int(chunk_tokens))
        self.max_files_per_tree = max(1, int(max_files_per_tree))
        self._project = ProjectContext(name="", root="")
        self._history: list[tuple[str, int]] = []

    # -- wiring ----------------------------------------------------------------

    @property
    def index(self) -> KnowledgeIndex:
        return self._index

    def attach_events(self, event_bus: EventBus | None) -> None:
        self._events = event_bus

    # -- embedding lifecycle (never a resident model) ---------------------------

    def use_embedding_model(self, embedder: Embedder) -> int:
        """Point the index at a real embedding model and rebuild what it holds.

        The rebuild is what keeps the vectors comparable: every stored chunk is
        re-embedded with the new backend, so a query is never scored against
        vectors from a different model. Call :meth:`release_embedding_model`
        when the batch is done — nothing here keeps a model resident.
        """
        self._index.embedder = embedder
        return self._rebuild_vectors()

    def release_embedding_model(self) -> int:
        """Give the model back: the index returns to the cheap hashing backend.

        A configured ``embedding_provider`` is kept, because releasing a model
        is about the vectors in memory rather than about forgetting where a
        model can be found: the next ingest or query resolves one again, on
        demand, and the rebuild keeps the two from ever being mixed.
        """
        self._index.embedder = HashingEmbedder()
        return self._rebuild_vectors()

    def refresh_embeddings(self) -> int:
        """Re-embed everything with the current backend (after switching models)."""
        return self._rebuild_vectors()

    def _rebuild_vectors(self) -> int:
        rebuilt = 0
        for source in self._index.sources():
            chunks = self._index.chunks_of(source.source_id)
            self._index.add_source(source, chunks)
            rebuilt += len(chunks)
        return rebuilt

    def _resolved_embedder(self) -> Embedder:
        """The backend to use right now: explicit, then provider, then hashing."""
        if not isinstance(self._index.embedder, HashingEmbedder):
            return self._index.embedder
        if self._provider is not None:
            try:
                candidate = self._provider()
            except Exception as exc:  # noqa: BLE001 - a provider that fails is unavailable
                _logger.debug("Embedding provider failed: %s", exc)
                candidate = None
            if candidate is not None:
                self._index.embedder = candidate
                return candidate
        return self._index.embedder

    @property
    def embedding_backend(self) -> str:
        return self._index.backend_name

    @property
    def embedding_model_available(self) -> bool:
        return self._index.embedding_available

    # -- ingestion -------------------------------------------------------------

    async def ingest_path(
        self,
        path: str | Path,
        *,
        project: str | None = None,
        source_type: SourceType | None = None,
        force: bool = False,
        prune: bool = False,
    ) -> IngestReport:
        """Ingest one file (or one directory tree), re-reading only what changed.

        A blank path and a path that does not exist are FAILED with the reason
        rather than guessed at: ``Path("")`` is the CURRENT DIRECTORY, so the
        naive reading of an empty argument is "index this entire working tree" —
        which is the worst possible answer to a typo.

        ``prune`` is the other half of an incremental update: an ingest can only
        report on files that EXIST, so a document that was deleted would leave
        its chunks retrievable forever — knowledge that looks current and is not.
        It is opt-in, and scoped to the tree you named, because a temporarily
        unreadable mount must not silently empty an index.
        """
        if not str(path).strip():
            return IngestReport(
                source=KnowledgeSource(
                    source_id=source_id_for(project or self._project.name, ""),
                    source_type=source_type or SourceType.TEXT,
                    path="",
                    project=project or self._project.name,
                ),
                status=IngestStatus.FAILED,
                reason="a path is required",
            )
        candidate = Path(path).expanduser()
        if not candidate.exists():
            return IngestReport(
                source=KnowledgeSource(
                    source_id=source_id_for(project or self._project.name, str(candidate)),
                    source_type=source_type or SourceType.TEXT,
                    path=str(candidate),
                    project=project or self._project.name,
                ),
                status=IngestStatus.FAILED,
                reason=f"no such file or directory: {candidate}",
            )
        if candidate.is_dir():
            reports = await self.ingest_tree(
                candidate,
                project=project,
                source_type=source_type,
                force=force,
                prune=prune,
            )
            pruned = sum(1 for report in reports if report.status is IngestStatus.PRUNED)
            reports = tuple(r for r in reports if r.status is not IngestStatus.PRUNED)
            added = sum(1 for report in reports if report.status is IngestStatus.ADDED)
            changed = sum(1 for report in reports if report.status is IngestStatus.UPDATED)
            failed = sum(1 for report in reports if report.status is IngestStatus.FAILED)
            skipped = sum(1 for report in reports if report.status is IngestStatus.SKIPPED)
            unchanged = sum(1 for report in reports if report.status is IngestStatus.UNCHANGED)
            if failed:
                status = IngestStatus.FAILED
            elif added:
                status = IngestStatus.ADDED
            elif changed:
                status = IngestStatus.UPDATED
            elif skipped and skipped == len(reports):
                # "Nothing changed" and "nothing was even readable" are different
                # answers for a directory that was pointed at the wrong place —
                # but only when EVERY file was skipped is the second one true.
                status = IngestStatus.SKIPPED
            else:
                status = IngestStatus.UNCHANGED
            return IngestReport(
                source=KnowledgeSource(
                    source_id=source_id_for(project or self._project.name, str(candidate)),
                    source_type=SourceType.TEXT,
                    path=str(candidate),
                    project=project or self._project.name,
                ),
                status=status,
                reason=(
                    f"{len(reports)} file(s): {unchanged} unchanged, {added} added, "
                    f"{changed} updated, {skipped} skipped, {failed} failed"
                    + (f", {pruned} pruned" if pruned else "")
                ),
                chunks=sum(report.chunks for report in reports),
            )
        return await self._ingest_file(candidate, project=project, source_type=source_type, force=force)

    async def ingest_tree(
        self,
        directory: str | Path,
        *,
        project: str | None = None,
        source_type: SourceType | None = None,
        force: bool = False,
        prune: bool = False,
    ) -> tuple[IngestReport, ...]:
        """Ingest every indexable file under ``directory``, skipping the noise.

        When the tree is larger than :attr:`max_files_per_tree`, the cap is
        REPORTED as a skipped entry rather than applied quietly: a partial index
        that looks complete is worse than one that says how much of it exists.
        """
        root = Path(directory).expanduser()
        if not str(directory).strip() or not root.is_dir():
            return ()
        reports: list[IngestReport] = []
        if prune:
            reports.extend(await self.prune_missing(root))
        files, capped = await asyncio.to_thread(
            _walk_indexable, root, self.max_files_per_tree
        )
        if capped:
            _logger.warning(
                "Indexing only the first %d files under %s; more exist",
                self.max_files_per_tree,
                root,
            )
            reports.append(
                IngestReport(
                    source=KnowledgeSource(
                        source_id=source_id_for(project or self._project.name, str(root)),
                        source_type=SourceType.TEXT,
                        path=str(root),
                        project=project or self._project.name,
                    ),
                    status=IngestStatus.SKIPPED,
                    reason=(
                        f"only the first {self.max_files_per_tree} indexable files were "
                        f"considered; more exist under {root} (raise max_files_per_tree "
                        "or ingest the tree in parts)"
                    ),
                )
            )
        for path in files:
            reports.append(
                await self._ingest_file(
                    path, project=project, source_type=source_type, force=force
                )
            )
        return tuple(reports)

    async def prune_missing(self, root: str | Path) -> tuple[IngestReport, ...]:
        """Forget every file-backed source under ``root`` whose file is gone.

        Scoped to a tree the caller names, and only to sources whose path IS a
        file path: a note or a taught article carries a virtual path
        (``note/…``, ``taught/…``) that is relative, so it can never be swept up
        by a prune of a real directory. Each removal is reported — and announced
        — rather than done quietly, because an index that shrinks without saying
        so is indistinguishable from one that was emptied by mistake.
        """
        base = Path(root).expanduser()
        if not str(root).strip() or not base.is_dir():
            return ()
        try:
            base = base.resolve()
        except OSError:  # pragma: no cover - a path that cannot be resolved is skipped
            return ()
        removed: list[IngestReport] = []
        for source in self._index.sources():
            candidate = Path(source.path)
            if not candidate.is_absolute():
                continue
            try:
                inside = candidate.resolve().is_relative_to(base)
            except OSError:  # pragma: no cover - unresolvable paths are left alone
                continue
            if not inside or candidate.exists():
                continue
            self._index.remove_source(source.source_id)
            report = IngestReport(
                source=source,
                status=IngestStatus.PRUNED,
                reason=f"the file no longer exists: {source.path}",
            )
            removed.append(report)
            await self._emit_pruned(source)
        if removed:
            _logger.info("Pruned %d missing source(s) under %s", len(removed), base)
        return tuple(removed)

    async def ingest_text(
        self,
        text: str,
        *,
        title: str,
        project: str = "",
        source_type: SourceType = SourceType.NOTE,
        path: str = "",
    ) -> IngestReport:
        """Ingest text that is not a file: a note, a pasted excerpt, a taught fact."""
        identifier = path or f"note/{_slug(title) or 'note'}"
        source_id = source_id_for(project or self._project.name, identifier)
        digest = _hash_text(text)
        existing = self._index.source(source_id)
        content_type = source_type
        chunks = _chunks_for(text, source_type=content_type, source_id=source_id, chunk_tokens=self.chunk_tokens)
        source = KnowledgeSource(
            source_id=source_id,
            source_type=content_type,
            path=identifier,
            project=project or self._project.name,
            content_hash=digest,
            version=(existing.version + 1) if existing else 1,
            chunk_count=len(chunks),
            bytes=len(text.encode("utf-8")),
            metadata={"title": title},
        )
        if existing is not None and existing.content_hash == digest:
            return IngestReport(source=existing, status=IngestStatus.UNCHANGED, reason="same content", chunks=len(chunks))
        status = IngestStatus.UPDATED if existing else IngestStatus.ADDED
        self._index.add_source(source, chunks, embedder=self._resolved_embedder())
        await self._emit_indexed(source, chunks, status)
        return IngestReport(source=source, status=status, chunks=len(chunks))

    async def ingest_article(self, article: KnowledgeArticle, *, project: str = "") -> IngestReport:
        """Bridge a taught article into the index, so one search covers both."""
        body = article.body if not article.tags else f"{article.body}\n\nTags: {', '.join(article.tags)}"
        return await self.ingest_text(
            body,
            title=article.title,
            project=project,
            source_type=SourceType.NOTE,
            path=f"taught/{article.id}",
        )

    async def _ingest_file(
        self,
        path: Path,
        *,
        project: str | None,
        source_type: SourceType | None,
        force: bool,
    ) -> IngestReport:
        project_name = project if project is not None else self._project.name
        kind = source_type_for(path, explicit=source_type)
        source_id = source_id_for(project_name, str(path))
        placeholder = KnowledgeSource(
            source_id=source_id,
            source_type=kind or SourceType.TEXT,
            path=str(path),
            project=project_name,
        )
        if kind is None:
            return IngestReport(
                source=placeholder,
                status=IngestStatus.SKIPPED,
                reason=f"unsupported file type: {path.suffix or 'no suffix'}",
            )
        try:
            size = path.stat().st_size
        except OSError as exc:
            return IngestReport(
                source=placeholder, status=IngestStatus.FAILED, reason=f"cannot stat the file: {exc}"
            )
        if size > self.max_file_bytes:
            return IngestReport(
                source=placeholder,
                status=IngestStatus.SKIPPED,
                reason=f"{size} bytes exceeds the {self.max_file_bytes}-byte limit",
            )
        existing = self._index.source(source_id)
        try:
            digest = await asyncio.to_thread(_hash_file, path)
        except OSError as exc:
            return IngestReport(
                source=placeholder, status=IngestStatus.FAILED, reason=f"cannot read the file: {exc}"
            )
        if existing is not None and existing.content_hash == digest and not force:
            return IngestReport(
                source=existing, status=IngestStatus.UNCHANGED, reason="same content", chunks=existing.chunk_count
            )
        try:
            extracted = await asyncio.to_thread(read_source, path, source_type=kind)
        except UnsupportedDocument as exc:
            return IngestReport(source=placeholder, status=IngestStatus.SKIPPED, reason=exc.reason)
        except Exception as exc:  # noqa: BLE001 - an unreadable file is a reported skip
            return IngestReport(
                source=placeholder, status=IngestStatus.FAILED, reason=f"{type(exc).__name__}: {exc}"
            )
        chunks = _chunks_for(
            extracted.text,
            source_type=extracted.source_type,
            source_id=source_id,
            chunk_tokens=self.chunk_tokens,
        )
        metadata = dict(extracted.metadata)
        if extracted.warning:
            metadata["warning"] = extracted.warning
        source = KnowledgeSource(
            source_id=source_id,
            source_type=extracted.source_type,
            path=str(path),
            project=project_name,
            content_hash=digest,
            version=(existing.version + 1) if existing else 1,
            chunk_count=len(chunks),
            bytes=size,
            metadata=metadata,
        )
        status = IngestStatus.UPDATED if existing else IngestStatus.ADDED
        if not chunks:
            self._index.remove_source(source_id)
            return IngestReport(
                source=source, status=IngestStatus.SKIPPED, reason="no text to index", chunks=0
            )
        self._index.add_source(source, chunks, embedder=self._resolved_embedder())
        await self._emit_indexed(source, chunks, status)
        return IngestReport(source=source, status=status, chunks=len(chunks))

    # -- retrieval -------------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        limit: int = 8,
        project: str | None = None,
        active_only: bool = False,
    ) -> tuple[KnowledgeHit, ...]:
        """Ranked hits for a query: reranked, deduplicated, and unbudgeted.

        ``active_only`` defaults to FALSE here and TRUE in :meth:`retrieve`: a
        search is the general question ("what do we know about X") and a
        retrieval is the project-aware one ("what does THIS project say about
        X"), which is the flow the phase describes. Both use the same scope
        rule — see :func:`_project_scope`.

        A near-duplicate of a hit that already ranked higher is dropped rather
        than returned: the same paragraph in two files is one answer, and
        handing a caller five hits of which two are the same text is worse than
        handing it three.
        """
        if not query.strip():
            return ()
        project_name = project if project is not None else self._project.name
        source_ids = _project_scope(self._index, project_name, active_only)
        candidates = self._index.search(
            query,
            limit=max(limit, DEFAULT_CANDIDATES),
            embedder=self._resolved_embedder(),
            source_ids=source_ids,
        )
        ranked = rank(
            candidates,
            index_sources={source.source_id: source for source in self._index.sources()},
            project=project_name,
        )
        hits: list[KnowledgeHit] = []
        for entry in ranked:
            if entry.duplicate_of:
                continue
            hits.append(entry.hit)
            if len(hits) >= max(1, limit):
                break
        await self._emit_retrieved(query, hits)
        return tuple(hits)

    async def retrieve(
        self,
        query: str,
        *,
        limit: int = 8,
        budget_tokens: int | None = None,
        project: str | None = None,
        active_only: bool = True,
        max_per_source: int | None = None,
    ) -> KnowledgeContext:
        """The budgeted context block for a query (Phase 11.4).

        Project-aware by default: when the active project has anything indexed,
        only its sources are candidates. When it has nothing indexed yet, the
        search falls back to the whole index (see :func:`_project_scope`) — a
        freshly opened project that answers "nothing" to every question is
        indistinguishable from a broken engine — and each hit still names the
        project it came from.
        """
        budget = self.max_context_tokens if budget_tokens is None else max(1, int(budget_tokens))
        if not query.strip():
            return KnowledgeContext(query=query, text="", budget=budget)
        project_name = project if project is not None else self._project.name
        source_ids = _project_scope(self._index, project_name, active_only)
        candidates = self._index.search(
            query,
            limit=max(limit, DEFAULT_CANDIDATES),
            embedder=self._resolved_embedder(),
            source_ids=source_ids,
        )
        ranked = rank(
            candidates,
            index_sources={source.source_id: source for source in self._index.sources()},
            project=project_name,
        )
        context = assemble(
            query,
            ranked,
            budget_tokens=budget,
            max_per_source=self.max_per_source if max_per_source is None else max_per_source,
            embedding_backend=self._index.backend_name,
        )
        await self._emit_retrieved(query, context.hits)
        self._history.append((query, len(context.hits)))
        del self._history[:-20]  # the report is "recent queries", so it stays bounded
        return context

    async def context_for(
        self,
        question: str,
        *,
        budget_tokens: int | None = None,
        active_task: str = "",
        project: str | None = None,
    ) -> KnowledgeContext:
        """Project awareness + relevant knowledge, in one budgeted block.

        This is the phase's worked example — *"fix the authentication issue"* —
        resolved without touching a file: which project, which branch, which
        language, what recently changed, what errors are open, what the tests
        said, and the few excerpts that actually match the question.
        """
        budget = self.max_context_tokens if budget_tokens is None else max(1, int(budget_tokens))
        if active_task:
            self.detector.set_active_task(active_task)
        context = self.project_context()
        project_name = project if project is not None else self._project.name
        # The header is budgeted too. It is short in a healthy project and long
        # in one with forty open errors, and the second case must not push the
        # block past the caller's budget unnoticed.
        project_block = context.render()
        project_cut = False
        if estimate_tokens(project_block) > max(1, int(budget * PROJECT_SHARE)):
            project_block = cut_text(project_block, max(1, int(budget * PROJECT_SHARE)))
            project_cut = True
        knowledge_budget = max(1, budget - estimate_tokens(project_block))
        knowledge = await self.retrieve(
            question,
            budget_tokens=knowledge_budget,
            project=project_name,
        )
        sections = [section for section in (project_block, knowledge.text) if section]
        text = "\n\n".join(sections)
        # ``tokens`` is what the block actually costs, measured on the block: the
        # caller's budget is a statement about the request it is about to send.
        spent = estimate_tokens(text)
        return KnowledgeContext(
            query=question,
            text=text,
            hits=knowledge.hits,
            tokens=spent,
            budget=budget,
            considered=knowledge.considered,
            omitted=knowledge.omitted,
            truncated=project_cut or knowledge.truncated or spent > budget,
            embedding_backend=knowledge.embedding_backend,
        )

    def _history_snapshot(self) -> tuple[tuple[str, int], ...]:
        return tuple(self._history[-20:])

    # -- project awareness -----------------------------------------------------

    def set_project(self, path: str | Path | None = None) -> ProjectContext:
        """Set (and return) the active project context for the given directory."""
        self._project = self.detector.detect(path)
        return self._project

    def set_active_task(self, task: str) -> None:
        self.detector.set_active_task(task)

    def project_context(self, path: str | Path | None = None) -> ProjectContext:
        """The active project's context, re-read on demand (never cached stale)."""
        if path is not None:
            return self.detector.detect(path)
        if not self._project.is_project:
            self._project = self.detector.detect(None)
        else:
            self._project = self.detector.detect(self._project.root)
        return self._project

    @property
    def active_project(self) -> str:
        return self._project.name

    # -- maintenance -----------------------------------------------------------

    def sources(self) -> tuple[KnowledgeSource, ...]:
        return self._index.sources()

    def source(self, source_id: str) -> KnowledgeSource | None:
        return self._index.source(source_id)

    def remove_source(self, source_id: str) -> int:
        """Forget one source — the file may be gone, or the user may want it gone."""
        return self._index.remove_source(source_id)

    def clear(self) -> None:
        """Forget everything (used by tests and by a deliberate purge)."""
        for source in self.sources():
            self._index.remove_source(source.source_id)

    def stats(self) -> dict[str, Any]:
        return {
            **self._index.stats(),
            "active_project": self.active_project,
            "max_context_tokens": self.max_context_tokens,
            "chunk_tokens": self.chunk_tokens,
            "recent_queries": [query for query, _hits in self._history_snapshot()],
        }

    def to_dict(self) -> dict[str, Any]:
        """A snapshot that survives a restart (sources, chunks, project)."""
        return {
            "sources": [source.to_dict() for source in self.sources()],
            "chunks": [
                chunk.to_dict()
                for source in self.sources()
                for chunk in self._index.chunks_of(source.source_id)
            ],
            "project": self._project.to_dict(),
            "max_context_tokens": self.max_context_tokens,
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        **kwargs: Any,
    ) -> KnowledgeManager:
        """Restore a snapshot. Vectors are recomputed locally and cheaply."""
        sources = {
            str(item["source_id"]): KnowledgeSource.from_dict(dict(item))
            for item in payload.get("sources", ())
            if isinstance(item, Mapping)
        }
        chunks: dict[str, list[KnowledgeChunk]] = {source_id: [] for source_id in sources}
        for item in payload.get("chunks", ()):
            if not isinstance(item, Mapping):
                continue
            chunk = KnowledgeChunk.from_dict(dict(item))
            chunks.setdefault(chunk.source_id, []).append(chunk)
        max_tokens = int(payload.get("max_context_tokens", DEFAULT_CONTEXT_TOKENS) or DEFAULT_CONTEXT_TOKENS)
        manager = cls(max_context_tokens=max_tokens, **kwargs)
        for source_id, source in sources.items():
            manager._index.add_source(source, tuple(chunks.get(source_id, ())))  # noqa: SLF001
        project = payload.get("project")
        if isinstance(project, Mapping):
            manager._project = ProjectContext.from_dict(dict(project))
        return manager

    # -- events ----------------------------------------------------------------

    async def _emit_indexed(
        self, source: KnowledgeSource, chunks: Sequence[KnowledgeChunk], status: IngestStatus
    ) -> None:
        if self._events is None:
            return
        await self._events.emit(
            EventType.KNOWLEDGE_INDEXED,
            source="knowledge",
            sources=1,
            chunks=len(chunks),
            source_id=source.source_id,
            path=source.path,
            status=status.value,
            project=source.project,
        )

    async def _emit_pruned(self, source: KnowledgeSource) -> None:
        """Announce a removal on the same event a reader watches for indexing."""
        if self._events is None:
            return
        await self._events.emit(
            EventType.KNOWLEDGE_INDEXED,
            source="knowledge",
            sources=0,
            chunks=0,
            source_id=source.source_id,
            path=source.path,
            status=IngestStatus.PRUNED.value,
            project=source.project,
        )

    async def _emit_retrieved(self, query: str, hits: Sequence[KnowledgeHit]) -> None:
        if self._events is None:
            return
        await self._events.emit(
            EventType.KNOWLEDGE_RETRIEVED,
            source="knowledge",
            query=query,
            hits=len(hits),
            sources=len({hit.chunk.source_id for hit in hits}),
        )


def _walk_indexable(root: Path, max_files: int) -> tuple[tuple[Path, ...], bool]:
    """Indexable files under ``root`` (stable order, noise excluded) and the cap.

    The second value says whether the walk STOPPED at ``max_files`` with more to
    see, so the caller can report a partial index as partial.
    """
    found: list[Path] = []
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if name not in IGNORED_DIRECTORIES and not name.startswith(".")
        )
        for filename in sorted(filenames):
            if len(found) >= max_files:
                return tuple(found), True
            candidate = Path(directory) / filename
            if source_type_for(candidate) is not None:
                found.append(candidate)
    return tuple(found), False


def _chunks_for(
    text: str,
    *,
    source_type: SourceType,
    source_id: str,
    chunk_tokens: int,
) -> tuple[KnowledgeChunk, ...]:
    pieces = chunk_document(text, source_type=source_type, max_tokens=chunk_tokens)
    return tuple(
        KnowledgeChunk(
            chunk_id=f"{source_id}:{piece.ordinal}",
            source_id=source_id,
            ordinal=piece.ordinal,
            text=piece.text,
            tokens=piece.tokens,
            start_line=piece.start_line,
            end_line=piece.end_line,
            heading=piece.heading,
        )
        for piece in pieces
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _project_scope(
    index: KnowledgeIndex, project: str, active_only: bool
) -> list[str] | None:
    """The source ids a project-scoped query may look at, or ``None`` for all.

    Two behaviours, one rule: scope to the active project WHEN it has anything
    indexed, and otherwise look everywhere. The second half matters more than it
    looks — a freshly opened project has nothing indexed yet, and answering
    "nothing" to every question in that state would be indistinguishable from a
    broken engine. Falling back and searching the rest of the index is the
    honest behaviour: the caller gets hits, and the hit's ``project`` field says
    which project each one came from.
    """
    if not active_only or not project:
        return None
    scoped = [
        source.source_id for source in index.sources() if source.project == project
    ]
    return scoped or None


def _slug(value: str) -> str:
    cleaned = "".join(char if char.isalnum() else "-" for char in str(value).strip().lower())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")


__all__ = [
    "DEFAULT_CANDIDATES",
    "DEFAULT_CONTEXT_TOKENS",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_FILES_PER_TREE",
    "EmbeddingProvider",
    "KnowledgeManager",
    "PROJECT_SHARE",
]
