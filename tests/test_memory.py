from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novacontrol.core.events import Event, EventBus
from conftest import collect_events
from novacontrol.memory import (
    HashingVectorIndex,
    InMemoryMemoryStore,
    MemoryManager,
    MemoryModule,
    MemoryNamespace,
    SqliteMemoryStore,
    VectorDocument,
)


class MemoryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_memory_store_retrieves_and_searches_records(self) -> None:
        manager = MemoryManager(InMemoryMemoryStore())
        await manager.remember(
            MemoryNamespace.CONVERSATION,
            "hello",
            {"speaker": "user"},
            text="User likes concise project updates",
            importance=0.7,
        )

        record = await manager.recall(MemoryNamespace.CONVERSATION, "hello")
        results = await manager.retrieve(MemoryNamespace.CONVERSATION, "concise updates")

        self.assertIsNotNone(record)
        self.assertEqual(results[0].record.key, "hello")

    async def test_expired_records_are_cleaned_up(self) -> None:
        store = InMemoryMemoryStore()
        manager = MemoryManager(store)
        await manager.remember(
            MemoryNamespace.SHORT_TERM,
            "temp",
            {"value": "gone soon"},
            ttl_seconds=-1,
        )

        removed = await manager.cleanup()
        record = await manager.recall(MemoryNamespace.SHORT_TERM, "temp")

        self.assertEqual(removed, 1)
        self.assertIsNone(record)

    async def test_sqlite_store_persists_records(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "memory.sqlite3"
            manager = MemoryManager(SqliteMemoryStore(path))
            await manager.remember(
                MemoryNamespace.KNOWLEDGE,
                "architecture",
                {"topic": "events"},
                text="Modules communicate through events",
            )

            second_manager = MemoryManager(SqliteMemoryStore(path))
            record = await second_manager.recall(MemoryNamespace.KNOWLEDGE, "architecture")

            self.assertIsNotNone(record)
            self.assertEqual(record.text, "Modules communicate through events")

    async def test_summarizer_returns_extracts(self) -> None:
        manager = MemoryManager(InMemoryMemoryStore())
        await manager.remember(
            MemoryNamespace.PROJECT,
            "phase-3",
            {"status": "active"},
            text="Memory phase is underway",
        )

        summary = await manager.summarize(MemoryNamespace.PROJECT)

        self.assertIn("phase-3", summary)


class MemoryModuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_module_handles_remember_events(self) -> None:
        bus = EventBus()
        manager = MemoryManager(InMemoryMemoryStore())
        module = MemoryModule(manager)

        seen = await collect_events(
            bus, "memory.stored",
            "memory.remember",
            {
                "namespace": MemoryNamespace.LONG_TERM,
                "key": "preference",
                "value": {"theme": "quiet"},
                "text": "Prefers quiet operational interfaces",
            },
            start_fn=module.start,
        )

        record = await manager.recall(MemoryNamespace.LONG_TERM, "preference")

        self.assertIsNotNone(record)
        self.assertEqual(seen[0].type, "memory.stored")


class VectorIndexTests(unittest.IsolatedAsyncioTestCase):
    async def test_hashing_vector_index_returns_similar_documents(self) -> None:
        index = HashingVectorIndex()
        await index.upsert(VectorDocument(id="a", text="event driven modular runtime"))
        await index.upsert(VectorDocument(id="b", text="voice recognition wake word"))

        results = await index.query("modular runtime", limit=1)

        self.assertEqual(results[0][0].id, "a")


if __name__ == "__main__":
    unittest.main()
