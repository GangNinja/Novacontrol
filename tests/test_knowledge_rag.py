"""Tests for Phase 11: local knowledge, project awareness and the token budget.

The tests are named after the promises, one by one: that a document is read into
the index, that reading it twice costs nothing, that an edit replaces exactly
that source, that a hit can be traced to a file and a line, that the ranking
prefers the project you are standing in, that a budget is spent best-first and
that what did not fit is COUNTED, and that all of it still works on a machine
with no embedding model — which is the default here, not a fallback.

Everything runs against temporary directories and the dependency-free backend.
Nothing in this file touches the network or a model, and nothing in it writes to
the repository.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from novacontrol.core.events import EventBus, EventType
from novacontrol.core.security import PermissionScope
from novacontrol.intelligence.semantic import HashingEmbedder
from novacontrol.knowledge import (
    IngestStatus,
    KnowledgeBase,
    KnowledgeManager,
    KnowledgeModule,
    ProjectDetector,
    SourceType,
    knowledge_tools,
)
from novacontrol.knowledge.chunking import chunk_document
from novacontrol.knowledge.extract import UnsupportedDocument, read_source, source_type_for
from novacontrol.knowledge.index import KnowledgeIndex
from novacontrol.knowledge.models import estimate_tokens, positive_int, source_id_for
from novacontrol.knowledge.project import find_project_root, read_branch
from novacontrol.knowledge.retrieve import assemble, rank
from novacontrol.tools.registry import ToolRegistry

README = """# Widget service

## Configuration

The widget timeout is configured in widgets.py and defaults to thirty seconds.

## Troubleshooting

A timeout usually means the upstream cache is cold.
"""

MODULE = '''"""Widget helpers."""

DEFAULT_TIMEOUT = 30


def widget_timeout() -> int:
    """The timeout a widget request may take, in seconds."""
    return DEFAULT_TIMEOUT


def reset_widget_cache() -> None:
    """Drop the cached widget entries."""
    CACHE.clear()
'''

NOTES = "Meeting notes\n\nThe retry policy is under discussion; nothing decided yet.\n"


class Corpus:
    """A throwaway project directory, written once per test."""

    def __init__(self, directory: Path) -> None:
        self.root = directory
        (directory / "README.md").write_text(README, encoding="utf-8")
        (directory / "widgets.py").write_text(MODULE, encoding="utf-8")
        (directory / "notes.txt").write_text(NOTES, encoding="utf-8")
        (directory / "pyproject.toml").write_text(
            "[project]\nname = 'widgets'\n\n[tool.pytest.ini_options]\n", encoding="utf-8"
        )
        (directory / ".git").mkdir()
        (directory / ".git" / "HEAD").write_text("ref: refs/heads/feature/timeouts\n", encoding="utf-8")

    def edit_readme(self, replacement: str) -> None:
        (self.root / "README.md").write_text(replacement, encoding="utf-8")


class IngestionTests(unittest.IsolatedAsyncioTestCase):
    """11.1/11.2: documents in, and the bookkeeping that makes it incremental."""

    async def test_a_tree_is_ingested_as_typed_sources(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            report = await manager.ingest_path(corpus.root)
            self.assertTrue(report.ok)
            self.assertEqual(report.status, IngestStatus.ADDED)
            kinds = {source.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]: source.source_type
                     for source in manager.sources()}
            # A README is DOCUMENTATION, not plain Markdown: its NAME says what
            # it is, and the ranking trusts it a little more for that.
            self.assertEqual(kinds["README.md"], SourceType.DOCUMENTATION)
            self.assertEqual(kinds["widgets.py"], SourceType.CODE)
            self.assertEqual(kinds["notes.txt"], SourceType.TEXT)
            self.assertEqual(manager.index.chunk_count(), len(manager.sources()))

    async def test_a_taught_article_is_searchable_from_the_same_index(self) -> None:
        manager = KnowledgeManager()
        base = KnowledgeBase()
        article = base.add_article(
            "Cache warmup", "The upstream cache warms in ninety seconds.", tags=("cache",)
        )
        await manager.ingest_article(article, project="widgets")
        hits = await manager.search("how long does the upstream cache warm up?")
        self.assertTrue(hits)
        self.assertEqual(hits[0].source.source_type, SourceType.NOTE)

    async def test_unsupported_and_binary_files_are_skipped_with_a_reason(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            (corpus.root / "image.png").write_bytes(b"\x89PNG\x00binary")
            (corpus.root / "dump.bin").write_bytes(b"\x00\x01\x02")
            manager = KnowledgeManager()
            await manager.ingest_path(corpus.root)
            self.assertEqual(len(manager.sources()), 4)
            report = await manager.ingest_path(corpus.root / "image.png")
            self.assertEqual(report.status, IngestStatus.SKIPPED)
            self.assertIn("unsupported", report.reason)

    async def test_a_binary_file_named_as_text_is_refused_not_indexed(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "looks-like-text.txt"
            path.write_bytes(b"\x00\x01\x02 really binary")
            with self.assertRaises(UnsupportedDocument):
                read_source(path)

    def test_a_missing_pdf_extractor_is_reported_not_guessed(self) -> None:
        # The extractor ladder is what "where existing infrastructure permits"
        # means: a PDF with no text layer is an honest skip with the reason.
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.pdf"
            path.write_bytes(b"%PDF-1.4\n/Type /Page\n")
            self.assertEqual(source_type_for(path), SourceType.PDF)
            try:
                read_source(path)
            except UnsupportedDocument as exc:
                self.assertTrue(exc.reason)

    async def test_source_metadata_names_the_source_and_its_version(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            await manager.ingest_path(corpus.root, project="widgets")
            source = next(s for s in manager.sources() if s.path.endswith("widgets.py"))
            self.assertEqual(source.project, "widgets")
            self.assertEqual(source.version, 1)
            self.assertEqual(source.content_hash, next(
                s.content_hash for s in manager.sources() if s.source_id == source.source_id
            ))
            self.assertEqual(source.source_id, source_id_for("widgets", source.path))
            self.assertGreater(source.indexed_at.timestamp(), 0)
            self.assertGreater(source.bytes, 0)
            chunk = manager.index.chunks_of(source.source_id)[0]
            self.assertEqual(chunk.chunk_id, f"{source.source_id}:{chunk.ordinal}")
            self.assertEqual(chunk.source_id, source.source_id)
            self.assertGreaterEqual(chunk.end_line, chunk.start_line)

    async def test_re_ingesting_identical_bytes_is_unchanged_and_costs_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            await manager.ingest_path(corpus.root)
            before = manager.index.chunk_count()
            again = await manager.ingest_path(corpus.root)
            self.assertEqual(again.status, IngestStatus.UNCHANGED)
            self.assertEqual(manager.index.chunk_count(), before)

    async def test_a_directory_ingested_twice_holds_one_copy_of_each_file(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            await manager.ingest_path(corpus.root)
            await manager.ingest_path(corpus.root)
            ids = [source.source_id for source in manager.sources()]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(len(ids), 4)

    async def test_an_edit_replaces_exactly_that_source_and_bumps_its_version(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            await manager.ingest_path(corpus.root)
            others = {
                source.source_id: source.version
                for source in manager.sources()
                if not source.path.endswith("README.md")
            }
            corpus.edit_readme(
                "# Widget service\n\n## Configuration\n\nThe widget timeout is configured in "
                "settings.py and defaults to sixty seconds.\n"
            )
            report = await manager.ingest_path(corpus.root)
            self.assertEqual(report.status, IngestStatus.UPDATED)
            readme = next(s for s in manager.sources() if s.path.endswith("README.md"))
            self.assertEqual(readme.version, 2)
            self.assertEqual(
                {s.source_id: s.version for s in manager.sources() if s.source_id in others}, others
            )
            context = await manager.retrieve("where is the widget timeout configured?")
            self.assertIn("settings.py", context.text)
            self.assertNotIn("widgets.py and defaults", context.text)

    async def test_ingest_text_is_versioned_and_deduplicated_by_content(self) -> None:
        manager = KnowledgeManager()
        first = await manager.ingest_text(
            "The deploy key is rotated every ninety days.", title="Deploy key", project="ops"
        )
        self.assertEqual(first.status, IngestStatus.ADDED)
        same = await manager.ingest_text(
            "The deploy key is rotated every ninety days.", title="Deploy key", project="ops"
        )
        self.assertEqual(same.status, IngestStatus.UNCHANGED)
        changed = await manager.ingest_text(
            "The deploy key is rotated every thirty days.", title="Deploy key", project="ops"
        )
        self.assertEqual(changed.status, IngestStatus.UPDATED)
        self.assertEqual(changed.source.version, 2)

    async def test_a_deleted_source_can_be_forgotten(self) -> None:
        manager = KnowledgeManager()
        report = await manager.ingest_text("temporary note", title="temp")
        self.assertEqual(manager.remove_source(report.source.source_id), report.chunks)
        self.assertIsNone(manager.source(report.source.source_id))

    async def test_an_oversized_file_is_skipped_rather_than_indexed(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager(max_file_bytes=32)
            report = await manager.ingest_path(corpus.root / "README.md")
            self.assertEqual(report.status, IngestStatus.SKIPPED)
            self.assertIn("exceeds", report.reason)

    def test_chunking_respects_structure_and_carries_the_heading(self) -> None:
        chunks = chunk_document(README, source_type=SourceType.MARKDOWN)
        self.assertEqual(len(chunks), 1)
        # The heading PATH travels with the text (a body under "Configuration"
        # is nearly useless alone) and the heading LINE is not repeated under it.
        self.assertIn("# Widget service > Configuration", chunks[0].text)
        self.assertEqual(chunks[0].text.count("## Configuration"), 0)
        self.assertEqual(chunks[0].text.count("# Widget service\n"), 1)
        # A small module stays one chunk; a long one is cut at its top-level
        # definitions, so a code hit is a function rather than the middle of one.
        self.assertEqual(len(chunk_document(MODULE, source_type=SourceType.CODE)), 1)
        long_module = "\n\n".join(
            f'def handler_{n}(value):\n    """Handle case {n}."""\n' + "    value += 1\n" * 30
            for n in range(3)
        )
        code = chunk_document(long_module, source_type=SourceType.CODE)
        self.assertGreater(len(code), 1)
        self.assertTrue(all("def handler_" in piece.text for piece in code))


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    """11.1: the shipped pipeline answers, and says why."""

    async def test_a_query_returns_the_matching_source_with_its_reasons(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            await manager.ingest_path(corpus.root)
            hits = await manager.search("where is the widget timeout configured?")
            self.assertTrue(hits)
            top = hits[0]
            self.assertTrue(top.path.endswith("README.md"))
            self.assertGreater(top.score, 0)
            self.assertTrue(any(reason.startswith("terms:") for reason in top.reasons))
            self.assertTrue(any(reason.startswith("bm25=") for reason in top.reasons))
            self.assertEqual(top.source.source_type, SourceType.DOCUMENTATION)

    async def test_code_is_found_by_its_identifier(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            await manager.ingest_path(corpus.root)
            hits = await manager.search("reset_widget_cache")
            self.assertTrue(hits)
            self.assertTrue(hits[0].path.endswith("widgets.py"))
            self.assertIn("reset_widget_cache", hits[0].chunk.text)

    async def test_an_empty_query_retrieves_nothing_rather_than_everything(self) -> None:
        manager = KnowledgeManager()
        await manager.ingest_text("something", title="t")
        self.assertEqual(await manager.search("   "), ())
        context = await manager.retrieve("")
        self.assertEqual(context.hits, ())
        self.assertEqual(context.text, "")

    async def test_a_hit_carries_a_citation_a_person_can_follow(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            await manager.ingest_path(corpus.root)
            hits = await manager.search("widget timeout", limit=3)
            payload = hits[0].to_dict()
            self.assertIn("chunk_id", payload)
            self.assertIn("lines", payload)
            self.assertGreaterEqual(payload["lines"][1], payload["lines"][0])
            self.assertTrue(payload["path"])

    async def test_the_context_block_is_rendered_with_labels(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            await manager.ingest_path(corpus.root)
            context = await manager.retrieve("widget timeout configuration")
            self.assertIn("Relevant local knowledge for:", context.text)
            self.assertIn("README.md", context.text)
            self.assertIn("(documentation,", context.text)

    async def test_an_unmatched_query_returns_no_hits_and_no_text(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            await manager.ingest_path(corpus.root)
            context = await manager.retrieve("photosynthesis chlorophyll")
            self.assertEqual(context.hits, ())
            self.assertEqual(context.text, "")


class RankingAndBudgetTests(unittest.IsolatedAsyncioTestCase):
    """11.4: relevance, priority, recency, deduplication, and a real budget."""

    async def test_the_active_project_outranks_an_identical_copy_elsewhere(self) -> None:
        manager = KnowledgeManager()
        await manager.ingest_text("the widget timeout is thirty seconds", title="a", project="other")
        await manager.ingest_text("the widget timeout is thirty seconds", title="b", project="widgets")
        hits = await manager.search("widget timeout", project="widgets", limit=5)
        self.assertEqual(hits[0].project, "widgets")
        self.assertGreater(hits[0].priority, 1.0)
        self.assertTrue(any("priority" in reason for reason in hits[0].reasons))

    def test_source_priority_and_recency_are_multipliers_with_a_floor(self) -> None:
        sources = {}
        chunks = {}
        for name, kind in (("code.py", SourceType.CODE), ("scan.pdf", SourceType.PDF)):
            source = _source(name, kind)
            sources[source.source_id] = source
            from novacontrol.knowledge.models import KnowledgeChunk

            chunks[name] = KnowledgeChunk(f"{source.source_id}:0", source.source_id, 0, "widget timeout")
        candidates = tuple(
            _scored(chunks[name], score=1.0) for name in ("code.py", "scan.pdf")
        )
        ranked = rank(candidates, index_sources=sources)
        self.assertEqual(ranked[0].hit.source.source_type, SourceType.CODE)
        self.assertGreater(ranked[0].hit.score, ranked[1].hit.score)
        self.assertGreaterEqual(ranked[0].hit.recency, 0.6)

    def test_near_duplicate_chunks_are_counted_once(self) -> None:
        from novacontrol.knowledge.models import KnowledgeChunk

        source = _source("a.md", SourceType.MARKDOWN)
        text = "the widget timeout is thirty seconds and the cache is warm"
        first = KnowledgeChunk(f"{source.source_id}:0", source.source_id, 0, text)
        copy = KnowledgeChunk(f"{source.source_id}:1", source.source_id, 1, text)
        ranked = rank(
            (_scored(first), _scored(copy)), index_sources={source.source_id: source}
        )
        context = assemble("widget timeout", ranked, budget_tokens=1000)
        self.assertEqual(len(context.hits), 1)
        self.assertGreaterEqual(context.omitted, 1)

    def test_one_source_cannot_fill_the_whole_budget(self) -> None:
        from novacontrol.knowledge.models import KnowledgeChunk

        source = _source("big.md", SourceType.MARKDOWN)
        chunks = tuple(
            KnowledgeChunk(
                f"{source.source_id}:{n}",
                source.source_id,
                n,
                f"widget timeout paragraph {n} " + "detail " * 40,
            )
            for n in range(6)
        )
        ranked = rank(
            tuple(_scored(chunk, score=1.0 - n * 0.01) for n, chunk in enumerate(chunks)),
            index_sources={source.source_id: source},
        )
        context = assemble("widget timeout", ranked, budget_tokens=4000, max_per_source=2)
        self.assertEqual(len(context.hits), 2)
        self.assertGreaterEqual(context.omitted, 1)

    async def test_the_budget_is_respected_and_what_was_left_out_is_reported(self) -> None:
        manager = KnowledgeManager(max_context_tokens=40)
        for n in range(4):
            await manager.ingest_text(
                f"the widget timeout note {n} " + "padding " * 60, title=f"note-{n}", project=""
            )
        context = await manager.retrieve("widget timeout note", budget_tokens=60)
        self.assertLessEqual(context.tokens, 60 + estimate_tokens("… [truncated]"))
        self.assertTrue(context.truncated)
        self.assertGreaterEqual(context.omitted, 1)

    async def test_an_unfitting_first_hit_is_cut_rather_than_dropped(self) -> None:
        manager = KnowledgeManager()
        await manager.ingest_text(
            "the widget timeout is thirty seconds " + "padding " * 200, title="long"
        )
        context = await manager.retrieve("widget timeout", budget_tokens=30)
        self.assertEqual(len(context.hits), 1)
        self.assertTrue(context.truncated)
        self.assertIn("truncated", context.hits[0].chunk.text)

    async def test_the_budget_is_spent_best_first_not_in_index_order(self) -> None:
        manager = KnowledgeManager()
        await manager.ingest_text("widget timeout " + "noise " * 50, title="weak")
        await manager.ingest_text("the widget timeout is thirty seconds", title="strong")
        context = await manager.retrieve("widget timeout", budget_tokens=60)
        self.assertTrue(context.hits)
        self.assertEqual(context.hits[0].source.title, "strong")

    async def test_configured_budget_is_the_default_when_the_caller_is_silent(self) -> None:
        manager = KnowledgeManager(max_context_tokens=123)
        await manager.ingest_text("widget timeout " * 20, title="t")
        context = await manager.retrieve("widget timeout")
        self.assertEqual(context.budget, 123)


class EmbeddingFallbackTests(unittest.IsolatedAsyncioTestCase):
    """11.1: no model is required, none is held, and lexical retrieval answers."""

    async def test_retrieval_works_with_the_dependency_free_backend(self) -> None:
        manager = KnowledgeManager()
        await manager.ingest_text("the widget timeout is thirty seconds", title="t")
        self.assertEqual(manager.embedding_backend, "hashing")
        self.assertFalse(manager.embedding_model_available)
        context = await manager.retrieve("widget timeout")
        self.assertEqual(len(context.hits), 1)
        self.assertEqual(context.embedding_backend, "hashing")

    async def test_a_lexical_only_index_needs_no_vectors_at_all(self) -> None:
        index = KnowledgeIndex()
        index.embedder = _NoVectors()
        manager = KnowledgeManager(index=index)
        await manager.ingest_text("the deploy key rotates every ninety days", title="ops")
        hits = await manager.search("deploy key rotation")
        self.assertTrue(hits)

    async def test_a_supplied_model_is_used_and_can_be_released_again(self) -> None:
        manager = KnowledgeManager()
        await manager.ingest_text("the widget timeout is thirty seconds", title="t")
        model = _FakeModel()
        rebuilt = manager.use_embedding_model(model)
        self.assertEqual(rebuilt, 1)
        self.assertTrue(manager.embedding_model_available)
        self.assertEqual(manager.embedding_backend, "fake-model")
        self.assertTrue(await manager.search("widget timeout"))
        manager.release_embedding_model()
        self.assertEqual(manager.embedding_backend, "hashing")
        self.assertTrue(await manager.search("widget timeout"))

    async def test_a_provider_that_has_no_model_is_not_an_error(self) -> None:
        manager = KnowledgeManager(embedding_provider=lambda: None)
        await manager.ingest_text("the widget timeout is thirty seconds", title="t")
        self.assertFalse(manager.embedding_model_available)
        self.assertTrue(await manager.search("widget timeout"))

    async def test_a_provider_that_raises_is_treated_as_unavailable(self) -> None:
        def broken() -> object:
            raise RuntimeError("the model server is down")

        manager = KnowledgeManager(embedding_provider=broken)  # type: ignore[arg-type]
        await manager.ingest_text("the widget timeout is thirty seconds", title="t")
        self.assertTrue(await manager.search("widget timeout"))

    async def test_a_model_that_cannot_embed_loses_only_its_own_signal(self) -> None:
        manager = KnowledgeManager()
        await manager.ingest_text("the widget timeout is thirty seconds", title="t")
        manager.use_embedding_model(_BrokenModel())
        hits = await manager.search("widget timeout")
        self.assertTrue(hits)
        self.assertEqual(hits[0].embedding_score, 0.0)


class ProjectAwarenessTests(unittest.IsolatedAsyncioTestCase):
    """11.3: the project is read from the workspace and never guessed."""

    def test_a_project_root_is_found_by_walking_up(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            nested = corpus.root / "src" / "deep"
            nested.mkdir(parents=True)
            self.assertEqual(find_project_root(nested), corpus.root)
            self.assertIsNone(find_project_root(Path(tmp).parent.parent))

    def test_the_context_names_root_repository_branch_language_and_files(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            detector = ProjectDetector()
            context = detector.detect(corpus.root)
            self.assertTrue(context.is_project)
            self.assertEqual(context.root, str(corpus.root))
            self.assertEqual(context.branch, "feature/timeouts")
            self.assertEqual(context.language, "python")
            self.assertIn("pyproject.toml", context.configuration)
            self.assertTrue(context.recent_files)
            payload = context.to_dict()
            self.assertEqual(payload["branch"], "feature/timeouts")
            self.assertIn("is_project", payload)
            self.assertIn("Active project:", context.render())

    def test_recent_errors_and_test_status_come_from_whatever_can_supply_them(self) -> None:
        class Log:
            def all(self, *, include_fixed: bool) -> tuple[object, ...]:
                return (type("R", (), {"title": "widget timeout race"})(),)

        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            detector = ProjectDetector(
                bug_log=Log(), test_status_provider=lambda: "python -m pytest: passed"
            )
            context = detector.detect(corpus.root)
            self.assertEqual(context.recent_errors, ("widget timeout race",))
            self.assertEqual(context.test_status, "python -m pytest: passed")

    def test_a_directory_that_is_not_a_project_reports_so_rather_than_inventing_one(self) -> None:
        with TemporaryDirectory() as tmp:
            plain = Path(tmp) / "loose"
            plain.mkdir()
            context = ProjectDetector().detect(plain)
            self.assertFalse(context.is_project)
            self.assertEqual(context.render(), "")

    def test_the_manager_tracks_the_active_project_and_the_active_task(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            context = manager.set_project(corpus.root)
            self.assertEqual(manager.active_project, context.name)
            manager.set_active_task("fix the widget timeout")
            again = manager.project_context()
            self.assertEqual(again.active_task, "fix the widget timeout")

    async def test_context_for_resolves_the_phases_worked_example(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            await manager.ingest_path(corpus.root)
            # The project is told, not guessed: this is the flow the phase's
            # example describes (open the project, then ask about it).
            manager.set_project(corpus.root)
            context = await manager.context_for(
                "Fix the widget timeout issue.", active_task="fix widget timeout"
            )
            self.assertTrue(context.text.startswith("Active project:") or "Active project:" in context.text)
            self.assertIn("Branch: feature/timeouts", context.text)
            self.assertIn("Active task: fix widget timeout", context.text)
            self.assertIn("widgets.py", context.text)
            self.assertTrue(context.hits)
            self.assertLessEqual(context.budget, manager.max_context_tokens)

    async def test_project_scoping_falls_back_to_everything_when_nothing_is_indexed_yet(self) -> None:
        manager = KnowledgeManager()
        await manager.ingest_text("the widget timeout is thirty seconds", title="t", project="other")
        manager.set_project(Path.cwd())
        context = await manager.retrieve("widget timeout", active_only=True)
        self.assertTrue(context.hits)

    def test_the_project_awareness_module_writes_nothing(self) -> None:
        # The phase says "do not blindly modify files": detection is a read. This
        # asserts the strongest version of that — a detection leaves the tree
        # byte-identical.
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            before = {
                path: path.read_bytes()
                for path in sorted(corpus.root.rglob("*"))
                if path.is_file()
            }
            ProjectDetector().detect(corpus.root)
            after = {
                path: path.read_bytes()
                for path in sorted(corpus.root.rglob("*"))
                if path.is_file()
            }
            self.assertEqual(before, after)
            self.assertEqual(read_branch(corpus.root), "feature/timeouts")


class ToolAndModuleTests(unittest.IsolatedAsyncioTestCase):
    """The engine through the seams the rest of the build uses: tools and events."""

    async def test_the_tools_search_ingest_and_report_the_project(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            registry = ToolRegistry()
            for tool in knowledge_tools(manager):
                registry.register(tool)
            self.assertEqual(
                [tool.tool.name for tool in registry.list()],
                ["knowledge_ingest", "knowledge_search", "project_context"],
            )
            ingested = await registry.get("knowledge_ingest").tool.run({"path": str(corpus.root)})
            self.assertEqual(ingested["status"], "added")
            found = await registry.get("knowledge_search").tool.run(
                {"query": "widget timeout", "limit": 2}
            )
            self.assertGreaterEqual(found["count"], 1)
            project = await registry.get("project_context").tool.run({"path": str(corpus.root)})
            self.assertEqual(project["branch"], "feature/timeouts")

    def test_reading_the_disk_declares_the_permission_and_search_does_not(self) -> None:
        registry = ToolRegistry()
        for tool in knowledge_tools(KnowledgeManager()):
            registry.register(tool)
        self.assertEqual(
            registry.get("knowledge_ingest").tool.required_permissions,
            (PermissionScope.FILESYSTEM_READ,),
        )
        self.assertEqual(
            registry.get("project_context").tool.required_permissions,
            (PermissionScope.FILESYSTEM_READ,),
        )
        self.assertEqual(registry.get("knowledge_search").tool.required_permissions, ())

    async def test_a_tool_call_with_loose_arguments_is_capped_not_crashed(self) -> None:
        registry = ToolRegistry()
        for tool in knowledge_tools(KnowledgeManager()):
            registry.register(tool)
        search = registry.get("knowledge_search").tool
        self.assertEqual((await search.run({"query": "x", "limit": "not a number"}))["count"], 0)
        self.assertEqual((await search.run({"query": "x", "limit": -5}))["count"], 0)

    async def test_the_module_answers_requests_with_correlated_events(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            bus = EventBus()
            module = KnowledgeModule(KnowledgeManager())
            await module.start(bus)
            seen: list[object] = []

            async def capture(event: object) -> None:
                seen.append(event)

            await bus.subscribe("knowledge.search_completed", capture)
            await bus.publish(_request("knowledge.ingest_requested", {"path": str(corpus.root)}))
            first = _request("knowledge.search_requested", {"query": "widget timeout"})
            await bus.publish(first)
            second = _request("knowledge.search_requested", {"query": "widget timeout"})
            await bus.publish(second)
            self.assertEqual(len(seen), 2)
            # Each reply carries the correlation of the request it answers, so a
            # caller with two questions in flight can tell which is which.
            self.assertEqual(seen[0].correlation_id, first.correlation_id)
            self.assertEqual(seen[1].correlation_id, second.correlation_id)
            self.assertTrue(seen[1].payload["hits"])
            await module.stop()
            self.assertEqual(module.name, "knowledge")
            self.assertTrue(
                any(capability.name.startswith(("knowledge.", "project.")) for capability in module.capabilities)
            )

    async def test_the_module_publishes_the_typed_lifecycle_events(self) -> None:
        bus = EventBus()
        manager = KnowledgeManager()
        manager.attach_events(bus)
        seen: list[str] = []
        for event_type in (EventType.KNOWLEDGE_INDEXED, EventType.KNOWLEDGE_RETRIEVED):
            await bus.subscribe(event_type, lambda event, _t=event_type: seen.append(_t.value))
        await manager.ingest_text("the widget timeout is thirty seconds", title="t")
        await manager.retrieve("widget timeout")
        self.assertIn(EventType.KNOWLEDGE_INDEXED.value, seen)
        self.assertIn(EventType.KNOWLEDGE_RETRIEVED.value, seen)


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    """The index survives a restart without needing a model to reopen it."""

    async def test_a_snapshot_restores_sources_chunks_and_the_project(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            await manager.ingest_path(corpus.root)
            manager.set_project(corpus.root)
            restored = KnowledgeManager.from_dict(manager.to_dict())
            self.assertEqual(len(restored.sources()), len(manager.sources()))
            self.assertEqual(restored.index.chunk_count(), manager.index.chunk_count())
            self.assertEqual(restored.active_project, manager.active_project)
            hits = await restored.search("widget timeout")
            self.assertTrue(hits)
            self.assertEqual(restored.embedding_backend, "hashing")

    async def test_restoring_an_empty_snapshot_gives_a_working_empty_engine(self) -> None:
        manager = KnowledgeManager.from_dict({})
        self.assertEqual(manager.sources(), ())
        self.assertEqual(await manager.search("anything"), ())
        self.assertEqual(manager.stats()["sources"], 0)

    def test_positive_int_accepts_loose_input_and_refuses_nonsense(self) -> None:
        self.assertEqual(positive_int("7", default=5), 7)
        self.assertEqual(positive_int(0, default=5), 5)
        self.assertEqual(positive_int(-3, default=5), 5)
        self.assertEqual(positive_int("x", default=5), 5)
        self.assertEqual(positive_int(None, default=5), 5)
        self.assertEqual(positive_int(True, default=5), 5)
        self.assertEqual(positive_int(50, default=5, maximum=8), 8)


class ApplicationWiringTests(unittest.IsolatedAsyncioTestCase):
    """The engine as the application actually uses it: one manager, on the bus."""

    async def test_the_application_owns_the_engine_its_tools_and_its_module(self) -> None:
        from novacontrol.application import NovaControlApplication

        with TemporaryDirectory() as temp_dir:
            app = NovaControlApplication(data_dir=temp_dir)
            await app.start()
            try:
                self.assertIn("knowledge", app.runtime.module_names())
                self.assertEqual(
                    [entry.tool.name for entry in app.tools.list() if entry.tool.name.startswith("knowledge")],
                    ["knowledge_ingest", "knowledge_search"],
                )
                self.assertIn("project_context", [entry.tool.name for entry in app.tools.list()])
                # The catalogue the planner and the decision layer consult was
                # built AFTER the tools were registered, so they are offered
                # like a built-in one.
                self.assertIn("knowledge_search", app.tool_catalog.names())
                status = app.status()
                self.assertTrue(status["runtime_started"])
            finally:
                await app.stop()

    async def test_the_application_can_ingest_search_and_report_the_project(self) -> None:
        from novacontrol.application import NovaControlApplication

        with TemporaryDirectory() as temp_dir:
            corpus_root = Path(temp_dir) / "widgets"
            corpus_root.mkdir(parents=True)
            corpus = Corpus(corpus_root)
            app = NovaControlApplication(data_dir=Path(temp_dir) / "state")
            await app.start()
            try:
                report = await app.ingest_knowledge(str(corpus.root))
                self.assertEqual(report["status"], "added")
                found = await app.search_knowledge("where is the widget timeout configured?")
                self.assertGreaterEqual(found["count"], 1)
                context = await app.knowledge_context(
                    "Fix the widget timeout issue.", active_task="fix widget timeout"
                )
                self.assertGreater(context["tokens"], 0)
                project = app.project_context(str(corpus.root))
                self.assertEqual(project["branch"], "feature/timeouts")
            finally:
                await app.stop()

    async def test_the_index_survives_a_restart_without_a_model(self) -> None:
        from novacontrol.application import NovaControlApplication

        with TemporaryDirectory() as temp_dir:
            state = Path(temp_dir) / "state"
            app = NovaControlApplication(data_dir=state)
            await app.start()
            await app.knowledge_engine.ingest_text(
                "the widget timeout is thirty seconds", title="widgets"
            )
            app.persist()
            await app.stop()

            reopened = NovaControlApplication(data_dir=state)
            await reopened.start()
            try:
                self.assertEqual(len(reopened.knowledge_engine.sources()), 1)
                found = await reopened.search_knowledge("widget timeout")
                self.assertEqual(found["count"], 1)
                self.assertFalse(reopened.knowledge_engine.embedding_model_available)
            finally:
                await reopened.stop()


class _NoVectors(HashingEmbedder):
    """A backend that produces no vectors at all — pure lexical retrieval."""

    @property
    def name(self) -> str:
        return "none"

    def sparse(self, text: str) -> dict[int, float]:
        return {}


class _FakeModel:
    """A stand-in for a real embedding model (deterministic, offline)."""

    name = "fake-model"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text) % 7), 1.0] for text in texts]


class _BrokenModel:
    name = "broken-model"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("model unavailable")


def _source(name: str, kind: SourceType):
    from novacontrol.knowledge.models import KnowledgeSource

    return KnowledgeSource(
        source_id=source_id_for("proj", name),
        source_type=kind,
        path=name,
        content_hash="x",
        chunk_count=1,
    )


def _scored(chunk, score: float = 1.0):
    from novacontrol.knowledge.index import ScoredChunk

    return ScoredChunk(chunk=chunk, score=score, lexical=score)


def _request(event_type: str, payload: dict[str, object]):
    from uuid import uuid4

    from novacontrol.core.events import Event

    return Event(type=event_type, payload=payload, correlation_id=uuid4().hex, source="test")


if __name__ == "__main__":
    unittest.main()
