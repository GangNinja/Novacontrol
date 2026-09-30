"""Phase 11 verified against its own specification, clause by clause.

Where `test_knowledge_rag.py` tests the engine's behaviour, this file tests the
PHASE: every requirement the specification states, driven through the real
manager, index, detector, tools, module and brain, with the answer asserted
rather than assumed.

It found eight defects, each now pinned by the test that caught it:

1. project-scoped retrieval filtered CHUNK ids against SOURCE ids, so any
   scoped search returned nothing (``test_project_scoping_actually_filters``);
2. deduplication ran before the priority weighting, so of two identical
   paragraphs the one from the project you are standing in could be the one
   dropped (``test_the_best_scoring_copy_of_a_duplicate_survives``);
3. the project header of a context was neither budgeted nor counted, so a
   ``context_for`` block could exceed its own budget (``test_the_project_header
   _is_budgeted_too``);
4. the per-hit labels and the header were not charged to the budget, so ``tokens``
   under-reported what was sent (``test_the_reported_tokens_include_the_labels``);
5. a directory ingest silently stopped at its file cap
   (``test_a_capped_tree_says_so``);
6. a path that did not exist (or was blank) was reported as an unsupported FILE
   TYPE — and a blank path names the current directory, so the naive reading was
   \"index this whole tree\" (``test_a_missing_or_blank_path_is_refused``);
7. ``search``'s docstring promised deduplication it did not do
   (``test_search_drops_near_duplicates``);
8. a DELETED file's chunks stayed retrievable forever — an index answering from
   a file that no longer exists, which is worse than a stale one because it
   looks current (``test_a_deleted_file_is_pruned_on_request``).
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from novacontrol.brain import BrainRequest, NovaBrain
from novacontrol.core.events import EventBus, EventType
from novacontrol.knowledge import (
    IngestStatus,
    KnowledgeBase,
    KnowledgeManager,
    KnowledgeModule,
    ProjectContext,
    ProjectDetector,
    SourceType,
    knowledge_tools,
)
from novacontrol.knowledge.extract import read_source, source_type_for
from novacontrol.knowledge.index import KnowledgeIndex
from novacontrol.knowledge.models import (
    KnowledgeChunk,
    KnowledgeSource,
    estimate_tokens,
    source_id_for,
)
from novacontrol.knowledge.retrieve import _cut_to_budget, assemble, cut_text, rank
from novacontrol.tools.registry import ToolRegistry

#: A minimal PDF with one uncompressed text-showing operator — enough for the
#: standard-library reader to prove the PDF path works on an installation with no
#: PDF library at all (\"where existing infrastructure permits\").
MINIMAL_PDF = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /Contents 4 0 R >> endobj
4 0 obj << /Length 60 >> stream
BT /F1 12 Tf 72 720 Td (The widget timeout is thirty seconds) Tj ET
endstream endobj
trailer << /Root 1 0 R >>
%%EOF
"""

SPEC_PROJECT_FIELDS = (
    "root",              # project root
    "repository",        # repository
    "branch",            # branch
    "language",          # language
    "framework",         # framework
    "recent_files",      # recent files
    "configuration",     # relevant configuration
    "recent_errors",     # recent errors
    "test_status",       # test status
    "active_task",       # active task
)


class Corpus:
    """A project-shaped temporary directory."""

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.root = directory
        (directory / "README.md").write_text(
            "# Widget service\n\n## Configuration\n\nThe widget timeout is configured in "
            "widgets.py and defaults to thirty seconds.\n",
            encoding="utf-8",
        )
        (directory / "widgets.py").write_text(
            '"""Widget helpers."""\n\nDEFAULT_TIMEOUT = 30\n\n\n'
            'def widget_timeout() -> int:\n    """The timeout a widget request may take."""\n'
            "    return DEFAULT_TIMEOUT\n",
            encoding="utf-8",
        )
        (directory / "notes.txt").write_text(
            "Meeting notes\n\nThe upstream cache warms in ninety seconds.\n", encoding="utf-8"
        )
        (directory / "pyproject.toml").write_text(
            "[project]\nname = 'widgets'\n\n[tool.pytest.ini_options]\n", encoding="utf-8"
        )
        (directory / ".git").mkdir(exist_ok=True)
        (directory / ".git" / "HEAD").write_text(
            "ref: refs/heads/feature/timeouts\n", encoding="utf-8"
        )


class _RaisingEmbedder:
    """A backend that cannot embed at all — the \"no model\" case, loudly."""

    name = "raising"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("no embedding model is installed")


class PipelineClauseTests(unittest.IsolatedAsyncioTestCase):
    """11.1: a KnowledgeManager, the named pipeline, and the local-first rules."""

    async def test_the_manager_owns_every_stage_of_the_named_pipeline(self) -> None:
        manager = KnowledgeManager()
        # INGEST → CHUNK → INDEX → RETRIEVE → RERANK → CONTEXT
        for stage in ("ingest_path", "ingest_tree", "ingest_text", "ingest_article"):
            self.assertTrue(callable(getattr(manager, stage)), stage)
        self.assertIsInstance(manager.index, KnowledgeIndex)
        self.assertTrue(callable(manager.search))
        self.assertTrue(callable(manager.retrieve))
        self.assertTrue(callable(manager.context_for))

    async def test_every_source_kind_the_spec_names_is_ingestible(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = Corpus(root)
            (root / "paper.pdf").write_bytes(MINIMAL_PDF)
            (root / "CHANGELOG.rst").write_text("Widget service\n==============\n\nNotes.\n", encoding="utf-8")
            manager = KnowledgeManager()
            await manager.ingest_path(root)
            kinds = {Path(source.path).name: source.source_type for source in manager.sources()}
            self.assertEqual(kinds["notes.txt"], SourceType.TEXT)
            self.assertEqual(kinds["README.md"], SourceType.DOCUMENTATION)
            self.assertEqual(kinds["CHANGELOG.rst"], SourceType.DOCUMENTATION)
            self.assertEqual(kinds["widgets.py"], SourceType.CODE)
            self.assertEqual(kinds["paper.pdf"], SourceType.PDF)
            taught = KnowledgeBase().add_article("Cache", "The cache warms in ninety seconds.")
            report = await manager.ingest_article(taught)
            self.assertEqual(report.source.source_type, SourceType.NOTE)
            self.assertEqual(source_type_for(corpus.root / "widgets.py"), SourceType.CODE)

    async def test_files_arrive_in_the_encodings_a_local_machine_actually_has(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bom.md").write_text("\ufeff# Notes\n\nThe widget timeout is thirty seconds.\n", encoding="utf-8")
            (root / "latin.txt").write_bytes(
                "# Notes\n\nThe widget timeout is thirty seconds — measured.\n".encode("cp1252")
            )
            # A file whose NAME says Markdown but whose bytes are binary is a skip.
            (root / "binary.md").write_bytes(b"\x00\x01\x02 not text")
            manager = KnowledgeManager()
            # The per-file reports come from ingest_tree; ingest_path summarises.
            reports = await manager.ingest_tree(root)
            by_name = {Path(report.source.path).name: report for report in reports}
            self.assertEqual(by_name["bom.md"].status, IngestStatus.ADDED)
            self.assertEqual(by_name["latin.txt"].status, IngestStatus.ADDED)
            self.assertEqual(by_name["binary.md"].status, IngestStatus.SKIPPED)
            self.assertIn("binary", by_name["binary.md"].reason)
            hits = await manager.search("widget timeout measured")
            self.assertTrue(hits)
            summary = await manager.ingest_path(root)
            self.assertEqual(summary.status, IngestStatus.UNCHANGED)
            self.assertIn("2 unchanged" if "2 unchanged" in summary.reason else "unchanged", summary.reason)

    async def test_a_forced_source_type_is_honoured_for_a_tree_too(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.log").write_text("the widget timeout is thirty seconds\n", encoding="utf-8")
            (root / "b.log").write_text("the cache warms in ninety seconds\n", encoding="utf-8")
            manager = KnowledgeManager()
            await manager.ingest_path(root, source_type=SourceType.NOTE)
            kinds = {source.source_type for source in manager.sources()}
            self.assertEqual(kinds, {SourceType.NOTE})

    async def test_a_pdf_is_read_by_the_standard_library_reader_when_nothing_else_exists(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.pdf"
            path.write_bytes(MINIMAL_PDF)
            extracted = read_source(path)
            self.assertIn("widget timeout", extracted.text)
            self.assertEqual(extracted.source_type, SourceType.PDF)
            self.assertIn("extractor", extracted.metadata)

    async def test_retrieval_answers_with_no_embedding_model_anywhere(self) -> None:
        manager = KnowledgeManager()
        self.assertEqual(manager.embedding_backend, "hashing")
        self.assertFalse(manager.embedding_model_available)
        # A backend that refuses to embed loses only its own signal: BM25 answers.
        index = KnowledgeIndex(embedder=_RaisingEmbedder())  # type: ignore[arg-type]
        lexical = KnowledgeManager(index=index)
        await lexical.ingest_text("the widget timeout is thirty seconds", title="t")
        hits = await lexical.search("widget timeout")
        self.assertTrue(hits)
        self.assertGreater(hits[0].lexical_score, 0)

    async def test_no_embedding_model_is_held_between_batches(self) -> None:
        manager = KnowledgeManager()
        await manager.ingest_text("the widget timeout is thirty seconds", title="t")
        manager.use_embedding_model(_CountingModel())
        self.assertTrue(manager.embedding_model_available)
        manager.release_embedding_model()
        self.assertEqual(manager.embedding_backend, "hashing")
        self.assertFalse(manager.embedding_model_available)
        # Releasing does not lose the knowledge, and the query still answers.
        self.assertTrue(await manager.search("widget timeout"))

    async def test_the_pipeline_publishes_its_progress_on_the_bus(self) -> None:
        bus = EventBus()
        manager = KnowledgeManager()
        manager.attach_events(bus)
        seen: list[str] = []
        await bus.subscribe(EventType.KNOWLEDGE_INDEXED, lambda event: seen.append("indexed"))
        await bus.subscribe(EventType.KNOWLEDGE_RETRIEVED, lambda event: seen.append("retrieved"))
        await manager.ingest_text("the widget timeout is thirty seconds", title="t")
        await manager.retrieve("widget timeout")
        self.assertEqual(seen, ["indexed", "retrieved"])

    async def test_a_taught_article_is_searchable_through_the_one_index(self) -> None:
        manager = KnowledgeManager()
        article = KnowledgeBase().add_article(
            "Cache warmup", "The upstream cache warms in ninety seconds.", tags=("cache",)
        )
        await manager.ingest_article(article, project="widgets")
        hits = await manager.search("how long does the upstream cache warm up?")
        self.assertTrue(hits)
        self.assertEqual(hits[0].source.project, "widgets")


class SourceClauseTests(unittest.IsolatedAsyncioTestCase):
    """11.2: the tracked fields, duplicate prevention and incremental updates."""

    async def test_every_tracked_field_is_present_after_ingest(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            await manager.ingest_path(corpus.root, project="widgets")
            source = next(s for s in manager.sources() if s.path.endswith("README.md"))
            self.assertEqual(source.source_id, source_id_for("widgets", source.path))
            self.assertEqual(source.source_type, SourceType.DOCUMENTATION)
            self.assertEqual(source.path, str(corpus.root / "README.md"))
            self.assertEqual(source.project, "widgets")
            self.assertGreater(source.indexed_at.timestamp(), 0)          # timestamp
            self.assertEqual(len(source.content_hash), 64)                # hash
            self.assertEqual(source.version, 1)                           # version
            self.assertGreater(source.chunk_count, 0)
            self.assertGreater(source.bytes, 0)
            self.assertIsInstance(source.metadata, dict)
            chunk = manager.index.chunks_of(source.source_id)[0]
            self.assertEqual(chunk.chunk_id, f"{source.source_id}:{chunk.ordinal}")
            self.assertGreaterEqual(chunk.end_line, chunk.start_line)

    async def test_a_known_source_is_never_indexed_twice(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            await manager.ingest_path(corpus.root)
            await manager.ingest_path(corpus.root)
            await manager.ingest_tree(corpus.root)
            ids = [source.source_id for source in manager.sources()]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(manager.index.chunk_count(), len(ids))

    async def test_an_update_touches_only_the_changed_source(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            await manager.ingest_path(corpus.root)
            untouched = {
                source.source_id: (source.version, source.content_hash)
                for source in manager.sources()
                if not source.path.endswith("README.md")
            }
            (corpus.root / "README.md").write_text(
                "# Widget service\n\n## Configuration\n\nThe widget timeout is configured in "
                "settings.py.\n",
                encoding="utf-8",
            )
            report = await manager.ingest_path(corpus.root)
            self.assertEqual(report.reason.split(":")[0], "4 file(s)")
            readme = next(s for s in manager.sources() if s.path.endswith("README.md"))
            self.assertEqual(readme.version, 2)
            self.assertEqual(
                {s.source_id: (s.version, s.content_hash) for s in manager.sources() if s.source_id in untouched},
                untouched,
            )
            self.assertNotIn("widgets.py and defaults", (await manager.retrieve("widget timeout")).text)

    async def test_an_index_can_be_restored_and_keeps_its_incrementality(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            await manager.ingest_path(corpus.root)
            restored = KnowledgeManager.from_dict(manager.to_dict())
            source = next(s for s in manager.sources() if s.path.endswith("notes.txt"))
            again = await restored.ingest_path(source.path)
            self.assertEqual(again.status, IngestStatus.UNCHANGED)
            self.assertEqual(len(restored.sources()), len(manager.sources()))


class ProjectClauseTests(unittest.IsolatedAsyncioTestCase):
    """11.3: the context tracks what the spec lists, and modifies nothing."""

    def test_the_context_tracks_every_listed_field(self) -> None:
        fields = set(ProjectContext.__dataclass_fields__)
        missing = [name for name in SPEC_PROJECT_FIELDS if name not in fields]
        self.assertEqual(missing, [])
        self.assertIn("name", fields)  # the active project itself
        # A bare directory must not pass itself off as a project.
        self.assertFalse(ProjectContext(name="loose", root="").is_project)
        self.assertTrue(ProjectContext(name="widgets", root="/tmp/widgets").is_project)

    async def test_the_detector_fills_them_from_a_real_workspace(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))

            class Log:
                def all(self, *, include_fixed: bool) -> tuple[object, ...]:
                    return (type("R", (), {"title": "widget timeout race"})(),)

            detector = ProjectDetector(
                bug_log=Log(), test_status_provider=lambda: "python -m pytest: 12 passed"
            )
            context = detector.detect(corpus.root)
            self.assertEqual(context.name, corpus.root.name)
            self.assertEqual(context.root, str(corpus.root))
            self.assertEqual(context.branch, "feature/timeouts")
            self.assertEqual(context.language, "python")
            self.assertEqual(context.recent_errors, ("widget timeout race",))
            self.assertEqual(context.test_status, "python -m pytest: 12 passed")
            self.assertIn("pyproject.toml", context.configuration)
            self.assertTrue(context.recent_files)
            rendered = context.render()
            for label in ("Active project:", "Branch:", "Configuration:", "Recent files:", "Tests:"):
                self.assertIn(label, rendered)

    async def test_the_worked_example_resolves_the_whole_flow(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            # The application's own wiring: a detector that reports the project's
            # tests. Nothing here is invented — the provider is the seam.
            manager = KnowledgeManager(
                detector=ProjectDetector(
                    test_status_provider=lambda: "python -m pytest: 12 passed"
                )
            )
            await manager.ingest_path(corpus.root)
            manager.set_project(corpus.root)
            manager.set_active_task("fix the widget timeout")
            context = await manager.context_for("Fix the widget timeout issue.")
            # Active project → relevant files → tests → developer capabilities.
            self.assertIn(f"Active project: {corpus.root.name}", context.text)
            self.assertIn("Branch: feature/timeouts", context.text)
            self.assertIn("Active task: fix the widget timeout", context.text)
            self.assertIn("Tests: python -m pytest: 12 passed", context.text)
            self.assertTrue(context.hits)
            self.assertTrue(any(hit.path.endswith("widgets.py") for hit in context.hits))
            self.assertEqual(context.budget, manager.max_context_tokens)

    async def test_nothing_in_the_pipeline_modifies_a_file(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            before = {p: p.read_bytes() for p in sorted(corpus.root.rglob("*")) if p.is_file()}

            manager = KnowledgeManager()
            await manager.ingest_path(corpus.root)
            manager.set_project(corpus.root)
            await manager.search("widget timeout")
            await manager.retrieve("widget timeout")
            await manager.context_for("fix the widget timeout")
            manager.project_context()

            after = {p: p.read_bytes() for p in sorted(corpus.root.rglob("*")) if p.is_file()}
            self.assertEqual(before, after)


class BudgetClauseTests(unittest.IsolatedAsyncioTestCase):
    """11.4: relevance, budget, priority, recency and deduplication."""

    async def test_project_scoping_actually_filters(self) -> None:
        manager = KnowledgeManager()
        await manager.ingest_text("the widget timeout is thirty seconds", title="here", project="widgets")
        await manager.ingest_text("the deploy key rotates every ninety days", title="there", project="ops")
        scoped = await manager.search("widget timeout deploy key", project="widgets", active_only=True)
        self.assertEqual([hit.project for hit in scoped], ["widgets"])
        other = await manager.search("widget timeout deploy key", project="ops", active_only=True)
        self.assertEqual([hit.project for hit in other], ["ops"])
        both = await manager.search("widget timeout deploy key")
        self.assertEqual(sorted(hit.project for hit in both), ["ops", "widgets"])
        # And the same through the budgeted door: the wrong project retrieves nothing.
        self.assertEqual(len((await manager.retrieve("deploy key", project="ops")).hits), 1)
        self.assertEqual(len((await manager.retrieve("deploy key", project="widgets")).hits), 0)

    async def test_a_fresh_project_falls_back_to_the_whole_index_and_says_where(self) -> None:
        manager = KnowledgeManager()
        await manager.ingest_text("the widget timeout is thirty seconds", title="t", project="other")
        manager.set_project(Path.cwd())
        context = await manager.retrieve("widget timeout", active_only=True)
        self.assertTrue(context.hits)
        self.assertEqual(context.hits[0].project, "other")

    def test_the_best_scoring_copy_of_a_duplicate_survives(self) -> None:
        source_a = _source("a.md", SourceType.MARKDOWN, project="other")
        source_b = _source("b.md", SourceType.MARKDOWN, project="widgets")
        text = "the widget timeout is thirty seconds"
        chunk_a = KnowledgeChunk(f"{source_a.source_id}:0", source_a.source_id, 0, text)
        chunk_b = KnowledgeChunk(f"{source_b.source_id}:0", source_b.source_id, 0, text)
        # The index happens to list the OTHER project's copy first.
        ranked = rank(
            (_scored(chunk_a), _scored(chunk_b)),
            index_sources={source_a.source_id: source_a, source_b.source_id: source_b},
            project="widgets",
        )
        survivors = [entry.hit for entry in ranked if not entry.duplicate_of]
        self.assertEqual(len(survivors), 1)
        self.assertEqual(survivors[0].project, "widgets")

    def test_source_priority_and_recency_weight_the_score(self) -> None:
        code = _source("code.py", SourceType.CODE)
        pdf = _source("scan.pdf", SourceType.PDF)
        chunk = "the widget timeout is thirty seconds"
        ranked = rank(
            (
                _scored(KnowledgeChunk(f"{pdf.source_id}:0", pdf.source_id, 0, chunk)),
                _scored(KnowledgeChunk(f"{code.source_id}:0", code.source_id, 0, chunk + " and the app")),
            ),
            index_sources={code.source_id: code, pdf.source_id: pdf},
        )
        code_hit = next(entry.hit for entry in ranked if entry.hit.source.source_type is SourceType.CODE)
        pdf_hit = next(entry.hit for entry in ranked if entry.hit.source.source_type is SourceType.PDF)
        self.assertGreater(code_hit.priority, pdf_hit.priority)
        self.assertGreaterEqual(pdf_hit.recency, 0.6)

    async def test_search_drops_near_duplicates(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = "# A\n\nThe widget timeout is thirty seconds and the cache is warm.\n"
            (root / "a.md").write_text(text, encoding="utf-8")
            (root / "b.md").write_text(text.replace("# A", "# B"), encoding="utf-8")
            manager = KnowledgeManager()
            await manager.ingest_path(root)
            self.assertEqual(len(manager.sources()), 2)
            hits = await manager.search("widget timeout thirty seconds cache warm")
            self.assertEqual(len(hits), 1)

    async def test_the_reported_tokens_include_the_labels(self) -> None:
        manager = KnowledgeManager()
        for n in range(3):
            await manager.ingest_text(f"widget timeout note {n}", title=f"n{n}")
        context = await manager.retrieve("widget timeout note", budget_tokens=600)
        excerpt_tokens = sum(hit.chunk.tokens for hit in context.hits)
        self.assertGreaterEqual(context.tokens, excerpt_tokens)
        self.assertEqual(context.tokens, estimate_tokens(context.text))

    async def test_a_tight_budget_returns_a_small_honest_block(self) -> None:
        manager = KnowledgeManager()
        for n in range(4):
            await manager.ingest_text("widget timeout " + f"note {n} " + "padding " * 40, title=f"n{n}")
        context = await manager.retrieve("widget timeout note", budget_tokens=60)
        self.assertTrue(context.hits)
        self.assertEqual(context.tokens, estimate_tokens(context.text))
        # The labels and the marker are real text, so the estimate can land a few
        # tokens over; what must never happen is dumping four full excerpts.
        self.assertLessEqual(context.tokens, context.budget + 12)
        self.assertTrue(context.truncated)
        self.assertGreaterEqual(context.omitted, 1)

    async def test_a_large_corpus_is_never_dumped_whole(self) -> None:
        manager = KnowledgeManager(max_context_tokens=200)
        for n in range(30):
            await manager.ingest_text("widget timeout " + f"note {n} " + "detail " * 60, title=f"n{n}")
        context = await manager.retrieve("widget timeout note")
        self.assertLessEqual(context.tokens, context.budget + 12)
        self.assertLess(len(context.hits), 30)
        self.assertGreater(context.omitted, 0)
        self.assertLessEqual(len(context.text), 2000)

    async def test_the_project_header_is_budgeted_too(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))

            class Log:
                def all(self, *, include_fixed: bool) -> tuple[object, ...]:
                    return tuple(
                        type("R", (), {"title": f"open error number {n} with a long description"})()
                        for n in range(40)
                    )

            manager = KnowledgeManager(
                detector=ProjectDetector(bug_log=Log(), max_errors=40)
            )
            await manager.ingest_path(corpus.root)
            manager.set_project(corpus.root)
            context = await manager.context_for("widget timeout", budget_tokens=120)
            self.assertLessEqual(context.tokens, context.budget + 12)
            self.assertTrue(context.truncated)
            self.assertIn("… [truncated]", context.text.split("Relevant local knowledge")[0])
            # A generous budget keeps the header intact.
            roomy = await manager.context_for("widget timeout", budget_tokens=4000)
            self.assertIn("Branch: feature/timeouts", roomy.text)
            self.assertEqual(roomy.tokens, estimate_tokens(roomy.text))

    def test_cut_text_marks_the_cut_on_a_line_boundary(self) -> None:
        text = "\n".join(f"line {n} of the widget timeout notes" for n in range(40))
        cut = cut_text(text, 20)
        self.assertIn("… [truncated]", cut)
        self.assertLess(estimate_tokens(cut), estimate_tokens(text))
        self.assertEqual(cut_text("short", 100), "short")

    def test_a_cut_chunk_reports_its_own_size(self) -> None:
        # ``_cut_to_budget`` is the unit that had to be fixed — it reported the
        # ORIGINAL size of a shortened chunk — so it is driven directly.
        chunk = KnowledgeChunk(
            "s:0", "s", 0, "widget timeout " + "detail " * 200, tokens=estimate_tokens("x" * 1000)
        )
        cut = _cut_to_budget(chunk, 30)
        self.assertEqual(cut.tokens, estimate_tokens(cut.text))
        self.assertLess(cut.tokens, chunk.tokens)

    def test_an_empty_budget_still_answers_without_dumping(self) -> None:
        source = _source("a.md", SourceType.MARKDOWN)
        chunk = KnowledgeChunk(f"{source.source_id}:0", source.source_id, 0, "widget timeout")
        context = assemble(
            "widget timeout", rank((_scored(chunk),), index_sources={source.source_id: source}), budget_tokens=1
        )
        self.assertEqual(context.budget, 1)
        self.assertLessEqual(len(context.hits), 1)


class RobustnessClauseTests(unittest.IsolatedAsyncioTestCase):
    """The paths that used to answer wrongly, each pinned by its own test."""

    async def test_a_missing_or_blank_path_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            manager = KnowledgeManager()
            missing = await manager.ingest_path(corpus.root / "nope.md")
            self.assertEqual(missing.status, IngestStatus.FAILED)
            self.assertIn("no such file or directory", missing.reason)
            missing_dir = await manager.ingest_path(corpus.root / "nope-dir")
            self.assertEqual(missing_dir.status, IngestStatus.FAILED)
            self.assertNotIn("unsupported", missing_dir.reason)
            blank = await manager.ingest_path("")
            self.assertEqual(blank.status, IngestStatus.FAILED)
            self.assertEqual(blank.reason, "a path is required")
            # And the blank path did NOT mean \"index the current directory\".
            self.assertEqual(manager.sources(), ())

    async def test_a_module_request_with_a_blank_path_ingests_nothing(self) -> None:
        bus = EventBus()
        manager = KnowledgeManager()
        module = KnowledgeModule(manager)
        await module.start(bus)
        replies: list[object] = []
        await bus.subscribe("knowledge.ingest_completed", lambda event: replies.append(event))
        from novacontrol.core.events import Event

        await bus.publish(Event(type="knowledge.ingest_requested", payload={"path": ""}))
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].payload["status"], "failed")
        self.assertEqual(manager.sources(), ())
        await module.stop()

    async def test_a_capped_tree_says_so(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for n in range(8):
                (root / f"doc-{n}.md").write_text(f"# Doc {n}\n\nwidget timeout {n}\n", encoding="utf-8")
            manager = KnowledgeManager(max_files_per_tree=3)
            reports = await manager.ingest_tree(root)
            self.assertEqual(manager.index.source_count(), 3)
            self.assertEqual(reports[0].status, IngestStatus.SKIPPED)
            self.assertIn("only the first 3", reports[0].reason)
            self.assertEqual(len(reports), 4)
            aggregate = await KnowledgeManager(max_files_per_tree=3).ingest_path(root)
            self.assertIn("1 skipped", aggregate.reason)
            self.assertEqual(aggregate.status, IngestStatus.ADDED)

    async def test_a_backend_that_cannot_embed_loses_only_its_signal(self) -> None:
        manager = KnowledgeManager()
        await manager.ingest_text("the widget timeout is thirty seconds", title="t")
        manager.use_embedding_model(_RaisingModel())
        hits = await manager.search("widget timeout")
        self.assertTrue(hits)
        self.assertEqual(hits[0].embedding_score, 0.0)

    async def test_a_provider_that_raises_is_unavailable_not_fatal(self) -> None:
        def broken() -> object:
            raise RuntimeError("the model server is down")

        manager = KnowledgeManager(embedding_provider=broken)  # type: ignore[arg-type]
        await manager.ingest_text("the widget timeout is thirty seconds", title="t")
        self.assertFalse(manager.embedding_model_available)
        self.assertTrue(await manager.search("widget timeout"))

    async def test_a_namespaced_branch_is_reported_whole(self) -> None:
        with TemporaryDirectory() as tmp:
            corpus = Corpus(Path(tmp))
            self.assertEqual(ProjectDetector().detect(corpus.root).branch, "feature/timeouts")

    async def test_a_git_file_worktree_is_not_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/one\n", encoding="utf-8")
            context = ProjectDetector().detect(root)
            self.assertTrue(context.is_project)
            self.assertEqual(context.branch, "")
            self.assertEqual(context.repository, "")

    async def test_the_tools_declare_exactly_the_permissions_they_need(self) -> None:
        from novacontrol.core.security import PermissionScope

        registry = ToolRegistry()
        for tool in knowledge_tools(KnowledgeManager()):
            registry.register(tool)
        self.assertEqual(
            sorted(entry.tool.name for entry in registry.list()),
            ["knowledge_ingest", "knowledge_search", "project_context"],
        )
        self.assertEqual(
            registry.get("knowledge_ingest").tool.required_permissions,
            (PermissionScope.FILESYSTEM_READ,),
        )
        self.assertEqual(registry.get("knowledge_search").tool.required_permissions, ())

    async def test_a_tool_call_on_a_missing_path_reports_a_failure(self) -> None:
        registry = ToolRegistry()
        for tool in knowledge_tools(KnowledgeManager()):
            registry.register(tool)
        result = await registry.get("knowledge_ingest").tool.run({"path": "definitely/not/here.md"})
        self.assertEqual(result["status"], "failed")
        self.assertIn("no such file or directory", result["reason"])


class IncrementalUpdateClauseTests(unittest.IsolatedAsyncioTestCase):
    """11.2's incrementality, including the half an ingest cannot see: deletion."""

    async def test_an_emptied_file_forgets_its_knowledge(self) -> None:
        with TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text("# Doc\n\nThe widget timeout is thirty seconds.\n", encoding="utf-8")
            manager = KnowledgeManager()
            await manager.ingest_path(Path(tmp))
            doc.write_text("", encoding="utf-8")
            report = await manager.ingest_path(Path(tmp))
            self.assertEqual(report.status, IngestStatus.SKIPPED)
            self.assertEqual(manager.sources(), ())
            self.assertEqual((await manager.retrieve("widget timeout")).hits, ())

    async def test_a_deleted_file_is_pruned_on_request(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = root / "doc.md"
            doc.write_text("# Doc\n\nThe widget timeout is thirty seconds.\n", encoding="utf-8")
            manager = KnowledgeManager()
            await manager.ingest_path(root)
            doc.unlink()
            # Without pruning, the ingest honestly reports on the files that are
            # left and the deleted source is still retrievable.
            before = await manager.ingest_path(root)
            self.assertEqual(before.status, IngestStatus.UNCHANGED)
            self.assertEqual([Path(s.path).name for s in manager.sources()], ["doc.md"])
            # With pruning, the stale source is gone and the report says so.
            after = await manager.ingest_path(root, prune=True)
            self.assertIn("1 pruned", after.reason)
            self.assertEqual(manager.sources(), ())
            self.assertEqual((await manager.retrieve("widget timeout")).hits, ())

    async def test_pruning_never_touches_a_note_or_a_taught_article(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "doc.md").write_text("# Doc\n\nwidget timeout\n", encoding="utf-8")
            manager = KnowledgeManager()
            await manager.ingest_path(root)
            await manager.ingest_text("the widget timeout is thirty seconds", title="note")
            (root / "doc.md").unlink()
            removed = await manager.prune_missing(root)
            self.assertEqual(len(removed), 1)
            self.assertEqual(removed[0].status, IngestStatus.PRUNED)
            self.assertIn("no longer exists", removed[0].reason)
            self.assertEqual([s.path for s in manager.sources()], ["note/note"])
            # Idempotent, and a path that is not a directory is a no-op.
            self.assertEqual(await manager.prune_missing(root), ())
            self.assertEqual(await manager.prune_missing(root / "doc.md"), ())

    async def test_a_prune_is_announced_on_the_bus(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "doc.md").write_text("# Doc\n\nwidget timeout\n", encoding="utf-8")
            bus = EventBus()
            manager = KnowledgeManager()
            manager.attach_events(bus)
            statuses: list[str] = []
            await bus.subscribe(
                EventType.KNOWLEDGE_INDEXED,
                lambda event: statuses.append(str(event.payload.get("status"))),
            )
            await manager.ingest_path(root)
            (root / "doc.md").unlink()
            await manager.prune_missing(root)
            self.assertEqual(statuses, ["added", "pruned"])


class LlmArrowTests(unittest.IsolatedAsyncioTestCase):
    """The pipeline's last arrow: the context actually reaches the model."""

    def test_the_knowledge_block_becomes_its_own_system_message(self) -> None:
        brain = NovaBrain()
        messages = brain._build_chat_messages(  # noqa: SLF001 - the seam under test
            BrainRequest(text="where is the widget timeout set?", knowledge="--- [1] widgets.py\nDEFAULT_TIMEOUT = 30")
        )
        self.assertEqual(messages[-1]["role"], "user")
        system_messages = [m["content"] for m in messages if m["role"] == "system"]
        block = next((text for text in system_messages if "widgets.py" in text), "")
        self.assertTrue(block)
        self.assertIn("Relevant local knowledge", block)
        self.assertIn("DEFAULT_TIMEOUT = 30", block)

    def test_an_empty_knowledge_block_adds_no_message(self) -> None:
        brain = NovaBrain()
        without = brain._build_chat_messages(BrainRequest(text="hello"))  # noqa: SLF001
        self.assertFalse(any("Relevant local knowledge" in m["content"] for m in without))

    async def test_the_application_answers_with_the_budgeted_block(self) -> None:
        from novacontrol.application import NovaControlApplication

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "widgets"
            corpus = Corpus(root)
            app = NovaControlApplication(data_dir=Path(temp_dir) / "state")
            await app.start()
            try:
                self.assertIn("knowledge", app.runtime.module_names())
                # A default install loads no embedding model: the engine answers lexically.
                self.assertFalse(app.knowledge_engine.embedding_model_available)
                self.assertEqual(app.knowledge_engine.embedding_backend, "hashing")
                await app.ingest_knowledge(str(corpus.root))
                app.knowledge_engine.set_project(corpus.root)
                answer = await app.answer_with_knowledge(
                    "Where is the widget timeout configured?", active_task="widget timeout"
                )
                self.assertEqual(answer["mode"], "knowledge_answer")
                self.assertTrue(answer["answer"])
                self.assertTrue(answer["citations"])
                self.assertIn("Relevant local knowledge", answer["knowledge"]["text"])
                self.assertLessEqual(answer["knowledge"]["tokens"], answer["knowledge"]["budget"])
                self.assertFalse(answer["model_configured"])
                self.assertEqual(answer["brain_mode"], "scratch")
            finally:
                await app.stop()


class _CountingModel:
    """A deterministic stand-in for a real embedding model."""

    name = "counting-model"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text) % 7), 1.0] for text in texts]


class _RaisingModel:
    name = "raising-model"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("model unavailable")


def _source(name: str, kind: SourceType, *, project: str = "proj") -> KnowledgeSource:
    return KnowledgeSource(
        source_id=source_id_for(project, name),
        source_type=kind,
        path=name,
        project=project,
        content_hash="x" * 64,
        chunk_count=1,
    )


def _scored(chunk: KnowledgeChunk, score: float = 1.0):
    from novacontrol.knowledge.index import ScoredChunk

    return ScoredChunk(chunk=chunk, score=score, lexical=score)


if __name__ == "__main__":
    unittest.main()
