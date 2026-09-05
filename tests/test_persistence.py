from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novacontrol.application import NovaControlApplication
from novacontrol.automation import AutomationStep
from novacontrol.persistence import JsonStateStore


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_state_store_round_trips_payload(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = JsonStateStore(temp_dir)
            store.write("demo", {"value": 1})

            self.assertEqual(store.read("demo")["value"], 1)

    async def test_application_persists_local_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            app = NovaControlApplication(data_dir=data_dir)
            app.projects.create_project("Persisted")
            app.knowledge.add_article("Events", "Use events")
            app.scheduler.schedule_once("wake", delay_seconds=60)
            app.automation.create_workflow("Flow", (AutomationStep("Step", "demo"),))
            await app.stop()

            restored = NovaControlApplication(data_dir=data_dir)

            self.assertEqual(restored.projects.list_projects()[0].name, "Persisted")
            self.assertEqual(restored.knowledge.search("events")[0].article.title, "Events")
            self.assertEqual(restored.scheduler.tasks()[0].name, "wake")
            self.assertEqual(restored.automation.list_workflows()[0].name, "Flow")


if __name__ == "__main__":
    unittest.main()
