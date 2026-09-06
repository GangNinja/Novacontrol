"""Bug log + vision controller + auto-approve settings tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from novacontrol.core.buglog import BugLog


class BugLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bugs.json"
        self.log = BugLog(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_persists_to_file(self):
        self.log.record("Open steam failed", where="desktop: Open steam", details={"error": "boom"})
        payload = self.path.read_text(encoding="utf-8")
        self.assertIn("Open steam failed", payload)
        self.assertIn("desktop: Open steam", payload)

    def test_recorded_fields_what_where_when(self):
        record = self.log.record("X failed", where="vision: guided_click")
        self.assertEqual(record.what, "X failed")
        self.assertEqual(record.where, "vision: guided_click")
        self.assertTrue(record.when)  # timestamped
        self.assertEqual(record.status, "open")

    def test_mark_fixed_and_clear(self):
        record = self.log.record("bug one", where="somewhere")
        self.assertEqual(self.log.open_count(), 1)
        self.log.mark_fixed(record.id)
        self.assertEqual(self.log.open_count(), 0)
        removed = self.log.clear_fixed()
        self.assertEqual(removed, 1)

    def test_reload_from_disk(self):
        self.log.record("survives restart", where="desktop")
        fresh = BugLog(self.path)
        self.assertEqual(len(fresh.all()), 1)
        self.assertEqual(fresh.all()[0].what, "survives restart")

    def test_corrupt_file_starts_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        log = BugLog(self.path)
        self.assertEqual(log.all(), ())


if __name__ == "__main__":
    unittest.main()
