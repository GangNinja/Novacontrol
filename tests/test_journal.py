from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novacontrol.core.events import Event, EventBus
from novacontrol.core.journal import InMemoryEventJournal, JsonlEventJournal


class JournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_journal_records_published_events(self) -> None:
        journal = InMemoryEventJournal()
        bus = EventBus(journal=journal)

        await bus.publish(Event(type="demo.event", payload={"value": 1}))

        events = await journal.read()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "demo.event")

    async def test_jsonl_journal_round_trips_events(self) -> None:
        with TemporaryDirectory() as temp_dir:
            journal = JsonlEventJournal(Path(temp_dir) / "events.jsonl")
            event = Event(type="demo.persisted", payload={"value": 2})

            await journal.append(event)
            events = await journal.read()

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].type, "demo.persisted")
            self.assertEqual(events[0].payload["value"], 2)
            self.assertEqual(events[0].correlation_id, event.correlation_id)


if __name__ == "__main__":
    unittest.main()
