"""NovaForge NF-03: timeline ordering, persistence, evidence and replay safety.

These tests use the same injected desktop seams as the NF-01/NF-02 suites (a
fake window + a capture that writes a real PNG), so the contract under test is
the timeline's: ordered events, bounded retention, verified persistence,
resolvable evidence, and a read-only replay that can never drive a session.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from novacontrol.novaforge import (
    MAX_EVENTS_PER_SESSION,
    NovaForgeManager,
    SessionRecord,
    SessionState,
    TimelineLog,
    TimelineQueryError,
    classify,
    derived_events,
    timeline_namespace,
)
from novacontrol.novaforge.models import FrameRecord, MissionConfig, ObservationRecord
from novacontrol.novaforge.timeline import (
    EVENT_SPECS,
    ORDERING_NOTE,
    TimelineEvent,
    compare,
    filter_events,
    frame_ref,
    observation_ref,
    paginate,
    timeline_namespace as namespace,
)
from novacontrol.persistence import JsonStateStore

from test_novaforge import FakeDesktop, _mission, _wait_for

WINDOW_TITLE = "NC FORGE TEST - disposable"


class DroppingStore(JsonStateStore):
    """A store that accepts writes and keeps none of them (the real failure mode)."""

    def __init__(self, root: str | Path, *, drop: bool = True) -> None:
        super().__init__(root)
        self.drop = drop

    def write(self, name: str, payload: dict[str, Any]) -> None:
        if self.drop:
            return
        super().write(name, payload)


def _log(*, started: float | None = 100.0) -> TimelineLog:
    return TimelineLog(session_id="a" * 32, started_monotonic=started)


def _event_record() -> SessionRecord:
    """A stopped session record with one frame, an error and an intervention."""
    record = SessionRecord(mission=MissionConfig("Probe", "observe", WINDOW_TITLE))
    record.transition(SessionState.STARTING)
    record.started_at = record.history[-1]["at"]
    record.transition(SessionState.RUNNING, reason="started")
    stamp = datetime.now(UTC)
    record.record_frame(
        FrameRecord(
            seq=1,
            captured_at=stamp.isoformat(),
            path=str(Path("missing") / "000001.png"),
            width=1000,
            height=600,
            size_bytes=1234,
            capture_ms=5.0,
            capture_mode="persistent",
            fresh=True,
            source="window_crop",
        )
    )
    record.record_observation(
        ObservationRecord(
            index=1,
            at=stamp.isoformat(),
            source="desktop_capture",
            summary="1000x600 frame",
            evidence=True,
            latency_ms=5.0,
            frame_seq=1,
        )
    )
    record.record_observation(
        ObservationRecord(
            index=2,
            at=(stamp + timedelta(milliseconds=10)).isoformat(),
            source="desktop_capture",
            summary="Capture failed",
            evidence=False,
            latency_ms=1.0,
            note="backend exploded",
        )
    )
    record.record_error("Capture failed: RuntimeError: backend exploded", where="capture")
    record.add_intervention("target", "target_lost", "paused", detail="window closed")
    record.record_error("A weird thing happened", where="something_else")
    record.transition(SessionState.STOPPING)
    record.transition(SessionState.STOPPED)
    record.ended_at = (stamp + timedelta(seconds=2)).isoformat()
    record.end_reason = "time_limit"
    record.input_cleanup = {"ok": True, "held_after": 0, "presses": 0, "releases": 0}
    return record


class TimelineModelTests(unittest.TestCase):
    """The model itself: vocabulary, ordering, dedupe, bounds and filtering."""

    def test_an_unknown_event_type_is_refused(self) -> None:
        log = _log()
        # ``action.executed`` is a REAL type since the remediation; the pin is
        # that an unregistered one is refused rather than silently recorded.
        with self.assertRaises(ValueError):
            log.append("action.teleported", payload={})
        self.assertEqual(log.events, [])

    def test_sequence_identifiers_and_elapsed_are_assigned_in_order(self) -> None:
        log = _log(started=100.0)
        first = log.append("session.created", now_monotonic=100.5)
        second = log.append("session.started", now_monotonic=101.25)
        third = log.append("frame.captured", payload={"frame_seq": 1}, now_monotonic=102.0)
        self.assertEqual([first.seq, second.seq, third.seq], [1, 2, 3])
        self.assertEqual(len({first.event_id, second.event_id, third.event_id}), 3)
        self.assertEqual([first.elapsed_ms, second.elapsed_ms, third.elapsed_ms], [500.0, 1250.0, 2000.0])
        self.assertEqual(first.session_id, log.session_id)
        self.assertTrue(first.at)
        # The ordering note is explicit that the wall clock is only a label.
        self.assertIn("wall-clock label", ORDERING_NOTE)

    def test_a_duplicate_event_id_is_ingested_once(self) -> None:
        log = _log()
        first = log.append("session.created", event_id="evt-1")
        again = log.append("session.created", event_id="evt-1")
        self.assertIs(first, again)
        self.assertEqual(len(log.events), 1)
        self.assertEqual(log.append("session.started").seq, 2)

    def test_concurrent_appends_have_unique_contiguous_sequences(self) -> None:
        log = _log(started=0.0)

        async def append(index: int) -> None:
            await asyncio.sleep(0)
            log.append("frame.rejected", payload={"reason": f"stale {index}"}, now_monotonic=float(index))

        async def run() -> None:
            await asyncio.gather(*(append(index) for index in range(60)))

        asyncio.run(run())
        sequences = sorted(event.seq for event in log.events)
        self.assertEqual(sequences, list(range(1, 61)))
        self.assertEqual(len({event.event_id for event in log.events}), 60)
        self.assertEqual(log.next_seq, 61)

    def test_the_log_is_bounded_and_reports_what_it_dropped(self) -> None:
        log = _log(started=0.0)
        for index in range(MAX_EVENTS_PER_SESSION + 7):
            log.append("frame.rejected", payload={"reason": f"stale {index}"}, now_monotonic=float(index))
        self.assertEqual(len(log.events), MAX_EVENTS_PER_SESSION)
        self.assertEqual(log.dropped, 7)
        self.assertEqual(log.events[0].seq, 8)
        self.assertEqual(log.next_seq, MAX_EVENTS_PER_SESSION + 8)
        restored = TimelineLog.from_dict(log.to_dict(), session_id=log.session_id)
        self.assertEqual(len(restored.events), MAX_EVENTS_PER_SESSION)
        self.assertEqual(restored.dropped, 7)

    def test_filtering_and_pagination_keep_event_identity_and_order(self) -> None:
        log = _log(started=0.0)
        for index in range(10):
            log.append(
                "frame.rejected" if index % 2 == 0 else "guardian.intervention",
                payload={"reason": str(index)},
                now_monotonic=float(index),
            )
        selected = filter_events(log.events, categories=["capture"])
        self.assertEqual([event.seq for event in selected], [1, 3, 5, 7, 9])
        page, info = paginate(log.events, after_seq=4, limit=3)
        self.assertEqual([event.seq for event in page], [5, 6, 7])
        self.assertEqual(info["next_after_seq"], 7)
        self.assertGreater(info["has_more"], 0)
        self.assertEqual(info["total"], 10)
        self.assertEqual(len(log.filtered(types=["frame.rejected"])), 5)

    def test_a_malformed_persisted_event_is_skipped_not_repaired(self) -> None:
        log = _log()
        log.append("session.created")
        payload = log.to_dict()
        payload["events"].append({"type": "made.up", "seq": 2})
        payload["events"].append({"type": "session.started", "seq": "nope"})
        payload["events"].append({"type": "session.started", "seq": 3, "session_id": "b" * 32})
        restored = TimelineLog.from_dict(payload, session_id=log.session_id)
        self.assertEqual([event.seq for event in restored.events], [1])

    def test_an_unrepresentable_number_or_deep_nesting_cannot_crash_a_load(self) -> None:
        """A corrupted snapshot is bounded, never an exception during boot.

        JSON parses an arbitrarily long integer exactly and ``float()`` refuses
        one that does not fit, so ``TimelineLog.from_dict`` raised
        ``OverflowError`` out of the manager's constructor; deep nesting is the
        same defect one dimension over. The event itself is not malformed and
        is kept — only the unusable figure reads as None and the nested value is
        a bounded placeholder.
        """
        payload = {
            "version": 1,
            "session_id": "a" * 32,
            "next_seq": 2,
            "events": [
                {
                    "type": "frame.captured",
                    "seq": 1,
                    "session_id": "a" * 32,
                    "payload": {"huge": int("9" * 400), "nested": [[[[["deep"]]]]]},
                }
            ],
        }
        restored = TimelineLog.from_dict(payload, session_id="a" * 32)
        self.assertEqual(len(restored.events), 1)
        self.assertIsNone(restored.events[0].payload["huge"])
        self.assertEqual(restored.events[0].payload["nested"], [["…"]])

    def test_a_store_namespace_requires_a_generated_session_id(self) -> None:
        self.assertEqual(namespace("a" * 32), "novaforge_timeline_" + "a" * 32)
        self.assertIsNone(namespace("../../etc/passwd"))
        self.assertIsNone(namespace("not-hex"))
        self.assertIsNone(namespace("a" * 31))

    def test_elapsed_is_unknown_rather_than_invented_without_a_monotonic_origin(self) -> None:
        log = TimelineLog(session_id="c" * 32, started_monotonic=None)
        event = log.append("session.created")
        self.assertIsNone(event.elapsed_ms)

    def test_a_derived_timeline_marks_every_event_and_orders_by_timestamp(self) -> None:
        record = _event_record()
        events = derived_events(record)
        self.assertTrue(events)
        self.assertTrue(all(event.derived for event in events))
        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))
        self.assertTrue(all(event.elapsed_ms is None for event in events))
        timestamps = [event.at for event in events]
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertEqual(
            [event.type for event in events].count("frame.captured"), 1
        )
        self.assertIn("observation.recorded", [event.type for event in events])
        self.assertIn("guardian.intervention", [event.type for event in events])
        self.assertIn("input.cleanup", [event.type for event in events])

    def test_classification_names_only_evidence_backed_categories(self) -> None:
        record = _event_record()
        events = derived_events(record)
        result = classify(record, events)
        categories = {entry["category"] for entry in result["classifications"]}
        self.assertIn("capture_failure", categories)
        self.assertIn("target_loss", categories)
        self.assertIn("timeout", categories)
        self.assertIn("unknown", categories)
        self.assertEqual(result["outcome"], "classified")
        self.assertFalse(result["completion"]["verified"])
        self.assertFalse(result["completion"]["claimed"])
        for entry in result["classifications"]:
            self.assertTrue(entry["evidence"], entry["category"])
            if entry["category"] != "unknown":
                self.assertTrue(entry["direct"])
        capture = next(item for item in result["classifications"] if item["category"] == "capture_failure")
        # A LIVE timeline raises capture.failed; a derived one can only cite the
        # recorded error whose ``where`` says the capture was what failed.
        self.assertTrue(
            any(
                ref["type"] in {"capture.failed", "frame.rejected", "error.recorded"}
                for ref in capture["evidence"]
            )
        )

    def test_a_clean_session_is_not_called_successful(self) -> None:
        record = SessionRecord(mission=MissionConfig("Probe", "observe", WINDOW_TITLE))
        result = classify(record, [])
        self.assertEqual(result["outcome"], "no_failure_recorded")
        self.assertEqual(result["classifications"], [])
        self.assertIn("not the same as success", result["detail"])
        self.assertFalse(result["completion"]["verified"])

    def test_comparison_reports_unmeasured_rows_as_unavailable(self) -> None:
        left = _event_record()
        right = _event_record()
        right.end_reason = "user"
        telemetry = {
            "capture": {
                "frames": 3,
                "stale_frames": 0,
                "capture_failures": 1,
                "frame_gaps": 0,
                "latency_ms": {"median_ms": 5.0, "p95_ms": None, "p95_available": False,
                               "p95_reason": "Only 3 latency sample(s) are available; a p95 needs at least 20."},
                "max_gap_ms": None,
            },
            "session": {"duration_seconds": 2.0, "active_seconds": 2.0, "paused_seconds": 0.0},
            "target": {"losses": 0, "changes": 0},
        }
        other = {
            "capture": {
                "frames": 9,
                "stale_frames": 1,
                "capture_failures": 0,
                "frame_gaps": 1,
                "latency_ms": {"median_ms": 7.0, "p95_ms": 9.0, "p95_available": True,
                               "p95_reason": ""},
                "max_gap_ms": 2200.0,
            },
            "session": {"duration_seconds": 4.0, "active_seconds": 3.0, "paused_seconds": 1.0},
            "target": {"losses": 1, "changes": 0},
        }
        result = compare(left, telemetry, right, other)
        rows = {row["key"]: row for row in result["rows"]}
        self.assertEqual(rows["frames"]["left"], 3)
        self.assertEqual(rows["frames"]["right"], 9)
        self.assertEqual(rows["frames"]["delta"], 6.0)
        self.assertFalse(rows["actions_executed"]["available"])
        self.assertIsNone(rows["actions_executed"]["left"])
        self.assertTrue(rows["actions_executed"]["reason"])
        # One side measured a p95, so the row is available and the unmeasured
        # side is None; the reason names why a p95 was withheld there.
        self.assertTrue(rows["latency_p95_ms"]["available"])
        self.assertIsNone(rows["latency_p95_ms"]["left"])
        self.assertEqual(rows["latency_p95_ms"]["right"], 9.0)
        self.assertIsNone(rows["latency_p95_ms"]["delta"])
        self.assertIn("p95 needs at least 20", rows["latency_p95_ms"]["reason"])
        self.assertFalse(rows["verified"]["available"])
        self.assertTrue(result["read_only"])


class TimelineManagerTests(unittest.IsolatedAsyncioTestCase):
    """The manager's integration: lifecycle events, persistence and evidence."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.desktop = FakeDesktop()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def manager(self, **overrides: Any) -> NovaForgeManager:
        options: dict[str, Any] = {
            "data_dir": self.data_dir,
            "capture": self.desktop.capture,
            "geometry": self.desktop.geometry,
            "window_lines": self.desktop.window_lines,
            "release_inputs": self.desktop.release,
            "frame_interval": 0.05,
            "pause_timeout": 2.0,
            "stop_timeout": 4.0,
        }
        options.update(overrides)
        return NovaForgeManager(**options)

    async def _one_session(
        self, manager: NovaForgeManager, *, frames: int = 2, timeout: float = 3.0
    ) -> str:
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(
            lambda: len(manager.get(session_id).frames) >= frames,
            timeout=timeout,
            message=f"{frames} frames",
        )
        await manager.stop(session_id)
        return session_id

    async def test_a_session_records_ordered_lifecycle_frame_and_close_events(self) -> None:
        manager = self.manager()
        session_id = await self._one_session(manager)
        payload = manager.timeline_payload(session_id, limit=200)
        self.assertEqual(payload["source"], "recorded")
        self.assertFalse(payload["source_note"].startswith("derived"))
        events = payload["events"]
        types = [event["type"] for event in events]
        for expected in (
            "session.created",
            "session.started",
            "frame.captured",
            "session.stopped",
            "input.cleanup",
            "telemetry.snapshot",
        ):
            self.assertIn(expected, types)
        self.assertNotIn("action.executed", types)
        self.assertNotIn("action.verified", types)
        sequences = [event["seq"] for event in events]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len(sequences), len(set(sequences)))
        self.assertTrue(all(event["session_id"] == session_id for event in events))
        self.assertTrue(all(event["elapsed_ms"] is not None for event in events))
        frame_event = next(event for event in events if event["type"] == "frame.captured")
        self.assertEqual(frame_event["payload"]["frame_seq"], 1)
        self.assertTrue(frame_event["evidence_available"])
        self.assertTrue(any(ref["kind"] == "frame" for ref in frame_event["evidence"]))
        self.assertEqual(payload["read_only"], True)
        self.assertFalse(payload["replay"]["can_start"])
        self.assertFalse(payload["replay"]["can_execute_action"])

    async def test_the_timeline_and_its_persistence_survive_a_restart(self) -> None:
        manager = self.manager()
        session_id = await self._one_session(manager)
        before = manager.timeline_payload(session_id, limit=200)
        self.assertEqual(before["persistence"]["state"], "verified", before["persistence"])

        restarted = self.manager()
        after = restarted.timeline_payload(session_id, limit=200)
        self.assertEqual(after["source"], "recorded")
        self.assertEqual([event["seq"] for event in after["events"]],
                         [event["seq"] for event in before["events"]])
        self.assertEqual(after["persistence"]["state"], "verified")

    async def test_a_dropped_store_write_is_reported_not_hidden(self) -> None:
        store = DroppingStore(self.data_dir)
        manager = self.manager(store=store)
        session_id = await self._one_session(manager)
        payload = manager.timeline_payload(session_id, limit=200)
        self.assertEqual(payload["persistence"]["state"], "failed")
        self.assertFalse(payload["persistence"]["ok"])
        self.assertIn("did not read back", payload["persistence"]["reason"])
        types = [event["type"] for event in payload["events"]]
        self.assertIn("timeline.persistence_failed", types)
        record = manager.get(session_id)
        self.assertTrue(any("timeline snapshot" in error["message"] for error in record.errors))
        self.assertIn("persistence_failure", {
            entry["category"] for entry in payload["classification"]["classifications"]
        })

    async def test_expired_frame_evidence_stays_inspectable_with_a_reason(self) -> None:
        manager = self.manager()
        session_id = await self._one_session(manager)
        payload = manager.timeline_payload(session_id, limit=200)
        frame_event = next(event for event in payload["events"] if event["type"] == "frame.captured")
        reference = next(ref for ref in frame_event["evidence"] if ref["kind"] == "frame")
        self.assertTrue(reference["available"])
        path = manager.frame_path(session_id, seq=reference["seq"])
        self.assertIsNotNone(path)
        path.unlink()

        after = manager.timeline_payload(session_id, limit=200)
        event = next(
            item for item in after["events"] if item["seq"] == frame_event["seq"]
        )
        expired = next(ref for ref in event["evidence"] if ref["kind"] == "frame")
        self.assertFalse(expired["available"])
        self.assertIn("no longer retained", expired["reason"])
        self.assertEqual(event["type"], "frame.captured")

    async def test_pagination_and_filters_work_over_a_real_timeline(self) -> None:
        manager = self.manager()
        session_id = await self._one_session(manager, frames=3)
        first = manager.timeline_payload(session_id, limit=2)
        self.assertEqual(first["page"]["count"], 2)
        self.assertTrue(first["page"]["has_more"])
        second = manager.timeline_payload(session_id, limit=2, after_seq=first["page"]["next_after_seq"])
        self.assertEqual(second["events"][0]["seq"], 3)
        self.assertEqual(
            [event["seq"] for event in (first["events"] + second["events"])[:3]],
            [1, 2, 3],
        )
        capture = manager.timeline_payload(session_id, limit=50, categories=["capture"])
        self.assertTrue(capture["events"])
        self.assertTrue(all(event["category"] == "capture" for event in capture["events"]))
        with self.assertRaises(TimelineQueryError):
            manager.timeline_payload(session_id, categories=["nonsense"])
        with self.assertRaises(TimelineQueryError):
            manager.timeline_payload(session_id, limit=0)

    async def test_a_later_event_keeps_its_sequence_after_a_filter(self) -> None:
        manager = self.manager()
        session_id = await self._one_session(manager)
        full = manager.timeline_payload(session_id, limit=200)
        filtered = manager.timeline_payload(session_id, limit=200, severities=["info"])
        identities = {event["event_id"]: event["seq"] for event in full["events"]}
        for event in filtered["events"]:
            self.assertEqual(identities[event["event_id"]], event["seq"])

    async def test_a_legacy_session_gets_a_derived_timeline(self) -> None:
        manager = self.manager()
        session_id = await self._one_session(manager)
        manager.store.write(namespace(session_id), {})  # simulate an NF-02 session
        legacy = self.manager()
        payload = legacy.timeline_payload(session_id, limit=200)
        self.assertEqual(payload["source"], "derived")
        self.assertTrue(payload["events"])
        self.assertTrue(all(event["derived"] for event in payload["events"]))
        self.assertTrue(all(event["elapsed_ms"] is None for event in payload["events"]))
        self.assertEqual(payload["persistence"]["state"], "not_stored")
        self.assertIn("reconstructed", payload["persistence"]["reason"])
        self.assertIn("timestamp", payload["limitations"][0])

    async def test_a_recorded_timeline_lists_only_the_limitations_that_apply(self) -> None:
        """The derived-order caveat must not appear beside a recorded timeline."""
        manager = self.manager()
        session_id = await self._one_session(manager, frames=1)
        payload = manager.timeline_payload(session_id, limit=200)
        self.assertEqual(payload["source"], "recorded")
        limitations = " ".join(payload["limitations"])
        self.assertNotIn("derived timeline", limitations.lower())
        self.assertIn("no action or verification stages", limitations)
        self.assertIn("elapsed_ms is null", limitations)

    async def test_a_target_loss_is_recorded_and_classified(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="a frame")
        self.desktop.window = None
        await _wait_for(
            lambda: manager.get(session_id).state is SessionState.PAUSED, message="target pause"
        )
        payload = manager.timeline_payload(session_id, limit=200)
        types = [event["type"] for event in payload["events"]]
        self.assertIn("target.lost", types)
        self.assertIn("guardian.intervention", types)
        categories = {entry["category"] for entry in payload["classification"]["classifications"]}
        self.assertIn("target_loss", categories)
        await manager.stop(session_id)

    async def test_a_capture_failure_has_an_event_and_a_classification(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="a frame")
        self.desktop.fail = True
        await _wait_for(
            lambda: any("Capture failed" in error["message"] for error in manager.get(session_id).errors),
            message="capture error",
        )
        await manager.stop(session_id)
        payload = manager.timeline_payload(session_id, limit=200)
        failure = next(event for event in payload["events"] if event["type"] == "capture.failed")
        self.assertEqual(failure["severity"], "error")
        self.assertTrue(failure["evidence_available"])
        classification = next(
            entry for entry in payload["classification"]["classifications"]
            if entry["category"] == "capture_failure"
        )
        self.assertTrue(classification["direct"])
        self.assertTrue(any(ref["seq"] == failure["seq"] for ref in classification["evidence"]))

    def test_an_observation_reference_resolves_to_the_observation_it_named(self) -> None:
        """An index is not an identity once the record trims its observation list.

        ``SessionRecord.record_observation`` keeps the newest 30 entries and
        numbers the next one from the current list length, so a long session
        holds many observations sharing ``index: 31``. Resolving by index alone
        returned the first (oldest retained) match — a different observation
        than the one the event referenced. The timestamp is the identity.
        """
        manager = self.manager()
        record = SessionRecord(mission=MissionConfig("Probe", "observe", WINDOW_TITLE))
        for number in range(32):
            record.record_observation(
                ObservationRecord(
                    index=len(record.observations) + 1,
                    at=f"2026-09-18T10:00:{number:02d}+00:00",
                    source="desktop_capture",
                    summary=f"observation-{number + 1}",
                    evidence=True,
                    latency_ms=1.0,
                    frame_seq=number + 1,
                )
            )
        newest = record.observations[-1]
        self.assertEqual(sum(item.index == newest.index for item in record.observations), 2)
        resolved = manager._resolve_ref(
            record,
            observation_ref(record.session_id, newest.index, newest.at),
        )
        self.assertTrue(resolved["available"], resolved)
        self.assertEqual(resolved["summary"], "observation-32")
        self.assertEqual(resolved["frame_seq"], 32, "resolved to a different observation")
        index_only = observation_ref(record.session_id, newest.index)
        ambiguous = manager._resolve_ref(record, index_only)
        self.assertFalse(ambiguous["available"])
        self.assertIn("ambiguous", ambiguous["reason"])

    def test_an_ambiguous_intervention_reference_is_reported_not_substituted(self) -> None:
        manager = self.manager()
        record = SessionRecord(mission=MissionConfig("Probe", "observe", WINDOW_TITLE))
        stamp = "2026-09-18T10:00:00+00:00"
        record.add_intervention("stop", "user", "requested", detail="first", at=stamp)
        record.add_intervention("cancel", "user", "stopped", detail="second", at=stamp)
        resolved = manager._resolve_ref(
            record,
            {"kind": "intervention", "session_id": record.session_id, "index": 0, "at": stamp},
        )
        self.assertFalse(resolved["available"])
        self.assertIn("ambiguous", resolved["reason"])
        self.assertNotIn("no timestamp", resolved["reason"])

    async def test_a_corrupt_frame_is_reported_by_its_own_reference(self) -> None:
        """A file that exists but is not a PNG is never swapped for another."""
        manager = self.manager()
        session_id = await self._one_session(manager, frames=2)
        record = manager.get(session_id)
        first = record.frames[0]
        Path(first.path).write_bytes(b"this is not a PNG")
        resolved = manager._resolve_ref(record, frame_ref(session_id, first.seq))
        self.assertTrue(resolved["available"], resolved)
        self.assertIn(f"seq={first.seq}", resolved["url"])
        self.assertEqual(resolved["captured_at"], first.captured_at)

    async def test_an_intervention_reference_survives_the_bounded_list_reindexing(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="a frame")
        record = manager.get(session_id)
        record.add_intervention("target", "target_changed", "paused", detail="first")
        second = record.add_intervention("target", "target_lost", "paused", detail="second")
        reference = {
            "kind": "intervention",
            "session_id": session_id,
            "index": len(record.interventions) - 1,
            "at": second["at"],
        }
        # The bounded list dropping its oldest entry shifts every index by one.
        record.interventions = record.interventions[1:]
        resolved = manager._resolve_ref(record, reference)
        self.assertTrue(resolved["available"], resolved)
        self.assertEqual(resolved["entry"]["at"], second["at"])
        self.assertEqual(resolved["entry"]["detail"], "second")
        untimed = {key: value for key, value in reference.items() if key != "at"}
        unresolved = manager._resolve_ref(record, untimed)
        self.assertFalse(unresolved["available"])
        self.assertIn("timestamp", unresolved["reason"])
        await manager.stop(session_id)

    def test_derived_intervention_events_carry_the_timestamp_that_identifies_them(self) -> None:
        record = SessionRecord(mission=MissionConfig("Probe", "observe", WINDOW_TITLE))
        entry = record.add_intervention("target", "target_lost", "paused", detail="closed")
        event = next(item for item in derived_events(record) if item.type == "guardian.intervention")
        self.assertEqual(event.evidence[0]["at"], entry["at"])

    async def test_comparison_and_classification_are_read_only(self) -> None:
        manager = self.manager()
        left = await self._one_session(manager, frames=1)
        right = await self._one_session(manager, frames=3)
        result = manager.compare_sessions(left, right)
        self.assertTrue(result["read_only"])
        self.assertEqual(result["left"]["session_id"], left)
        self.assertEqual(result["right"]["session_id"], right)
        rows = {row["key"]: row for row in result["rows"]}
        self.assertGreaterEqual(rows["frames"]["left"], 1)
        self.assertGreater(rows["frames"]["right"], rows["frames"]["left"])
        self.assertFalse(rows["actions_executed"]["available"])
        self.assertEqual(result["left"]["timeline"]["source"], "recorded")
        self.assertIn("classifications", result["left"]["classification"])
        # Comparison never mutates either session.
        self.assertIs(manager.get(left).state, SessionState.STOPPED)
        self.assertIs(manager.get(right).state, SessionState.STOPPED)

    async def test_an_evicted_session_takes_its_timeline_with_it(self) -> None:
        manager = self.manager(max_sessions_kept=1)
        first = await self._one_session(manager, frames=1)
        second = await self._one_session(manager, frames=1)
        self.assertIsNone(manager.get(first))
        self.assertEqual(manager.store.read(namespace(first)).get("cleared"), True)
        with self.assertRaises(KeyError):
            manager.timeline_payload(first)
        self.assertTrue(manager.timeline_payload(second)["events"])

    async def test_a_reconciled_session_appends_the_restart_facts(self) -> None:
        manager = self.manager()
        # Persist a RUNNING record plus its timeline the way a killed process
        # would leave them, without a loop that could overwrite the snapshot.
        record = SessionRecord(mission=MissionConfig("Probe", "observe", WINDOW_TITLE))
        record.transition(SessionState.STARTING)
        record.started_at = record.history[-1]["at"]
        record.transition(SessionState.RUNNING, reason="started")
        session_id = record.session_id
        manager._sessions[session_id] = record
        manager._current_id = session_id
        log = TimelineLog(session_id=session_id, started_monotonic=100.0)
        log.append("session.created")
        log.append("session.started")
        manager._timelines[session_id] = log
        manager._timeline_dirty.add(session_id)
        manager._persist()

        restarted = self.manager()
        self.assertIs(restarted.get(session_id).state, SessionState.STOPPED)
        payload = restarted.timeline_payload(session_id, limit=200)
        types = [event["type"] for event in payload["events"]]
        self.assertIn("session.reconciled", types)
        self.assertIn("input.cleanup", types)
        reconciled = next(event for event in payload["events"] if event["type"] == "session.reconciled")
        self.assertIsNone(reconciled["elapsed_ms"], "a restart loses the monotonic origin")

    async def test_shutdown_flushes_evidence_when_the_stop_path_fails(self) -> None:
        """A failed stop must not be how the end of the evidence is lost."""
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="a frame")
        task = manager._task

        async def broken_stop(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("the stop path exploded")

        with patch.object(NovaForgeManager, "stop", broken_stop):
            await manager.shutdown()  # must not raise
        stored = manager.store.read(namespace(session_id))
        types = [event.get("type") for event in (stored.get("events") or [])]
        self.assertIn("frame.captured", types, "recorded evidence must be flushed to the store")
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def test_the_timeline_never_fails_a_session(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="a frame")
        record = manager.get(session_id)

        def explode(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("timeline exploded")

        from unittest import mock

        with mock.patch.object(NovaForgeManager, "_timeline_for", explode):
            stopped = await manager.stop(session_id)
        self.assertEqual(stopped["state"], SessionState.STOPPED.value)
        self.assertEqual(stopped["input_cleanup"]["ok"], True)


class ReplaySafetyTests(unittest.TestCase):
    """Replay is read-only: nothing in its server or panel path can drive a session."""

    def test_the_timeline_module_has_no_lifecycle_calls(self) -> None:
        source = Path("src/novacontrol/novaforge/timeline.py").read_text(encoding="utf-8")
        for forbidden in (".start(", ".pause(", ".resume(", ".stop(", ".cancel(", ".emergency_stop("):
            self.assertNotIn(forbidden, source, f"timeline.py must not call {forbidden}")
        self.assertNotIn("release_inputs", source)
        self.assertNotIn("subprocess", source)

    def test_timeline_routes_are_get_only_and_authenticated(self) -> None:
        from novacontrol.api.models import ApiSurface

        routes = {(route.method, route.path): route for route in ApiSurface.default().routes}
        timeline_routes = {
            key: route for key, route in routes.items() if "/timeline" in key[1]
        }
        self.assertEqual(len(timeline_routes), 3)
        for (method, _path), route in timeline_routes.items():
            self.assertEqual(method, "GET", "replay must not accept a mutating method")
            self.assertTrue(route.authenticated)

    def test_the_handler_only_reads_the_manager(self) -> None:
        source = Path("src/novacontrol/api/app.py").read_text(encoding="utf-8")
        start = source.index('@app.get("/novaforge/sessions/{session_id}/timeline")')
        end = source.index('@app.get("/novaforge/frames/{session_id}")')
        block = source[start:end]
        for forbidden in ("novaforge.start", "novaforge.pause", "novaforge.resume",
                          "novaforge.stop", "novaforge.cancel", "novaforge.emergency_stop",
                          "novaforge.timeline_payload", "novaforge.compare_sessions"):
            if forbidden.endswith("timeline_payload") or forbidden.endswith("compare_sessions"):
                self.assertIn(forbidden, block)
            else:
                self.assertNotIn(forbidden, block, f"replay handler calls {forbidden}")


class SchemaCompatibilityTests(unittest.TestCase):
    """A stored event keeps its meaning across versions, or is refused."""

    def test_round_trip_preserves_every_field(self) -> None:
        log = _log(started=0.0)
        original = log.append(
            "frame.captured",
            payload={"frame_seq": 4, "width": 100},
            evidence=[{"kind": "frame", "session_id": log.session_id, "seq": 4}],
            now_monotonic=1.5,
        )
        restored = TimelineEvent.from_dict(original.to_dict())
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.to_dict(), original.to_dict())

    def test_a_future_version_is_read_but_its_version_is_reported(self) -> None:
        payload = TimelineEvent(
            type="session.created", seq=1, session_id="a" * 32, event_id="e",
            category="lifecycle", severity="info", source="session", at="now",
        ).to_dict()
        payload["version"] = 99
        restored = TimelineEvent.from_dict(payload)
        assert restored is not None
        self.assertEqual(restored.version, 99)

    def test_payloads_are_bounded(self) -> None:
        log = _log()
        event = log.append(
            "error.recorded",
            payload={"message": "x" * 5000, "items": list(range(100))},
            severity="error",
        )
        self.assertLessEqual(len(event.payload["message"]), 300)
        self.assertLessEqual(len(event.payload["items"]), 20)

    def test_a_log_written_for_another_session_is_not_attached(self) -> None:
        other = TimelineLog(session_id="b" * 32)
        other.append("session.created")
        restored = TimelineLog.from_dict(other.to_dict(), session_id="a" * 32)
        self.assertEqual(restored.events, [])

    def test_json_serialisation_is_stable_for_the_store(self) -> None:
        log = _log(started=0.0)
        log.append("session.created")
        text = json.dumps(log.to_dict(), sort_keys=True)
        self.assertIn("session.created", text)
        self.assertEqual(json.loads(text)["version"], 1)


if __name__ == "__main__":
    unittest.main()
