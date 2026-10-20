"""NovaForge NF-01 tests: mission validation, lifecycle, evidence and persistence.

These use injected capture/geometry seams, never the real desktop: the contract
under test is the session manager's (state machine, freshness handling, cleanup,
persistence), and a fake window is enough to prove it. The real capture path is
verified separately by the disposable-window acceptance probe.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from PIL import Image

from novacontrol.novaforge import (
    MissionConfig,
    MissionValidationError,
    NovaForgeManager,
    SESSION_TERMINAL_STATES,
    SESSION_TRANSITIONS,
    SessionConflictError,
    SessionRecord,
    SessionState,
    SessionTransitionError,
)
from novacontrol.novaforge.guardian import (
    guardian_state,
    input_safety_decision,
    safety_state,
)
from novacontrol.novaforge.models import TargetWindow, utc_now
from novacontrol.novaforge.telemetry import (
    GAP_FLOOR_MS,
    LATENCY_WINDOW,
    P95_MIN_SAMPLES,
    TelemetrySnapshot,
    gap_threshold_for,
)

WINDOW_TITLE = "NC FORGE TEST - disposable"


@dataclass
class FakeWindow:
    """The same shape the desktop layer's WindowGeometry provides."""

    title: str = WINDOW_TITLE
    handle: int = 0x1234
    pid: int = 4242
    bounds: tuple[int, int, int, int] = (100, 50, 1100, 650)
    client: tuple[int, int, int, int] = (0, 0, 1000, 600)
    scale: float = 1.0
    foreground: bool = True


class FakeDesktop:
    """A deterministic window + capture double that writes real PNG files."""

    def __init__(self, *, screen: tuple[int, int] = (1200, 800)) -> None:
        self.window: FakeWindow | None = FakeWindow()
        self.screen = screen
        self.captures: list[str] = []
        self.stale = False
        self.fail = False
        self.raise_lines = False
        self.raise_geometry = False
        self.release_calls = 0
        self.presses = 2
        self.releases = 2

    async def window_lines(self) -> list[str] | None:
        if self.raise_lines:
            raise RuntimeError("window probe exploded")
        if self.window is None:
            return ["other|Something Else"]
        return [f"python|{self.window.title}"]

    async def geometry(self, title: str) -> FakeWindow | None:
        if self.raise_geometry:
            raise RuntimeError("geometry probe exploded")
        if self.window is None or title.lower() not in self.window.title.lower():
            return None
        return self.window

    async def capture(self, path: str) -> dict[str, Any]:
        self.captures.append(path)
        if self.fail:
            raise RuntimeError("capture backend exploded")
        image = Image.new("RGB", self.screen, (12, 18, 30))
        image.save(path, format="PNG")
        return {
            "fresh": not self.stale,
            "verification": "stale file" if self.stale else "frame written now",
            "captured_at": datetime.now(UTC).isoformat(),
            "capture_ms": 7.5,
            "capture_mode": "persistent",
        }

    async def release(self) -> tuple[int, int]:
        self.release_calls += 1
        return self.presses, self.releases


async def _wait_for(predicate, *, timeout: float = 3.0, message: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out waiting for {message}.")


def _mission(**overrides: Any) -> dict[str, Any]:
    payload = {
        "name": "Probe Run",
        "objective": "Observe the disposable window for a few frames.",
        "target_title": "NC FORGE TEST",
        "success_criteria": "Frames are captured.",
    }
    payload.update(overrides)
    return payload


class MissionValidationTests(unittest.IsolatedAsyncioTestCase):
    def test_blank_required_fields_are_reported(self) -> None:
        mission = MissionConfig(name="", objective=" ", target_title="")
        self.assertEqual(len(mission.errors()), 3)
        self.assertIn("Mission name is required.", mission.errors())

    def test_limits_are_bounded(self) -> None:
        self.assertTrue(MissionConfig("n", "o", "t", time_limit_seconds=0).errors())
        self.assertTrue(MissionConfig("n", "o", "t", max_actions=0).errors())
        self.assertTrue(MissionConfig("n", "o", "t", time_limit_seconds=99999).errors())
        self.assertEqual(MissionConfig("n", "o", "t", time_limit_seconds=60, max_actions=10).errors(), [])

    def test_invalid_numeric_text_fails_instead_of_becoming_none(self) -> None:
        mission = MissionConfig.from_dict(
            {"name": "n", "objective": "o", "target_title": "t", "max_actions": "lots"}
        )
        self.assertEqual(mission.max_actions, -1)
        self.assertTrue(any("Action budget" in problem for problem in mission.errors()))

    async def test_validation_reports_a_missing_window(self) -> None:
        desktop = FakeDesktop()
        desktop.window = None
        manager = NovaForgeManager(
            data_dir=Path(self._tmp.name), capture=desktop.capture, geometry=desktop.geometry,
            window_lines=desktop.window_lines,
        )
        result = await manager.validate(_mission(target_title="Does Not Exist"))
        self.assertFalse(result["valid"])
        self.assertIn("No open window matches", result["errors"][0])

    async def test_validation_rejects_an_ambiguous_target(self) -> None:
        desktop = FakeDesktop()

        async def lines() -> list[str]:
            return ["a|NC FORGE TEST one", "b|NC FORGE TEST two"]

        manager = NovaForgeManager(
            data_dir=Path(self._tmp.name), capture=desktop.capture, geometry=desktop.geometry,
            window_lines=lines,
        )
        result = await manager.validate(_mission(target_title="NC FORGE TEST"))
        self.assertFalse(result["valid"])
        self.assertEqual(len(result["candidates"]), 2)
        self.assertIn("more specific title", result["errors"][0])

    async def test_validation_returns_the_measured_target(self) -> None:
        desktop = FakeDesktop()
        manager = NovaForgeManager(
            data_dir=Path(self._tmp.name), capture=desktop.capture, geometry=desktop.geometry,
            window_lines=desktop.window_lines,
        )
        result = await manager.validate(_mission())
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["target"]["handle"], 0x1234)
        self.assertEqual(result["target"]["width"], 1000)

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()


class StateMachineTests(unittest.TestCase):
    def test_every_legal_move_is_accepted(self) -> None:
        for source, targets in SESSION_TRANSITIONS.items():
            for target in targets:
                record = SessionRecord(mission=MissionConfig("n", "o", "t"), state=source)
                record.transition(target)
                self.assertIs(record.state, target)

    def test_every_illegal_move_raises(self) -> None:
        for source, targets in SESSION_TRANSITIONS.items():
            for target in SessionState:
                if target in targets or target is source:
                    continue
                record = SessionRecord(mission=MissionConfig("n", "o", "t"), state=source)
                with self.assertRaises(SessionTransitionError):
                    record.transition(target)

    def test_terminal_states_have_no_exits(self) -> None:
        for state in SESSION_TERMINAL_STATES:
            self.assertEqual(SESSION_TRANSITIONS[state], frozenset())


class SessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.desktop = FakeDesktop()
        self.events: list[tuple[str, dict[str, Any]]] = []

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

        async def publish(event_type: str, payload: dict[str, Any]) -> None:
            self.events.append((event_type, payload))

        options.setdefault("publish", publish)
        return NovaForgeManager(**options)

    async def test_start_runs_and_records_real_frames(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        self.assertIs(session["state"], SessionState.RUNNING.value)
        self.assertEqual(session["target"]["title"], WINDOW_TITLE)
        session_id = session["session_id"]

        await _wait_for(lambda: len(manager.get(session_id).frames) >= 2, message="two frames")
        record = manager.get(session_id)
        self.assertGreaterEqual(len(record.observations), 2)
        self.assertTrue(all(frame.fresh for frame in record.frames))
        self.assertTrue(record.observations[-1].evidence)
        self.assertEqual(manager.describe(record)["frame"]["source"], "window_crop")
        self.assertEqual(manager.describe(record)["frame"]["width"], 1000)
        self.assertEqual(manager.describe(record)["frame"]["height"], 600)
        await manager.stop(session_id)

    async def test_pause_is_acknowledged_and_stops_capturing(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="first frame")

        paused = await manager.pause(session_id)
        self.assertEqual(paused["state"], SessionState.PAUSED.value)
        self.assertEqual(paused["pause_reason"], "user")
        self.assertFalse(paused["can"]["pause"])
        self.assertTrue(paused["can"]["resume"])
        count = len(manager.get(session_id).frames)
        await asyncio.sleep(0.25)
        self.assertEqual(len(manager.get(session_id).frames), count, "a paused session captured more frames")

        resumed = await manager.resume(session_id)
        self.assertEqual(resumed["state"], SessionState.RUNNING.value)
        await _wait_for(lambda: len(manager.get(session_id).frames) > count, message="capture to resume")
        stopped = await manager.stop(session_id)
        self.assertEqual(stopped["state"], SessionState.STOPPED.value)

    async def test_pause_requires_a_running_session(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        self.assertEqual(session["state"], SessionState.RUNNING.value)
        await manager.stop(session["session_id"])
        with self.assertRaises(SessionTransitionError):
            await manager.pause(session["session_id"])
        with self.assertRaises(SessionTransitionError):
            await manager.resume(session["session_id"])

    async def test_stop_releases_input_and_cannot_run_twice(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        stopped = await manager.stop(session["session_id"])
        self.assertEqual(stopped["state"], SessionState.STOPPED.value)
        self.assertEqual(self.desktop.release_calls, 1)
        self.assertEqual(stopped["input_cleanup"]["held_after"], 0)
        self.assertTrue(stopped["input_cleanup"]["ok"])
        again = await manager.stop(session["session_id"])
        self.assertEqual(again["state"], SessionState.STOPPED.value)
        self.assertEqual(self.desktop.release_calls, 1, "stop is idempotent")

    async def test_second_start_is_refused_while_active(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        with self.assertRaises(SessionConflictError):
            await manager.start(_mission(name="Second"))
        await manager.stop(session["session_id"])

    async def test_start_refuses_an_invalid_mission(self) -> None:
        manager = self.manager()
        with self.assertRaises(MissionValidationError) as ctx:
            await manager.start(_mission(name="", target_title="nope"))
        self.assertTrue(ctx.exception.errors)
        self.assertEqual(manager.history(), [], "a refused start must not create a session")

    async def test_start_refuses_a_window_that_changed_after_validation(self) -> None:
        manager = self.manager()
        validation = await manager.validate(_mission())
        self.assertTrue(validation["valid"])
        handle = validation["target"]["handle"]
        pid = validation["target"]["pid"]

        # A different window now carries the validated title.
        self.desktop.window = FakeWindow(handle=0x5678, pid=777)
        with self.assertRaises(MissionValidationError) as ctx:
            await manager.start({**_mission(), "expected_handle": handle, "expected_pid": pid})
        self.assertIn("changed since it was validated", str(ctx.exception))
        self.assertEqual(manager.history(), [], "a refused start must not record a session")

    async def test_start_without_a_pin_uses_the_title_match_it_validated(self) -> None:
        """The pin is opt-in for callers; the server still validates the title."""
        manager = self.manager()
        self.desktop.window = FakeWindow(handle=0x5678, pid=777)
        started = await manager.start(_mission())
        self.assertEqual(started["target"]["handle"], 0x5678)
        await manager.stop(started["session_id"])

    async def test_malformed_target_pin_is_refused_not_ignored(self) -> None:
        manager = self.manager()
        with self.assertRaises(MissionValidationError):
            await manager.start({**_mission(), "expected_handle": "not-a-handle"})
        self.assertEqual(manager.history(), [])

    async def test_target_loss_pauses_and_resume_requires_the_window(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="first frame")

        self.desktop.window = None
        await _wait_for(
            lambda: manager.get(session_id).state is SessionState.PAUSED,
            message="target-loss pause",
        )
        self.assertEqual(manager.get(session_id).pause_reason, "target_lost")
        with self.assertRaises(MissionValidationError):
            await manager.resume(session_id)

        self.desktop.window = FakeWindow()
        resumed = await manager.resume(session_id)
        self.assertEqual(resumed["state"], SessionState.RUNNING.value)
        await manager.stop(session_id)

    async def test_target_replaced_by_a_different_window_pauses(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="first frame")
        self.desktop.window = FakeWindow(handle=0x9999, pid=999)
        await _wait_for(
            lambda: manager.get(session_id).state is SessionState.PAUSED,
            message="target-change pause",
        )
        self.assertEqual(manager.get(session_id).pause_reason, "target_changed")
        with self.assertRaises(MissionValidationError):
            await manager.resume(session_id)
        await manager.stop(session_id)

    async def test_time_limit_stops_without_claiming_completion(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission(time_limit_seconds=1))
        session_id = session["session_id"]
        await _wait_for(
            lambda: manager.get(session_id).state in SESSION_TERMINAL_STATES,
            timeout=4.0,
            message="time-limit stop",
        )
        record = manager.get(session_id)
        self.assertIs(record.state, SessionState.STOPPED)
        self.assertEqual(record.end_reason, "time_limit")
        self.assertEqual(record.to_dict()["verification"]["status"], "unverified")
        self.assertEqual(record.to_dict()["verification"]["supported"], False)

    async def test_a_stale_capture_is_never_shown_as_live(self) -> None:
        manager = self.manager()
        self.desktop.stale = True
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).observations, message="stale observation")
        record = manager.get(session_id)
        self.assertEqual(record.frames, [])
        self.assertFalse(record.observations[-1].evidence)
        self.assertIn("no fresh frame", record.observations[-1].summary.lower())
        self.assertTrue(record.errors)
        self.assertIsNone(manager.describe(record)["frame"])
        await manager.stop(session_id)

    async def test_a_raising_capture_is_recorded_not_fatal(self) -> None:
        manager = self.manager()
        self.desktop.fail = True
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).errors, message="capture error")
        record = manager.get(session_id)
        self.assertFalse(record.observations[-1].evidence)
        self.assertIn("Capture failed", record.errors[-1]["message"])
        await manager.stop(session_id)

    async def test_frames_are_bounded_on_disk(self) -> None:
        manager = self.manager(max_frames_on_disk=3)
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: len(manager.get(session_id).frames) >= 6, message="six frames")
        directory = manager.frames_root / session_id
        self.assertLessEqual(len(list(directory.glob("*.png"))), 3)
        await manager.stop(session_id)

    async def test_evicted_sessions_take_their_frames_with_them(self) -> None:
        """The session cap is a bound on disk too, not just on memory.

        ``max_frames_on_disk`` bounds one session. Without removing an evicted
        session's directory, the newest 40 records would be bounded while every
        past run's PNGs stayed on disk for the life of the installation.
        """
        manager = self.manager(max_sessions_kept=2)
        ids: list[str] = []
        for _ in range(3):
            session = await manager.start(_mission())
            session_id = session["session_id"]
            ids.append(session_id)
            await _wait_for(lambda: manager.get(session_id).frames, message="a frame")
            await manager.stop(session_id)
            self.assertTrue((manager.frames_root / session_id).is_dir())

        self.assertIsNone(manager.get(ids[0]), "the oldest session is evicted")
        self.assertFalse(
            (manager.frames_root / ids[0]).exists(),
            "an evicted session's frames must not linger on disk",
        )
        self.assertEqual([row["session_id"] for row in manager.history(limit=10)], ids[:0:-1])
        for session_id in ids[1:]:
            self.assertTrue((manager.frames_root / session_id).is_dir())

    async def test_a_lowered_session_cap_is_written_back_on_load(self) -> None:
        """A snapshot written under a larger cap must not keep evicted sessions.

        Trimming happens inside ``_persist`` during a run, so a snapshot can only
        be over cap when the cap changed between runs (or the stored file came
        from an older build). Loading must write the trimmed set back, or every
        boot re-reads records the backend has already evicted.
        """
        manager = self.manager(max_sessions_kept=5)
        ids: list[str] = []
        for _ in range(3):
            session = await manager.start(_mission())
            session_id = session["session_id"]
            ids.append(session_id)
            await _wait_for(lambda: manager.get(session_id).frames, message="a frame")
            await manager.stop(session_id)
        self.assertEqual(len(manager.store.read("novaforge_sessions")["sessions"]), 3)

        restarted = self.manager(max_sessions_kept=2)
        self.assertIsNone(restarted.get(ids[0]))
        stored = restarted.store.read("novaforge_sessions")["sessions"]
        self.assertEqual(
            {row["session_id"] for row in stored}, set(ids[1:]),
            "the persisted snapshot must match the trimmed set",
        )
        self.assertFalse((restarted.frames_root / ids[0]).exists())

    async def test_history_rows_are_compact_and_carry_no_raw_snapshot(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="a frame")
        await manager.stop(session_id)
        row = manager.history()[0]
        # ``SessionRecord.to_dict`` carries the persisted snapshot (with its
        # latency window); a history row is a summary and must not, or twenty
        # rows ship twenty arrays nobody reads.
        self.assertNotIn("telemetry", row)
        self.assertNotIn("latency_window_ms", str(row))
        self.assertNotIn("guardian", row)
        self.assertIn("can", row)

    async def test_session_and_history_survive_a_restart(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="first frame")
        stopped = await manager.stop(session_id)
        self.assertEqual(stopped["state"], SessionState.STOPPED.value)

        restarted = self.manager()
        loaded = restarted.get(session_id)
        self.assertIsNotNone(loaded)
        self.assertIs(loaded.state, SessionState.STOPPED)
        self.assertTrue(restarted.history())
        self.assertEqual(restarted.history()[0]["session_id"], session_id)
        self.assertIsNone(restarted.active())

    async def test_a_session_left_running_is_reconciled_as_stopped(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="first frame")
        # Simulate a crash: persist the snapshot while RUNNING without stopping.
        manager._persist()
        record = manager.get(session_id)
        self.assertIs(record.state, SessionState.RUNNING)

        restarted = self.manager()
        reconciled = restarted.get(session_id)
        self.assertIs(reconciled.state, SessionState.STOPPED)
        self.assertEqual(reconciled.end_reason, "application_restarted")
        self.assertTrue(reconciled.input_cleanup["ok"])
        self.assertIsNone(restarted.active())
        await manager.stop(session_id)

    async def test_events_carry_metadata_not_frame_bytes(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: any(kind == "novaforge.frame.captured" for kind, _ in self.events), message="frame event")
        kinds = {kind for kind, _ in self.events}
        self.assertIn("novaforge.session.changed", kinds)
        frame_events = [payload for kind, payload in self.events if kind == "novaforge.frame.captured"]
        self.assertIn("seq", frame_events[0])
        self.assertNotIn("path", frame_events[0])
        self.assertNotIn("image", frame_events[0])
        await manager.stop(session_id)

    async def test_frame_path_is_confined_and_seq_checked(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="first frame")
        frame = manager.get(session_id).frames[0]
        self.assertEqual(manager.frame_path(session_id, seq=frame.seq), Path(frame.path))
        self.assertIsNone(manager.frame_path(session_id, seq=999))
        self.assertIsNone(manager.frame_path("unknown-session"))

    async def test_mission_draft_is_persisted(self) -> None:
        manager = self.manager()
        saved = manager.save_mission(_mission(name="Draft"))
        self.assertTrue(saved["valid"])
        self.assertTrue(saved["persisted"])
        self.assertEqual(manager.saved_mission()["name"], "Draft")
        restarted = self.manager()
        self.assertEqual(restarted.saved_mission()["name"], "Draft")

    async def test_a_record_reconciled_on_restart_is_written_back(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="first frame")
        manager._persist()  # simulate a crash while RUNNING

        restarted = self.manager()
        self.assertIs(restarted.get(session_id).state, SessionState.STOPPED)
        stored = restarted.store.read("novaforge_sessions")
        row = next(item for item in stored["sessions"] if item["session_id"] == session_id)
        self.assertEqual(row["state"], "STOPPED", "the snapshot still claims RUNNING")
        self.assertEqual(row["end_reason"], "application_restarted")
        await manager.stop(session_id)

    async def test_a_dropped_mission_snapshot_is_not_reported_as_saved(self) -> None:
        class _DropStore:
            def read(self, _name: str) -> dict[str, Any]:
                return {}

            def write(self, _name: str, _payload: dict[str, Any]) -> None:
                return None

        manager = self.manager(store=_DropStore())
        saved = manager.save_mission(_mission(name="Draft"))
        self.assertTrue(saved["valid"])
        self.assertFalse(saved["persisted"], "a refused write must not be called saved")

    async def test_a_raising_window_probe_is_reported_not_raised(self) -> None:
        manager = self.manager()
        self.desktop.raise_lines = True
        listing = await manager.windows()
        self.assertFalse(listing["available"])
        self.assertIn("Window enumeration failed", listing["reason"])
        validation = await manager.validate(_mission())
        self.assertFalse(validation["valid"])
        self.assertTrue(any("unavailable" in error for error in validation["errors"]))

    async def test_a_raising_geometry_probe_is_a_validation_reason(self) -> None:
        manager = self.manager()
        self.desktop.raise_geometry = True
        validation = await manager.validate(_mission())
        self.assertFalse(validation["valid"])
        self.assertTrue(any("could not be measured" in error for error in validation["errors"]))
        with self.assertRaises(MissionValidationError) as ctx:
            await manager.start(_mission())
        self.assertIn("could not be measured", str(ctx.exception))

    async def test_an_unreadable_target_pauses_instead_of_failing_the_session(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="first frame")
        self.desktop.raise_geometry = True
        await _wait_for(
            lambda: manager.get(session_id).state is SessionState.PAUSED,
            message="unreadable-target pause",
        )
        record = manager.get(session_id)
        self.assertEqual(record.pause_reason, "target_unreadable")
        self.assertTrue(any("could not be read" in item["message"] for item in record.errors))
        with self.assertRaises(MissionValidationError) as ctx:
            await manager.resume(session_id)
        self.assertIn("could not be read", str(ctx.exception))

        self.desktop.raise_geometry = False
        resumed = await manager.resume(session_id)
        self.assertEqual(resumed["state"], SessionState.RUNNING.value)
        await manager.stop(session_id)

    async def test_time_limit_is_enforced_while_paused(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission(time_limit_seconds=1))
        session_id = session["session_id"]
        await asyncio.sleep(0.1)
        await manager.pause(session_id)
        await _wait_for(
            lambda: manager.get(session_id).state in SESSION_TERMINAL_STATES,
            timeout=4.0,
            message="time limit while paused",
        )
        record = manager.get(session_id)
        self.assertIs(record.state, SessionState.STOPPED)
        self.assertEqual(record.end_reason, "time_limit")

    async def test_action_budget_is_reported_as_not_enforced(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission(max_actions=1))
        session_id = session["session_id"]
        await _wait_for(lambda: len(manager.get(session_id).frames) >= 2, message="two frames")
        record = manager.get(session_id)
        self.assertIs(record.state, SessionState.RUNNING)
        self.assertEqual(record.actions_executed, 0)
        self.assertFalse(record.to_dict()["limits"]["max_actions_enforced"])
        await manager.stop(session_id)

    async def test_duplicate_titles_are_all_listed_and_refused(self) -> None:
        async def duplicate_lines() -> list[str]:
            return [
                "python|NC FORGE TEST - disposable",
                "other|NC FORGE TEST - disposable",
            ]

        manager = self.manager(window_lines=duplicate_lines)
        listing = await manager.windows()
        self.assertEqual(len(listing["windows"]), 2, "a second window was silently dropped")
        validation = await manager.validate(_mission())
        self.assertFalse(validation["valid"])
        self.assertIn("2 windows match", " ".join(validation["errors"]))

        async def repeated_line() -> list[str]:
            return ["python|NC FORGE TEST - disposable", "python|NC FORGE TEST - disposable"]

        repeated = self.manager(window_lines=repeated_line)
        self.assertEqual(len((await repeated.windows())["windows"]), 1)
        self.assertTrue((await repeated.validate(_mission()))["valid"], "a repeated line is one window")


class TelemetryAggregationTests(unittest.TestCase):
    """Known samples in, the same figures out; missing samples stay missing."""

    def test_latency_percentiles_match_known_samples(self) -> None:
        snapshot = TelemetrySnapshot()
        snapshot.begin(0.0)
        for index in range(1, 41):
            snapshot.record_frame(float(index), f"2026-01-01T00:00:{index % 60:02d}+00:00", float(index))
        summary = snapshot.latency_summary()
        self.assertEqual(summary["samples"], 40)
        self.assertEqual(summary["min_ms"], 1.0)
        self.assertEqual(summary["median_ms"], 20.0)
        self.assertEqual(summary["p95_ms"], 38.0)
        self.assertEqual(summary["max_ms"], 40.0)
        self.assertEqual(summary["mean_ms"], 20.5)
        self.assertTrue(summary["p95_available"])
        self.assertEqual(summary["window"], 40)
        self.assertIn("all 40", summary["basis"])

    def test_a_small_sample_has_a_median_but_no_p95(self) -> None:
        snapshot = TelemetrySnapshot()
        snapshot.begin(0.0)
        for value in (10.0, 20.0, 30.0, 40.0, 50.0):
            snapshot.record_frame(value, "", 0.0)
        summary = snapshot.latency_summary()
        self.assertEqual(summary["median_ms"], 30.0)
        self.assertLess(5, P95_MIN_SAMPLES)
        self.assertIsNone(summary["p95_ms"])
        self.assertFalse(summary["p95_available"])
        self.assertIn("at least", summary["p95_reason"])

    def test_the_latency_window_is_bounded_and_named(self) -> None:
        snapshot = TelemetrySnapshot()
        snapshot.begin(0.0)
        for index in range(LATENCY_WINDOW + 30):
            snapshot.record_frame(float(index), "", 0.0)
        summary = snapshot.latency_summary()
        self.assertEqual(len(snapshot.latency_window_ms), LATENCY_WINDOW)
        self.assertEqual(summary["samples"], LATENCY_WINDOW + 30)
        self.assertIn(f"newest {LATENCY_WINDOW}", summary["basis"])

    def test_zero_frames_is_a_measurement_and_actions_are_unsupported(self) -> None:
        snapshot = TelemetrySnapshot()
        snapshot.begin(0.0)
        payload = snapshot.describe(
            state="RUNNING", duration_seconds=1.0, state_changes=1, end_reason="", now=5.0
        )
        self.assertEqual(payload["capture"]["frames"], 0, "zero frames is a real count")
        self.assertEqual(payload["capture"]["capture_failures"], 0)
        self.assertIsNone(payload["capture"]["latency_ms"]["median_ms"])
        self.assertIsNone(payload["loop"]["observed_frame_rate_hz"])
        self.assertFalse(payload["loop"]["rate_available"])
        self.assertIn("No frames", payload["loop"]["rate_reason"])
        self.assertFalse(payload["actions"]["available"])
        self.assertIsNone(payload["actions"]["executed"], "an unmeasured count is not zero")
        for key in ("cpu", "memory", "gpu", "npu"):
            self.assertFalse(payload["resources"][key]["available"])
            self.assertIsNone(payload["resources"][key]["value"])
            self.assertTrue(payload["resources"][key]["reason"])

    def test_stale_and_failed_captures_are_counted_separately_from_frames(self) -> None:
        snapshot = TelemetrySnapshot()
        snapshot.begin(0.0)
        snapshot.record_frame(5.0, "", 1.0)
        snapshot.record_stale()
        snapshot.record_stale()
        snapshot.record_capture_failure()
        snapshot.record_frame(7.0, "", 4.0)  # 3s gap, over the 1.5s floor
        payload = snapshot.describe(
            state="RUNNING", duration_seconds=4.0, state_changes=1, end_reason="", now=5.0
        )
        self.assertEqual(payload["capture"]["frames"], 2)
        self.assertEqual(payload["capture"]["stale_frames"], 2)
        self.assertEqual(payload["capture"]["capture_failures"], 1)
        self.assertEqual(payload["capture"]["latency_ms"]["samples"], 2)
        self.assertEqual(payload["capture"]["frame_gaps"], 1)
        self.assertGreaterEqual(payload["capture"]["max_gap_ms"], GAP_FLOOR_MS)

    def test_pause_time_is_accumulated_from_monotonic_segments(self) -> None:
        snapshot = TelemetrySnapshot()
        snapshot.begin(100.0)
        snapshot.note_pause(110.0)
        snapshot.note_resume(115.0)
        payload = snapshot.describe(
            state="RUNNING", duration_seconds=20.0, state_changes=3, end_reason="", now=120.0
        )
        self.assertAlmostEqual(payload["session"]["active_seconds"], 15.0, places=3)
        self.assertAlmostEqual(payload["session"]["paused_seconds"], 5.0, places=3)
        self.assertEqual(payload["session"]["pause_count"], 1)

    def test_a_snapshot_round_trips_and_an_interrupted_one_says_so(self) -> None:
        snapshot = TelemetrySnapshot()
        snapshot.begin(0.0)
        for value in (4.0, 8.0, 12.0):
            snapshot.record_frame(value, "", 0.0)
        snapshot.record_stale()
        snapshot.mark_interrupted()
        restored = TelemetrySnapshot.from_dict(snapshot.to_dict())
        self.assertEqual(restored.frames, 3)
        self.assertEqual(restored.stale_frames, 1)
        self.assertEqual(restored.latency_window_ms, [4.0, 8.0, 12.0])
        self.assertTrue(restored.interrupted)
        payload = restored.describe(
            state="STOPPED", duration_seconds=1.0, state_changes=2,
            end_reason="application_restarted", now=0.0,
        )
        self.assertTrue(payload["session"]["interrupted"])
        self.assertTrue(any("last values persisted" in note for note in payload["measurement"]["notes"]))

    def test_a_malformed_persisted_snapshot_falls_back_to_defaults(self) -> None:
        restored = TelemetrySnapshot.from_dict(
            {"frames": "lots", "latency_window_ms": ["nope", -3, 7], "pause_count": None}
        )
        self.assertEqual(restored.frames, 0)
        self.assertEqual(restored.latency_window_ms, [7.0])
        self.assertEqual(restored.pause_count, 0)

    def test_a_gap_threshold_follows_the_configured_cadence(self) -> None:
        self.assertEqual(gap_threshold_for(0.05), GAP_FLOOR_MS)
        self.assertEqual(gap_threshold_for(2.0), 5000.0)


class FakePermissions:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.calls = 0

    def check(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls += 1

        class Decision:
            allow = self.allow

            def to_dict(self) -> dict[str, Any]:
                return {"allow": self.allow, "source": "test", "reason": "test refusal"}

        return Decision()


class GuardianSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.desktop = FakeDesktop()
        self.events: list[tuple[str, dict[str, Any]]] = []

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

        async def publish(event_type: str, payload: dict[str, Any]) -> None:
            self.events.append((event_type, payload))

        options.setdefault("publish", publish)
        return NovaForgeManager(**options)

    async def test_cancel_aborts_releases_input_and_is_idempotent(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="first frame")

        cancelled = await manager.cancel(session_id)
        self.assertEqual(cancelled["state"], SessionState.STOPPED.value)
        self.assertEqual(cancelled["end_reason"], "cancelled")
        self.assertFalse(cancelled["already_stopped"])
        self.assertTrue(cancelled["input_cleanup"]["ok"])
        self.assertEqual(self.desktop.release_calls, 1)
        self.assertTrue(any(entry["kind"] == "cancel" for entry in cancelled["interventions"]))
        self.assertEqual(cancelled["guardian"]["safety_state"], "closed")

        again = await manager.cancel(session_id)
        self.assertTrue(again["already_stopped"])
        self.assertIsNone(again["stop_latency_ms"])
        self.assertEqual(self.desktop.release_calls, 1, "a repeated cancel released input twice")

    async def test_emergency_stop_is_bounded_confirmed_and_idempotent(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="first frame")

        stopped = await manager.emergency_stop(session_id)
        self.assertEqual(stopped["state"], SessionState.STOPPED.value)
        self.assertEqual(stopped["end_reason"], "emergency_stop")
        self.assertFalse(stopped["already_stopped"])
        self.assertIsNotNone(stopped["stop_latency_ms"])
        self.assertLessEqual(stopped["stop_latency_ms"], manager.stop_timeout * 1000)
        self.assertEqual(stopped["input_cleanup"]["held_after"], 0)
        intervention = [e for e in stopped["interventions"] if e["kind"] == "emergency_stop"][-1]
        self.assertEqual(intervention["outcome"], "stopped")
        self.assertIn("ms", intervention["detail"])
        self.assertFalse(stopped["can"]["emergency_stop"])

        again = await manager.emergency_stop(session_id)
        self.assertTrue(again["already_stopped"])
        self.assertEqual(self.desktop.release_calls, 1)

    async def test_a_slow_loop_is_still_ended_within_the_bounded_wait(self) -> None:
        class SlowDesktop(FakeDesktop):
            async def capture(self, path: str) -> dict[str, Any]:
                await asyncio.sleep(0.3)
                return await super().capture(path)

        slow = SlowDesktop()
        manager = self.manager(
            capture=slow.capture, release_inputs=slow.release, stop_timeout=0.2
        )
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await asyncio.sleep(0.1)  # let the loop enter a capture that outlasts the wait
        stopped = await manager.stop(session_id)
        self.assertEqual(stopped["state"], SessionState.STOPPED.value)
        self.assertEqual(slow.release_calls, 1)
        self.assertTrue(stopped["input_cleanup"]["ok"])

    async def test_cleanup_note_does_not_claim_matched_counters_when_they_differ(self) -> None:
        self.desktop.presses = 1
        self.desktop.releases = 2
        manager = self.manager()
        session = await manager.start(_mission())
        stopped = await manager.emergency_stop(session["session_id"])
        cleanup = stopped["input_cleanup"]
        # No key is held (releases >= presses), but the counters disagree and the
        # note shown in the Guardian panel must say that rather than "matched".
        self.assertTrue(cleanup["ok"])
        self.assertIn("disagree", cleanup["note"])
        self.assertNotIn("matching", cleanup["note"])

    async def test_a_raising_release_is_reported_not_swallowed(self) -> None:
        async def failing_release() -> tuple[int, int]:
            raise RuntimeError("guard exploded")

        manager = self.manager(release_inputs=failing_release)
        session = await manager.start(_mission())
        stopped = await manager.emergency_stop(session["session_id"])
        self.assertEqual(stopped["state"], SessionState.STOPPED.value)
        cleanup = stopped["input_cleanup"]
        self.assertFalse(cleanup["ok"])
        self.assertIsNone(cleanup["held_after"])
        self.assertIn("guard exploded", cleanup["note"])
        self.assertEqual(stopped["guardian"]["cleanup"]["state"], "failed")

    async def test_no_frames_are_recorded_after_an_emergency_stop(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="first frame")
        await manager.emergency_stop(session_id)
        count = len(manager.get(session_id).frames)
        await asyncio.sleep(0.15)
        self.assertEqual(len(manager.get(session_id).frames), count)

    async def test_a_loop_exception_still_releases_input(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]

        def explode(_record: SessionRecord) -> bool:
            raise RuntimeError("loop failure injected by the test")

        with mock.patch.object(NovaForgeManager, "_limit_reached", explode):
            await _wait_for(
                lambda: manager.get(session_id).state in SESSION_TERMINAL_STATES,
                timeout=3.0,
                message="failed session",
            )
        record = manager.get(session_id)
        self.assertIs(record.state, SessionState.FAILED)
        self.assertEqual(self.desktop.release_calls, 1)
        self.assertTrue(record.input_cleanup["ok"])

    async def test_target_loss_blocks_input_and_records_an_intervention(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="first frame")
        self.desktop.window = None
        await _wait_for(
            lambda: manager.get(session_id).state is SessionState.PAUSED,
            message="target-loss pause",
        )
        record = manager.get(session_id)
        decision = input_safety_decision(record)
        self.assertFalse(decision["allow"])
        self.assertIn(decision["state"], {"held", "unsupported"})
        guardian = manager.describe(record)["guardian"]
        self.assertFalse(guardian["target"]["valid"])
        self.assertEqual(guardian["target"]["state"], "lost")
        self.assertFalse(guardian["input"]["allow"])
        self.assertTrue(any(e["kind"] == "target" and e["outcome"] == "paused" for e in record.interventions))
        await manager.stop(session_id)

    async def test_pause_and_resume_are_measured(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="first frame")
        await manager.pause(session_id)
        await asyncio.sleep(0.1)
        await manager.resume(session_id)
        telemetry = manager.describe(manager.get(session_id))["telemetry"]
        self.assertEqual(telemetry["session"]["pause_count"], 1)
        self.assertGreaterEqual(telemetry["session"]["paused_seconds"], 0.05)
        self.assertGreater(telemetry["session"]["active_seconds"], 0)
        await manager.stop(session_id)

    async def test_telemetry_and_interventions_survive_a_restart(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: len(manager.get(session_id).frames) >= 2, message="two frames")
        stopped = await manager.emergency_stop(session_id)
        frames = stopped["telemetry"]["capture"]["frames"]
        self.assertGreaterEqual(frames, 2)

        restarted = self.manager()
        loaded = restarted.get(session_id)
        self.assertIsNotNone(loaded)
        payload = restarted.describe(loaded)
        self.assertEqual(payload["telemetry"]["capture"]["frames"], frames)
        self.assertGreaterEqual(payload["telemetry"]["capture"]["latency_ms"]["samples"], 2)
        self.assertTrue(any(e["kind"] == "emergency_stop" for e in payload["guardian"]["interventions"]))
        self.assertEqual(payload["guardian"]["cleanup"]["state"], "ok")

    async def test_target_validated_flag_gates_the_input_boundary(self) -> None:
        # A timestamp alone is not a confirmation: a RUNNING record nobody has
        # re-checked yet must refuse input with a named state.
        unconfirmed = SessionRecord(
            mission=MissionConfig("n", "o", "t"), state=SessionState.RUNNING
        )
        unconfirmed.target = TargetWindow("t", 1, 2, (0, 0, 3, 3), (0, 0, 3, 3))
        unconfirmed.telemetry.note_target_check(utc_now())
        decision = input_safety_decision(unconfirmed)
        self.assertFalse(decision["allow"])
        self.assertEqual(decision["state"], "pending")
        unconfirmed.target_validated = True
        self.assertEqual(input_safety_decision(unconfirmed)["state"], "unsupported")

        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="first frame")
        record = manager.get(session_id)
        self.assertTrue(record.target_validated, "a matching tick must confirm the target")

        # A different window now carries the title: the confirmation is cleared.
        self.desktop.window = FakeWindow(handle=0x9999, pid=999)
        await _wait_for(
            lambda: record.state is SessionState.PAUSED, message="target-change pause"
        )
        self.assertFalse(record.target_validated)
        self.assertEqual(input_safety_decision(record)["state"], "held")

        # Resume revalidates, so the boundary may allow the new run again.
        self.desktop.window = FakeWindow()
        await manager.resume(session_id)
        self.assertTrue(record.target_validated, "resume performs a fresh check")
        await manager.stop(session_id)

    async def test_a_closed_session_reports_a_closed_target(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="first frame")
        stopped = await manager.stop(session_id)
        self.assertEqual(stopped["guardian"]["target"]["state"], "closed")

        # Even a session that ended after a target loss reports closed; the loss
        # stays in the counters and the recorded error, not as a live condition.
        lost = await manager.start(_mission(name="Lost target"))
        lost_id = lost["session_id"]
        await _wait_for(lambda: manager.get(lost_id).frames, message="first frame")
        self.desktop.window = None
        await _wait_for(
            lambda: manager.get(lost_id).state is SessionState.PAUSED,
            message="target-loss pause",
        )
        closed = await manager.stop(lost_id)
        self.assertEqual(closed["guardian"]["target"]["state"], "closed")
        self.assertGreaterEqual(closed["telemetry"]["target"]["losses"], 1)

    async def test_stop_while_paused_releases_once(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="first frame")
        await manager.pause(session_id)
        stopped = await manager.stop(session_id)
        self.assertEqual(stopped["state"], SessionState.STOPPED.value)
        self.assertEqual(stopped["end_reason"], "user")
        self.assertTrue(stopped["input_cleanup"]["ok"])
        self.assertEqual(self.desktop.release_calls, 1)

    async def test_stop_after_cancel_changes_nothing(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="first frame")
        await manager.cancel(session_id)
        record = manager.get(session_id)
        kinds = [entry["kind"] for entry in record.interventions]
        self.assertEqual(kinds, ["cancel"])
        again = await manager.stop(session_id)
        self.assertEqual(again["state"], SessionState.STOPPED.value)
        self.assertEqual(
            [entry["kind"] for entry in record.interventions], kinds,
            "a stop after a terminal abort must not record a second intervention",
        )
        late_cancel = await manager.cancel(session_id)
        self.assertTrue(late_cancel["already_stopped"])
        self.assertEqual(self.desktop.release_calls, 1)
        self.assertEqual(
            [entry["kind"] for entry in record.interventions], kinds,
            "a cancel after a stop must not record a second intervention",
        )

    async def test_emergency_stop_immediately_after_start_is_confirmed(self) -> None:
        manager = self.manager()
        session = await manager.start(_mission())
        stopped = await manager.emergency_stop(session["session_id"])
        self.assertEqual(stopped["state"], SessionState.STOPPED.value)
        self.assertEqual(self.desktop.release_calls, 1)
        self.assertTrue(stopped["input_cleanup"]["ok"])

    async def test_a_stubborn_capture_cannot_add_evidence_after_a_timed_out_abort(self) -> None:
        class StubbornDesktop(FakeDesktop):
            async def capture(self, path: str) -> dict[str, Any]:
                self.captures.append(path)
                try:
                    await asyncio.sleep(0.3)
                except asyncio.CancelledError:
                    # Swallow the cancellation the way a poorly written capture
                    # backend might, and finish anyway.
                    await asyncio.sleep(0.2)
                image = Image.new("RGB", self.screen, (12, 18, 30))
                image.save(path, format="PNG")
                return {
                    "fresh": True,
                    "verification": "frame written now",
                    "captured_at": datetime.now(UTC).isoformat(),
                    "capture_ms": 5.0,
                    "capture_mode": "persistent",
                }

        stubborn = StubbornDesktop()
        manager = self.manager(
            capture=stubborn.capture, release_inputs=stubborn.release, stop_timeout=0.1
        )
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: stubborn.captures, message="capture in flight")

        stopped = await manager.emergency_stop(session_id)
        self.assertEqual(stopped["state"], SessionState.STOPPED.value)
        self.assertEqual(self.desktop.release_calls, 0)
        self.assertEqual(stubborn.release_calls, 1)
        intervention = [e for e in stopped["interventions"] if e["kind"] == "emergency_stop"][-1]
        self.assertEqual(intervention["outcome"], "timeout")
        self.assertIn("exceeded", intervention["detail"])
        self.assertTrue(
            any("EXCEEDED" in entry["message"] for entry in stopped["errors"]),
            "a bounded abort that ran out of time must say so",
        )

        # The in-flight capture finishes after the confirmed stop; the loop must
        # discard it instead of recording evidence for an already-stopped run.
        await asyncio.sleep(0.6)
        record = manager.get(session_id)
        self.assertEqual(record.frames, [])
        self.assertEqual(record.observations, [])
        self.assertEqual(manager.describe(record)["telemetry"]["capture"]["frames"], 0)

    async def test_a_raising_publish_cannot_fail_a_guardian_stop(self) -> None:
        async def broken(_event_type: str, _payload: dict[str, Any]) -> None:
            raise RuntimeError("observer down")

        manager = self.manager(publish=broken)
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).frames, message="first frame")
        stopped = await manager.emergency_stop(session_id)
        self.assertEqual(stopped["state"], SessionState.STOPPED.value)
        self.assertTrue(stopped["input_cleanup"]["ok"])

    async def test_a_raising_store_does_not_fail_a_guardian_stop(self) -> None:
        class RaisingStore:
            def read(self, _name: str) -> dict[str, Any]:
                return {}

            def write(self, _name: str, _payload: dict[str, Any]) -> None:
                raise RuntimeError("disk full")

        manager = self.manager(store=RaisingStore())
        session = await manager.start(_mission())
        stopped = await manager.emergency_stop(session["session_id"])
        self.assertEqual(stopped["state"], SessionState.STOPPED.value)
        self.assertTrue(stopped["input_cleanup"]["ok"])

    async def test_error_count_and_recovery_are_reported_honestly(self) -> None:
        manager = self.manager()
        self.desktop.fail = True
        session = await manager.start(_mission())
        session_id = session["session_id"]
        await _wait_for(lambda: manager.get(session_id).errors, message="capture error")
        record = manager.get(session_id)
        telemetry = manager.describe(record)["telemetry"]
        self.assertEqual(telemetry["session"]["errors"], len(record.errors))
        self.assertGreaterEqual(telemetry["session"]["errors"], 1)
        recovery = telemetry["recovery"]
        self.assertFalse(recovery["available"], "no recovery path exists to measure")
        self.assertEqual(recovery["attempts"], 0)
        self.assertIn("No retry or recovery path", recovery["reason"])
        await manager.stop(session_id)

    async def test_permission_refusal_creates_no_session(self) -> None:
        permissions = FakePermissions(allow=False)
        manager = self.manager(permissions=permissions)
        with self.assertRaises(MissionValidationError) as ctx:
            await manager.start(_mission())
        self.assertIn("test refusal", str(ctx.exception))
        self.assertIsNone(manager.active())
        self.assertEqual(manager.history(), [])
        self.assertGreaterEqual(permissions.calls, 1)

    def test_guardian_state_is_derived_from_the_session_state(self) -> None:
        record = SessionRecord(mission=MissionConfig("n", "o", "t"))
        self.assertEqual(safety_state(record), "unarmed")
        record.transition(SessionState.STARTING)
        self.assertEqual(safety_state(record), "starting")
        record.transition(SessionState.RUNNING)
        self.assertEqual(safety_state(record), "observing")
        record.transition(SessionState.STOPPING)
        self.assertEqual(safety_state(record), "stopping")
        record.transition(SessionState.STOPPED)
        self.assertEqual(safety_state(record), "closed")

    def test_action_budget_is_reported_configured_but_unenforceable(self) -> None:
        record = SessionRecord(mission=MissionConfig("n", "o", "t", max_actions=5))
        record.target = TargetWindow("t", 1, 2, (0, 0, 3, 3), (0, 0, 3, 3))
        guardian = guardian_state(
            record, frame_interval_seconds=1.0, max_frames_on_disk=30,
            max_sessions_kept=40, stop_timeout_seconds=8.0,
        )
        budget = guardian["limits"]["action_budget"]
        self.assertTrue(budget["configured"])
        self.assertFalse(budget["enforced"])
        self.assertEqual(budget["state"], "configured_not_enforceable")
        self.assertIn("No action-execution path", budget["reason"])
        guarantees = {item["name"]: item for item in guardian["guarantees"]}
        self.assertFalse(guarantees["action_budget"]["enforced"])
        self.assertFalse(guarantees["gameplay_input"]["enforced"])
        self.assertTrue(guarantees["input_release"]["enforced"])


class FrameToolingTests(unittest.TestCase):
    def test_crop_uses_physical_window_bounds(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "frame.png"
            Image.new("RGB", (1200, 800), (1, 2, 3)).save(path)
            target = TargetWindow(
                title="t", handle=1, pid=2,
                bounds=(100, 50, 500, 350), client=(0, 0, 400, 300),
            )
            from novacontrol.novaforge.manager import _crop_to_target

            width, height, size, source, note = _crop_to_target(path, target)
            self.assertEqual((width, height), (400, 300))
            self.assertEqual(source, "window_crop")
            self.assertEqual(note, "")
            self.assertGreater(size, 0)
            with Image.open(path) as image:
                self.assertEqual(image.size, (400, 300))

    def test_partially_offscreen_window_is_cropped_and_flagged(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "frame.png"
            Image.new("RGB", (200, 100), (1, 2, 3)).save(path)
            target = TargetWindow(
                title="t", handle=1, pid=2,
                bounds=(-50, -20, 100, 80), client=(0, 0, 150, 100),
            )
            from novacontrol.novaforge.manager import _crop_to_target

            width, height, _size, source, note = _crop_to_target(path, target)
            self.assertEqual((width, height), (100, 80))
            self.assertEqual(source, "window_crop")
            self.assertIn("clipped", note)

    def test_offscreen_window_keeps_the_full_frame_and_says_so(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "frame.png"
            Image.new("RGB", (200, 100), (1, 2, 3)).save(path)
            target = TargetWindow(
                title="t", handle=1, pid=2,
                bounds=(900, 900, 1200, 1200), client=(0, 0, 300, 300),
            )
            from novacontrol.novaforge.manager import _crop_to_target

            width, height, _size, source, note = _crop_to_target(path, target)
            self.assertEqual((width, height), (200, 100))
            self.assertEqual(source, "screen")
            self.assertIn("outside the captured display", note)


if __name__ == "__main__":
    unittest.main()
