from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from novacontrol.application import NovaControlApplication
from novacontrol.automation import AutomationStep
from novacontrol.persistence import JsonStateStore


class StateStoreRobustnessTests(unittest.IsolatedAsyncioTestCase):
    """A snapshot that cannot be read is "no state yet", never a failed boot.

    ``NovaControlApplication`` reads this store while it is being constructed,
    so a file that is empty or truncated — a killed process, or a second process
    replacing the snapshot at that instant — used to raise ``JSONDecodeError``
    out of the constructor and stop the build from starting at all.
    """

    def test_an_unreadable_snapshot_reports_no_state_instead_of_raising(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = JsonStateStore(root)
            cases: dict[str, str | bytes] = {
                "empty": "",
                "whitespace": "   \n",
                "truncated": '{"value": 1',
                "not-an-object": "[1, 2, 3]",
                "scalar": '"text"',
                "garbage": "not json at all",
                # Bytes no UTF-8 decoder accepts. ``UnicodeDecodeError`` is a
                # ``ValueError``, not an ``OSError``: the read has to name it or
                # this file still stops the build from starting.
                "utf16": '{"value": 1}'.encode("utf-16"),
                "binary": bytes([0x00, 0xFF, 0xFE, 0x81, 0x7F]),
                # A kill mid-write can cut a multi-byte character in half: this
                # is a valid JSON prefix whose last byte starts an é.
                "split-multibyte": '{"name": "caf\u00e9'.encode("utf-8")[:-1],
            }
            for name, raw in cases.items():
                with self.subTest(case=name):
                    path = root / f"{name}.json"
                    if isinstance(raw, bytes):
                        path.write_bytes(raw)
                    else:
                        path.write_text(raw, encoding="utf-8")

                    self.assertEqual(store.read(name), {})
            # A directory where the snapshot should be is unreadable too.
            (root / "a-directory.json").mkdir()

            self.assertEqual(store.read("a-directory"), {})

    def test_a_write_leaves_no_temporary_behind_and_round_trips(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = JsonStateStore(root)
            store.write("demo", {"value": 1, "nested": {"a": [1, 2]}})
            store.write("demo", {"value": 2})

            self.assertEqual(store.read("demo"), {"value": 2})
            # The replace left exactly the snapshot: no .tmp for a later read to
            # mistake for state.
            self.assertEqual([path.name for path in root.iterdir()], ["demo.json"])

    async def test_an_empty_snapshot_does_not_stop_the_application_booting(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "tasks.json").write_text("", encoding="utf-8")

            app = NovaControlApplication(data_dir=data_dir)

            self.assertEqual(app.tasks.list(), ())
            await app.stop()


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
