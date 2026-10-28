"""NF-15 — unified game observation: model, scheduler, lifecycle and UI surface.

The tests here use a fake window probe and a fake capture that writes a real
file, so the scheduler, the identity rules and the pruning are exercised without
touching a desktop. The live evidence is separate (see the NF-15 report); nothing
in this file claims a game was observed.
"""

from __future__ import annotations

import asyncio
import re
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from PIL import Image

try:
    from fastapi.testclient import TestClient  # noqa: F401 - requires httpx
except Exception:  # pragma: no cover - httpx is an optional dev dependency
    TestClient = None  # type: ignore[assignment]

from test_web_api import _IsolatedApiTestCase

from novacontrol.novaforge import NovaForgeManager
from novacontrol.novaforge.manager import _target_visibility_issue
from novacontrol.novaforge.models import TargetWindow
from novacontrol.novaforge.observer import (
    DEFAULT_CAPTURE_TIMEOUT,
    FRAME_STATUSES,
    MAX_INTERVAL,
    MIN_INTERVAL,
    OBSERVATION_STATES,
    CapturedFrame,
    GameObserver,
    WindowState,
    target_visibility,
    window_state,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"nf15-test-frame" * 8
TITLE = "NC OBSERVE TEST - disposable"


def paint(path: str, width: int = 800, height: int = 500, *, seed: int = 0) -> None:
    """Write a PAINTED png: bands, not a flat fill.

    The real validator calls a solid-colour frame blank, so a fixture that fills
    one colour is testing the blank rule rather than a capture. Bands keep the
    frame non-uniform and its content hash distinct per ``seed``.
    """
    image = Image.new("RGB", (width, height), (20 + seed, 40, 60))
    for row in range(0, height, 24):
        shade = (row * 7 + seed * 31) % 256
        for x in range(0, width, 32):
            box = (x, row, min(width, x + 32), min(height, row + 24))
            image.paste((shade, (shade * 3) % 256, (x * 5) % 256), box)
    image.save(path, format="PNG")


class FakeWindow:
    """The shape the desktop layer's geometry object provides."""

    def __init__(
        self,
        *,
        title: str = TITLE,
        handle: int = 0x4321,
        pid: int = 5150,
        bounds: tuple[int, int, int, int] = (100, 50, 900, 550),
        minimized: bool = False,
        visible: bool = True,
        foreground: bool = True,
    ) -> None:
        self.title = title
        self.handle = handle
        self.pid = pid
        self.bounds = bounds
        self.client = (0, 0, bounds[2] - bounds[0], bounds[3] - bounds[1])
        self.scale = 1.0
        self.foreground = foreground
        self.minimized = minimized
        self.visible = visible


class FakeSeam:
    """A window probe plus a capture that reports what the test asked for."""

    def __init__(self, window: FakeWindow | None = None) -> None:
        self.window = window or FakeWindow()
        self.calls: list[tuple[WindowState, str]] = []
        self.reject = False
        self.rejection = ""
        self.rejection_code = "target_unreadable"
        self.capture_delay = 0.0
        self.blocker: asyncio.Event | None = None
        self.raise_capture = False

    async def resolve(self, title: str) -> FakeWindow | None:
        if self.window is None:
            return None
        return self.window if title.lower() in self.window.title.lower() else None

    async def capture(
        self, window: WindowState, path: str, *, previous_hash: str = ""
    ) -> CapturedFrame:
        self.calls.append((window, path))
        if self.blocker is not None:
            await self.blocker.wait()
        if self.capture_delay:
            await asyncio.sleep(self.capture_delay)
        if self.raise_capture:
            raise RuntimeError("capture worker exploded")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(PNG_BYTES)
        if self.reject:
            return CapturedFrame(
                ok=False,
                path=path,
                backend="printwindow",
                source="window_surface",
                elapsed_ms=2.0,
                reason=self.rejection or "The frame is blank.",
                reason_code=self.rejection_code,
                verdict={
                    "capture_ok": True,
                    "target_visible": True,
                    "fresh": False,
                    "content_ok": False,
                    "livable": False,
                    "content": "blank",
                    "limitations": ["A blank frame is how some renderers answer."],
                },
            )
        return CapturedFrame(
            ok=True,
            path=path,
            width=window.width,
            height=window.height,
            size_bytes=len(PNG_BYTES),
            backend="printwindow",
            source="window_surface",
            elapsed_ms=4.0,
            age_seconds=0.01,
            verdict={
                "capture_ok": True,
                "target_visible": True,
                "fresh": True,
                "content_ok": True,
                "livable": True,
                "content": "valid",
                "limitations": [],
            },
            content_hash=f"content-{len(self.calls)}",
        )


class GatedSeam(FakeSeam):
    """A window probe that waits, so two starts can race deterministically."""

    def __init__(self, window: FakeWindow | None = None) -> None:
        super().__init__(window)
        self.gate = asyncio.Event()

    async def resolve(self, title: str) -> FakeWindow | None:
        await self.gate.wait()
        return await super().resolve(title)


class FakeIdentity:
    """A pid -> identity probe a test can end or replace."""

    def __init__(self, *, pid: int = 5150, start_time: str = "2026-10-24T09:00:00Z") -> None:
        self.pid = pid
        self.start_time = start_time
        self.alive = True
        self.calls = 0

    async def __call__(self, pid: int) -> dict[str, Any] | None:
        self.calls += 1
        if not self.alive or pid != self.pid:
            return None
        return {"pid": self.pid, "name": "fakegame", "start_time": self.start_time}


class DictStore:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.fail_read = False
        self.fail_write = False

    def read(self, namespace: str) -> Any:
        if self.fail_read:
            raise OSError("the store is unreadable")
        return self.data.get(namespace, {})

    def write(self, namespace: str, payload: Any) -> None:
        if self.fail_write:
            raise OSError("the store is unwritable")
        self.data[namespace] = payload


async def wait_for(predicate, *, timeout: float = 4.0, message: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out waiting for {message}.")


def make_observer(
    seam: FakeSeam,
    tmp: Path,
    *,
    identity: Any | None = None,
    store: Any | None = None,
    events: list[tuple[str, dict[str, Any]]] | None = None,
    **overrides: Any,
) -> GameObserver:
    options: dict[str, Any] = {
        "frame_dir": tmp / "observed",
        "resolve": seam.resolve,
        "capture": seam.capture,
        "identity": identity,
        "interval": 0.02,
        "min_interval": 0.005,
        "max_interval": 5.0,
        "poll_interval": 0.01,
        "capture_timeout": 1.0,
        "max_failures": 5,
        "max_frames": 12,
    }
    if store is not None:
        options["store"] = store
    if events is not None:
        async def publish(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))

        options["publish"] = publish
    options.update(overrides)
    return GameObserver(**options)


class ObservationModelTests(unittest.IsolatedAsyncioTestCase):
    """The model keeps "not measured" and "failed" apart."""

    def test_states_and_statuses_are_the_declared_vocabulary(self) -> None:
        self.assertEqual(
            OBSERVATION_STATES, ("idle", "observing", "paused", "stopped", "failed")
        )
        self.assertEqual(FRAME_STATUSES, ("observed", "rejected", "unavailable"))

    def test_window_state_reads_a_mapping_and_refuses_a_zero_handle(self) -> None:
        state = window_state(
            {
                "title": TITLE,
                "handle": 12,
                "pid": 34,
                "bounds": [0, 0, 200, 100],
                "client": [0, 0, 200, 100],
            }
        )
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.width, 200)
        self.assertIsNone(window_state({"handle": 0, "pid": 1, "bounds": [0, 0, 10, 10]}))
        self.assertIsNone(window_state(None))

    def test_visibility_rules_are_shared_with_the_session_manager(self) -> None:
        cases = [
            FakeWindow(minimized=True),
            FakeWindow(visible=False),
            FakeWindow(bounds=(0, 0, 4, 4)),
            FakeWindow(bounds=(-32000, -32000, -31200, -31700)),
            FakeWindow(),
        ]
        for window in cases:
            target = TargetWindow(
                title=window.title,
                handle=window.handle,
                pid=window.pid,
                bounds=window.bounds,
                client=window.client,
                foreground=window.foreground,
                minimized=window.minimized,
                visible=window.visible,
            )
            state = window_state(window)
            assert state is not None
            self.assertEqual(
                target_visibility(state),
                _target_visibility_issue(target),
                "an observation and a session must not disagree about a target",
            )

    async def test_an_untested_observer_reports_no_observation_and_no_numbers(self) -> None:
        with TemporaryDirectory() as tmp:
            observer = make_observer(FakeSeam(), Path(tmp))
            status = observer.status()
            self.assertEqual(status["state"], "idle")
            self.assertFalse(status["tested"])
            self.assertIsNone(status["last_observation"])
            self.assertIsNone(status["last_success"])
            self.assertIsNone(status["measured"]["rate_hz"])
            self.assertIsNone(status["measured"]["latency_ms"]["last"])
            self.assertEqual(status["counts"]["attempts"], 0)
            self.assertEqual(status["backend"], "")
            self.assertIsNone(observer.frame_path())

    async def test_a_refusal_before_any_write_is_not_a_paused_session(self) -> None:
        with TemporaryDirectory() as tmp:
            seam = FakeSeam()
            seam.window = None
            observer = make_observer(seam, Path(tmp))
            result = await observer.start(title=TITLE)
            self.assertFalse(result["started"])
            self.assertEqual(result["reason_code"], "target_missing")
            self.assertEqual(observer.status()["state"], "idle")
            self.assertEqual(observer.status()["session_id"], "")
            self.assertEqual(seam.calls, [])


class ObservationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def observer(self, seam: FakeSeam, **kwargs: Any) -> GameObserver:
        observer = make_observer(seam, self.tmp, **kwargs)
        self.addAsyncCleanup(observer.shutdown)
        return observer

    async def test_a_bound_target_is_observed_with_identity_and_measurements(self) -> None:
        seam = FakeSeam()
        observer = self.observer(seam)
        result = await observer.start(title=TITLE, game="Fake Game", pid=5150)
        self.assertTrue(result["started"], result)
        await wait_for(lambda: len(observer.status()["history"]) >= 2, message="two reads")
        await observer.stop()
        status = observer.status()
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["game"], "Fake Game")
        self.assertEqual(status["target"]["pid"], 5150)
        self.assertEqual(status["target"]["handle"], 0x4321)
        self.assertEqual(status["target"]["identity"], "handle:17185:pid:5150")
        last = status["last_observation"]
        assert last is not None
        self.assertEqual(last["status"], "observed")
        self.assertTrue(last["fresh"])
        self.assertTrue(last["target_valid"])
        self.assertEqual(last["width"], 800)
        self.assertEqual(last["backend"], "printwindow")
        self.assertIsNotNone(last["capture_ms"])
        self.assertIsNotNone(last["schedule_delay_ms"])
        self.assertEqual(status["last_success"]["seq"], last["seq"])
        self.assertEqual(status["counts"]["observed"], len(status["history"]))
        self.assertGreater(status["measured"]["rate_hz"] or 0, 0)
        self.assertIsNotNone(status["measured"]["latency_ms"]["average"])
        self.assertTrue(observer.frame_path() is not None)
        assert observer.frame_path() is not None
        self.assertTrue(observer.frame_path().is_file())

    async def test_a_minimized_target_pauses_and_resumes_without_a_frame(self) -> None:
        seam = FakeSeam()
        seam.window.minimized = True
        observer = self.observer(seam)
        self.assertTrue((await observer.start(title=TITLE))["started"])
        await wait_for(
            lambda: observer.status()["pause"]["code"] == "target_minimized",
            message="a minimized pause",
        )
        self.assertEqual(seam.calls, [], "a paused target must not be captured")
        paused = observer.status()["last_observation"]
        assert paused is not None
        self.assertEqual(paused["status"], "unavailable")
        self.assertIsNone(paused["fresh"])
        self.assertFalse(paused["target_valid"])
        self.assertEqual(paused["reason_code"], "target_minimized")

        seam.window.minimized = False
        await wait_for(
            lambda: observer.status()["state"] == "observing"
            and (observer.status()["last_observation"] or {}).get("status") == "observed",
            message="an automatic resume",
        )
        self.assertTrue(seam.calls, "capture must resume once the target is valid")

    async def test_a_hidden_target_is_not_captured(self) -> None:
        seam = FakeSeam()
        seam.window.visible = False
        observer = self.observer(seam)
        await observer.start(title=TITLE)
        await wait_for(
            lambda: observer.status()["pause"]["code"] == "target_hidden",
            message="a hidden pause",
        )
        self.assertEqual(seam.calls, [])

    async def test_a_user_pause_never_resumes_itself(self) -> None:
        seam = FakeSeam()
        observer = self.observer(seam)
        await observer.start(title=TITLE)
        await wait_for(lambda: observer.status()["counts"]["observed"] >= 1, message="a frame")
        paused = await observer.pause()
        self.assertTrue(paused["paused"])
        self.assertTrue(paused["pause"]["user_requested"])
        calls = len(seam.calls)
        await asyncio.sleep(0.1)
        self.assertEqual(len(seam.calls), calls, "a user pause must stop capture")
        self.assertEqual(observer.status()["state"], "paused")
        refused = await observer.observe_once()
        self.assertEqual(refused["reason_code"], "paused_by_user")
        resumed = await observer.resume()
        self.assertTrue(resumed["resumed"], resumed)
        await wait_for(
            lambda: len(seam.calls) > calls,
            message="capture after a manual resume",
        )

    async def test_stop_is_idempotent_and_ends_the_loop(self) -> None:
        seam = FakeSeam()
        observer = self.observer(seam)
        await observer.start(title=TITLE)
        await wait_for(lambda: observer.status()["counts"]["observed"] >= 1, message="a frame")
        first = await observer.stop()
        self.assertTrue(first["stopped"])
        calls = len(seam.calls)
        second = await observer.stop()
        self.assertTrue(second["already_stopped"])
        await asyncio.sleep(0.05)
        self.assertEqual(len(seam.calls), calls, "a stopped observer must not capture")
        self.assertEqual(observer.status()["state"], "stopped")

    async def test_a_concurrent_read_is_answered_busy_not_queued(self) -> None:
        seam = FakeSeam()
        seam.blocker = asyncio.Event()
        observer = self.observer(seam)
        await observer.start(title=TITLE)
        await wait_for(lambda: len(seam.calls) == 1, message="a capture in flight")
        busy = await observer.observe_once()
        self.assertEqual(busy["status"], "busy")
        self.assertEqual(len(seam.calls), 1, "no overlapping capture may start")
        seam.blocker.set()
        await wait_for(lambda: observer.status()["counts"]["observed"] >= 1, message="a frame")

    async def test_a_capture_timeout_is_a_record_and_ends_a_failing_loop(self) -> None:
        seam = FakeSeam()
        seam.capture_delay = 0.2
        observer = self.observer(seam, capture_timeout=0.03, max_failures=2)
        await observer.start(title=TITLE)
        await wait_for(
            lambda: observer.status()["state"] == "failed", message="the failure ceiling"
        )
        status = observer.status()
        self.assertEqual(status["reason"], "consecutive_capture_failures")
        self.assertGreaterEqual(status["consecutive_failures"], 2)
        last = status["last_observation"]
        assert last is not None
        self.assertEqual(last["reason_code"], "capture_timeout")
        self.assertEqual(last["status"], "unavailable")
        self.assertIsNone(status["last_success"])

    async def test_each_failure_record_reports_the_count_including_itself(self) -> None:
        """A rejected row saying "0 consecutive failures" describes the wrong run.

        The record is the diagnostic a failure is read from, so its own failure
        must be counted: the first rejected row is 1, not 0, and the row that
        trips the ceiling matches the status figure.
        """
        seam = FakeSeam()
        seam.reject = True
        observer = self.observer(seam, max_failures=3)
        await observer.start(title=TITLE)
        await wait_for(
            lambda: observer.status()["state"] == "failed", message="the ceiling"
        )
        status = observer.status()
        rows = [row for row in status["history"] if row["status"] == "rejected"]
        self.assertEqual(len(rows), 3, status["history"])
        self.assertEqual(
            [row["consecutive_failures"] for row in rows],
            [1, 2, 3],
            "every record counts the failure it is reporting",
        )
        self.assertEqual(status["consecutive_failures"], 3)
        self.assertEqual(rows[-1]["consecutive_failures"], status["consecutive_failures"])

    async def test_a_rejected_frame_is_deleted_and_never_becomes_a_success(self) -> None:
        seam = FakeSeam()
        seam.reject = True
        observer = self.observer(seam, max_failures=10)
        await observer.start(title=TITLE)
        await wait_for(lambda: observer.status()["counts"]["rejected"] >= 1, message="a rejection")
        status = observer.status()
        last = status["last_observation"]
        assert last is not None
        self.assertEqual(last["status"], "rejected")
        self.assertFalse(last["fresh"])
        self.assertEqual(last["reason_code"], "target_unreadable")
        self.assertIsNone(status["last_success"])
        self.assertIsNone(observer.frame_path())
        self.assertEqual(list((self.tmp / "observed").rglob("*.png")), [])

    async def test_an_exited_process_stops_instead_of_pausing(self) -> None:
        seam = FakeSeam()
        identity = FakeIdentity()
        observer = self.observer(seam, identity=identity)
        await observer.start(
            title=TITLE, pid=5150, start_time=identity.start_time
        )
        await wait_for(lambda: observer.status()["counts"]["observed"] >= 1, message="a frame")
        seam.window = None
        identity.alive = False
        await wait_for(
            lambda: observer.status()["state"] == "stopped", message="a confirmed exit"
        )
        status = observer.status()
        self.assertEqual(status["reason"], "process_exited")
        last = status["last_observation"]
        assert last is not None
        self.assertEqual(last["reason_code"], "process_exited")
        self.assertFalse(last["target_valid"])

    async def test_a_missing_window_with_a_live_process_only_pauses(self) -> None:
        seam = FakeSeam()
        identity = FakeIdentity()
        observer = self.observer(seam, identity=identity)
        await observer.start(title=TITLE, pid=5150, start_time=identity.start_time)
        await wait_for(lambda: observer.status()["counts"]["observed"] >= 1, message="a frame")
        seam.window = None
        await wait_for(
            lambda: observer.status()["pause"]["code"] == "window_missing",
            message="a paused window",
        )
        self.assertEqual(observer.status()["state"], "paused")
        self.assertNotIn(observer.status()["reason"].lower(), {"process exited"})

    async def test_a_recreated_window_is_rebound_but_a_new_process_is_not(self) -> None:
        seam = FakeSeam()
        identity = FakeIdentity()
        events: list[tuple[str, dict[str, Any]]] = []
        observer = self.observer(seam, identity=identity, events=events)
        await observer.start(title=TITLE, pid=5150, start_time=identity.start_time)
        await wait_for(lambda: observer.status()["counts"]["observed"] >= 1, message="a frame")
        seam.window.handle = 0x5555
        await wait_for(
            lambda: observer.status()["target"]["handle"] == 0x5555,
            message="a rebound handle",
        )
        self.assertTrue(any(kind == "novaforge.observation.rebound" for kind, _ in events))
        calls = len(seam.calls)
        seam.window.pid = 9999
        await wait_for(
            lambda: observer.status()["pause"]["code"] == "target_replaced",
            message="a replaced process",
        )
        await asyncio.sleep(0.05)
        self.assertEqual(len(seam.calls), calls, "a replaced process is never captured")
        self.assertEqual(observer.status()["state"], "paused")

    async def test_observation_frames_and_history_are_bounded(self) -> None:
        seam = FakeSeam()
        observer = self.observer(seam, max_frames=2, max_history=3)
        await observer.start(title=TITLE)
        await wait_for(lambda: observer.status()["counts"]["observed"] >= 5, message="five reads")
        await observer.stop()
        status = observer.status()
        self.assertEqual(len(status["history"]), 3)
        directory = self.tmp / "observed" / status["session_id"]
        self.assertEqual(len(list(directory.glob("*.png"))), 2)
        self.assertEqual(status["frames"]["kept"], 2)

    async def test_the_schedule_is_clamped_and_the_measured_rate_is_reported(self) -> None:
        with TemporaryDirectory() as tmp:
            seam = FakeSeam()
            observer = GameObserver(
                frame_dir=Path(tmp) / "frames",
                resolve=seam.resolve,
                capture=seam.capture,
                interval=0.02,
                min_interval=0.005,
                poll_interval=0.01,
            )
            self.addAsyncCleanup(observer.shutdown)
            result = await observer.start(title=TITLE, interval=0.0001)
            self.assertTrue(result["started"])
            # A caller cannot raise the capture frequency past the hard floor,
            # even by configuring a smaller minimum: 0.01 s is the ceiling.
            self.assertEqual(observer.min_interval, 0.01)
            self.assertEqual(result["interval"]["applied_seconds"], 0.01)
            self.assertEqual(result["interval"]["requested_seconds"], 0.0001)
            await wait_for(lambda: observer.status()["counts"]["observed"] >= 3, message="reads")
            await observer.stop()
            self.assertGreater(observer.status()["measured"]["rate_hz"] or 0, 0)
            self.assertEqual(observer.applied_interval(60.0), MAX_INTERVAL)
            self.assertEqual(observer.applied_interval(0.0), observer.interval)

    async def test_two_observers_are_isolated(self) -> None:
        first = self.observer(FakeSeam())
        second_window = FakeSeam(FakeWindow(title="SECOND WINDOW", handle=0x9999, pid=999))
        second = self.observer(second_window)
        await first.start(title=TITLE, game="one")
        await second.start(title="SECOND WINDOW", game="two")
        await wait_for(
            lambda: first.status()["counts"]["observed"] >= 1
            and second.status()["counts"]["observed"] >= 1,
            message="both observers",
        )
        await first.stop()
        await second.stop()
        first_status = first.status()
        second_status = second.status()
        self.assertNotEqual(first_status["session_id"], second_status["session_id"])
        self.assertEqual(first_status["game"], "one")
        self.assertEqual(second_status["game"], "two")
        # How many reads each loop completed before both stopped is timing, so
        # the isolation claim is about OWNERSHIP: every row of an observer
        # belongs to that observer's session, game and sequence, and neither
        # loop writes into the other's history.
        for status, game, other in (
            (first_status, "one", "two"),
            (second_status, "two", "one"),
        ):
            rows = status["history"]
            self.assertTrue(rows, "each observer recorded its own reads")
            self.assertEqual([row["seq"] for row in rows], list(range(1, len(rows) + 1)))
            for row in rows:
                self.assertEqual(row["session_id"], status["session_id"])
                self.assertEqual(row["game"], game)
                self.assertNotEqual(row["game"], other)

    async def test_persisted_observation_is_restored_as_previous_not_current(self) -> None:
        store = DictStore()
        seam = FakeSeam()
        observer = self.observer(seam, store=store)
        await observer.start(title=TITLE, game="persisted game")
        await wait_for(lambda: observer.status()["counts"]["observed"] >= 1, message="a frame")
        await observer.stop()
        self.assertIn("novaforge_observation", store.data)
        snapshot = store.data["novaforge_observation"]
        self.assertEqual(snapshot["session"]["game"], "persisted game")
        self.assertTrue(snapshot["rows"])

        restored = self.observer(FakeSeam(), store=store)
        status = restored.status()
        self.assertEqual(status["state"], "idle")
        self.assertFalse(status["tested"])
        previous = status["previous"]
        assert previous is not None
        self.assertTrue(previous["restored"])
        self.assertEqual(previous["session"]["game"], "persisted game")
        self.assertTrue(previous["rows"])

    async def test_a_paused_target_does_not_record_a_row_every_poll(self) -> None:
        seam = FakeSeam(FakeWindow(minimized=True))
        observer = self.observer(seam)
        await observer.start(title=TITLE)
        await wait_for(
            lambda: observer.status()["counts"]["unavailable"] >= 1,
            message="the pause record",
        )
        first = observer.status()
        await asyncio.sleep(0.3)
        after = observer.status()
        self.assertEqual(after["state"], "paused")
        self.assertEqual(after["pause"]["code"], "target_minimized")
        self.assertEqual(len(after["history"]), len(first["history"]))
        self.assertEqual(after["counts"]["unavailable"], first["counts"]["unavailable"])
        self.assertEqual(first["counts"]["attempts"], 1)
        self.assertEqual(seam.calls, [], "a paused target is never captured")

    async def test_a_reported_start_time_is_adopted_and_a_reused_pid_is_refused(self) -> None:
        seam = FakeSeam()
        identity = FakeIdentity()
        observer = self.observer(seam, identity=identity, identity_ttl=0.0)
        started = await observer.start(title=TITLE)
        self.assertTrue(started["started"], started)
        self.assertEqual(started["target"]["start_time"], identity.start_time)
        self.assertIn(identity.start_time, started["target"]["identity"])
        seam.window = None
        identity.start_time = "2026-10-25T10:00:00Z"
        await wait_for(
            lambda: observer.status()["state"] == "stopped",
            message="the reused pid",
        )
        self.assertEqual(observer.status()["reason"], "process_replaced")

    async def test_an_unverifiable_pinned_start_time_is_refused(self) -> None:
        async def broken(pid: int) -> dict[str, Any] | None:
            raise RuntimeError("the identity probe exploded")

        observer = self.observer(FakeSeam(), identity=broken)
        result = await observer.start(title=TITLE, start_time="2026-10-24T09:00:00Z")
        self.assertFalse(result["started"])
        self.assertEqual(result["reason_code"], "process_identity_unavailable")
        self.assertEqual(observer.status()["state"], "idle")

    async def test_a_pinned_pid_on_a_window_without_one_is_refused(self) -> None:
        observer = self.observer(FakeSeam(FakeWindow(pid=0)))
        result = await observer.start(title=TITLE, pid=5150)
        self.assertFalse(result["started"])
        self.assertEqual(result["reason_code"], "target_pid_unavailable")
        self.assertEqual(observer.status()["session_id"], "")

    async def test_stop_clears_a_user_pause(self) -> None:
        observer = self.observer(FakeSeam())
        await observer.start(title=TITLE)
        await observer.pause()
        stopped = await observer.stop()
        self.assertTrue(stopped["stopped"])
        status = observer.status()
        self.assertEqual(status["pause"]["code"], "")
        self.assertFalse(status["pause"]["user_requested"])

    async def test_rebinds_are_published_only_when_the_handle_changes(self) -> None:
        seam = FakeSeam()
        events: list[tuple[str, dict[str, Any]]] = []
        observer = self.observer(seam, identity=FakeIdentity(), events=events)
        await observer.start(title=TITLE)
        await wait_for(lambda: observer.status()["counts"]["observed"] >= 1, message="a frame")
        await observer.pause()
        await observer.resume()
        self.assertFalse(
            [event for event, _ in events if event.endswith("rebound")],
            "resuming the same handle is not a rebind",
        )
        seam.window = FakeWindow(handle=0x7777, pid=5150)
        await wait_for(
            lambda: observer.status()["counts"]["observed"] >= 2,
            message="the recreated window",
        )
        rebounds = [payload for event, payload in events if event.endswith("rebound")]
        self.assertEqual(len(rebounds), 1)
        self.assertEqual(rebounds[0]["previous_handle"], 0x4321)
        self.assertEqual(rebounds[0]["handle"], 0x7777)

    async def test_a_target_no_backend_can_see_pauses_instead_of_failing(self) -> None:
        seam = FakeSeam()
        seam.reject = True
        seam.rejection_code = "target_not_foreground"
        seam.rejection = "No wired capture backend can see the target in its current state."
        observer = self.observer(seam, max_failures=3)
        await observer.start(title=TITLE)
        await wait_for(
            lambda: observer.status()["state"] == "paused", message="the paused target"
        )
        await asyncio.sleep(0.2)
        status = observer.status()
        self.assertEqual(status["pause"]["code"], "target_not_foreground")
        self.assertEqual(status["state"], "paused", "focus loss must not terminate observation")
        self.assertEqual(status["counts"]["rejected"], 0)
        self.assertEqual(status["consecutive_failures"], 0)
        self.assertEqual(len(status["history"]), 1, "the pause is recorded once, not per poll")
        seam.reject = False
        await wait_for(
            lambda: observer.status()["state"] == "observing"
            and observer.status()["counts"]["observed"] >= 1,
            message="the restored target",
        )
        self.assertEqual(observer.status()["pause"]["code"], "")

    async def test_a_resized_window_is_captured_at_its_new_size(self) -> None:
        seam = FakeSeam()
        observer = self.observer(seam)
        await observer.start(title=TITLE)
        await wait_for(lambda: observer.status()["counts"]["observed"] >= 1, message="a frame")
        seam.window = FakeWindow(bounds=(100, 50, 500, 300))
        await wait_for(
            lambda: (observer.status()["last_observation"] or {}).get("width") == 400,
            message="the resized frame",
        )
        last = observer.status()["last_observation"]
        assert last is not None
        self.assertEqual((last["width"], last["height"]), (400, 250))
        self.assertTrue(last["target_valid"])

    async def test_stop_cancels_a_capture_the_loop_is_awaiting(self) -> None:
        seam = FakeSeam()
        seam.capture_delay = 0.5
        observer = self.observer(seam)
        await observer.start(title=TITLE)
        await asyncio.sleep(0.05)
        began = time.monotonic()
        stopped = await observer.stop()
        elapsed = time.monotonic() - began
        self.assertTrue(stopped["stopped"])
        self.assertEqual(observer.status()["state"], "stopped")
        self.assertLess(elapsed, 1.0, "stop must cancel the in-flight capture, not wait it out")
        self.assertEqual(observer.status()["counts"]["observed"], 0)

    async def test_a_stop_discards_a_capture_already_in_flight(self) -> None:
        seam = FakeSeam()
        observer = self.observer(seam)
        await observer.start(title=TITLE)
        await wait_for(lambda: observer.status()["counts"]["observed"] >= 1, message="a frame")
        # Stop the schedule but keep the session observing, so one manual read
        # is the only capture in flight when stop lands.
        await observer._cancel_loop()  # noqa: SLF001 - the race needs one reader
        seam.capture_delay = 0.3
        before = observer.status()["counts"]
        task = asyncio.create_task(observer.observe_once())
        await asyncio.sleep(0.05)
        stopped = await observer.stop()
        self.assertTrue(stopped["stopped"])
        result = await task
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason_code"], "stopped")
        after = observer.status()
        self.assertEqual(after["state"], "stopped")
        self.assertEqual(after["counts"]["observed"], before["observed"])
        self.assertEqual(after["counts"]["rejected"], before["rejected"])

    async def test_concurrent_starts_bind_exactly_one_session(self) -> None:
        seam = GatedSeam()
        observer = self.observer(seam)
        first = asyncio.create_task(observer.start(title=TITLE))
        await asyncio.sleep(0)
        second = asyncio.create_task(observer.start(title=TITLE))
        await asyncio.sleep(0.05)
        seam.gate.set()
        results = await asyncio.gather(first, second)
        started = [item for item in results if item.get("started")]
        refused = [item for item in results if not item.get("started")]
        self.assertEqual(len(started), 1, results)
        self.assertEqual(len(refused), 1, results)
        self.assertEqual(refused[0]["reason_code"], "start_in_progress")
        self.assertEqual(observer.status()["counts"]["attempts"] <= 1, True)
        await observer.stop()

    async def test_a_restart_after_a_terminal_failure_leaves_one_capture_loop(self) -> None:
        """A failed loop's final awaits must not outlive a new session.

        A terminal failure finishes while publishes are still pending. If a
        start lands in that window, the old loop must stop: otherwise two loops
        capture one target, and the old loop's exit clears the new task (so a
        later stop cannot cancel it).
        """
        seam = FakeSeam()
        seam.reject = True
        release = asyncio.Event()
        events: list[tuple[str, dict[str, Any]]] = []

        async def publish(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))
            if event_type.endswith(".failed"):
                await release.wait()

        observer = self.observer(seam, max_failures=1, publish=publish, interval=0.02)
        await observer.start(title=TITLE)
        await wait_for(
            lambda: observer.status()["state"] == "failed", message="the terminal failure"
        )
        # The failed event is blocked inside the old loop, which is still alive.
        seam.reject = False
        restarted = await observer.start(title=TITLE)
        self.assertTrue(restarted["started"], restarted)
        session = observer.status()["session_id"]
        release.set()
        await wait_for(
            lambda: observer.status()["counts"]["observed"] >= 1,
            message="the new session's first frame",
        )
        await asyncio.sleep(0.1)
        loops = [
            task
            for task in asyncio.all_tasks()
            if task.get_name().startswith("novaforge-observer-")
        ]
        self.assertEqual(len(loops), 1, [task.get_name() for task in loops])
        self.assertTrue(loops[0].get_name().endswith(session))
        self.assertEqual(observer.status()["state"], "observing")
        stopped = await observer.stop()
        self.assertTrue(stopped["stopped"])
        frames = observer.status()["counts"]["observed"]
        await asyncio.sleep(0.1)
        self.assertEqual(
            observer.status()["counts"]["observed"], frames, "stop ends every loop"
        )

    async def test_the_identity_probe_is_cached_while_the_window_is_missing(self) -> None:
        seam = FakeSeam()
        identity = FakeIdentity()
        observer = self.observer(seam, identity=identity, identity_ttl=5.0)
        await observer.start(title=TITLE)
        await wait_for(lambda: observer.status()["counts"]["observed"] >= 1, message="a frame")
        seam.window = None
        await wait_for(
            lambda: observer.status()["state"] == "paused", message="the missing window"
        )
        calls = identity.calls
        await asyncio.sleep(0.3)
        self.assertEqual(
            identity.calls, calls, "a paused observer must not spawn a probe every poll"
        )

    async def test_a_broken_store_never_breaks_observation(self) -> None:
        store = DictStore()
        store.fail_read = True
        store.fail_write = True
        observer = self.observer(FakeSeam(), store=store)
        self.assertIsNone(observer.status()["previous"])
        await observer.start(title=TITLE)
        await wait_for(lambda: observer.status()["counts"]["observed"] >= 1, message="a frame")
        self.assertTrue(await observer.stop())


class ManagerObservationTests(unittest.IsolatedAsyncioTestCase):
    """The observer uses the manager's OWN probe and capture router."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    async def _manager(self, *, surface: bool = True) -> tuple[NovaForgeManager, FakeSeam, list[str]]:
        seam = FakeSeam()
        desktop_calls: list[str] = []

        async def desktop_capture(path: str) -> dict[str, Any]:
            desktop_calls.append(path)
            paint(path, 1200, 800, seed=3)
            return {"fresh": True, "capture_ms": 3.0}

        async def capture_window(handle: int, path: str) -> dict[str, Any]:
            paint(path, 800, 500, seed=1)
            return {
                "ok": True,
                "width": 800,
                "height": 500,
                "size_bytes": Path(path).stat().st_size,
                "elapsed_ms": 2.0,
            }

        async def window_lines() -> list[str]:
            return _lines()

        manager = NovaForgeManager(
            data_dir=self.tmp,
            capture=desktop_capture,
            capture_window=capture_window if surface else None,
            geometry=seam.resolve,
            window_lines=window_lines,
            capture_supported=True,
        )
        self.addAsyncCleanup(manager.shutdown)
        return manager, seam, desktop_calls

    async def test_observation_uses_the_capability_router(self) -> None:
        manager, seam, desktop_calls = await self._manager()
        state = window_state(seam.window)
        assert state is not None
        path = self.tmp / "observed" / "direct.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        frame = await manager.capture_observation(state, str(path))
        self.assertTrue(frame.ok, frame.reason)
        self.assertEqual(frame.backend, "printwindow")
        self.assertEqual(frame.source, "window_surface")
        self.assertEqual(frame.reason_code, "ok")
        self.assertTrue(frame.content_hash)
        self.assertEqual(desktop_calls, [], "a background target must not be a desktop crop")

    async def test_a_frame_that_never_arrived_is_not_dressed_as_an_identity_failure(self) -> None:
        """Measured in the NF-23 verification pass: when NO backend produced a
        frame, the refusal still claimed `capture_ok` and `content="not_target"`,
        so the panel diagnosed "the frame is not the target" for a frame that
        never existed. The record must say no pixels were produced, and its
        content must be UNVERIFIED rather than an identity claim."""
        seam = FakeSeam()
        state = window_state(seam.window)
        assert state is not None

        async def failing_display(path: str) -> dict[str, Any]:
            raise RuntimeError("display capture exploded")

        async def refusing_window(handle: int, path: str) -> dict[str, Any]:
            return {"ok": False, "error": "target_unreadable"}

        manager = NovaForgeManager(
            data_dir=self.tmp,
            capture=failing_display,
            capture_window=refusing_window,
            geometry=seam.resolve,
            window_lines=_lines,
            capture_supported=True,
        )
        self.addAsyncCleanup(manager.shutdown)
        path = self.tmp / "observed" / "missing.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        frame = await manager.capture_observation(state, str(path))
        self.assertFalse(frame.ok)
        self.assertFalse(frame.verdict["capture_ok"])
        self.assertEqual(frame.verdict["content"], "unverified")
        self.assertFalse(frame.verdict["livable"])

    async def test_the_historical_observation_adapter_still_returns_a_frame(self) -> None:
        manager, seam, _ = await self._manager()
        payload = await manager._capture_observation_frame(seam.window)  # noqa: SLF001
        self.assertTrue(payload["fresh"], payload)
        self.assertEqual(payload["backend"], "printwindow")
        self.assertEqual(payload["handle"], seam.window.handle)
        self.assertEqual(payload["width"], 800)
        self.assertIsNotNone(payload["age_seconds"])

    async def test_manager_observation_lifecycle_and_frame_path(self) -> None:
        manager, _, _ = await self._manager()
        self.assertIsNone(manager.observation_frame_path())
        result = await manager.start_observation({"title": TITLE, "interval": 0.02})
        self.assertTrue(result["started"], result)
        await wait_for(
            lambda: manager.observation_status()["counts"]["observed"] >= 1,
            message="an observed frame",
        )
        path = manager.observation_frame_path()
        self.assertIsNotNone(path)
        assert path is not None
        self.assertTrue(path.is_file())
        paused = await manager.pause_observation()
        self.assertTrue(paused["paused"])
        resumed = await manager.resume_observation()
        self.assertTrue(resumed["resumed"])
        stopped = await manager.stop_observation()
        self.assertTrue(stopped["stopped"])
        self.assertTrue((await manager.stop_observation())["already_stopped"])
        self.assertEqual(manager.observation_status()["state"], "stopped")

    async def test_a_host_without_capture_refuses_observation(self) -> None:
        seam = FakeSeam()

        async def desktop_capture(path: str) -> dict[str, Any]:
            raise AssertionError("capture must not be attempted on a host without it")

        async def window_lines() -> list[str]:
            return _lines()

        manager = NovaForgeManager(
            data_dir=self.tmp,
            capture=desktop_capture,
            geometry=seam.resolve,
            window_lines=window_lines,
            capture_supported=False,
        )
        self.addAsyncCleanup(manager.shutdown)
        result = await manager.start_observation({"title": TITLE})
        self.assertFalse(result["started"])
        self.assertEqual(result["reason_code"], "capture_unsupported")
        self.assertEqual(manager.observation_status()["state"], "idle")
        self.assertIsNone(manager.observation_frame_path())

    async def test_the_manager_refuses_a_non_numeric_pid(self) -> None:
        manager, _, _ = await self._manager()
        result = await manager.start_observation({"title": TITLE, "pid": "not-a-pid"})
        self.assertFalse(result["started"])
        self.assertEqual(result["reason_code"], "invalid_pid")


def _lines() -> list[str]:
    return [f"python|{TITLE}"]


class ObservationApiTests(_IsolatedApiTestCase):
    """The HTTP surface, through the real application."""

    def _isolated_application(self, **kwargs: Any):
        app = super()._isolated_application(**kwargs)
        self.seam = FakeSeam()
        self.identity = FakeIdentity()

        async def desktop_capture(path: str) -> dict[str, Any]:
            paint(path, 1200, 800, seed=3)
            return {"fresh": True, "capture_ms": 3.0}

        async def capture_window(handle: int, path: str) -> dict[str, Any]:
            paint(path, 800, 500, seed=1)
            return {"ok": True, "width": 800, "height": 500,
                    "size_bytes": Path(path).stat().st_size, "elapsed_ms": 2.0}

        async def window_lines() -> list[str]:
            return _lines()

        self.manager = NovaForgeManager(
            data_dir=Path(self._tmp.name),
            capture=desktop_capture,
            capture_window=capture_window,
            geometry=self.seam.resolve,
            window_lines=window_lines,
            process_identity=self.identity,
            publish=app._novaforge_publish,
            capture_supported=True,
        )
        app.novaforge = self.manager
        return app

    def _wait(self, predicate, *, timeout: float = 4.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("condition was not reached in time")

    def test_status_starts_untested_and_the_frame_route_is_404(self) -> None:
        body = self._client.get("/novaforge/observation").json()
        self.assertEqual(body["state"], "idle")
        self.assertFalse(body["tested"])
        self.assertIsNone(body["last_observation"])
        self.assertIsNone(body["last_success"])
        self.assertIsNone(body["measured"]["rate_hz"])
        response = self._client.get("/novaforge/observation/frame")
        self.assertEqual(response.status_code, 404)

    def test_start_observe_pause_resume_stop_and_a_validated_frame(self) -> None:
        started = self._client.post(
            "/novaforge/observation/start",
            json={"title": TITLE, "game": "Fake Game", "interval": 0.05},
        )
        self.assertEqual(started.status_code, 200, started.text)
        body = started.json()
        self.assertTrue(body["started"], body)
        session_id = body["session_id"]
        self.assertTrue(session_id.startswith("obs-"))
        # A rate needs two capture starts to be a measurement; one frame is an
        # observation, not a rate.
        self._wait(lambda: self.manager.observation_status()["counts"]["observed"] >= 2)

        status = self._client.get("/novaforge/observation").json()
        self.assertEqual(status["state"], "observing")
        self.assertEqual(status["game"], "Fake Game")
        self.assertEqual(status["last_observation"]["status"], "observed")
        self.assertTrue(status["last_observation"]["validation"]["livable"])
        self.assertIsNotNone(status["measured"]["rate_hz"])

        frame = self._client.get("/novaforge/observation/frame")
        self.assertEqual(frame.status_code, 200)
        self.assertEqual(frame.headers["content-type"], "image/png")
        self.assertIn("no-store", frame.headers["cache-control"])

        once = self._client.post("/novaforge/observation/observe")
        self.assertEqual(once.status_code, 200)
        self.assertIn(once.json()["status"], {"observed", "busy"})

        paused = self._client.post("/novaforge/observation/pause").json()
        self.assertTrue(paused["paused"])
        self.assertTrue(paused["pause"]["user_requested"])
        resumed = self._client.post("/novaforge/observation/resume").json()
        self.assertTrue(resumed["resumed"], resumed)
        stopped = self._client.post("/novaforge/observation/stop").json()
        self.assertTrue(stopped["stopped"])
        again = self._client.post("/novaforge/observation/stop").json()
        self.assertTrue(again["already_stopped"])

    def test_a_missing_target_is_a_refusal_not_a_crash(self) -> None:
        self.seam.window = None
        response = self._client.post(
            "/novaforge/observation/start", json={"title": "Nothing Here"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["started"])
        self.assertEqual(body["reason_code"], "target_missing")
        self.assertEqual(body["state"], "idle")

    def test_a_second_start_is_refused_while_observing(self) -> None:
        first = self._client.post(
            "/novaforge/observation/start", json={"title": TITLE, "interval": 0.05}
        ).json()
        self.assertTrue(first["started"], first)
        second = self._client.post(
            "/novaforge/observation/start", json={"title": TITLE}
        ).json()
        self.assertFalse(second["started"])
        self.assertEqual(second["reason_code"], "already_observing")
        self._client.post("/novaforge/observation/stop")

    def test_the_panel_routes_exist_in_the_declared_surface(self) -> None:
        from novacontrol.api.models import ApiSurface

        paths = {(route.method, route.path) for route in ApiSurface.default().routes}
        for method, path in [
            ("GET", "/novaforge/observation"),
            ("POST", "/novaforge/observation/start"),
            ("POST", "/novaforge/observation/pause"),
            ("POST", "/novaforge/observation/resume"),
            ("POST", "/novaforge/observation/stop"),
            ("POST", "/novaforge/observation/observe"),
            ("GET", "/novaforge/observation/frame"),
        ]:
            self.assertIn((method, path), paths)


class ObservationPanelContractTests(unittest.TestCase):
    """The card exists, starts with no live frame, and claims no understanding."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (
            Path("src/novacontrol/web/static/index.html").read_text(encoding="utf-8")
        )

    def test_every_control_the_card_needs_is_present(self) -> None:
        for element in (
            "forgeObservationState",
            "forgeObservationTarget",
            "forgeObservationInterval",
            "forgeObservationGame",
            "forgeObservationStart",
            "forgeObservationPause",
            "forgeObservationResume",
            "forgeObservationStop",
            "forgeObservationOnce",
            "forgeObservationRefresh",
            "forgeObservationHint",
            "forgeObservationFrameWrap",
            "forgeObservationFrame",
            "forgeObservationFrameLabel",
            "forgeObservationFrameEmpty",
            "forgeObservationFacts",
            "forgeObservationHistory",
        ):
            self.assertIn(f'id="{element}"', self.html, element)

    def test_the_frame_starts_hidden_and_without_a_source(self) -> None:
        wrapper = re.search(
            r'<div id="forgeObservationFrameWrap"[^>]*>', self.html
        )
        image = re.search(r'<img id="forgeObservationFrame"[^>]*>', self.html)
        self.assertIsNotNone(wrapper)
        self.assertIsNotNone(image)
        assert wrapper is not None and image is not None
        self.assertIn("hidden", wrapper.group(0), "a frame is fetched only after a validation")
        self.assertNotIn("src=", image.group(0), "the placeholder must not carry a frame URL")

    def test_the_card_says_observation_is_not_understanding_or_control(self) -> None:
        self.assertIn("not understanding or controlling it", self.html)
        self.assertIn("Only a frame that passed target, freshness and content validation", self.html)


class ObservationDefaultsTests(unittest.TestCase):
    def test_the_capture_timeout_is_bounded_and_the_default_interval_is_sane(self) -> None:
        self.assertGreater(DEFAULT_CAPTURE_TIMEOUT, 0)
        self.assertLess(MIN_INTERVAL, MAX_INTERVAL)
