from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory

from novacontrol.audit import (
    AUDIT_DIRECTORY,
    AUDIT_FILENAME,
    REDACTED,
    AuditLogger,
    AuditRecord,
    InMemoryAuditSink,
    JsonlAuditSink,
    Redactor,
    RetentionPolicy,
    json_safe,
)

CLOCK = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)


class _RemoteSink(InMemoryAuditSink):
    """A sink that claims to be remote, for the local-only check."""

    name = "remote"
    local = False


class RedactorTests(unittest.TestCase):
    """Redaction happens on the way in, and prose is not eaten."""

    def setUp(self) -> None:
        self.redactor = Redactor()

    def _redact(self, text: str) -> str:
        return self.redactor.redact_text(text).text

    def test_credential_shapes_are_replaced(self) -> None:
        cases = {
            "openai key sk-abcdefghijklmnopqrstuvwxyz012345": "sk-",
            "authorization: Bearer abcdefghijklmnopqrstuvwxyz": "Bearer abcdef",
            "token ghp_abcdefghijklmnopqrstuvwxyz0123": "ghp_",
            "url https://user:hunter2@example.com/x": "hunter2",
        }
        for text, secret in cases.items():
            with self.subTest(text=text):
                cleaned = self._redact(text)
                self.assertIn(REDACTED, cleaned)
                self.assertNotIn(secret, cleaned)

    def test_a_private_key_block_is_redacted_whole(self) -> None:
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END RSA PRIVATE KEY-----"
        cleaned = self._redact(text)

        self.assertIn(REDACTED, cleaned)
        self.assertNotIn("MIIBOgIBAAJBAK", cleaned)

    def test_an_assignment_keeps_its_key_and_loses_its_value(self) -> None:
        cleaned = self._redact("password=hunter2 and api_key: sk-AAAABBBBCCCCDDDD")

        self.assertIn("password=", cleaned)
        self.assertIn("api_key:", cleaned)
        self.assertNotIn("hunter2", cleaned)
        self.assertNotIn("sk-AAAABBBBCCCCDDDD", cleaned)

    def test_prose_about_credentials_is_left_alone(self) -> None:
        text = "the token expired and the password prompt appeared again"

        self.assertEqual(self._redact(text), text)

    def test_a_sensitive_key_hides_its_value_whatever_it_looks_like(self) -> None:
        safe, report = self.redactor.redact_value(
            {"api_key": "not-obviously-a-secret", "note": "kept"}
        )

        self.assertEqual(safe["api_key"], REDACTED)
        self.assertEqual(safe["note"], "kept")
        self.assertEqual(report.count, 1)
        self.assertIn("sensitive_key", report.kinds)

    def test_nested_and_sequence_values_are_redacted(self) -> None:
        safe, report = self.redactor.redact_value(
            {"steps": [{"token": "abc"}, {"detail": "Bearer abcdefghijklmnop"}]}
        )

        self.assertEqual(safe["steps"][0]["token"], REDACTED)
        self.assertNotIn("abcdefghijklmnop", safe["steps"][1]["detail"])
        self.assertGreaterEqual(report.count, 2)

    def test_an_empty_and_a_disabled_redactor_are_no_ops(self) -> None:
        self.assertEqual(self.redactor.redact_text("").count, 0)
        disabled = Redactor(enabled=False)
        self.assertEqual(disabled.redact_text("password=hunter2").text, "password=hunter2")


class AuditLoggerTests(unittest.TestCase):
    """The audit trail is redacted on the way in and bounded by policy."""

    def setUp(self) -> None:
        self.sink = InMemoryAuditSink()
        self.logger = AuditLogger(sink=self.sink, clock=lambda: CLOCK)

    def test_a_record_captures_the_operational_facts(self) -> None:
        row = self.logger.record(
            task_id="task-1",
            user_request="open notepad",
            outcome="completed",
            route="desktop_automation",
            intent="desktop_automation",
            decision={"route": "direct_tool"},
            plan=({"step_id": "s1", "action": "open_app", "tool": "desktop.open"},),
            tools=("desktop.open",),
            actions=("open_app",),
            verification={"verified": True},
            permission_decisions=({"step_id": "s1", "allow": True},),
            model="local",
            provider="scratch",
            latency_ms=12.5,
            resources={"ram_delta_bytes": 1024},
        )

        self.assertEqual(row.task_id, "task-1")
        self.assertEqual(row.outcome, "completed")
        self.assertTrue(row.ok)
        self.assertEqual(row.tools, ("desktop.open",))
        self.assertEqual(row.latency_ms, 12.5)
        self.assertEqual(row.redactions, 0)
        stored = self.logger.entries()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].task_id, "task-1")
        self.assertEqual(self.logger.count(), 1)

    def test_no_reasoning_or_prompt_fields_exist_on_a_record(self) -> None:
        row = AuditRecord(task_id="t", user_request="hi").to_dict()

        for forbidden in ("prompt", "reasoning", "chain_of_thought", "completion", "answer"):
            self.assertNotIn(forbidden, row)

    def test_a_secret_in_the_request_never_reaches_the_trail(self) -> None:
        row = self.logger.record(
            task_id="task-2",
            user_request="set the api_key to sk-abcdefghijklmnopqrstuvwxyz",
            outcome="completed",
        )

        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", row.user_request)
        self.assertIn(REDACTED, row.user_request)
        self.assertGreaterEqual(row.redactions, 1)
        self.assertIn("api_key", row.redacted_kinds)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", json.dumps(row.to_dict()))

    def test_a_secret_in_structured_metadata_is_redacted_too(self) -> None:
        row = self.logger.record(
            task_id="task-3",
            user_request="sign in",
            outcome="completed",
            decision={"credentials": {"password": "hunter2"}},
            resources={"bearer": "Bearer abcdefghijklmnop"},
        )

        blob = json.dumps(row.to_dict())
        self.assertNotIn("hunter2", blob)
        self.assertNotIn("abcdefghijklmnop", blob)
        self.assertGreaterEqual(row.redactions, 2)

    def test_redaction_can_be_turned_off_by_the_operator(self) -> None:
        logger = AuditLogger(
            sink=InMemoryAuditSink(),
            retention=RetentionPolicy(redact_sensitive=False),
            clock=lambda: CLOCK,
        )
        row = logger.record(
            task_id="t", user_request="set password=hunter2", outcome="completed"
        )

        self.assertEqual(row.user_request, "set password=hunter2")
        self.assertEqual(row.redactions, 0)

    def test_a_remote_sink_is_refused_when_the_trail_is_local_only(self) -> None:
        with self.assertRaises(ValueError):
            AuditLogger(sink=_RemoteSink())

    def test_a_remote_sink_is_allowed_only_when_local_only_is_off(self) -> None:
        logger = AuditLogger(
            sink=_RemoteSink(),
            retention=RetentionPolicy(local_only=False),
            clock=lambda: CLOCK,
        )
        row = logger.record(task_id="t", user_request="hi", outcome="completed")

        self.assertEqual(row.task_id, "t")

    def test_delete_removes_one_record_and_clear_removes_the_rest(self) -> None:
        first = self.logger.record(task_id="a", user_request="one")
        self.logger.record(task_id="b", user_request="two")

        self.assertTrue(self.logger.delete(first.id))
        self.assertFalse(self.logger.delete("missing"))
        self.assertEqual([row.task_id for row in self.logger.entries()], ["b"])

        self.assertEqual(self.logger.clear(), 1)
        self.assertEqual(self.logger.entries(), ())
        self.assertEqual(self.logger.count(), 0)

    def test_retention_by_age_drops_old_records_and_keeps_recent_ones(self) -> None:
        logger = AuditLogger(
            sink=InMemoryAuditSink(),
            retention=RetentionPolicy(retention_days=7, max_records=0),
            clock=lambda: CLOCK,
        )
        logger.record(
            task_id="old",
            user_request="ancient",
            outcome="completed",
            timestamp=(CLOCK - timedelta(days=30)).isoformat(),
        )
        logger.record(
            task_id="recent",
            user_request="yesterday",
            outcome="completed",
            timestamp=(CLOCK - timedelta(days=1)).isoformat(),
        )

        removed = logger.prune(now=CLOCK)

        self.assertEqual(removed, 1)
        self.assertEqual([row.task_id for row in logger.entries()], ["recent"])
        self.assertEqual(logger.count(), 1)

    def test_an_unreadable_timestamp_is_kept_rather_than_deleted(self) -> None:
        logger = AuditLogger(
            sink=InMemoryAuditSink(),
            retention=RetentionPolicy(retention_days=1, max_records=0),
            clock=lambda: CLOCK,
        )
        logger.record(task_id="odd", user_request="x", timestamp="not a date")

        self.assertEqual(logger.prune(now=CLOCK), 0)
        self.assertEqual(len(logger.entries()), 1)

    def test_the_hard_cap_bounds_the_trail_however_old_it_is(self) -> None:
        logger = AuditLogger(
            sink=InMemoryAuditSink(),
            retention=RetentionPolicy(retention_days=0, max_records=2),
            clock=lambda: CLOCK,
        )
        for index in range(4):
            logger.record(task_id=f"t{index}", user_request="x")

        kept = logger.entries()
        self.assertEqual(len(kept), 2)
        self.assertEqual([row.task_id for row in kept], ["t2", "t3"])

    def test_report_describes_the_trail_without_leaking_it(self) -> None:
        self.logger.record(
            task_id="t",
            user_request="set password=hunter2",
            outcome="completed",
        )

        report = self.logger.report()

        self.assertEqual(report["records"], 1)
        self.assertEqual(report["sink"], "memory")
        self.assertTrue(report["local_only"])
        self.assertEqual(report["retention"]["retention_days"], 30)
        self.assertEqual(report["redaction_marker"], REDACTED)
        self.assertGreaterEqual(report["redactions_total"], 1)
        self.assertNotIn("hunter2", json.dumps(report))


class JsonlAuditSinkTests(unittest.TestCase):
    """The durable trail is a local JSONL file that survives a rewrite."""

    def test_records_round_trip_through_the_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = f"{temp_dir}/{AUDIT_DIRECTORY}/{AUDIT_FILENAME}"
            logger = AuditLogger(sink=JsonlAuditSink(path), clock=lambda: CLOCK)

            logger.record(task_id="a", user_request="one", outcome="completed")
            logger.record(task_id="b", user_request="two", outcome="failed")

            reopened = AuditLogger(sink=JsonlAuditSink(path), clock=lambda: CLOCK)
            self.assertEqual([row.task_id for row in reopened.entries()], ["a", "b"])

    def test_a_damaged_line_does_not_make_the_trail_unreadable(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = f"{temp_dir}/{AUDIT_DIRECTORY}/{AUDIT_FILENAME}"
            sink = JsonlAuditSink(path)
            sink.append({"id": "1", "task_id": "good", "timestamp": CLOCK.isoformat()})
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("{not json\n")

            rows = sink.read()

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["task_id"], "good")

    def test_delete_and_clear_rewrite_the_file_without_what_was_removed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            sink = JsonlAuditSink(f"{temp_dir}/audit/audit.jsonl")
            sink.append({"id": "1", "task_id": "a"})
            sink.append({"id": "2", "task_id": "b"})

            self.assertTrue(sink.delete("1"))
            self.assertFalse(sink.delete("missing"))
            self.assertEqual([row["task_id"] for row in sink.read()], ["b"])
            self.assertEqual(sink.clear(), 1)
            self.assertEqual(sink.read(), [])

    def test_a_sink_creates_its_parent_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            sink = JsonlAuditSink(f"{temp_dir}/nested/audit.jsonl")
            sink.append({"id": "1"})

            self.assertTrue((sink.path).exists())


class JsonSafeTests(unittest.TestCase):
    def test_a_json_safe_copy_replaces_what_cannot_serialize(self) -> None:
        class Odd:
            def __repr__(self) -> str:
                return "<odd>"

        safe = json_safe({"ok": 1, "odd": Odd(), "items": (1, 2), "nested": {"x": {1, 2}}})

        self.assertEqual(safe["ok"], 1)
        self.assertEqual(safe["items"], [1, 2])
        self.assertIsInstance(safe["odd"], str)
        self.assertIsInstance(safe["nested"]["x"], list)
        # The result must actually serialize.
        json.dumps(safe)

    def test_a_record_round_trips_through_its_dict(self) -> None:
        row = AuditRecord(
            task_id="t",
            user_request="hi",
            decision={"route": "chat"},
            plan=({"step_id": "s1", "action": "x"},),
            tools=("a",),
            permission_decisions=({"allow": True},),
        )

        restored = AuditRecord.from_dict(row.to_dict())

        self.assertEqual(restored.task_id, "t")
        self.assertEqual(restored.decision, {"route": "chat"})
        self.assertEqual(restored.plan[0]["step_id"], "s1")
        self.assertEqual(restored.permission_decisions[0]["allow"], True)


if __name__ == "__main__":
    unittest.main()
