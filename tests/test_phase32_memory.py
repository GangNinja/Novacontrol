"""Phase 32 — structured long-term memory and personal knowledge."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from novacontrol.agents.models import AgentRole, AgentTask
from novacontrol.agents.pipeline import AgentRun, SpecialistPipeline
from novacontrol.application import NovaControlApplication
from novacontrol.brain.models import BrainRequest
from novacontrol.memory import (
    KnowledgeAccess,
    KnowledgeCategory,
    KnowledgeKind,
    KnowledgePrivacy,
    KnowledgeRecord,
    KnowledgeRetention,
    KnowledgeSource,
    KnowledgeStatus,
    MemoryManager,
    PersonalKnowledgeService,
    SqliteMemoryStore,
)
from novacontrol.memory.personal import (
    SCHEMA_VERSION,
    remember_explicit_text,
    utcnow,
)


class PersonalKnowledgeTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "memory.sqlite3"
        self.manager = MemoryManager(SqliteMemoryStore(self.path))
        self.memory = PersonalKnowledgeService(self.manager)

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def remember(self, content: str, **fields):
        result = await self.memory.remember(content, **fields)
        self.assertTrue(result.ok, result.to_dict())
        self.assertIsNotNone(result.record)
        return result.record


class AcquisitionTests(PersonalKnowledgeTestBase):
    async def test_explicit_user_fact_carries_source_and_provenance(self) -> None:
        record = await self.remember(
            "The primary NovaControl workspace is C:/work/novacontrol",
            subject="nova",
            predicate="workspace",
            category=KnowledgeCategory.FACT,
            source_type=KnowledgeSource.USER,
            source="chat:explicit",
        )
        self.assertEqual(record.status, KnowledgeStatus.CURRENT)
        self.assertTrue(record.verified)
        self.assertEqual(record.source_type, KnowledgeSource.USER)
        self.assertEqual(record.source, "chat:explicit")
        self.assertAlmostEqual(record.confidence, 0.96)
        self.assertIsNotNone(record.last_verified_at)
        self.assertIn("nova", record.entities)

        inspected = await self.memory.inspect(record.id)
        self.assertTrue(inspected["found"])
        self.assertEqual(inspected["record"]["source"], "chat:explicit")

    async def test_inference_and_assumption_are_not_promoted(self) -> None:
        inferred = await self.remember(
            "The user probably prefers dark mode",
            category=KnowledgeCategory.PREFERENCE,
            kind=KnowledgeKind.OBSERVATION,
            source_type=KnowledgeSource.INFERENCE,
        )
        assumed = await self.remember(
            "The next project will use Postgres",
            category=KnowledgeCategory.ACTIVE_PROJECT,
            kind=KnowledgeKind.ASSUMPTION,
            source_type=KnowledgeSource.INFERENCE,
        )
        self.assertFalse(inferred.verified)
        self.assertEqual(inferred.effective_status(), KnowledgeStatus.UNVERIFIED)
        self.assertFalse(assumed.verified)
        self.assertLessEqual(assumed.confidence, 0.35)
        hits = await self.memory.retrieve("dark mode", limit=5)
        self.assertFalse(any(hit.record.id == inferred.id for hit in hits))
        hits = await self.memory.retrieve(
            "dark mode", limit=5, include_unverified=True
        )
        self.assertTrue(any(hit.record.id == inferred.id for hit in hits))

    async def test_duplicate_detection_does_not_create_a_second_record(self) -> None:
        first = await self.remember(
            "Prefers concise status updates",
            category=KnowledgeCategory.PREFERENCE,
            predicate="communication_style",
        )
        duplicate = await self.memory.remember(
            "Prefers concise status updates",
            category=KnowledgeCategory.PREFERENCE,
            predicate="communication_style",
        )
        self.assertEqual(duplicate.action, "duplicate")
        self.assertEqual(duplicate.duplicate_of, first.id)
        self.assertEqual(len(self.memory.records), 1)
        self.assertEqual(duplicate.record.updated_at >= first.updated_at, True)

    async def test_private_and_sensitive_acquisition_defaults(self) -> None:
        private = await self.remember(
            "Personal medical appointment is Friday",
            category=KnowledgeCategory.HISTORY,
            privacy=KnowledgePrivacy.SENSITIVE,
        )
        self.assertEqual(private.privacy, KnowledgePrivacy.SENSITIVE)
        agent_hits = await self.memory.retrieve(
            "medical appointment", access=KnowledgeAccess.agent()
        )
        self.assertFalse(any(hit.record.id == private.id for hit in agent_hits))
        user_hits = await self.memory.retrieve(
            "medical appointment", access=KnowledgeAccess.user()
        )
        self.assertFalse(any(hit.record.id == private.id for hit in user_hits))
        sensitive_hits = await self.memory.retrieve(
            "medical appointment",
            access=KnowledgeAccess.user(allow_sensitive=True),
        )
        self.assertTrue(any(hit.record.id == private.id for hit in sensitive_hits))

    async def test_naive_timestamps_and_invalid_fields_are_rejected_safely(self) -> None:
        record = await self.remember(
            "The release window ends in 2100",
            subject="release",
            predicate="window",
            valid_until=datetime(2100, 1, 1),
        )
        self.assertIsNotNone(record.valid_until)
        self.assertIsNotNone(record.valid_until.tzinfo)
        # A naive timestamp used to raise TypeError when status was read.
        self.assertEqual(record.effective_status(), KnowledgeStatus.CURRENT)
        invalid = await self.memory.remember("Bad ttl", ttl_seconds="not-a-number")
        self.assertFalse(invalid.ok)
        invalid = await self.memory.remember("Bad metadata", metadata=["not", "a", "map"])
        self.assertFalse(invalid.ok)

    async def test_opt_out_is_not_an_explicit_acquisition(self) -> None:
        self.assertIsNone(
            remember_explicit_text(
                "Please do not remember this: my private medical appointment"
            )
        )
        self.assertIsNotNone(
            remember_explicit_text("Remember this: I prefer concise updates")
        )


class RetrievalTests(PersonalKnowledgeTestBase):
    async def test_exact_entity_and_project_relevance_rejects_unrelated_memory(self) -> None:
        alpha = await self.remember(
            "Project Alpha uses SQLite for durable state",
            subject="Project Alpha",
            predicate="database",
            category=KnowledgeCategory.ACTIVE_PROJECT,
            project="alpha",
            entities=["Project Alpha", "SQLite"],
        )
        beta = await self.remember(
            "Project Beta uses Rust for its parser",
            subject="Project Beta",
            predicate="language",
            category=KnowledgeCategory.ACTIVE_PROJECT,
            project="beta",
            entities=["Project Beta", "Rust"],
        )
        hits = await self.memory.retrieve("What database does Project Alpha use?")
        self.assertTrue(hits)
        self.assertEqual(hits[0].record.id, alpha.id)
        self.assertFalse(any(hit.record.id == beta.id for hit in hits))

        scoped = await self.memory.retrieve(
            "What database does the project use?",
            access=KnowledgeAccess.user(project="alpha"),
        )
        self.assertTrue(scoped)
        self.assertTrue(all(hit.record.project in {"", "alpha"} for hit in scoped))

    async def test_validity_and_recency_affect_retrieval(self) -> None:
        expired = await self.remember(
            "The launch deadline is 1 January 2020",
            subject="launch",
            predicate="deadline",
            category=KnowledgeCategory.GOAL,
            valid_until=utcnow() - timedelta(days=1),
            retention=KnowledgeRetention.LONG_TERM,
        )
        current = await self.remember(
            "The launch deadline is 1 December 2026",
            subject="launch",
            predicate="deadline",
            category=KnowledgeCategory.GOAL,
        )
        hits = await self.memory.retrieve("launch deadline")
        self.assertEqual([hit.record.id for hit in hits], [current.id])
        historical = await self.memory.retrieve(
            "launch deadline", include_expired=True, include_historical=True
        )
        self.assertTrue(any(hit.record.id == expired.id for hit in historical))
        cleaned = await self.memory.cleanup()
        self.assertGreaterEqual(cleaned["expired"], 1)
        self.assertEqual(
            self.memory.records[expired.id].effective_status(),
            KnowledgeStatus.EXPIRED,
        )

    async def test_context_budget_does_not_inject_every_record(self) -> None:
        for index in range(8):
            await self.remember(
                f"Project Atlas decision {index}: use module boundary {index} "
                "and keep operational reports concise",
                subject="Project Atlas",
                predicate=f"module_boundary_{index}",
                category=KnowledgeCategory.DECISION,
                project="atlas",
                entities=["Project Atlas"],
            )
        context = await self.memory.context_for(
            "What decisions were made about Project Atlas?",
            budget_tokens=50,
        )
        self.assertTrue(context.text)
        self.assertLessEqual(context.tokens, 50)
        self.assertGreater(context.omitted, 0)
        self.assertIn("Project Atlas", context.text)

    async def test_context_budget_never_exceeds_its_ceiling(self) -> None:
        await self.remember(
            "Project Atlas contains a deliberately long decision sentence "
            "with many operational details that cannot fit a tiny budget",
            subject="Project Atlas",
            predicate="detail",
            category=KnowledgeCategory.DECISION,
            project="atlas",
        )
        context = await self.memory.context_for(
            "Project Atlas decision", budget_tokens=5
        )
        self.assertEqual(context.text, "")
        self.assertLessEqual(context.tokens, 5)
        self.assertGreater(context.omitted, 0)

    async def test_graph_retrieval_returns_connected_record(self) -> None:
        project = await self.remember(
            "Project Atlas is the active memory project",
            subject="Project Atlas",
            predicate="status",
            category=KnowledgeCategory.ACTIVE_PROJECT,
            project="atlas",
        )
        decision = await self.remember(
            "Project Atlas stores durable state in SQLite",
            subject="Project Atlas",
            predicate="database",
            category=KnowledgeCategory.DECISION,
            project="atlas",
        )
        relation = await self.memory.relate(
            project.id, "has decision", target_id=decision.id, confidence=0.9
        )
        self.assertIsNotNone(relation)
        graph = await self.memory.graph(project.id)
        self.assertEqual(len(graph.nodes), 2)
        self.assertEqual(len(graph.edges), 1)
        self.assertIn("SQLite", " ".join(graph.entities) + " SQLite")


class EvolutionTests(PersonalKnowledgeTestBase):
    async def test_newer_explicit_decision_supersedes_with_history(self) -> None:
        old = await self.remember(
            "We will use SQLite for the project database",
            subject="project atlas",
            predicate="database",
            category=KnowledgeCategory.DECISION,
        )
        newer = await self.memory.remember(
            "We will use Postgres for the project database",
            subject="project atlas",
            predicate="database",
            category=KnowledgeCategory.DECISION,
            supersedes=[old.id],
        )
        self.assertTrue(newer.ok)
        self.assertEqual(newer.action, "created")
        self.assertEqual(self.memory.records[old.id].status, KnowledgeStatus.SUPERSEDED)
        self.assertEqual(self.memory.records[old.id].superseded_by, newer.record.id)
        current = await self.memory.retrieve("project database")
        self.assertEqual(current[0].record.id, newer.record.id)
        history = await self.memory.retrieve(
            "SQLite project database", include_historical=True
        )
        old_hit = next(hit for hit in history if hit.record.id == old.id)
        self.assertEqual(old_hit.record.status, KnowledgeStatus.SUPERSEDED)
        self.assertTrue(old_hit.record.history)

    async def test_unresolved_contradiction_is_detected_not_silently_resolved(self) -> None:
        old = await self.remember(
            "The deployment window is Tuesday",
            subject="deployment",
            predicate="window",
            category=KnowledgeCategory.DECISION,
        )
        incoming = await self.memory.remember(
            "The deployment window is Thursday",
            subject="deployment",
            predicate="window",
            category=KnowledgeCategory.DECISION,
        )
        self.assertEqual(incoming.action, "conflict")
        self.assertFalse(incoming.ok)
        self.assertEqual(self.memory.records[old.id].status, KnowledgeStatus.CONTRADICTED)
        self.assertEqual(
            self.memory.records[incoming.record.id].status, KnowledgeStatus.CONTRADICTED
        )
        resolved = await self.memory.resolve_conflict(incoming.record.id, keep=True)
        self.assertEqual(resolved.action, "resolved")
        self.assertEqual(self.memory.records[incoming.record.id].status, KnowledgeStatus.CURRENT)
        self.assertEqual(self.memory.records[old.id].status, KnowledgeStatus.SUPERSEDED)

    async def test_withdrawing_one_conflict_side_restores_the_other(self) -> None:
        old = await self.remember(
            "The deployment window is Tuesday",
            subject="deployment",
            predicate="window",
            category=KnowledgeCategory.DECISION,
        )
        incoming = await self.memory.remember(
            "The deployment window is Thursday",
            subject="deployment",
            predicate="window",
            category=KnowledgeCategory.DECISION,
        )
        self.assertEqual(incoming.action, "conflict")
        withdrawn = await self.memory.resolve_conflict(
            incoming.record.id, keep=False, reason="Thursday was a mistake"
        )
        self.assertTrue(withdrawn.ok)
        self.assertEqual(
            self.memory.records[incoming.record.id].status, KnowledgeStatus.ARCHIVED
        )
        self.assertEqual(self.memory.records[old.id].status, KnowledgeStatus.CURRENT)
        hits = await self.memory.retrieve("deployment window")
        self.assertEqual([hit.record.id for hit in hits], [old.id])

    async def test_resolving_a_conflict_records_user_evidence(self) -> None:
        await self.remember(
            "The deployment window is Tuesday",
            subject="deployment",
            predicate="window",
            category=KnowledgeCategory.DECISION,
        )
        incoming = await self.memory.remember(
            "The deployment window is Thursday",
            subject="deployment",
            predicate="window",
            category=KnowledgeCategory.DECISION,
            source_type=KnowledgeSource.CONVERSATION,
            source="agent note",
            verified=False,
        )
        self.assertEqual(incoming.action, "conflict")
        resolved = await self.memory.resolve_conflict(incoming.record.id, keep=True)
        self.assertTrue(resolved.ok)
        record = self.memory.records[incoming.record.id]
        self.assertEqual(record.source_type, KnowledgeSource.CORRECTION)
        self.assertEqual(record.source, "user conflict resolution")
        self.assertTrue(record.verified)

    async def test_correcting_a_superseded_record_clears_old_lineage(self) -> None:
        old = await self.remember(
            "We will use SQLite for the project database",
            subject="project atlas",
            predicate="database",
            category=KnowledgeCategory.DECISION,
        )
        newer = await self.remember(
            "We will use Postgres for the project database",
            subject="project atlas",
            predicate="database",
            category=KnowledgeCategory.DECISION,
            supersedes=[old.id],
        )
        corrected = await self.memory.correct(
            old.id,
            "We will use DuckDB for the project database",
            reason="the old decision was revised again",
        )
        self.assertTrue(corrected.ok)
        self.assertEqual(corrected.record.superseded_by, "")
        self.assertEqual(corrected.record.merged_into, "")
        self.assertEqual(
            self.memory.records[newer.id].status, KnowledgeStatus.SUPERSEDED
        )
        hits = await self.memory.retrieve("project database")
        self.assertEqual([hit.record.id for hit in hits], [corrected.record.id])

    async def test_same_predicate_in_two_projects_does_not_conflict(self) -> None:
        alpha = await self.remember(
            "Project Atlas uses SQLite",
            subject="platform",
            predicate="database",
            category=KnowledgeCategory.DECISION,
            project="alpha",
        )
        beta = await self.remember(
            "Project Beta uses Postgres",
            subject="platform",
            predicate="database",
            category=KnowledgeCategory.DECISION,
            project="beta",
        )
        self.assertEqual(alpha.status, KnowledgeStatus.CURRENT)
        self.assertEqual(beta.status, KnowledgeStatus.CURRENT)
        hits = await self.memory.retrieve("platform database", limit=10)
        self.assertEqual({hit.record.id for hit in hits}, {alpha.id, beta.id})

    async def test_correction_preserves_previous_version_and_provenance(self) -> None:
        original = await self.remember(
            "The user's timezone is UTC",
            subject="user",
            predicate="timezone",
            category=KnowledgeCategory.FACT,
        )
        corrected = await self.memory.correct(
            original.id,
            "The user's timezone is Europe/London",
            reason="user corrected the timezone",
        )
        self.assertTrue(corrected.ok)
        self.assertEqual(corrected.record.id, original.id)
        self.assertEqual(corrected.record.version, 2)
        self.assertEqual(corrected.record.source_type, KnowledgeSource.CORRECTION)
        self.assertEqual(len(corrected.record.history), 1)
        self.assertEqual(corrected.record.history[0].content, original.content)
        hits = await self.memory.retrieve("timezone")
        self.assertEqual(hits[0].record.content, "The user's timezone is Europe/London")

    async def test_explicit_deletion_removes_the_record_and_relations(self) -> None:
        record = await self.remember(
            "A temporary note about Project Atlas",
            subject="Project Atlas",
            category=KnowledgeCategory.FACT,
        )
        other = await self.remember(
            "Project Atlas uses a local index",
            subject="Project Atlas",
            predicate="index",
            category=KnowledgeCategory.DECISION,
        )
        await self.memory.relate(record.id, "supports", target_id=other.id)
        self.assertTrue(await self.memory.delete(record.id))
        self.assertNotIn(record.id, self.memory.records)
        self.assertFalse(any(edge.source_id == record.id for edge in self.memory.relations.values()))
        self.assertFalse((await self.memory.inspect(record.id))["found"])
        self.assertFalse(
            any(hit.record.id == record.id for hit in await self.memory.retrieve("temporary note"))
        )


class PersistenceTests(PersonalKnowledgeTestBase):
    async def test_restart_rebuilds_index_and_preserves_history(self) -> None:
        record = await self.remember(
            "The user prefers concise project updates",
            category=KnowledgeCategory.PREFERENCE,
            predicate="communication_style",
            source="chat",
            metadata={"client": "web"},
        )
        await self.remember(
            "Project Atlas uses SQLite",
            subject="Project Atlas",
            predicate="database",
            category=KnowledgeCategory.DECISION,
            project="atlas",
        )
        restarted = PersonalKnowledgeService(
            MemoryManager(SqliteMemoryStore(self.path))
        )
        await restarted.load()
        self.assertIn(record.id, restarted.records)
        restored = restarted.records[record.id]
        self.assertEqual(restored.source, "chat")
        self.assertEqual(restored.metadata["client"], "web")
        hits = await restarted.retrieve("concise project updates")
        self.assertEqual(hits[0].record.id, record.id)
        self.assertEqual((await restarted.status())["records"], 2)

    async def test_relations_survive_restart(self) -> None:
        source = await self.remember(
            "Project Atlas uses SQLite",
            subject="Project Atlas",
            predicate="database",
            category=KnowledgeCategory.DECISION,
            project="atlas",
        )
        target = await self.remember(
            "The SQLite schema is stored in memory.sqlite3",
            subject="SQLite",
            predicate="storage",
            category=KnowledgeCategory.FACT,
            project="atlas",
        )
        relation =         await self.memory.relate(
            source.id, "uses", target_id=target.id
        )
        restarted = PersonalKnowledgeService(
            MemoryManager(SqliteMemoryStore(self.path))
        )
        await restarted.load()
        self.assertIn(relation.id, restarted.relations)
        graph = await restarted.graph(source.id)
        self.assertEqual(len(graph.edges), 1)
        self.assertEqual(graph.edges[0].target_label, target.content[:120])


class ConsolidationTests(PersonalKnowledgeTestBase):
    async def test_duplicate_records_merge_with_provenance_and_undo(self) -> None:
        now = utcnow()
        first = KnowledgeRecord(
            id="merge-a",
            content="The user prefers concise project updates",
            subject="user",
            predicate="communication_style",
            category=KnowledgeCategory.PREFERENCE,
            source_type=KnowledgeSource.USER,
            source="chat",
            confidence=0.8,
            verified=True,
            created_at=now,
            updated_at=now,
            entities=("user",),
        )
        second = KnowledgeRecord(
            id="merge-b",
            content="The user prefers concise project updates",
            subject="user",
            predicate="communication_style",
            category=KnowledgeCategory.PREFERENCE,
            source_type=KnowledgeSource.USER,
            source="api",
            confidence=0.9,
            verified=True,
            created_at=now,
            updated_at=now,
            entities=("user", "NovaControl"),
        )
        for record in (first, second):
            await self.manager.remember(
                "long_term",
                f"pk:{record.id}",
                {"schema": SCHEMA_VERSION, "record": record.to_dict()},
                text=record.content,
                importance=record.confidence,
            )
        service = PersonalKnowledgeService(
            MemoryManager(SqliteMemoryStore(self.path))
        )
        await service.load()
        result = await service.consolidate()
        self.assertEqual(result.groups, 1)
        self.assertEqual(len(result.merged), 1)
        self.assertEqual(len(result.kept), 1)
        canonical = service.records[result.kept[0]]
        self.assertIn("consolidated_from", canonical.metadata)
        self.assertIn("NovaControl", canonical.entities)
        merged = service.records[result.merged[0]]
        self.assertEqual(merged.status, KnowledgeStatus.MERGED)
        self.assertEqual(merged.merged_into, canonical.id)
        self.assertEqual(canonical.source, "api")
        self.assertEqual(merged.source, "chat")

        restored = await service.undo_consolidation(result.batch_id)
        self.assertEqual(restored, 1)
        self.assertEqual(service.records[merged.id].status, KnowledgeStatus.CURRENT)
        self.assertNotIn("consolidation_batch", service.records[canonical.id].metadata)
        self.assertNotIn("consolidated_from", service.records[canonical.id].metadata)

    async def test_consolidation_does_not_merge_similar_but_different_facts(self) -> None:
        await self.remember(
            "Project Atlas uses SQLite",
            subject="Project Atlas",
            predicate="database",
            category=KnowledgeCategory.DECISION,
        )
        await self.remember(
            "Project Atlas uses Postgres",
            subject="Project Atlas",
            predicate="alternative_database",
            category=KnowledgeCategory.DECISION,
        )
        result = await self.memory.consolidate()
        self.assertEqual(result.groups, 0)
        self.assertFalse(result.merged)


class PrivacyAndExportTests(PersonalKnowledgeTestBase):
    async def test_agent_access_and_export_are_scoped(self) -> None:
        public = await self.remember(
            "Project Atlas uses a local index",
            category=KnowledgeCategory.FACT,
            project="atlas",
            privacy=KnowledgePrivacy.PUBLIC,
        )
        private = await self.remember(
            "The user's private project note",
            category=KnowledgeCategory.FACT,
            project="atlas",
            privacy=KnowledgePrivacy.PRIVATE,
        )
        sensitive = await self.remember(
            "A sensitive account detail",
            category=KnowledgeCategory.FACT,
            project="atlas",
            privacy=KnowledgePrivacy.SENSITIVE,
        )
        agent = await self.memory.retrieve(
            "Project Atlas", access=KnowledgeAccess.agent(project="atlas")
        )
        ids = {hit.record.id for hit in agent}
        self.assertIn(public.id, ids)
        self.assertNotIn(private.id, ids)
        self.assertNotIn(sensitive.id, ids)

        exported = await self.memory.export()
        exported_ids = {row["id"] for row in exported["records"]}
        self.assertNotIn(sensitive.id, exported_ids)
        self.assertIn(private.id, exported_ids)
        sensitive_export = await self.memory.export(include_sensitive=True)
        self.assertIn(
            sensitive.id, {row["id"] for row in sensitive_export["records"]}
        )

    async def test_privacy_isolation_survives_restart(self) -> None:
        record = await self.remember(
            "Private restart note",
            category=KnowledgeCategory.FACT,
            privacy=KnowledgePrivacy.PRIVATE,
        )
        restarted = PersonalKnowledgeService(
            MemoryManager(SqliteMemoryStore(self.path))
        )
        await restarted.load()
        hits = await restarted.retrieve(
            "restart note", access=KnowledgeAccess.agent()
        )
        self.assertFalse(any(hit.record.id == record.id for hit in hits))

    async def test_relation_endpoints_do_not_leak_a_hidden_target(self) -> None:
        source = await self.remember(
            "Project Atlas public summary",
            subject="Project Atlas",
            category=KnowledgeCategory.FACT,
            project="atlas",
            privacy=KnowledgePrivacy.PUBLIC,
        )
        hidden = await self.remember(
            "A sensitive deployment credential note",
            subject="deployment secret",
            category=KnowledgeCategory.FACT,
            project="atlas",
            privacy=KnowledgePrivacy.SENSITIVE,
        )
        await self.memory.relate(
            source.id,
            "depends on",
            target_id=hidden.id,
            access=KnowledgeAccess.user(allow_sensitive=True),
        )
        inspected = await self.memory.inspect(source.id)
        self.assertEqual(inspected["relations"], [])
        exported = await self.memory.export()
        self.assertNotIn(hidden.id, {row["id"] for row in exported["records"]})
        self.assertEqual(exported["relations"], [])
        explicit = await self.memory.export(include_sensitive=True)
        self.assertEqual(len(explicit["relations"]), 1)

    async def test_agent_without_a_project_scope_gets_no_project_records(self) -> None:
        await self.remember(
            "Project Atlas uses a local index",
            subject="Project Atlas",
            category=KnowledgeCategory.FACT,
            project="atlas",
        )
        global_note = await self.remember(
            "The user prefers concise updates",
            category=KnowledgeCategory.PREFERENCE,
            predicate="communication_style",
        )
        hits = await self.memory.retrieve(
            "Project Atlas concise updates", access=KnowledgeAccess.agent()
        )
        self.assertEqual({hit.record.id for hit in hits}, {global_note.id})


class PipelineContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_pipeline_puts_permitted_personal_context_in_the_run(self) -> None:
        row = {
            "record": {
                "content": "Project Atlas uses concise status updates",
                "category": "preference",
                "confidence": 0.96,
            },
            "score": 3.0,
            "reasons": ["entity match"],
        }
        task = AgentTask(
            "report Project Atlas status",
            metadata={"personal_knowledge": [row]},
        )
        run = AgentRun(
            agent="test-agent",
            role=AgentRole.RESEARCH,
            task_id=task.id,
            goal=task.goal,
        )
        pipeline = SpecialistPipeline()
        agent = SimpleNamespace(
            local_context=lambda *_args: {},
            context_query=lambda *_args: "Project Atlas status",
        )
        block = await pipeline._stage_context(agent, task, None, run)
        self.assertIn("personal_knowledge", block)
        self.assertIn("concise status updates", run.context_text)
        self.assertIn("Project Atlas status", block["query"])


class ApplicationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.data = Path(self.directory.name) / "data"
        self.app = NovaControlApplication(data_dir=self.data)

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def test_application_chat_memory_and_restart(self) -> None:
        result = await self.app.personal_remember(
            "I prefer concise status updates",
            category="preference",
            source="chat",
        )
        self.assertTrue(result["ok"])
        hits = await self.app.personal_search("concise status")
        self.assertEqual(hits["count"], 1)

        chat = await self.app._handle_memory(
            BrainRequest(text="remember this: I prefer concise status updates"),
            "remember this: I prefer concise status updates",
        )
        route, payload = chat
        self.assertEqual(route, "memory")
        self.assertEqual(payload["mode"], "memory_store")
        self.assertIn("personal", payload)
        self.assertTrue(payload["personal"]["ok"])

        rebuilt = NovaControlApplication(data_dir=self.data)
        await rebuilt.personal_memory.load()
        restored = await rebuilt.personal_search("concise status")
        self.assertEqual(restored["count"], 1)

    async def test_phase29_and_phase31_seams_use_personal_context(self) -> None:
        await self.app.personal_remember(
            "Use concise status updates when reporting project work",
            category="preference",
            predicate="reporting_style",
            project="atlas",
        )
        skill_context = await self.app.retrieve_skills_with_context(
            "concise project updates"
        )
        self.assertIn("concise status updates", skill_context["personal_context"])
        self.assertGreaterEqual(skill_context["personal_context_tokens"], 1)

        plan = await self.app.plan_computer_task("open calculator")
        self.assertIn("personal_context", plan)
        self.assertIn("calculator", str(plan.get("objective", "")))

    async def test_agent_receives_only_permitted_project_context(self) -> None:
        allowed = await self.app.personal_remember(
            "Project Atlas uses SQLite",
            category="decision",
            project="atlas",
            privacy="personal",
        )
        private = await self.app.personal_remember(
            "Project Atlas private credential hint",
            category="fact",
            project="atlas",
            privacy="private",
        )
        other = await self.app.personal_remember(
            "Project Beta uses Rust",
            category="decision",
            project="beta",
            privacy="personal",
        )

        class Coordinator:
            def __init__(self) -> None:
                self.task = None

            async def delegate(self, task, registry):  # noqa: ANN001
                self.task = task
                return SimpleNamespace(
                    to_dict=lambda: {
                        "task_id": task.id,
                        "content": "delegated",
                        "metadata": {},
                    }
                )

        coordinator = Coordinator()
        self.app.coordinator = coordinator
        route, payload = await self.app._handle_agent(
            BrainRequest(text="use Project Atlas context", context={"project": "atlas"}),
            "use Project Atlas context",
        )
        self.assertEqual(route, "agent")
        metadata = coordinator.task.metadata
        ids = {row["record"]["id"] for row in metadata["personal_knowledge"]}
        self.assertIn(allowed["record"]["id"], ids)
        self.assertNotIn(private["record"]["id"], ids)
        self.assertNotIn(other["record"]["id"], ids)
        self.assertTrue(metadata["personal_knowledge_access"]["private_excluded"])
        self.assertEqual(payload["content"], "delegated")

    async def test_chat_message_builder_includes_personal_context(self) -> None:
        messages = self.app.brain._build_chat_messages(
            BrainRequest(
                text="How should I report this?",
                context={
                    "personal_knowledge_context": (
                        "Relevant personal knowledge: the user prefers concise updates"
                    )
                },
            )
        )
        personal = [
            message["content"]
            for message in messages
            if "personal knowledge" in message["content"].lower()
        ]
        self.assertTrue(personal)
        self.assertIn("concise updates", personal[0])


if __name__ == "__main__":
    unittest.main()
